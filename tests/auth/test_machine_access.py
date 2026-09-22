"""Machine auth boundaries, keyless credentials, and request-scoped audit."""

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth import AccessToken
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from httplib2 import Response

from auth.machine_auth import GoogleMachineVerifier, WorkspaceMultiAuth
from auth.machine_credentials import IrsaSupplier, LockedCredentials, MachineRuntime
from auth.machine_policy import (
    MachinePolicy,
    WifConfig,
    TOOL_RULES,
    ToolRule,
    authorize_tool,
    load_config,
    select_scopes,
)


@pytest.fixture
def policy_data():
    from pathlib import Path

    return yaml.safe_load(
        Path("helm-chart/workspace-mcp/examples/machine-access.values.yaml").read_text()
    )["machineAccess"]


@pytest.fixture
def runtime(policy_data):
    return MachineRuntime(
        MachinePolicy.model_validate(policy_data["policy"]),
        WifConfig.model_validate(policy_data["wif"]),
    )


@pytest.fixture
def signed(runtime, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = GoogleMachineVerifier(runtime, "full")
    monkeypatch.setattr(
        verifier.jwks,
        "get_signing_key_from_jwt",
        lambda raw: SimpleNamespace(key=key.public_key()),
    )
    client = runtime.policy.clients["wengoagent-prod"]
    claims = {
        "iss": runtime.policy.issuer,
        "sub": client.subject,
        "email": client.email,
        "email_verified": True,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "aud": runtime.policy.endpoints["full"].audience,
    }

    def sign(changes=None, omit=(), signing_key=None):
        payload = {**claims, **(changes or {})}
        for field in omit:
            payload.pop(field, None)
        return jwt.encode(
            payload, signing_key or key, algorithm="RS256", headers={"kid": "test-key"}
        )

    return verifier, sign


@pytest.mark.asyncio
async def test_verified_client_resolves_separate_target_without_human_session(
    signed, monkeypatch
):
    verifier, sign = signed
    token = await verifier.verify_token(sign({"workspace_account": "attacker"}))
    assert token.client.email != token.target.email
    assert token.target.email.startswith("workspace-editorial@")
    from auth.oauth21_session_store import ensure_session_from_access_token

    with pytest.raises(ValueError, match="Machine ID tokens"):
        await ensure_session_from_access_token(token, token.client.email)
    from auth.auth_info_middleware import AuthInfoMiddleware

    state = {}

    async def set_state(key, value, **kwargs):
        assert kwargs == {"serializable": False}
        state[key] = value

    ctx = SimpleNamespace(set_state=set_state)
    monkeypatch.setattr("auth.auth_info_middleware.get_access_token", lambda: token)
    monkeypatch.setattr(
        "auth.auth_info_middleware.get_http_headers",
        lambda **kw: pytest.fail("human fallback"),
    )
    await AuthInfoMiddleware()._process_request_for_auth(
        SimpleNamespace(fastmcp_context=ctx)
    )
    assert state == {
        "authenticated_user_email": token.target.email,
        "authenticated_via": "machine_sa",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,omit",
    [
        ({"sub": "unknown"}, ()),
        ({"sub": "123456789012345678909"}, ()),
        ({"email": "different@client-project.iam.gserviceaccount.com"}, ()),
        ({"email_verified": "true"}, ()),
        ({"email_verified": False}, ()),
        ({"aud": "https://other.example.com/mcp"}, ()),
        ({"aud": ["https://workspace-mcp.example.com/mcp"]}, ()),
        ({"iss": "https://attacker.example"}, ()),
        ({"exp": int(time.time()) - 60}, ()),
        ({"iat": int(time.time()) + 600}, ()),
        ({}, ("exp",)),
        ({}, ("sub",)),
        ({}, ("email",)),
    ],
)
async def test_reject_untrusted_claims(signed, changes, omit):
    verifier, sign = signed
    assert await verifier.verify_token(sign(changes, omit)) is None


@pytest.mark.asyncio
async def test_reject_wrong_signature_and_jwks_failure(signed, monkeypatch):
    verifier, sign = signed
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await verifier.verify_token(sign(signing_key=other_key)) is None
    monkeypatch.setattr(
        verifier.jwks,
        "get_signing_key_from_jwt",
        Mock(side_effect=jwt.PyJWKClientError("unavailable")),
    )
    assert await verifier.verify_token(sign()) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "target",
        "endpoint",
        "readonly",
        "scope",
        "issuer",
        "project",
        "numeric_id",
    ],
)
def test_policy_fails_closed(policy_data, mutation):
    policy = policy_data["policy"]
    client = policy["clients"]["wengoagent-prod"]
    if mutation == "duplicate":
        policy["clients"]["duplicate"] = copy.deepcopy(client)
    if mutation == "target":
        client["workspace_account"] = "absent"
    if mutation == "endpoint":
        client["endpoints"] = ["absent"]
    if mutation == "readonly":
        client["permissions"] = "read-only"
    if mutation == "scope":
        client["scope_ceiling"] = ["https://www.googleapis.com/auth/cloud-platform"]
    if mutation == "issuer":
        policy["issuer"] = "https://attacker.example"
    if mutation == "project":
        policy["workspace_accounts"]["editorial"]["email"] = (
            "workspace-editorial@other-project.iam.gserviceaccount.com"
        )
    if mutation == "numeric_id":
        client["subject"] = 123456789012345678901
    with pytest.raises(ValueError):
        MachinePolicy.model_validate(policy)


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"clients": {}, "clients": {}}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_config(path, MachinePolicy)


