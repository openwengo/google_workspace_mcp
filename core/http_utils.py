"""
SSRF-safe HTTP fetching utilities.

Provides async HTTP fetch functions with protection against SSRF attacks,
DNS rebinding, and redirect-based bypasses. Extracted from gdrive/drive_tools.py
for reuse across modules (Drive uploads, Gmail URL attachments, etc.).

``URL_FETCH_TRUSTED_PREFIXES`` (comma-separated URL prefixes, e.g.
``https://wengoagent.wengo.com/v1/links/``) lets URLs under those prefixes
resolve to private/internal addresses. Matching compares the parsed scheme,
hostname and port exactly and the path on segment boundaries; it is never a
raw string ``startswith``, so ``https://trusted.host@evil/`` cannot match.
"""

import ipaddress
import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import AsyncIterator, NamedTuple, Optional
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunparse

import httpx

logger = logging.getLogger(__name__)

_TRUSTED_PREFIXES_ENV = "URL_FETCH_TRUSTED_PREFIXES"
_DEFAULT_PORTS = {"http": 80, "https": 443}


class SSRFFetchError(RuntimeError):
    """Raised when SSRF-safe fetching fails after validation succeeds."""


class TrustedUrlPrefix(NamedTuple):
    scheme: str
    hostname: str
    port: int
    path: str


def _has_dot_segment(path: str) -> bool:
    """True if ``path`` could climb out of a prefix once a server decodes it.

    Percent-decodes until stable (catches ``%2e%2e`` and double encoding),
    treats ``\\`` as a separator, and ignores ``;params`` so ``..;`` counts.
    """
    for _ in range(5):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    for segment in path.replace("\\", "/").split("/"):
        if segment.split(";", 1)[0] in (".", ".."):
            return True
    return False


def _parse_trusted_prefix(raw: str) -> TrustedUrlPrefix:
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    error = f"Invalid {_TRUSTED_PREFIXES_ENV} entry {raw!r}"
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"{error}: scheme must be http or https.")
    if not parts.hostname:
        raise ValueError(f"{error}: missing hostname.")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{error}: credentials are not allowed.")
    if parts.query or parts.fragment:
        raise ValueError(f"{error}: query strings and fragments are not allowed.")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise ValueError(f"{error}: invalid port.") from exc
    path = parts.path or "/"
    if _has_dot_segment(path):
        raise ValueError(f"{error}: path must not contain '.' or '..' segments.")
    return TrustedUrlPrefix(scheme, parts.hostname, port, path)


@lru_cache(maxsize=4)
def _parse_trusted_prefixes(raw: str) -> tuple[TrustedUrlPrefix, ...]:
    return tuple(
        _parse_trusted_prefix(entry.strip())
        for entry in raw.split(",")
        if entry.strip()
    )


def get_trusted_url_prefixes() -> tuple[TrustedUrlPrefix, ...]:
    """Return the parsed ``URL_FETCH_TRUSTED_PREFIXES``; raise ``ValueError`` if invalid."""
    return _parse_trusted_prefixes(os.getenv(_TRUSTED_PREFIXES_ENV, ""))


def is_trusted_url(url: str) -> bool:
    """True if ``url`` falls under a configured ``URL_FETCH_TRUSTED_PREFIXES`` entry."""
    prefixes = get_trusted_url_prefixes()
    if not prefixes:
        return False

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return False
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return False
    path = parts.path or "/"
    if _has_dot_segment(path):
        return False

    for prefix in prefixes:
        if (scheme, parts.hostname, port) != (
            prefix.scheme,
            prefix.hostname,
            prefix.port,
        ):
            continue
        boundary = prefix.path if prefix.path.endswith("/") else f"{prefix.path}/"
        if path == prefix.path or path.startswith(boundary):
            return True
    return False


def redact_url(url: str) -> str:
    """Return a redacted URL safe for logs and exceptions."""
    parsed_url = urlparse(url)
    if not parsed_url.hostname:
        return "<redacted>"

    path = parsed_url.path or "/"
    return f"{parsed_url.hostname}{path}"


