"""Google ID-token authentication alongside the existing human OAuth provider."""

import asyncio
import os
import time
from threading import Lock
from urllib.parse import urlsplit

import jwt
from fastmcp.server.auth import AccessToken, MultiAuth, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from pydantic import PrivateAttr

from auth.machine_policy import ISSUER, MachinePolicy, WifConfig, load_config


class MachineAccessToken(AccessToken):
    client_key: str
    endpoint: str
    _runtime: object = PrivateAttr()

    @property
    def runtime(self):
        return self._runtime

    @property
    def client(self):
        return self.runtime.policy.clients[self.client_key]

    @property
    def target(self):
        return self.runtime.policy.workspace_accounts[self.client.workspace_account]


def get_machine_token():
    try:
        token = get_access_token()
    except RuntimeError:
        return None
    return token if isinstance(token, MachineAccessToken) else None


def validate_machine_arguments(arguments):
    # Also cover aliases normalized later by CamelCaseArgumentsMiddleware.
    canonical = {
        key.replace("_", "").lower(): value for key, value in arguments.items()
    }
    if canonical.get("filepath") or (
        isinstance(canonical.get("fileurl"), str)
        and urlsplit(canonical["fileurl"]).scheme.lower() == "file"
    ):
        raise PermissionError(
            "Machine uploads accept inline content or HTTP(S) URLs, not local files"
        )


def machine_access_enabled():
    return os.getenv("MCP_MACHINE_ACCESS_ENABLED", "false").lower() in {
        "true",
        "1",
        "yes",
    }


class GoogleMachineVerifier(TokenVerifier):
    def __init__(self, runtime, endpoint):
        super().__init__(required_scopes=[])
        self.runtime, self.endpoint = runtime, endpoint
        self.audience = runtime.policy.endpoints[endpoint].audience
        self.jwks = jwt.PyJWKClient(
            "https://www.googleapis.com/oauth2/v3/certs", lifespan=300, timeout=10
        )
        # Bound cold/rotation JWKS fetch concurrency without blocking the event loop.
        self._jwks_lock = Lock()

    def _verify(self, raw):
        with self._jwks_lock:
            key = self.jwks.get_signing_key_from_jwt(raw).key
        claims = jwt.decode(
            raw,
            key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=self.audience,
            leeway=30,
            options={
                "require": [
                    "iss",
                    "sub",
                    "aud",
                    "exp",
                    "iat",
                    "email",
                    "email_verified",
                ],
                "strict_aud": True,
            },
        )
        if claims["email_verified"] is not True:
            return None
        for name, client in self.runtime.policy.clients.items():
            if client.subject == claims["sub"] and client.email == claims["email"]:
                if self.endpoint not in client.endpoints:
                    return None
                token = MachineAccessToken(
                    token=raw,
                    client_id=f"machine:{name}",
                    scopes=[],
                    expires_at=int(claims["exp"]),
                    claims=claims,
                    client_key=name,
                    endpoint=self.endpoint,
                )
                token._runtime = self.runtime
                return token
        return None

    async def verify_token(self, token):
        from auth.machine_audit import emit_audit

        try:
            if len(token) > 16384:
                return None
            # A routing hint only; authorization always verifies the signature.
            if jwt.get_unverified_header(token).get("alg") != "RS256":
                return None
            result = await asyncio.to_thread(self._verify, token)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            result = None
        if result is None:
            emit_audit(
                "workspace.machine.authentication_rejected", profile=self.endpoint
            )
        return result


class WorkspaceMultiAuth(MultiAuth):
    """Preserve the human scope gate when the shared protocol gate is empty."""

    def __init__(self, provider, runtime, endpoint):
        self.machine_runtime = runtime
        self.machine_verifier = GoogleMachineVerifier(runtime, endpoint)
        super().__init__(
            server=provider, verifiers=[self.machine_verifier], required_scopes=[]
        )

    async def verify_token(self, token):
        # Do not use unverified issuer claims to authorize a request.
        try:
            human = await self.server.verify_token(token)
        except Exception:
            human = None
        if human is not None:
            if set(self.server.required_scopes or []) <= set(human.scopes):
                return human
            return None
        return await self.machine_verifier.verify_token(token)


def human_auth_provider(provider):
    return provider.server if isinstance(provider, WorkspaceMultiAuth) else provider


def compose_machine_auth(provider, *, endpoint="full", runtime=None):
    if runtime is None:
        if not machine_access_enabled():
            return provider
        from auth.machine_credentials import MachineRuntime

        policy = load_config(os.environ["MCP_MACHINE_POLICY_FILE"], MachinePolicy)
        wif = load_config(os.environ["MCP_MACHINE_WIF_FILE"], WifConfig)
        runtime = MachineRuntime(policy, wif)
        from auth.machine_audit import configure_audit_logging, emit_audit

        configure_audit_logging()
        emit_audit("workspace.machine.policy_loaded", policy_version=policy.version)
    if endpoint not in runtime.policy.endpoints:
        return provider  # This profile remains human-only.
    return WorkspaceMultiAuth(provider, runtime, endpoint)


def validate_machine_endpoint(provider, endpoint_url):
    if isinstance(provider, WorkspaceMultiAuth):
        if provider.machine_verifier.audience != endpoint_url:
            raise ValueError(
                "Machine policy audience does not match the configured MCP endpoint"
            )


def require_live_machine(token):
    if token.expires_at is None or token.expires_at <= time.time():
        raise PermissionError("Machine token expired")
    if token.endpoint not in token.client.endpoints:
        raise PermissionError("Machine endpoint is not authorized")