@pytest.mark.asyncio
async def test_composed_auth_keeps_human_scope_gate(runtime, signed):
    provider = SimpleNamespace(
        base_url="https://workspace-mcp.example.com",
        resource_base_url=None,
        required_scopes=["openid"],
        verify_token=AsyncMock(),
    )
    combined = WorkspaceMultiAuth(provider, runtime, "full")
    combined.machine_verifier = signed[0]
    provider.verify_token.return_value = AccessToken(
        token="human", client_id="human", scopes=["openid"]
    )
    assert (await combined.verify_token("human")).client_id == "human"
    provider.verify_token.return_value = AccessToken(
        token="human", client_id="human", scopes=[]
    )
    assert await combined.verify_token("human") is None
    provider.verify_token.return_value = None
    assert (
        await combined.verify_token(signed[1]())
    ).target == runtime.policy.workspace_accounts["editorial"]


def test_scopes_and_tool_policy_are_separate_from_shared_target(runtime, monkeypatch):
    writer = runtime.policy.clients["wengoagent-prod"]
    reader = writer.model_copy(
        update={
            "permissions": "read-only",
            "scope_ceiling": ["https://www.googleapis.com/auth/drive.readonly"],
        }
    )
    monkeypatch.setitem(TOOL_RULES, "read", ToolRule(frozenset({"drive"}), frozenset()))
    monkeypatch.setitem(
        TOOL_RULES, "write", ToolRule(frozenset({"drive"}), frozenset({"drive"}))
    )
    assert authorize_tool(reader, "read")
    with pytest.raises(PermissionError):
        authorize_tool(reader, "write")
    with pytest.raises(PermissionError):
        authorize_tool(writer, "unregistered")
    assert select_scopes(writer, "drive", False) == select_scopes(
        reader, "drive", False
    )
    assert select_scopes(writer, "drive", True) != select_scopes(reader, "drive", False)


