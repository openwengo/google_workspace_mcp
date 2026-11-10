"""Serve fixed tool subsets on separate hostnames within one HTTP process."""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import json
import logging
import os
import re
from urllib.parse import urlsplit

from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.http import StarletteWithLifespan
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Route
from starlette.websockets import WebSocketClose

from auth.oauth_config import get_oauth_config
from auth.machine_auth import compose_machine_auth, human_auth_provider
from auth.portal_client import (
    CLOUDFLARE_PORTAL_CALLBACK,
    PortalSettings,
    build_portal_client,
    read_client_secret,
)
from auth.scopes import BASE_SCOPES
from core.tool_registry import get_tool_components
from core.tool_tier_loader import ToolTierLoader


logger = logging.getLogger(__name__)
PROFILES_ENV = "WORKSPACE_MCP_TOOL_PROFILES"
_RESERVED_PATHS = {
    "/health",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
    "/consent",
    "/oauth2callback",
}


@dataclass(frozen=True)
class ToolProfile:
    key: str
    name: str
    origin: str
    host: str
    path: str
    services: tuple[str, ...]
    tools: tuple[str, ...]
    tier: str
    portal: PortalSettings | None = None


_HOSTNAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


def _portal_settings(key: str, value) -> PortalSettings:
    if not isinstance(value, dict):
        raise ValueError(f"Tool profile {key!r}: portal must be an object")
    unknown = set(value) - {
        "edge_host",
        "client_id",
        "client_secret_ssm_parameter",
        "redirect_uris",
    }
    if unknown:
        raise ValueError(
            f"Unknown settings in tool profile {key!r} portal: {sorted(unknown)}"
        )
    edge_host = value.get("edge_host")
    if not isinstance(edge_host, str) or not _HOSTNAME.fullmatch(edge_host):
        raise ValueError(
            f"Tool profile {key!r}: portal.edge_host must be a lowercase hostname"
        )
    client_id = value.get("client_id")
    if not isinstance(client_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", client_id
    ):
        raise ValueError(
            f"Tool profile {key!r}: portal.client_id must be 1-128 letters, digits, '.', '_' or '-'"
        )
    parameter = value.get("client_secret_ssm_parameter")
    if not isinstance(parameter, str) or not re.fullmatch(
        r"[A-Za-z0-9_./-]{1,2048}", parameter
    ):
        raise ValueError(
            f"Tool profile {key!r}: portal.client_secret_ssm_parameter must be an SSM parameter name"
        )
    redirect_uris = _string_list(
        value.get("redirect_uris", [CLOUDFLARE_PORTAL_CALLBACK]),
        f"{key}.portal.redirect_uris",
    )
    for uri in redirect_uris:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError(
                f"Tool profile {key!r}: portal.redirect_uris must be HTTPS URLs without credentials or fragment"
            )
    if not redirect_uris:
        raise ValueError(f"Tool profile {key!r}: portal.redirect_uris is empty")
    return PortalSettings(edge_host, client_id, parameter, redirect_uris)


