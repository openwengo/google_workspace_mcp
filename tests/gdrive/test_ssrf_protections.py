"""
Unit tests for SSRF protections and DNS pinning helpers.
"""

import os
import socket
import sys

import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core import http_utils


@pytest.mark.asyncio
async def test_resolve_and_validate_host_fails_closed_on_dns_error(monkeypatch):
    """DNS resolution failures must fail closed."""

    def fake_getaddrinfo(hostname, port):
        raise socket.gaierror("mocked resolution failure")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="Refusing request \\(fail-closed\\)"):
        await http_utils.resolve_and_validate_host("example.com")


@pytest.mark.asyncio
async def test_resolve_and_validate_host_rejects_ipv6_private(monkeypatch):
    """IPv6 internal addresses must be rejected."""

    def fake_getaddrinfo(hostname, port):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("fd00::1", 0, 0, 0),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private/internal networks"):
        await http_utils.resolve_and_validate_host("ipv6-internal.example")


@pytest.mark.asyncio
async def test_resolve_and_validate_host_deduplicates_addresses(monkeypatch):
    """Duplicate DNS answers should be de-duplicated while preserving order."""

    def fake_getaddrinfo(hostname, port):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 0),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 0),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0),
            ),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert await http_utils.resolve_and_validate_host("example.com") == [
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    ]