def test_irsa_uses_fresh_sdk_credentials_and_rejects_static_creds(monkeypatch):
    snapshots = iter(
        [
            SimpleNamespace(access_key="one", secret_key="secret", token="session1"),
            SimpleNamespace(access_key="two", secret_key="secret", token="session2"),
        ]
    )
    sdk_credentials = SimpleNamespace(
        method="assume-role-with-web-identity",
        get_frozen_credentials=lambda: next(snapshots),
    )
    session = SimpleNamespace(get_credentials=lambda: sdk_credentials)
    monkeypatch.setattr(
        "auth.machine_credentials.boto3.Session", lambda **kwargs: session
    )
    supplier = IrsaSupplier("eu-west-3")
    assert supplier.get_aws_security_credentials(None, None).access_key_id == "one"
    assert supplier.get_aws_security_credentials(None, None).access_key_id == "two"
    sdk_credentials.method = "env"
    with pytest.raises(RefreshError):
        supplier.get_aws_security_credentials(None, None)


def test_cache_pins_target_and_separates_scopes(runtime, monkeypatch):
    monkeypatch.setattr(
        "auth.machine_credentials.IrsaSupplier", lambda region: Mock(spec=IrsaSupplier)
    )
    target = runtime.policy.workspace_accounts["editorial"]
    read = runtime.credentials(
        target, ["https://www.googleapis.com/auth/drive.readonly"]
    )
    assert read is runtime.credentials(
        target, ["https://www.googleapis.com/auth/drive.readonly"]
    )
    write = runtime.credentials(target, ["https://www.googleapis.com/auth/drive"])
    assert read is not write
    assert read.source.service_account_email == target.unique_id
    assert read.quota_project_id == runtime.policy.api_consumer_project
    second = target.model_copy(update={"unique_id": "999"})
    assert read is not runtime.credentials(
        second, ["https://www.googleapis.com/auth/drive.readonly"]
    )


@pytest.mark.asyncio
async def test_refresh_is_serialized_and_rotates_again_after_expiry():
    source = SimpleNamespace(
        quota_project_id="project", token=None, expiry=None, refresh_count=0
    )

    def refresh(request):
        time.sleep(0.01)
        source.refresh_count += 1
        source.token = f"token-{source.refresh_count}"
        source.expiry = datetime.utcnow() + timedelta(hours=1)

    source.refresh = refresh
    credential = LockedCredentials(source)

    async def use():
        headers = {}
        await asyncio.to_thread(
            credential.before_request,
            Mock(),
            "GET",
            "https://docs.googleapis.com",
            headers,
        )
        return headers

    results = await asyncio.gather(*(use() for _ in range(8)))
    assert source.refresh_count == 1
    assert all(h["authorization"] == "Bearer token-1" for h in results)
    assert all(h["x-goog-user-project"] == "project" for h in results)
    credential.expiry = datetime.utcnow() - timedelta(seconds=1)
    assert (await use())["authorization"] == "Bearer token-2"


