"""A profile's Cloudflare MCP portal client and its token-only edge hostname."""

import base64
import hashlib
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from mcp.shared.auth import OAuthToken

import auth.portal_client as portal_client
from auth.portal_client import CLOUDFLARE_PORTAL_CALLBACK
from auth.scopes import BASE_SCOPES, DOCS_READONLY_SCOPE, DOCS_WRITE_SCOPE
from core.tool_profiles import PROFILES_ENV, load_tool_profiles
from tests.core.test_tool_profiles import (  # noqa: F401 (fixture)
    PROFILES,
    approve_consent,
    configured_server,
)


CLIENT_ID = "cloudflare-mcp-portal"
SECRET = "s" * 64
EDGE = "docs-edge.example.test"
PORTAL = {
    "edge_host": EDGE,
    "client_id": CLIENT_ID,
    "client_secret_ssm_parameter": "exploit.google_workspace_mcp.portal.docs",
}
DOCS_SCOPES = set(BASE_SCOPES) | {DOCS_READONLY_SCOPE, DOCS_WRITE_SCOPE}


@pytest.fixture
def portal_server(configured_server, monkeypatch):  # noqa: F811
    source, config = configured_server
    profiles = {**PROFILES, "docs": {**PROFILES["docs"], "portal": PORTAL}}
    monkeypatch.setenv(PROFILES_ENV, json.dumps(profiles))
    reads = []

    def read(parameter):
        reads.append(parameter)
        return SECRET

    monkeypatch.setattr("core.tool_profiles.read_client_secret", read)
    return source, reads


def basic(client_id=CLIENT_ID, secret=SECRET):
    raw = f"{client_id}:{secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def pkce():
    verifier = "v" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.mark.asyncio
async def test_edge_host_serves_post_token_only(portal_server):
    source, reads = portal_server
    app = source.http_app(stateless_http=True, json_response=True)
    assert reads == [PORTAL["client_secret_ssm_parameter"]]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            edge = f"https://{EDGE}"
            for method, path in [
                ("GET", "/authorize?client_id=x"),
                ("GET", "/consent"),
                ("GET", "/oauth2callback"),
                ("GET", "/.well-known/oauth-authorization-server"),
                ("GET", "/.well-known/oauth-protected-resource/mcp"),
                ("POST", "/mcp"),
                ("POST", "/register"),
                ("POST", "/revoke"),
                ("GET", "/token"),
                ("POST", "/token/"),
                ("POST", "//token"),
                ("GET", "/health"),
            ]:
                response = await client.request(method, edge + path)
                assert response.status_code == 404, (method, path, response.text)

            form = {
                "grant_type": "authorization_code",
                "code": "unknown",
                "code_verifier": "v" * 64,
            }
            refused = {
                "no credentials": ({}, {**form, "client_id": CLIENT_ID}),
                # What the portal sends; the edge gate Worker adds the client_id.
                "basic without client_id": (basic(), form),
                "wrong secret": (
                    basic(secret="x" * 64),
                    {**form, "client_id": CLIENT_ID},
                ),
                "secret in the form": (
                    {},
                    {**form, "client_id": CLIENT_ID, "client_secret": SECRET},
                ),
                "other client in basic": (
                    basic(client_id="other"),
                    {**form, "client_id": CLIENT_ID},
                ),
            }
            for case, (headers, data) in refused.items():
                response = await client.post(
                    edge + "/token", headers=headers, data=data
                )
                assert response.status_code == 401, (case, response.text)
                assert response.json()["error"] == "invalid_client", case

            accepted = await client.post(
                edge + "/token", headers=basic(), data={**form, "client_id": CLIENT_ID}
            )
            # Authenticated: only the code is refused (FastMCP answers 401 here).
            assert accepted.json()["error"] == "invalid_grant", accepted.text

            # The profile's own hostname is unchanged, and other profiles don't
            # know the portal client.
            assert (
                await client.post("https://docs.example.test/mcp")
            ).status_code == 401
            assert (
                await app.state.profile_servers["sheets"].auth.get_client(CLIENT_ID)
                is None
            )
            assert await source.auth.get_client(CLIENT_ID) is None


