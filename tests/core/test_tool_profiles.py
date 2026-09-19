"""Host profiles exercise real MCP/OAuth routing without contacting Google."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import (
    JTIMapping,
    ProxyDCRClient,
    UpstreamTokenSet,
)
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from pydantic import AnyUrl

import auth.oauth21_session_store as session_store
import core.server as server_module
from auth.provider_context import get_request_auth_provider
from auth.scopes import (
    BASE_SCOPES,
    DOCS_READONLY_SCOPE,
    DOCS_WRITE_SCOPE,
    DRIVE_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    SHEETS_READONLY_SCOPE,
)
from core.server import SecureFastMCP, create_google_auth_provider
from core.tool_profiles import PROFILES_ENV, load_tool_profiles
from core.tool_registry import get_tool_components


PROFILES = {
    "docs": {"url": "https://docs.example.test/mcp", "services": ["docs"]},
    "sheets": {"url": "https://sheets.example.test/mcp", "services": ["sheets"]},
}


@pytest.fixture
def configured_server(monkeypatch):
    config = SimpleNamespace(
        client_id="google-client",
        client_secret="test-google-secret",
        redirect_path="/oauth2callback",
        base_uri="https://internal.example.test",
        base_url="https://internal.example.test:8000",
        get_oauth_base_url=lambda: "https://full.example.test",
        is_oauth21_enabled=lambda: True,
        is_external_oauth21_provider=lambda: False,
    )
    monkeypatch.setattr("core.tool_profiles.get_oauth_config", lambda: config)
    monkeypatch.setenv(PROFILES_ENV, json.dumps(PROFILES))
    monkeypatch.delenv("WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)
    monkeypatch.setattr(server_module, "USER_GOOGLE_EMAIL", None)
    for name in (
        "WORKSPACE_MCP_OAUTH_PROXY_TOKEN_EXPIRY_THRESHOLD_SECONDS",
        "WORKSPACE_MCP_OAUTH_PROXY_ACCESS_TOKEN_EXPIRY_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    provider = create_google_auth_provider(
        config=config,
        base_url=config.get_oauth_base_url(),
        client_storage=MemoryStore(),
        jwt_signing_key=b"test-signing-key-long-enough-for-hs256",
    )
    source = SecureFastMCP("Full workspace", auth=provider)
    monkeypatch.setattr(server_module, "server", source)
    monkeypatch.setattr(server_module, "_auth_provider", provider)
    monkeypatch.setattr(session_store, "_auth_provider", provider)

    @source.tool
    async def get_doc_content():
        await asyncio.sleep(0)
        credentials = await session_store._build_credentials_from_provider()
        assert session_store.get_auth_provider() is server_module.get_auth_provider()
        assert set(credentials.scopes) == set(
            server_module.get_auth_provider().client_registration_options.valid_scopes
        )
        return credentials.refresh_token

    @source.tool
    async def create_doc():
        return "created"

    @source.tool
    async def read_sheet_values():
        await asyncio.sleep(0)
        credentials = await session_store._build_credentials_from_provider()
        return credentials.refresh_token

    @source.tool
    async def send_gmail_message():
        raise AssertionError("An excluded tool must never execute")

    for name, scopes in {
        "get_doc_content": [DOCS_READONLY_SCOPE],
        "create_doc": [DOCS_WRITE_SCOPE],
        "read_sheet_values": [SHEETS_READONLY_SCOPE],
        "send_gmail_message": [GMAIL_SEND_SCOPE],
    }.items():
        get_tool_components(source)[name].fn._required_google_scopes = scopes

    return source, config


async def seed_token(provider, identity):
    now = time.time()
    scopes = provider.client_registration_options.default_scopes
    # Deliberately identical keys: only endpoint namespaces separate these records.
    upstream = UpstreamTokenSet(
        upstream_token_id="same-upstream-id",
        access_token=f"google-access-{identity}",
        refresh_token=f"refresh-{identity}",
        refresh_token_expires_at=None,
        expires_at=now + 3600,
        token_type="Bearer",
        scope=" ".join(scopes),
        client_id="test-client",
        created_at=now,
    )
    await provider._upstream_token_store.put(key="same-upstream-id", value=upstream)
    await provider._jti_mapping_store.put(
        key="same-jti",
        value=JTIMapping(
            jti="same-jti", upstream_token_id="same-upstream-id", created_at=now
        ),
    )
    provider._token_validator.verify_token = AsyncMock(
        return_value=AccessToken(
            token=upstream.access_token,
            client_id="test-client",
            scopes=scopes,
            expires_at=int(now + 3600),
            claims={"email": "user@example.com"},
        )
    )
    return provider.jwt_issuer.issue_access_token(
        client_id="test-client", scopes=scopes, jti="same-jti"
    )


async def rpc(client, host, bearer, method, params=None, path="/mcp"):
    return await client.post(
        f"https://{host}{path}",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


@pytest.mark.asyncio
async def test_host_tools_calls_and_concurrent_credential_recovery(configured_server):
    source, _ = configured_server
    app = source.http_app(stateless_http=True, json_response=True)
    servers = {"full": source, **app.state.profile_servers}
    tokens = {name: await seed_token(view.auth, name) for name, view in servers.items()}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            expected = {
                "full": {
                    "get_doc_content",
                    "create_doc",
                    "read_sheet_values",
                    "send_gmail_message",
                },
                "docs": {"get_doc_content", "create_doc"},
                "sheets": {"read_sheet_values"},
            }
            for name, names in expected.items():
                response = await rpc(
                    client, f"{name}.example.test", tokens[name], "tools/list"
                )
                assert response.status_code == 200, response.text
                assert {t["name"] for t in response.json()["result"]["tools"]} == names

            denied = await rpc(
                client,
                "docs.example.test",
                tokens["docs"],
                "tools/call",
                {"name": "send_gmail_message"},
            )
            payload = denied.json()
            assert "error" in payload or payload["result"].get("isError")

            calls = [
                ("docs", "get_doc_content"),
                ("sheets", "read_sheet_values"),
                ("full", "get_doc_content"),
            ] * 3
            responses = await asyncio.gather(
                *(
                    rpc(
                        client,
                        f"{name}.example.test",
                        tokens[name],
                        "tools/call",
                        {"name": tool},
                    )
                    for name, tool in calls
                )
            )
            for (name, _), response in zip(calls, responses):
                assert (
                    response.json()["result"]["content"][0]["text"] == f"refresh-{name}"
                )
    assert get_request_auth_provider() is None
    assert session_store.get_auth_provider() is source.auth


@pytest.mark.asyncio
async def test_discovery_audiences_registration_and_unknown_hosts(configured_server):
    source, _ = configured_server
    app = source.http_app(stateless_http=True, json_response=True)
    docs = app.state.profile_servers["docs"].auth
    sheets = app.state.profile_servers["sheets"].auth
    token = await seed_token(docs, "docs")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            for name in ("docs", "sheets", "full"):
                origin = f"https://{name}.example.test"
                response = await client.get(
                    f"{origin}/.well-known/oauth-protected-resource/mcp"
                )
                assert response.status_code == 200, response.text
                metadata = response.json()
                assert metadata["resource"] == f"{origin}/mcp"
                assert metadata["authorization_servers"] == [origin + "/"]
                assert "no-store" in response.headers["cache-control"]
                authorization = await client.get(
                    f"{origin}/.well-known/oauth-authorization-server"
                )
                assert authorization.json()["token_endpoint"] == f"{origin}/token"
                unauthenticated = await client.post(f"{origin}/mcp")
                assert unauthenticated.status_code == 401
                assert (
                    f"{origin}/.well-known/oauth-protected-resource/mcp"
                    in unauthenticated.headers["www-authenticate"]
                )

            wrong_audience = await rpc(
                client, "sheets.example.test", token, "tools/list"
            )
            assert wrong_audience.status_code == 401
            root_with_docs_token = await rpc(
                client, "full.example.test", token, "tools/list"
            )
            assert root_with_docs_token.status_code == 401
            unknown = await rpc(client, "unknown.example.test", token, "tools/list")
            assert unknown.status_code == 404
            assert (await client.get("http://pod-ip/health")).status_code == 200

            registration = await client.post(
                "https://docs.example.test/register",
                json={
                    "redirect_uris": ["https://client.example.test/callback"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            assert registration.status_code == 201, registration.text
            client_id = registration.json()["client_id"]
            assert await docs.get_client(client_id) is not None
            assert await sheets.get_client(client_id) is None
            params = {
                "client_id": client_id,
                "redirect_uri": "https://client.example.test/callback",
                "response_type": "code",
                "code_challenge": "a" * 43,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": "https://docs.example.test/mcp",
            }
            authorized = await client.get(
                "https://docs.example.test/authorize", params=params
            )
            assert authorized.status_code == 302, authorized.text
            assert authorized.headers["location"].startswith(
                "https://docs.example.test/consent"
            )
            params["resource"] = "https://sheets.example.test/mcp"
            rejected = await client.get(
                "https://docs.example.test/authorize", params=params
            )
            assert rejected.status_code == 302
            target = urlsplit(rejected.headers["location"])
            assert target.hostname == "client.example.test"
            assert "error" in parse_qs(target.query)


@pytest.mark.asyncio
async def test_profile_namespaces_survive_another_replica(configured_server):
    source, _ = configured_server
    app_a = source.http_app(stateless_http=True)
    docs_a = app_a.state.profile_servers["docs"].auth
    token = await seed_token(docs_a, "docs")
    app_b = source.http_app(stateless_http=True)
    docs_b = app_b.state.profile_servers["docs"].auth
    docs_b._token_validator.verify_token = docs_a._token_validator.verify_token
    assert await docs_b.load_access_token(token) is not None
    assert (
        await app_b.state.profile_servers["sheets"].auth.load_access_token(token)
        is None
    )


def test_profiles_respect_global_restrictions(configured_server):
    source, _ = configured_server
    source.local_provider.remove_tool("create_doc")
    app = source.http_app(stateless_http=True)
    from core.tool_registry import get_tool_components

    assert set(get_tool_components(app.state.profile_servers["docs"])) == {
        "get_doc_content"
    }


@pytest.mark.parametrize(
    "change,match",
    [
        ({"services": ["typo"]}, "Unknown services"),
        ({"tools": ["missing_tool"]}, "unavailable tools"),
        ({"url": "https://full.example.test/mcp"}, "must not replace"),
        ({"url": "https://sheets.example.test/mcp"}, "different hostname"),
        ({"url": "http://remote.example.test/mcp"}, "HTTPS"),
        ({"url": "https://user:password@docs.example.test/mcp"}, "HTTPS"),
        ({"url": "https://docs.example.test/mcp?all=true"}, "HTTPS"),
        ({"url": "https://docs.example.test/oauth2callback"}, "distinct MCP path"),
        ({"tier": "typo"}, "Invalid tier"),
        ({"services": [], "tools": []}, "requires services or tools"),
        ({"service": "docs"}, "Unknown settings"),
    ],
)
def test_invalid_profiles_fail_at_startup(
    configured_server, monkeypatch, change, match
):
    source, _ = configured_server
    profiles = {**PROFILES, "docs": {**PROFILES["docs"], **change}}
    monkeypatch.setenv(PROFILES_ENV, json.dumps(profiles))
    with pytest.raises(ValueError, match=match):
        source.http_app(stateless_http=True)


@pytest.mark.parametrize("raw", ["[]", "null", "not json"])
def test_invalid_json(monkeypatch, raw):
    monkeypatch.setenv(PROFILES_ENV, raw)
    with pytest.raises(ValueError, match="JSON object"):
        load_tool_profiles()


def test_legacy_auth_cannot_enable_profiles(configured_server):
    source, _ = configured_server
    source.auth = None
    with pytest.raises(ValueError, match="built-in Google OAuth"):
        source.http_app(stateless_http=True)


def test_unconfigured_server_keeps_original_routes(configured_server, monkeypatch):
    source, _ = configured_server
    monkeypatch.delenv(PROFILES_ENV)
    app = source.http_app(stateless_http=True)
    assert not hasattr(app.state, "profile_servers")
    assert "/mcp" in {route.path for route in app.routes}


@pytest.mark.asyncio
async def test_custom_endpoint_path(configured_server, monkeypatch):
    source, _ = configured_server
    monkeypatch.setenv(
        PROFILES_ENV,
        json.dumps(
            {"docs": {**PROFILES["docs"], "url": "https://docs.example.test/com"}}
        ),
    )
    app = source.http_app(stateless_http=True, json_response=True)
    token = await seed_token(app.state.profile_servers["docs"].auth, "docs")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            response = await rpc(
                client,
                "docs.example.test",
                token,
                "tools/call",
                {"name": "get_doc_content"},
                path="/com",
            )
            assert response.json()["result"]["content"][0]["text"] == "refresh-docs"
            metadata = await client.get(
                "https://docs.example.test/.well-known/oauth-protected-resource/com"
            )
            assert metadata.json()["resource"] == "https://docs.example.test/com"


async def approve_consent(client, provider, location):
    """Follow local browser consent and return the Google redirect parameters."""
    page = await client.get(location)
    assert page.status_code == 200, page.text
    txn_id = parse_qs(urlsplit(location).query)["txn_id"][0]
    transaction = await provider._transaction_store.get(key=txn_id)
    response = await client.post(
        str(provider.base_url).rstrip("/") + "/consent",
        data={
            "txn_id": txn_id,
            "csrf_token": transaction.csrf_token,
            "action": "approve",
        },
    )
    assert response.status_code == 302, response.text
    target = urlsplit(response.headers["location"])
    assert target.hostname == "accounts.google.com"
    return parse_qs(target.query)


@pytest.mark.asyncio
async def test_profile_discovery_registration_and_google_consent_scopes(
    configured_server,
):
    source, _ = configured_server
    full_scopes = set(source.auth.client_registration_options.valid_scopes)
    app = source.http_app(stateless_http=True)
    expected = {
        "docs": set(BASE_SCOPES) | {DOCS_READONLY_SCOPE, DOCS_WRITE_SCOPE},
        "sheets": set(BASE_SCOPES) | {SHEETS_READONLY_SCOPE},
        "full": full_scopes,
    }
    etags = set()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            for name, scopes in expected.items():
                provider = (
                    source.auth
                    if name == "full"
                    else app.state.profile_servers[name].auth
                )
                origin = f"https://{name}.example.test"
                for path in (
                    "/.well-known/oauth-protected-resource/mcp",
                    "/.well-known/oauth-authorization-server",
                ):
                    discovery = await client.get(origin + path)
                    assert set(discovery.json()["scopes_supported"]) == scopes
                    etags.add(discovery.headers["etag"])
                registration = await client.post(
                    origin + "/register",
                    json={
                        "redirect_uris": ["https://client.example.test/callback"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert registration.status_code == 201, registration.text
                assert set(registration.json()["scope"].split()) == scopes
                assert set(provider._cimd_manager.default_scope.split()) == scopes
                authorized = await client.get(
                    origin + "/authorize",
                    params={
                        "client_id": registration.json()["client_id"],
                        "redirect_uri": "https://client.example.test/callback",
                        "response_type": "code",
                        "code_challenge": "a" * 43,
                        "code_challenge_method": "S256",
                        **(
                            {"scope": " ".join(sorted(scopes))}
                            if name == "full"
                            else {}
                        ),
                    },
                )
                assert authorized.status_code == 302, authorized.text
                google = await approve_consent(
                    client, provider, authorized.headers["location"]
                )
                assert set(google["scope"][0].split()) == scopes
                assert google["access_type"] == ["offline"]
                assert google["prompt"] == ["consent"]
                if name != "full":
                    assert google["include_granted_scopes"] == ["false"]
    assert len(etags) == 3
    assert set(source.auth.client_registration_options.valid_scopes) == full_scopes


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", ["existing_registration", "cimd"])
async def test_old_and_cimd_clients_cannot_expand_profile_consent(
    configured_server, client_type
):
    source, _ = configured_server
    app = source.http_app(stateless_http=True)
    provider = app.state.profile_servers["docs"].auth
    scoped = set(BASE_SCOPES) | {DOCS_READONLY_SCOPE, DOCS_WRITE_SCOPE}
    registered = ProxyDCRClient(
        client_id=(
            "https://client.example.test/metadata.json"
            if client_type == "cimd"
            else "client-registered-before-profile-scopes"
        ),
        redirect_uris=[AnyUrl("https://client.example.test/callback")],
        scope=" ".join(sorted(scoped | {GMAIL_SEND_SCOPE})),
        token_endpoint_auth_method="none",
    )
    if client_type == "cimd":
        provider._cimd_manager.get_client = AsyncMock(return_value=registered)
    else:
        await provider._client_store.put(key=registered.client_id, value=registered)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            params = {
                "client_id": registered.client_id,
                "redirect_uri": "https://client.example.test/callback",
                "response_type": "code",
                "code_challenge": "a" * 43,
                "code_challenge_method": "S256",
            }
            authorized = await client.get(
                "https://docs.example.test/authorize", params=params
            )
            assert authorized.status_code == 302, authorized.text
            google = await approve_consent(
                client, provider, authorized.headers["location"]
            )
            assert set(google["scope"][0].split()) == scoped
            narrower = set(BASE_SCOPES) | {DOCS_READONLY_SCOPE}
            limited = await client.get(
                "https://docs.example.test/authorize",
                params={**params, "scope": " ".join(sorted(narrower))},
            )
            assert limited.status_code == 302, limited.text
            google = await approve_consent(
                client, provider, limited.headers["location"]
            )
            assert set(google["scope"][0].split()) == narrower
            rejected = await client.get(
                "https://docs.example.test/authorize",
                params={**params, "scope": GMAIL_SEND_SCOPE},
            )
            assert rejected.status_code == 302
            error = parse_qs(urlsplit(rejected.headers["location"]).query)
            assert error["error"] == ["invalid_scope"]

            registration = await client.post(
                "https://docs.example.test/register",
                json={
                    "redirect_uris": ["https://client.example.test/callback"],
                    "scope": GMAIL_SEND_SCOPE,
                },
            )
            assert registration.status_code == 400
            assert registration.json()["error"] == "invalid_client_metadata"
            assert GMAIL_SEND_SCOPE in registration.json()["error_description"]
    # Scope restriction is local to the lookup, not a destructive storage rewrite.
    stored = await provider._client_store.get(key=registered.client_id)
    assert GMAIL_SEND_SCOPE in stored.scope.split()
    with pytest.raises(AuthorizeError) as error:
        await provider.authorize(
            registered,
            AuthorizationParams(
                scopes=[GMAIL_SEND_SCOPE],
                state=None,
                code_challenge="a" * 43,
                redirect_uri=registered.redirect_uris[0],
                redirect_uri_provided_explicitly=True,
            ),
        )
    assert error.value.error == "invalid_scope"


@pytest.mark.parametrize("selection", ["service", "explicit_tools"])
def test_profile_scopes_follow_filtered_tools_and_cross_service_dependencies(
    configured_server, monkeypatch, selection
):
    source, _ = configured_server
    source.local_provider.remove_tool("create_doc")

    @source.tool
    async def search_drive_files():
        return "files"

    get_tool_components(source)["search_drive_files"].fn._required_google_scopes = [
        DRIVE_READONLY_SCOPE
    ]
    profile = {
        "url": "https://docs.example.test/mcp",
        "tools": ["search_drive_files"],
    }
    if selection == "service":
        profile["services"] = ["docs"]
    else:
        profile["tools"].append("get_doc_content")
    monkeypatch.setenv(PROFILES_ENV, json.dumps({"docs": profile}))
    app = source.http_app(stateless_http=True)
    provider = app.state.profile_servers["docs"].auth
    assert set(provider.client_registration_options.valid_scopes) == set(
        BASE_SCOPES
    ) | {DOCS_READONLY_SCOPE, DRIVE_READONLY_SCOPE}