@pytest.mark.asyncio
async def test_audit_keeps_partial_mutations_and_never_logs_content(
    signed, monkeypatch
):
    from auth.machine_audit import (
        MachineAccessMiddleware,
        MachineHttpRequest,
        _active_audit,
    )

    token = await signed[0].verify_token(signed[1]())
    monkeypatch.setitem(
        TOOL_RULES, "mutate", ToolRule(frozenset({"docs"}), frozenset({"docs"}))
    )
    monkeypatch.setattr("auth.machine_audit.get_machine_token", lambda: token)
    monkeypatch.setattr(
        "auth.machine_audit.get_http_headers",
        lambda: {
            "x-airunner-identity": "runner",
            "x-airunner-consumeraccount": "consumer",
        },
    )
    events = []
    monkeypatch.setattr(
        "auth.machine_audit.emit_audit",
        lambda event, **kw: events.append({"event": event, **kw}),
    )
    responses = iter(
        [
            {"documentId": "new-doc"},
            HttpError(Response({"status": "403"}), b"private error"),
        ]
    )

    def execute(self, **kwargs):
        assert kwargs["num_retries"] == 0
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(HttpRequest, "execute", execute)

    async def call_next(ctx):
        request = MachineHttpRequest(
            Mock(),
            lambda r, c: c,
            "https://docs.googleapis.com/v1/documents/new-doc:batchUpdate",
            method="POST",
            methodId="docs.documents.batchUpdate",
            body="private body",
            machine_token=token,
            audit=_active_audit.get(),
        )
        request.execute(num_retries=3)
        try:
            request.execute()
        except HttpError:
            pass  # Some existing tools catch errors and return text.
        return "An error occurred"

    ctx = SimpleNamespace(message=SimpleNamespace(name="mutate", arguments={}))
    await MachineAccessMiddleware().on_call_tool(ctx, call_next)
    event = events[-1]
    assert event["outcome"] == "partial"
    assert event["principal"]["email"] != event["google_service_account"]
    assert event["airunner"]["consumer_account"] == "consumer"
    assert event["file_ids"] == ["new-doc"]
    assert "private" not in json.dumps(events)
    assert _active_audit.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,args",
    [
        ({"x-airunner-job": "bad\nheader"}, {}),
        ({}, {"file_path": "/secret"}),
        ({}, {"fileUrl": "file:///secret"}),
    ],
)
async def test_audit_rejects_invalid_headers_and_local_uploads_before_call(
    signed, monkeypatch, headers, args
):
    from auth.machine_audit import MachineAccessMiddleware

    token = await signed[0].verify_token(signed[1]())
    monkeypatch.setitem(
        TOOL_RULES, "create", ToolRule(frozenset({"drive"}), frozenset({"drive"}))
    )
    monkeypatch.setattr("auth.machine_audit.get_machine_token", lambda: token)
    monkeypatch.setattr("auth.machine_audit.get_http_headers", lambda: headers)
    call = AsyncMock()
    with pytest.raises(PermissionError):
        await MachineAccessMiddleware().on_call_tool(
            SimpleNamespace(message=SimpleNamespace(name="create", arguments=args)),
            call,
        )
    call.assert_not_called()


@pytest.mark.asyncio
async def test_machine_service_branch_never_uses_oauth_store(signed, monkeypatch):
    import auth.service_decorator as decorator

    token = await signed[0].verify_token(signed[1]())
    monkeypatch.setattr(decorator, "get_machine_token", lambda: token)
    factory = AsyncMock(return_value=("service", token.target.email))
    monkeypatch.setattr("auth.machine_credentials.get_machine_service", factory)
    monkeypatch.setattr(
        decorator,
        "get_authenticated_google_service_oauth21",
        AsyncMock(side_effect=AssertionError("human credentials")),
    )
    result = await decorator._authenticate_service(
        True,
        "drive",
        "v3",
        "list_drive_items",
        "attacker@example.com",
        [],
        "session",
        token.client.email,
    )
    assert result == ("service", token.target.email)
    assert factory.call_args.args == (token, "drive", "v3", "list_drive_items")


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.mark.asyncio
async def test_machine_doc_creation_uses_drive_without_annotations_and_reports_partial(
    signed, monkeypatch
):
    from gdocs.docs_tools import create_doc

    token = await signed[0].verify_token(signed[1]())
    for module in ("auth.machine_credentials", "gdocs.docs_tools"):
        monkeypatch.setattr(module + ".get_machine_token", lambda: token)
    drive, docs = Mock(), Mock()
    drive.files().get().execute.return_value = {
        "driveId": "shared-drive",
        "mimeType": "application/vnd.google-apps.folder",
    }
    drive.files().create().execute.return_value = {"id": "new-document"}
    monkeypatch.setattr(
        "auth.machine_credentials.get_machine_service",
        AsyncMock(return_value=(drive, token.target.email)),
    )
    resolve = AsyncMock(return_value="real-folder")
    monkeypatch.setattr("gdrive.drive_helpers.resolve_folder_id", resolve)
    docs.documents().batchUpdate().execute.side_effect = RuntimeError("network failure")
    with pytest.raises(RuntimeError, match="new-document was created"):
        await _unwrap(create_doc)(docs, token.target.email, "Test", "content")
    body = drive.files().create.call_args.kwargs["body"]
    assert body == {
        "name": "Test",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["real-folder"],
    }
    docs.documents().create.assert_not_called()
    drive.close.assert_called_once()
    assert resolve.call_args.args[1] == token.target.creation_folder_id