@pytest.mark.asyncio
async def test_portal_authorizes_on_profile_host_and_redeems_on_edge(portal_server):
    source, _ = portal_server
    app = source.http_app(stateless_http=True, json_response=True)
    provider = app.state.profile_servers["docs"].auth
    portal = await provider.get_client(CLIENT_ID)
    assert portal.token_endpoint_auth_method == "client_secret_basic"
    assert set(portal.scope.split()) == DOCS_SCOPES
    verifier, challenge = pkce()
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": CLOUDFLARE_PORTAL_CALLBACK,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "portal-state",
        "resource": "https://docs.example.test/mcp",
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            authorized = await client.get(
                "https://docs.example.test/authorize", params=params
            )
            assert authorized.status_code == 302, authorized.text
            location = authorized.headers["location"]
            assert location.startswith("https://docs.example.test/consent")
            google = await approve_consent(client, provider, location)
            assert set(google["scope"][0].split()) == DOCS_SCOPES

            unregistered = await client.get(
                "https://docs.example.test/authorize",
                params={**params, "redirect_uri": "https://evil.example.test/cb"},
            )
            assert unregistered.status_code == 400
            assert "evil.example.test" not in unregistered.headers.get("location", "")

            # What the Google callback stores, then the portal's code exchange.
            await provider._code_store.put(
                key="portal-code",
                value=ClientCode(
                    code="portal-code",
                    client_id=CLIENT_ID,
                    redirect_uri=CLOUDFLARE_PORTAL_CALLBACK,
                    code_challenge=challenge,
                    code_challenge_method="S256",
                    scopes=sorted(DOCS_SCOPES),
                    idp_tokens={},
                    expires_at=time.time() + 60,
                    created_at=time.time(),
                ),
            )
            provider.exchange_authorization_code = AsyncMock(
                return_value=OAuthToken(access_token="issued", token_type="Bearer")
            )
            token = await client.post(
                f"https://{EDGE}/token",
                headers=basic(),
                data={
                    "grant_type": "authorization_code",
                    "code": "portal-code",
                    "code_verifier": verifier,
                    "redirect_uri": CLOUDFLARE_PORTAL_CALLBACK,
                    "client_id": CLIENT_ID,
                    "resource": "https://docs.example.test/mcp",
                },
            )
            assert token.status_code == 200, token.text
            assert token.json()["access_token"] == "issued"
            client_arg = provider.exchange_authorization_code.await_args.args[0]
            assert client_arg.client_id == CLIENT_ID


@pytest.mark.parametrize(
    "change,match",
    [
        ({"edge_host": "docs.example.test"}, "hostname of its own"),
        ({"edge_host": "sheets.example.test"}, "different hostname"),
        ({"edge_host": "full.example.test"}, "full endpoint"),
        ({"edge_host": "Docs-Edge.example.test"}, "lowercase hostname"),
        ({"edge_host": "docs-edge.example.test:8443"}, "lowercase hostname"),
        ({"client_id": "google-client"}, "differ from the Google client id"),
        ({"client_id": "bad id"}, "client_id"),
        ({"client_secret_ssm_parameter": ""}, "SSM parameter name"),
        ({"redirect_uris": ["http://portal.example.test/cb"]}, "HTTPS"),
        ({"redirect_uris": []}, "empty"),
        ({"secret": SECRET}, "Unknown settings"),
    ],
)
def test_invalid_portal_settings_fail_at_startup(
    portal_server, monkeypatch, change, match
):
    source, reads = portal_server
    portal = {**PORTAL, **change}
    profiles = {**PROFILES, "docs": {**PROFILES["docs"], "portal": portal}}
    if change.get("edge_host") == "sheets.example.test":
        # The edge host comes first, then a profile reuses it.
        profiles = {"docs": profiles["docs"], "sheets": PROFILES["sheets"]}
    monkeypatch.setenv(PROFILES_ENV, json.dumps(profiles))
    with pytest.raises(ValueError, match=match):
        source.http_app(stateless_http=True)
    assert reads == []


def test_missing_secret_stops_startup(portal_server, monkeypatch):
    source, _ = portal_server

    def missing(parameter):
        raise RuntimeError(f"ParameterNotFound: {parameter}")

    monkeypatch.setattr("core.tool_profiles.read_client_secret", missing)
    with pytest.raises(RuntimeError, match="ParameterNotFound"):
        source.http_app(stateless_http=True)


def test_http_profile_cannot_have_a_portal(monkeypatch):
    profiles = {
        "docs": {
            "url": "http://localhost/mcp",
            "services": ["docs"],
            "portal": PORTAL,
        }
    }
    monkeypatch.setenv(PROFILES_ENV, json.dumps(profiles))
    with pytest.raises(ValueError, match="HTTPS url"):
        load_tool_profiles()


@pytest.mark.parametrize(
    "value,ok",
    [(SECRET, True), ("short", False), (" " + SECRET, False), ("é" * 40, False)],
)
def test_read_client_secret_decrypts_and_checks(monkeypatch, value, ok):
    calls = []

    class FakeSSM:
        def get_parameter(self, **kwargs):
            calls.append(kwargs)
            return {"Parameter": {"Value": value}}

    import boto3

    monkeypatch.setattr(boto3, "client", lambda service: FakeSSM())
    if ok:
        assert portal_client.read_client_secret("p") == value
    else:
        with pytest.raises(ValueError) as error:
            portal_client.read_client_secret("p")
        assert value.strip() not in str(error.value)
    assert calls == [{"Name": "p", "WithDecryption": True}]


def test_parsed_portal_defaults(monkeypatch):
    profiles = {"docs": {**PROFILES["docs"], "portal": PORTAL}}
    monkeypatch.setenv(PROFILES_ENV, json.dumps(profiles))
    (profile,) = load_tool_profiles()
    assert profile.portal.edge_host == EDGE
    assert profile.portal.redirect_uris == (CLOUDFLARE_PORTAL_CALLBACK,)
