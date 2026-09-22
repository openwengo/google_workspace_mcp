"""Static client-to-Workspace-SA authorization. No credentials live in this file."""

import hashlib
import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ISSUER = "https://accounts.google.com"
SCOPE_PREFIX = "https://www.googleapis.com/auth/"
SUPPORTED_SERVICES = frozenset({"drive", "docs", "sheets"})
SUPPORTED_SCOPES = frozenset(
    SCOPE_PREFIX + scope
    for scope in (
        "drive",
        "drive.readonly",
        "drive.metadata",
        "drive.metadata.readonly",
        "documents",
        "documents.readonly",
        "spreadsheets",
        "spreadsheets.readonly",
    )
)


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkspaceAccount(PolicyModel):
    email: str
    unique_id: str = Field(pattern=r"^[0-9]{1,32}$")
    creation_folder_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


class ClientPolicy(PolicyModel):
    subject: str = Field(pattern=r"^[0-9]{1,32}$")
    email: str
    workspace_account: str
    endpoints: list[str] = Field(min_length=1)
    services: list[str] = Field(min_length=1)
    permissions: Literal["read-only", "read-write"]
    scope_ceiling: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_permissions(self):
        if not set(self.services) <= SUPPORTED_SERVICES:
            raise ValueError("Machine services must be drive, docs, or sheets")
        if not set(self.scope_ceiling) <= SUPPORTED_SCOPES:
            raise ValueError("Unsupported machine scope")
        if self.permissions == "read-only" and any(
            not scope.endswith(".readonly") for scope in self.scope_ceiling
        ):
            raise ValueError("Read-only policies cannot contain write scopes")
        return self


class Endpoint(PolicyModel):
    audience: str

    @field_validator("audience")
    @classmethod
    def validate_audience(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path
        ):
            raise ValueError("Machine audience must be an exact HTTPS MCP endpoint URL")
        return value


class MachinePolicy(PolicyModel):
    schema_version: Literal[1]
    issuer: Literal["https://accounts.google.com"]
    api_consumer_project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    endpoints: dict[str, Endpoint] = Field(min_length=1)
    workspace_accounts: dict[str, WorkspaceAccount] = Field(min_length=1)
    clients: dict[str, ClientPolicy] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_mappings(self):
        if "full" not in self.endpoints:
            raise ValueError("Declare the full endpoint, even if no clients use it")
        subjects, targets, audiences = set(), set(), set()
        for mapping in (self.clients, self.workspace_accounts, self.endpoints):
            if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key) for key in mapping):
                raise ValueError("Invalid policy key")
        for target in self.workspace_accounts.values():
            if not re.fullmatch(
                rf"[a-z][a-z0-9-]{{4,28}}[a-z0-9]@{re.escape(self.api_consumer_project)}\.iam\.gserviceaccount\.com",
                target.email,
            ):
                raise ValueError(
                    "Workspace targets must belong to the dedicated project"
                )
            if target.unique_id in targets:
                raise ValueError("Duplicate Workspace target identity")
            targets.add(target.unique_id)
        for endpoint in self.endpoints.values():
            if endpoint.audience in audiences:
                raise ValueError("Endpoint audiences must be distinct")
            audiences.add(endpoint.audience)
        for client in self.clients.values():
            if not re.fullmatch(
                r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]+\.iam\.gserviceaccount\.com",
                client.email,
            ):
                raise ValueError("Client must be a Google service account")
            if client.subject in subjects:
                raise ValueError("Duplicate client identity mapping")
            subjects.add(client.subject)
            if client.workspace_account not in self.workspace_accounts:
                raise ValueError("Client references an unknown Workspace target")
            if not set(client.endpoints) <= self.endpoints.keys():
                raise ValueError("Client references an unknown endpoint")
        return self

    @property
    def version(self):
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    self.model_dump(), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )


class WifConfig(PolicyModel):
    audience: str = Field(
        pattern=r"^//iam\.googleapis\.com/projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+$"
    )
    aws_region: str = Field(pattern=r"^[a-z]{2}-[a-z]+-[0-9]+$")


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"Duplicate configuration key: {key}")
        obj[key] = value
    return obj


def load_config(path, model):
    # Refuse duplicate JSON keys instead of silently replacing an identity/policy.
    return model.model_validate(
        json.loads(Path(path).read_text(), object_pairs_hook=_unique_object)
    )


# Populated by service decorators at registration time. New non-Workspace tools
# remain unavailable to machines. Authorization does not depend on caller hints.
@dataclass(frozen=True)
class ToolRule:
    services: frozenset[str]
    write_services: frozenset[str]


TOOL_RULES: dict[str, ToolRule] = {}
_METADATA_READ_TOOLS = frozenset(
    {
        "search_drive_files",
        "list_drive_items",
        "get_drive_file_permissions",
        "check_drive_file_public_access",
        "get_drive_shareable_link",
        "search_docs",
        "list_docs_in_folder",
        "list_spreadsheets",
    }
)


def register_machine_tool(func, requirements):
    if func.__module__.split(".")[0] not in {"gdrive", "gdocs", "gsheets"}:
        return
    if func.__name__ == "import_to_google_slides":
        return
    services = set(requirements)
    writes = {
        service
        for service, scopes in requirements.items()
        if any(not scope.endswith(".readonly") for scope in scopes)
    }
    if func.__name__ in {"create_doc", "create_spreadsheet"}:
        services.add("drive")
        writes.add("drive")
    if services <= SUPPORTED_SERVICES:
        TOOL_RULES[func.__name__] = ToolRule(frozenset(services), frozenset(writes))


def authorize_tool(client, tool_name):
    rule = TOOL_RULES.get(tool_name)
    if rule is None or not rule.services <= set(client.services):
        raise PermissionError("Tool is not enabled for this machine identity")
    if rule.write_services and client.permissions != "read-write":
        raise PermissionError("Machine identity has read-only access")
    # Preflight every dependency before any API write can occur.
    for service in rule.services:
        select_scopes(client, service, service in rule.write_services, tool_name)
    return rule


def select_scopes(client, service, write, tool_name=""):
    suffix = "" if write else ".readonly"
    candidates = {
        "drive": ["drive" + suffix],
        "docs": ["documents" + suffix, "drive" + suffix],
        "sheets": ["spreadsheets" + suffix, "drive" + suffix],
    }
    if service == "drive" and not write and tool_name in _METADATA_READ_TOOLS:
        candidates["drive"].insert(0, "drive.metadata.readonly")
    for scope in candidates.get(service, []):
        value = SCOPE_PREFIX + scope
        if value in client.scope_ceiling:
            return (value,)
    raise PermissionError(
        f"Machine scope policy does not permit this {service} operation"
    )
