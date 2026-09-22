"""Request-scoped machine authorization and JSON audit; no document annotations."""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

from auth.machine_auth import (
    get_machine_token,
    require_live_machine,
    validate_machine_arguments,
)
from auth.machine_policy import authorize_tool

logger = logging.getLogger("workspace.audit")
_active_audit = ContextVar("workspace_machine_audit", default=None)


def configure_audit_logging():
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def emit_audit(event, **fields):
    try:
        logger.info(
            json.dumps(
                {
                    "event": event,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    **fields,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    except Exception:
        # A broken log sink must not turn a committed write into a retryable tool
        # failure. Collection/retention health is a deployment responsibility.
        try:
            logging.getLogger(__name__).error("Machine audit log sink failed")
        except Exception:
            pass


def airunner_fields(headers):
    result = {"source": "caller_headers"}
    for name, field_name in (
        ("x-airunner-identity", "identity"),
        ("x-airunner-consumeraccount", "consumer_account"),
        ("x-airunner-job", "job"),
    ):
        value = headers.get(name)
        if value is not None and (
            len(value.encode("utf-8")) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise PermissionError("Invalid airunner audit header")
        result[field_name] = value
    return result


@dataclass
class MachineAudit:
    token: object
    tool: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    airunner: dict = field(default_factory=dict)
    api_events: list = field(default_factory=list)

    def fields(self):
        client, target = self.token.client, self.token.target
        return {
            "event_id": self.event_id,
            "tool": self.tool,
            "profile": self.token.endpoint,
            "principal": {
                "kind": "machine_sa",
                "issuer": "https://accounts.google.com",
                "subject": client.subject,
                "email": client.email,
            },
            "google_service_account": target.email,
            "google_service_account_unique_id": target.unique_id,
            "authorization": {
                "client_policy": self.token.client_key,
                "workspace_account": client.workspace_account,
                "policy_version": self.token.runtime.policy.version,
            },
            "airunner": self.airunner,
        }

    def api_result(self, method, uri, result=None, error=None):
        ids = set(
            re.findall(
                r"/(?:files|documents|spreadsheets)/([A-Za-z0-9_-]+)",
                urlsplit(uri).path,
            )
        )
        if isinstance(result, dict):
            id_fields = ["documentId", "spreadsheetId"]
            if method.startswith("drive.files."):
                id_fields.append("id")
            for key in id_fields:
                value = result.get(key)
                if isinstance(value, str) and re.fullmatch(
                    r"[A-Za-z0-9_-]{1,256}", value
                ):
                    ids.add(value)
        mutation = method.rsplit(".", 1)[-1] not in {
            "get",
            "list",
            "export",
            "download",
            "batchGet",
            "getByDataFilter",
            "batchGetByDataFilter",
        }
        outcome = (
            "success"
            if error is None
            else "failed"
            if isinstance(error, HttpError)
            else "unknown"
        )
        # 5xx responses may follow a committed write, as can transport errors.
        status = getattr(getattr(error, "resp", None), "status", None)
        if mutation and isinstance(status, int) and status >= 500:
            outcome = "unknown"
        event = {
            "google_method": method,
            "file_ids": sorted(ids),
            "mutation": mutation,
            "outcome": outcome,
            "http_status": status,
            "error_type": type(error).__name__ if error else None,
        }
        self.api_events.append(event)
        emit_audit("workspace.machine.api_request", **self.fields(), **event)


class MachineHttpRequest(HttpRequest):
    def __init__(self, *args, machine_token, audit, **kwargs):
        super().__init__(*args, **kwargs)
        self._machine_token = machine_token
        self._audit = audit

    def execute(self, http=None, num_retries=0):
        require_live_machine(self._machine_token)
        read_method = self.method in {"GET", "HEAD"} or self.methodId.rsplit(".", 1)[
            -1
        ] in {
            "getByDataFilter",
            "batchGetByDataFilter",
        }
        if not read_method and self._machine_token.client.permissions != "read-write":
            raise PermissionError("Machine identity cannot issue Google API writes")
        try:
            # Do not replay machine writes after ambiguous transport failures.
            result = super().execute(
                http=http, num_retries=num_retries if read_method else 0
            )
        except Exception as exc:
            if self._audit:
                self._audit.api_result(self.methodId, self.uri, error=exc)
            raise
        if self._audit:
            self._audit.api_result(self.methodId, self.uri, result=result)
        return result


def machine_request_builder(token):
    audit = _active_audit.get()

    def build_request(*args, **kwargs):
        return MachineHttpRequest(*args, machine_token=token, audit=audit, **kwargs)

    return build_request


class MachineAccessMiddleware(Middleware):
    async def on_request(self, context, call_next):
        token = get_machine_token()
        if token is not None:
            require_live_machine(token)
            if context.method in {"prompts/get", "resources/read"}:
                emit_audit(
                    "workspace.machine.authorization_rejected",
                    **MachineAudit(token, context.method).fields(),
                )
                raise PermissionError("Machine access currently supports tools only")
            if context.method in {
                "prompts/list",
                "resources/list",
                "resources/templates/list",
            }:
                return []
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        token = get_machine_token()
        if token is None:
            return tools
        allowed = []
        for tool in tools:
            try:
                authorize_tool(token.client, tool.name)
            except PermissionError:
                continue
            allowed.append(tool)
        return allowed

    async def on_call_tool(self, context, call_next):
        token = get_machine_token()
        if token is None:
            return await call_next(context)
        audit = MachineAudit(token, context.message.name)
        marker = _active_audit.set(audit)
        started = time.monotonic()
        outcome, error_type = "completed", None
        try:
            audit.airunner = airunner_fields(get_http_headers() or {})
            require_live_machine(token)
            authorize_tool(token.client, audit.tool)
            validate_machine_arguments(context.message.arguments or {})
            result = await call_next(context)
            # FastMCP may represent an exception as an error result.
            if getattr(result, "isError", False):
                outcome = "failed"
            return result
        except BaseException as exc:
            outcome = "denied" if isinstance(exc, PermissionError) else "failed"
            error_type = type(exc).__name__
            raise
        finally:
            failures = [e for e in audit.api_events if e["outcome"] != "success"]
            writes = [
                e
                for e in audit.api_events
                if e["mutation"] and e["outcome"] == "success"
            ]
            if any(e["outcome"] == "unknown" and e["mutation"] for e in failures):
                outcome = "unknown"
            elif failures or outcome != "completed":
                outcome = (
                    "partial"
                    if writes
                    else ("failed" if outcome == "completed" else outcome)
                )
            emit_audit(
                "workspace.machine.tool_call",
                **audit.fields(),
                outcome=outcome,
                error_type=error_type,
                duration_ms=round((time.monotonic() - started) * 1000),
                file_ids=sorted(
                    {fid for e in audit.api_events for fid in e["file_ids"]}
                ),
                api_request_count=len(audit.api_events),
            )
            _active_audit.reset(marker)