@pytest.mark.asyncio
async def test_creation_rejects_my_drive_before_write(signed, monkeypatch):
    from auth.machine_credentials import create_machine_file

    token = await signed[0].verify_token(signed[1]())
    monkeypatch.setattr("auth.machine_credentials.get_machine_token", lambda: token)
    drive = Mock()
    drive.files().get().execute.return_value = {
        "mimeType": "application/vnd.google-apps.folder"
    }
    monkeypatch.setattr(
        "auth.machine_credentials.get_machine_service",
        AsyncMock(return_value=(drive, token.target.email)),
    )
    monkeypatch.setattr(
        "gdrive.drive_helpers.resolve_folder_id",
        AsyncMock(return_value="my-drive-folder"),
    )
    with pytest.raises(PermissionError, match="shared drive"):
        await create_machine_file(
            "create_doc", "Test", "application/vnd.google-apps.document"
        )
    drive.files().create.assert_not_called()
    drive.close.assert_called_once()


@pytest.mark.asyncio
async def test_machine_spreadsheet_initializes_tabs_without_editor_create(
    signed, monkeypatch
):
    from gsheets.sheets_tools import create_spreadsheet

    token = await signed[0].verify_token(signed[1]())
    monkeypatch.setattr("gsheets.sheets_tools.get_machine_token", lambda: token)
    create = AsyncMock(return_value="new-sheet")
    monkeypatch.setattr("auth.machine_credentials.create_machine_file", create)
    service = Mock()
    service.spreadsheets().get().execute.return_value = {
        "spreadsheetId": "new-sheet",
        "spreadsheetUrl": "https://example/sheet",
        "properties": {"title": "Test", "locale": "fr_FR"},
        "sheets": [{"properties": {"sheetId": 0}}],
    }
    result = await _unwrap(create_spreadsheet)(
        service, token.target.email, "Test", ["First", "Second"]
    )
    assert "new-sheet" in result
    service.spreadsheets().create.assert_not_called()
    assert service.spreadsheets().batchUpdate.call_args.kwargs == {
        "spreadsheetId": "new-sheet",
        "body": {
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": 0, "title": "First"},
                        "fields": "title",
                    }
                },
                {"addSheet": {"properties": {"title": "Second"}}},
            ]
        },
    }


def test_actual_tool_dependencies_keep_supporting_drive_readonly(runtime):
    import gdocs.docs_tools  # noqa: F401 -- populate the actual decorator registry

    rule = authorize_tool(runtime.policy.clients["wengoagent-prod"], "insert_doc_image")
    assert rule.services == {"drive", "docs"}
    assert rule.write_services == {"docs"}


@pytest.mark.asyncio
async def test_cached_credentials_cannot_bypass_client_scope_ceiling(
    signed, monkeypatch
):
    from auth.machine_credentials import get_machine_service

    token = await signed[0].verify_token(signed[1]())
    monkeypatch.setitem(
        TOOL_RULES, "mutate", ToolRule(frozenset({"docs"}), frozenset({"docs"}))
    )
    token.runtime.policy.clients["wengoagent-prod"] = token.client.model_copy(
        update={"scope_ceiling": ["https://www.googleapis.com/auth/documents.readonly"]}
    )
    cache = Mock(side_effect=AssertionError("cache must not be consulted"))
    monkeypatch.setattr(token.runtime, "credentials", cache)
    with pytest.raises(PermissionError, match="scope policy"):
        await get_machine_service(token, "docs", "v1", "mutate")
    cache.assert_not_called()