async def resolve_and_validate_host(
    hostname: str, *, allow_private: bool = False
) -> list[str]:
    """
    Resolve a hostname to IP addresses and validate none are private/internal.

    Uses getaddrinfo to handle both IPv4 and IPv6. Fails closed on DNS errors.
    ``allow_private`` skips the private/internal checks (DNS must still resolve);
    only pass it for URLs matched by ``URL_FETCH_TRUSTED_PREFIXES``.

    Returns:
        list[str]: Validated resolved IP address strings.

    Raises:
        ValueError: If hostname resolves to private/internal IPs or DNS fails.
    """
    if not hostname:
        raise ValueError("Invalid URL: no hostname")

    # Block localhost variants
    if not allow_private and hostname.lower() in (
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
    ):
        raise ValueError("URLs pointing to localhost are not allowed")

    # Resolve hostname using getaddrinfo (handles both IPv4 and IPv6)
    try:
        loop = asyncio.get_running_loop()
        addr_infos = await loop.run_in_executor(
            None, socket.getaddrinfo, hostname, None
        )
    except socket.gaierror as e:
        raise ValueError(
            f"Cannot resolve hostname '{hostname}': {e}. "
            "Refusing request (fail-closed)."
        )

    if not addr_infos:
        raise ValueError(f"No addresses found for hostname: {hostname}")

    resolved_ips: list[str] = []
    seen_ips: set[str] = set()
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        if not allow_private and not ip.is_global:
            raise ValueError(
                f"URLs pointing to private/internal networks are not allowed: "
                f"{hostname} resolves to {ip_str}"
            )
        if ip_str not in seen_ips:
            seen_ips.add(ip_str)
            resolved_ips.append(ip_str)

    return resolved_ips


async def validate_url_not_internal(url: str) -> list[str]:
    """
    Validate that a URL doesn't point to internal/private networks (SSRF protection).

    URLs under a ``URL_FETCH_TRUSTED_PREFIXES`` entry may resolve to private
    addresses. Called per redirect hop, so a trusted URL cannot redirect to an
    untrusted internal one.

    Returns:
        list[str]: Validated resolved IP addresses for the hostname.

    Raises:
        ValueError: If URL points to localhost or private IP ranges.
    """
    parsed = urlparse(url)
    allow_private = is_trusted_url(url)
    if allow_private:
        logger.info(
            f"[ssrf] {redact_url(url)} matches {_TRUSTED_PREFIXES_ENV}; "
            "private addresses allowed"
        )
    return await resolve_and_validate_host(parsed.hostname, allow_private=allow_private)


def format_host_header(hostname: str, scheme: str, port: Optional[int]) -> str:
    """Format the Host header value for IPv4/IPv6 hostnames."""
    host_value = hostname
    if ":" in host_value and not host_value.startswith("["):
        host_value = f"[{host_value}]"

    is_default_port = (scheme == "http" and (port is None or port == 80)) or (
        scheme == "https" and (port is None or port == 443)
    )
    if not is_default_port and port is not None:
        host_value = f"{host_value}:{port}"
    return host_value


def build_pinned_url(parsed_url, ip_address_str: str) -> str:
    """Build a URL that targets a resolved IP while preserving path/query."""
    pinned_host = ip_address_str
    if ":" in pinned_host and not pinned_host.startswith("["):
        pinned_host = f"[{pinned_host}]"

    userinfo = ""
    if parsed_url.username is not None:
        userinfo = parsed_url.username
        if parsed_url.password is not None:
            userinfo += f":{parsed_url.password}"
        userinfo += "@"

    port_part = f":{parsed_url.port}" if parsed_url.port is not None else ""
    netloc = f"{userinfo}{pinned_host}{port_part}"

    path = parsed_url.path or "/"
    return urlunparse(
        (
            parsed_url.scheme,
            netloc,
            path,
            parsed_url.params,
            parsed_url.query,
            parsed_url.fragment,
        )
    )


async def fetch_url_with_pinned_ip(
    url: str, *, timeout: Optional[httpx.Timeout] = None
) -> httpx.Response:
    """
    Fetch URL content by connecting to a validated, pre-resolved IP address.

    This prevents DNS rebinding between validation and the outbound connection.
    """
    parsed_url = urlparse(url)
    redacted_url = redact_url(url)
    if parsed_url.scheme not in ("http", "https"):
        raise ValueError(f"Only http:// and https:// are supported: {redacted_url}")
    if not parsed_url.hostname:
        raise ValueError(f"Invalid URL: missing hostname ({redacted_url})")

    resolved_ips = await validate_url_not_internal(url)
    host_header = format_host_header(
        parsed_url.hostname, parsed_url.scheme, parsed_url.port
    )

    last_error: Optional[Exception] = None
    for resolved_ip in resolved_ips:
        pinned_url = build_pinned_url(parsed_url, resolved_ip)
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=timeout
            ) as client:
                request = client.build_request(
                    "GET",
                    pinned_url,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": parsed_url.hostname},
                )
                return await client.send(request)
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning(
                f"[ssrf_safe_fetch] Failed request via resolved IP {resolved_ip} for host "
                f"{parsed_url.hostname}: {exc.__class__.__name__}"
            )

    raise SSRFFetchError(
        "Failed to fetch URL after trying "
        f"{len(resolved_ips)} validated IP(s): {redacted_url}"
    ) from last_error