class PortalTokenEndpoint:
    """A portal's edge hostname: POST /token of the profile app, nothing else.

    Cloudflare's backend redeems codes and refresh tokens on that public name.
    Everything else (/mcp, /authorize, /consent, /register, metadata) stays on
    the profile's own hostname.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == "/token"
        ):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocketClose()(scope, receive, send)
        else:
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)


def _string_list(value, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be an array of non-empty strings")
    return tuple(value)


def load_tool_profiles() -> list[ToolProfile]:
    """Validate configuration before constructing any endpoint or OAuth provider."""
    raw = os.getenv(PROFILES_ENV, "").strip()
    if not raw:
        return []
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{PROFILES_ENV} must be a JSON object") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{PROFILES_ENV} must be a JSON object keyed by profile name")

    profiles = []
    hosts = set()
    services_available = set(ToolTierLoader().get_available_services())
    for key, entry in config.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key):
            raise ValueError(f"Invalid tool profile name: {key!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"Tool profile {key!r} must be an object")
        unknown = set(entry) - {"url", "name", "services", "tools", "tier", "portal"}
        if unknown:
            raise ValueError(
                f"Unknown settings in tool profile {key!r}: {sorted(unknown)}"
            )
        url = entry.get("url")
        if not isinstance(url, str):
            raise ValueError(f"Tool profile {key!r} requires a public endpoint URL")
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            not host
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme != "https"
                and not (parsed.scheme == "http" and host in {"localhost", "127.0.0.1"})
            )
        ):
            raise ValueError(
                f"Tool profile {key!r} requires an HTTPS URL without credentials, query or fragment"
            )
        port = parsed.port  # Also validates malformed/out-of-range ports.
        authority = host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        path = parsed.path or "/mcp"
        if not re.fullmatch(r"/[A-Za-z0-9_-]+", path) or path in _RESERVED_PATHS:
            raise ValueError(
                f"Tool profile {key!r} needs a distinct MCP path, such as /mcp"
            )
        if host in hosts:
            raise ValueError("Each tool profile must use a different hostname")
        hosts.add(host)
        services = _string_list(entry.get("services", []), f"{key}.services")
        if set(services) - services_available:
            raise ValueError(
                f"Unknown services in tool profile {key!r}: {sorted(set(services) - services_available)}"
            )
        tools = _string_list(entry.get("tools", []), f"{key}.tools")
        tier = entry.get("tier", "complete")
        if tier not in ("core", "extended", "complete"):
            raise ValueError(f"Invalid tier in tool profile {key!r}: {tier!r}")
        name = entry.get("name", f"Google Workspace: {key}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Tool profile {key!r} needs a non-empty name")
        if not services and not tools:
            raise ValueError(f"Tool profile {key!r} requires services or tools")
        portal = None
        if "portal" in entry:
            portal = _portal_settings(key, entry["portal"])
            if parsed.scheme != "https":
                raise ValueError(f"Tool profile {key!r}: a portal needs an HTTPS url")
            if portal.edge_host in hosts:
                raise ValueError(
                    f"Tool profile {key!r}: portal.edge_host must be a hostname of its own"
                )
            hosts.add(portal.edge_host)
        profiles.append(
            ToolProfile(
                key,
                name,
                f"{parsed.scheme}://{authority}",
                host,
                path,
                services,
                tools,
                tier,
                portal,
            )
        )
    return profiles


def select_profile_tools(profile: ToolProfile, available: dict) -> list:
    """Intersect service selections with the already-filtered global registry."""
    unavailable = set(profile.tools) - available.keys()
    if unavailable:
        raise ValueError(
            f"Tool profile {profile.key!r} explicitly requests unavailable tools: "
            f"{sorted(unavailable)}. Check names, loaded services and global restrictions."
        )
    names = set(profile.tools)
    if profile.services:
        names.update(
            ToolTierLoader().get_tools_up_to_tier(profile.tier, list(profile.services))
        )
    selected = [available[name] for name in sorted(names) if name in available]
    if not selected:
        raise ValueError(f"Tool profile {profile.key!r} has no enabled tools")
    return selected


def get_profile_scopes(tools: list) -> list[str]:
    """Include identity and the API scopes declared by the selected tool functions.

    Service decorators also record dependencies on other APIs, such as Drive
    comments on a document. Reading the filtered tools avoids requesting scopes
    for tools excluded by a tier, read-only mode, permissions or the block list.
    """
    scopes = set(BASE_SCOPES)
    for tool in tools:
        fn = getattr(tool, "fn", tool)
        scopes.update(getattr(fn, "_required_google_scopes", []))
    return sorted(scopes)


def build_profile_http_app(source, default_app, **http_kwargs):
    """Compose independently authenticated MCP applications behind Host routes."""
    profiles = load_tool_profiles()
    machine_runtime = getattr(source.auth, "machine_runtime", None)
    if machine_runtime is not None:
        unknown = set(machine_runtime.policy.endpoints) - {
            "full",
            *(p.key for p in profiles),
        }
        if unknown:
            raise ValueError(
                f"Machine policy references unconfigured profiles: {sorted(unknown)}"
            )
    if not profiles:
        return default_app

    from core.server import (
        SecureFastMCP,
        _maybe_apply_storage_prefix,
        create_google_auth_provider,
        health_check,
    )

    config = get_oauth_config()
    source_provider = human_auth_provider(source.auth)
    if (
        not config.is_oauth21_enabled()
        or config.is_external_oauth21_provider()
        or not isinstance(source_provider, GoogleProvider)
    ):
        raise ValueError(
            f"{PROFILES_ENV} requires the built-in Google OAuth 2.1 provider"
        )
    if http_kwargs.get("transport", "http") not in ("http", "streamable-http"):
        raise ValueError(f"{PROFILES_ENV} requires streamable HTTP transport")

    default_hosts = {
        urlsplit(url).hostname
        for url in (config.get_oauth_base_url(), config.base_uri, config.base_url)
        if url
    } - {None}
    available = get_tool_components(source)
    selections = []
    for profile in profiles:
        if profile.host in default_hosts:
            raise ValueError(
                f"Tool profile {profile.key!r} must not replace the full endpoint's hostname"
            )
        if profile.path == config.redirect_path:
            raise ValueError(
                f"Tool profile {profile.key!r} overlaps the OAuth callback path"
            )
        if profile.portal is not None:
            if profile.portal.edge_host in default_hosts:
                raise ValueError(
                    f"Tool profile {profile.key!r}: portal.edge_host must not be the full endpoint's hostname"
                )
            if profile.portal.client_id == config.client_id:
                raise ValueError(
                    f"Tool profile {profile.key!r}: portal.client_id must differ from the Google client id"
                )
        selections.append((profile, select_profile_tools(profile, available)))
    # Read every secret before building anything: a missing one stops the startup.
    portal_secrets = {
        profile.key: read_client_secret(profile.portal.client_secret_ssm_parameter)
        for profile, _ in selections
        if profile.portal is not None
    }

    apps = [default_app]
    # Portal edge hostnames come first: /health and every other route stay off them.
    edge_routes = []
    routes = [Route("/health", health_check, methods=["GET"])]
    profile_servers = {}
    for profile, tools in selections:
        # Share the existing encrypted backend/connection, adding only a collection
        # namespace. The full endpoint retains its existing keys and token audience.
        provider = create_google_auth_provider(
            config=config,
            base_url=profile.origin,
            client_storage=_maybe_apply_storage_prefix(
                source_provider._client_storage, f"profile_{profile.key}"
            ),
            jwt_signing_key=source_provider._jwt_signing_key,
            scopes=get_profile_scopes(tools),
        )
        hosts = [profile.host]
        if profile.portal is not None:
            provider.portal_client = build_portal_client(
                profile.portal,
                portal_secrets[profile.key],
                provider._default_scope_str,
            )
            hosts.append(profile.portal.edge_host)
        view = SecureFastMCP(
            name=profile.name,
            auth=compose_machine_auth(
                provider, endpoint=profile.key, runtime=machine_runtime
            ),
            tools=[tool.model_copy() for tool in tools],
            middleware=list(source.middleware),
            dereference_schemas=False,  # Already included in the copied middleware.
        )
        child_kwargs = {
            **http_kwargs,
            "path": profile.path,
            "allowed_hosts": hosts,
            "allowed_origins": [
                *(http_kwargs.get("allowed_origins") or []),
                profile.origin,
            ],
        }
        child = view.http_app(**child_kwargs)
        profile_servers[profile.key] = view
        apps.append(child)
        routes.append(Host(profile.host, app=child, name=profile.key))
        logger.info(
            "Tool profile %s: %s%s (%d tools)",
            profile.key,
            profile.origin,
            profile.path,
            len(tools),
        )
        if profile.portal is not None:
            edge_routes.append(
                Host(
                    profile.portal.edge_host,
                    app=PortalTokenEndpoint(child),
                    name=f"{profile.key}_portal",
                )
            )
            logger.info(
                "Tool profile %s: portal client %s, token endpoint https://%s/token",
                profile.key,
                profile.portal.client_id,
                profile.portal.edge_host,
            )
    routes.extend(Host(host, app=default_app) for host in sorted(default_hosts))

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for child in apps:
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    app = StarletteWithLifespan(routes=[*edge_routes, *routes], lifespan=lifespan)
    app.state.path = getattr(default_app.state, "path", "/mcp")
    app.state.profile_servers = profile_servers
    return app