@pytest.mark.asyncio
async def test_fetch_url_with_pinned_ip_uses_pinned_target_and_host_header(monkeypatch):
    """Requests should target a validated IP while preserving Host + SNI hostname."""
    captured = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method, url, headers=None, extensions=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["extensions"] = extensions or {}
            return {"url": url}

        async def send(self, request):
            return httpx.Response(200, request=httpx.Request("GET", request["url"]))

    async def fake_validate_url_not_internal(_url):
        return ["93.184.216.34"]

    monkeypatch.setattr(
        http_utils, "validate_url_not_internal", fake_validate_url_not_internal
    )
    monkeypatch.setattr(http_utils.httpx, "AsyncClient", FakeAsyncClient)

    response = await http_utils.fetch_url_with_pinned_ip(
        "https://example.com/path/to/file.txt?x=1"
    )

    assert response.status_code == 200
    assert captured["method"] == "GET"
    assert captured["url"] == "https://93.184.216.34/path/to/file.txt?x=1"
    assert captured["headers"]["Host"] == "example.com"
    assert captured["extensions"]["sni_hostname"] == "example.com"
    assert captured["client_kwargs"]["trust_env"] is False
    assert captured["client_kwargs"]["follow_redirects"] is False
    assert captured["client_kwargs"]["timeout"] is None


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_threads_timeout_to_pinned_fetch(monkeypatch):
    """Timeouts should flow through ssrf_safe_fetch to the pinned fetch helper."""
    captured = {}
    timeout = httpx.Timeout(5.0)

    async def fake_fetch(url, *, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        return httpx.Response(200, request=httpx.Request("GET", url), content=b"ok")

    monkeypatch.setattr(http_utils, "fetch_url_with_pinned_ip", fake_fetch)

    response = await http_utils.ssrf_safe_fetch(
        "https://example.com/start", timeout=timeout
    )

    assert response.status_code == 200
    assert captured["url"] == "https://example.com/start"
    assert captured["timeout"] is timeout


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_follows_relative_redirects(monkeypatch):
    """Relative redirects should be resolved and re-checked."""
    calls = []

    async def fake_fetch(url, *, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(
                302,
                headers={"location": "/next"},
                request=httpx.Request("GET", url),
            )
        return httpx.Response(200, request=httpx.Request("GET", url), content=b"ok")

    monkeypatch.setattr(http_utils, "fetch_url_with_pinned_ip", fake_fetch)

    response = await http_utils.ssrf_safe_fetch("https://example.com/start")

    assert response.status_code == 200
    assert calls == ["https://example.com/start", "https://example.com/next"]


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_rejects_disallowed_redirect_scheme(monkeypatch):
    """Redirects to non-http(s) schemes should be blocked."""

    async def fake_fetch(url, *, timeout=None):
        return httpx.Response(
            302,
            headers={"location": "file:///etc/passwd"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(http_utils, "fetch_url_with_pinned_ip", fake_fetch)

    with pytest.raises(ValueError, match="Redirect to disallowed scheme"):
        await http_utils.ssrf_safe_fetch("https://example.com/start")


@pytest.mark.asyncio
async def test_fetch_url_with_pinned_ip_redacts_secret_query_in_errors():
    with pytest.raises(ValueError) as exc_info:
        await http_utils.fetch_url_with_pinned_ip(
            "ftp://user:pass@example.com/path/to/file?token=secret#fragment"
        )

    message = str(exc_info.value)
    assert "example.com/path/to/file" in message
    assert "token=secret" not in message
    assert "fragment" not in message


@pytest.mark.asyncio
async def test_ssrf_safe_fetch_redacts_redirect_source_in_errors(monkeypatch):
    async def fake_fetch(url, *, timeout=None):
        return httpx.Response(
            302,
            headers={},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(http_utils, "fetch_url_with_pinned_ip", fake_fetch)

    with pytest.raises(http_utils.SSRFFetchError) as exc_info:
        await http_utils.ssrf_safe_fetch("https://example.com/start?token=secret")

    message = str(exc_info.value)
    assert "example.com/start" in message
    assert "token=secret" not in message


TRUSTED_PREFIX = "https://wengoagent.wengo.com/v1/links/"


def _resolve_to(ip_str):
    def fake_getaddrinfo(hostname, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip_str, 0))]

    return fake_getaddrinfo


@pytest.mark.asyncio
async def test_trusted_prefix_allows_private_address(monkeypatch):
    monkeypatch.setenv("URL_FETCH_TRUSTED_PREFIXES", TRUSTED_PREFIX)
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("172.16.48.33"))

    assert await http_utils.validate_url_not_internal(
        "https://wengoagent.wengo.com/v1/links/abc?format=csv"
    ) == ["172.16.48.33"]


@pytest.mark.asyncio
async def test_trusted_prefix_does_not_cover_other_paths(monkeypatch):
    monkeypatch.setenv("URL_FETCH_TRUSTED_PREFIXES", TRUSTED_PREFIX)
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("172.16.48.33"))

    with pytest.raises(ValueError, match="private/internal networks"):
        await http_utils.validate_url_not_internal(
            "https://wengoagent.wengo.com/v1/admin"
        )


@pytest.mark.asyncio
async def test_private_address_rejected_without_trusted_prefixes(monkeypatch):
    monkeypatch.delenv("URL_FETCH_TRUSTED_PREFIXES", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("172.16.48.33"))

    with pytest.raises(ValueError, match="private/internal networks"):
        await http_utils.validate_url_not_internal(TRUSTED_PREFIX + "abc")


def test_trusted_prefixes_accept_comma_separated_list(monkeypatch):
    monkeypatch.setenv(
        "URL_FETCH_TRUSTED_PREFIXES",
        f" {TRUSTED_PREFIX} , http://files.internal:8080/exports ,",
    )

    assert http_utils.is_trusted_url(TRUSTED_PREFIX + "abc")
    assert http_utils.is_trusted_url("http://files.internal:8080/exports")
    assert http_utils.is_trusted_url("http://files.internal:8080/exports/a.xlsx")


@pytest.mark.parametrize(
    "url",
    [
        # Host confusion: prefix host as userinfo or as a subdomain label.
        "https://wengoagent.wengo.com@evil.example/v1/links/abc",
        "https://wengoagent.wengo.com.evil.example/v1/links/abc",
        # Scheme and port must match exactly.
        "http://wengoagent.wengo.com/v1/links/abc",
        "https://wengoagent.wengo.com:8443/v1/links/abc",
        # Traversal out of the prefix, raw and encoded.
        "https://wengoagent.wengo.com/v1/links/../admin",
        "https://wengoagent.wengo.com/v1/links/%2e%2e/admin",
        "https://wengoagent.wengo.com/v1/links/%252e%252e/admin",
        "https://wengoagent.wengo.com/v1/links/..%2fadmin",
        "https://wengoagent.wengo.com/v1/links/..;/admin",
        "https://wengoagent.wengo.com/v1/links/..\\admin",
        # The prefix's trailing slash is part of the match.
        "https://wengoagent.wengo.com/v1/links",
        "https://wengoagent.wengo.com/v1/linksX/abc",
    ],
)
def test_trusted_prefix_rejects_lookalike_urls(monkeypatch, url):
    monkeypatch.setenv("URL_FETCH_TRUSTED_PREFIXES", TRUSTED_PREFIX)

    assert not http_utils.is_trusted_url(url)


def test_trusted_prefix_without_trailing_slash_matches_on_segment_boundary(
    monkeypatch,
):
    monkeypatch.setenv(
        "URL_FETCH_TRUSTED_PREFIXES", "https://wengoagent.wengo.com/v1/links"
    )

    assert http_utils.is_trusted_url("https://wengoagent.wengo.com/v1/links")
    assert http_utils.is_trusted_url("https://wengoagent.wengo.com/v1/links/abc")
    assert not http_utils.is_trusted_url("https://wengoagent.wengo.com/v1/linksX")


def test_trusted_prefix_host_and_scheme_are_case_insensitive(monkeypatch):
    monkeypatch.setenv("URL_FETCH_TRUSTED_PREFIXES", TRUSTED_PREFIX)

    assert http_utils.is_trusted_url("HTTPS://WengoAgent.Wengo.com:443/v1/links/x")
    assert not http_utils.is_trusted_url("https://wengoagent.wengo.com/V1/links/x")


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://wengoagent.wengo.com/v1/links/",
        "wengoagent.wengo.com/v1/links/",
        "https://user:pass@wengoagent.wengo.com/v1/links/",
        "https://wengoagent.wengo.com/v1/links/?token=x",
        "https://wengoagent.wengo.com/v1/../links/",
        "https://wengoagent.wengo.com:notaport/v1/links/",
    ],
)
def test_invalid_trusted_prefix_raises(monkeypatch, raw):
    monkeypatch.setenv("URL_FETCH_TRUSTED_PREFIXES", raw)

    with pytest.raises(ValueError, match="URL_FETCH_TRUSTED_PREFIXES"):
        http_utils.get_trusted_url_prefixes()


@pytest.mark.asyncio
async def test_trusted_url_cannot_redirect_to_untrusted_private_url(monkeypatch):
    """Each redirect hop is re-validated against the trusted prefixes."""
    monkeypatch.setenv("URL_FETCH_TRUSTED_PREFIXES", TRUSTED_PREFIX)
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("172.16.48.33"))
    requested = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def build_request(self, method, url, headers=None, extensions=None):
            return {"url": url, "host": headers["Host"]}

        async def send(self, request):
            requested.append(request["host"])
            return httpx.Response(
                302,
                headers={"location": "https://wengoagent.wengo.com/v1/admin"},
                request=httpx.Request("GET", request["url"]),
            )

    monkeypatch.setattr(http_utils.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(ValueError, match="private/internal networks"):
        await http_utils.ssrf_safe_fetch(TRUSTED_PREFIX + "abc")

    assert requested == ["wengoagent.wengo.com"]