def test_machine_auth_opt_in_and_missing_config_fail_closed(monkeypatch):
    from auth.machine_auth import compose_machine_auth

    provider = Mock()
    monkeypatch.delenv("MCP_MACHINE_ACCESS_ENABLED", raising=False)
    assert compose_machine_auth(provider) is provider
    monkeypatch.setenv("MCP_MACHINE_ACCESS_ENABLED", "true")
    monkeypatch.delenv("MCP_MACHINE_POLICY_FILE", raising=False)
    with pytest.raises(KeyError):
        compose_machine_auth(provider)


@pytest.mark.asyncio
async def test_low_level_api_guard_rejects_readonly_write(signed, monkeypatch):
    from auth.machine_audit import MachineHttpRequest

    token = await signed[0].verify_token(signed[1]())
    token.runtime.policy.clients["wengoagent-prod"] = token.client.model_copy(
        update={"permissions": "read-only"}
    )
    execute = Mock(side_effect=AssertionError("no HTTP"))
    monkeypatch.setattr(HttpRequest, "execute", execute)
    request = MachineHttpRequest(
        Mock(),
        lambda r, c: c,
        "https://docs.googleapis.com/v1/documents/id:batchUpdate",
        method="POST",
        methodId="docs.documents.batchUpdate",
        machine_token=token,
        audit=None,
    )
    with pytest.raises(PermissionError):
        request.execute()
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_permission_ids_are_not_logged_as_file_ids(signed, monkeypatch):
    from auth.machine_audit import MachineAudit

    token = await signed[0].verify_token(signed[1]())
    monkeypatch.setattr("auth.machine_audit.emit_audit", lambda *args, **kw: None)
    audit = MachineAudit(token, "manage_drive_access")
    audit.api_result(
        "drive.permissions.create",
        "https://www.googleapis.com/drive/v3/files/file-id/permissions",
        result={"id": "permission-id"},
    )
    assert audit.api_events[0]["file_ids"] == ["file-id"]


def test_audit_sink_failure_does_not_fail_a_committed_operation(monkeypatch):
    from auth.machine_audit import emit_audit

    monkeypatch.setattr(
        "auth.machine_audit.logger.info", Mock(side_effect=OSError("broken sink"))
    )
    emit_audit("workspace.machine.api_request", outcome="success")


def test_real_google_auth_exchange_uses_wif_target_scope_and_consumer_project(runtime):
    """Exercise google-auth's actual AWS/ST S/impersonation flow without network."""
    from urllib.parse import parse_qs
    from google.auth import aws

    class Supplier(aws.AwsSecurityCredentialsSupplier):
        def get_aws_region(self, context, request):
            return "eu-west-3"

        def get_aws_security_credentials(self, context, request):
            return aws.AwsSecurityCredentials("test-key", "test-secret", "test-session")

    runtime._supplier = Supplier()
    target = runtime.policy.workspace_accounts["editorial"]
    scope = "https://www.googleapis.com/auth/drive.readonly"
    credential = runtime.credentials(target, [scope])
    calls = []

    def request(url, method="GET", headers=None, body=None, **kwargs):
        calls.append(url)
        if url == "https://sts.googleapis.com/v1/token":
            form = parse_qs(body.decode() if isinstance(body, bytes) else body)
            assert form["audience"] == [runtime.wif.audience]
            assert form["subject_token_type"] == [
                "urn:ietf:params:aws:token-type:aws4_request"
            ]
            response = {
                "access_token": "federated-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        else:
            assert (
                url
                == f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{target.unique_id}:generateAccessToken"
            )
            payload = json.loads(body)
            assert payload["scope"] == [scope]
            assert not payload.get("delegates")
            assert "subject" not in payload
            assert headers["authorization"] == "Bearer federated-token"
            assert headers["x-goog-user-project"] == runtime.policy.api_consumer_project
            response = {
                "accessToken": "workspace-token",
                "expireTime": (datetime.utcnow() + timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        return SimpleNamespace(status=200, data=json.dumps(response).encode())

    credential.refresh(request)
    assert credential.token == "workspace-token"
    assert len(calls) == 2