async def ssrf_safe_fetch(
    url: str, *, timeout: Optional[httpx.Timeout] = None
) -> httpx.Response:
    """
    Fetch a URL with SSRF protection that covers redirects and DNS rebinding.

    Validates the initial URL and every redirect target against private/internal
    networks. Disables automatic redirect following and handles redirects manually.

    Args:
        url: The URL to fetch.

    Returns:
        httpx.Response with the final response content.

    Raises:
        ValueError: If any URL in the redirect chain points to a private network.
        SSRFFetchError: If the HTTP request fails.
    """
    max_redirects = 10
    current_url = url

    for _ in range(max_redirects):
        resp = await fetch_url_with_pinned_ip(current_url, timeout=timeout)
        redacted_current_url = redact_url(current_url)

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                raise SSRFFetchError(
                    f"Redirect with no Location header from {redacted_current_url}"
                )

            # Resolve relative redirects against the current URL
            location = urljoin(current_url, location)

            redirect_parsed = urlparse(location)
            if redirect_parsed.scheme not in ("http", "https"):
                raise ValueError(
                    f"Redirect to disallowed scheme: {redirect_parsed.scheme}"
                )

            current_url = location
            continue

        return resp

    raise SSRFFetchError(
        f"Too many redirects (max {max_redirects}) fetching {redact_url(url)}"
    )


@asynccontextmanager
async def ssrf_safe_stream(
    url: str,
    *,
    timeout: httpx.Timeout = httpx.Timeout(30.0, connect=5.0),
) -> AsyncIterator[httpx.Response]:
    """
    SSRF-safe streaming fetch: validates each redirect target against private
    networks, then streams the final response body without buffering it all
    in memory.

    Usage::

        async with ssrf_safe_stream(file_url) as resp:
            async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                ...
    """
    max_redirects = 10
    current_url = url

    # Resolve redirects manually so every hop is SSRF-validated
    for _ in range(max_redirects):
        parsed = urlparse(current_url)
        redacted_url = redact_url(current_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Only http:// and https:// are supported: {redacted_url}")
        if not parsed.hostname:
            raise ValueError(f"Invalid URL: missing hostname ({redacted_url})")

        resolved_ips = await validate_url_not_internal(current_url)
        host_header = format_host_header(parsed.hostname, parsed.scheme, parsed.port)

        last_error: Optional[Exception] = None
        resp: Optional[httpx.Response] = None
        for resolved_ip in resolved_ips:
            pinned_url = build_pinned_url(parsed, resolved_ip)
            client = httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=timeout
            )
            try:
                request = client.build_request(
                    "GET",
                    pinned_url,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": parsed.hostname},
                )
                resp = await client.send(request, stream=True)
                break
            except httpx.HTTPError as exc:
                last_error = exc
                await client.aclose()
                logger.warning(
                    f"[ssrf_safe_stream] Failed via IP {resolved_ip} for "
                    f"{parsed.hostname}: {exc.__class__.__name__}"
                )
            except Exception:
                await client.aclose()
                raise

        if resp is None:
            raise SSRFFetchError(
                f"Failed to fetch URL after trying {len(resolved_ips)} validated IP(s): "
                f"{redacted_url}"
            ) from last_error

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            await resp.aclose()
            await client.aclose()
            if not location:
                raise SSRFFetchError(
                    f"Redirect with no Location header from {redacted_url}"
                )
            location = urljoin(current_url, location)
            redirect_parsed = urlparse(location)
            if redirect_parsed.scheme not in ("http", "https"):
                raise ValueError(
                    f"Redirect to disallowed scheme: {redirect_parsed.scheme}"
                )
            current_url = location
            continue

        # Non-redirect — yield the streaming response
        try:
            yield resp
        finally:
            await resp.aclose()
            await client.aclose()
        return

    raise SSRFFetchError(
        f"Too many redirects (max {max_redirects}) fetching {redact_url(url)}"
    )
