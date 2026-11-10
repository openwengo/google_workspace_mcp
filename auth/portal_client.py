"""A tool profile's confidential OAuth client for a Cloudflare MCP portal.

The portal is registered manually on the profile's authorization server, with a
client secret, instead of through DCR. Its users authorize from the profile's
own hostname. Cloudflare's backend redeems codes and refresh tokens on a
separate public hostname, which serves only POST /token. See
docs/tool-profiles.md.
"""

from dataclasses import dataclass

from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from pydantic import AnyUrl


# Cloudflare sends every manually configured OAuth server's users back here.
CLOUDFLARE_PORTAL_CALLBACK = "https://oauth-callbacks.cloudflareaccess.com/cdn-cgi/access/outbound-oauth-callback"
MIN_SECRET_LENGTH = 32


@dataclass(frozen=True)
class PortalSettings:
    edge_host: str
    client_id: str
    client_secret_ssm_parameter: str
    redirect_uris: tuple[str, ...]


def read_client_secret(parameter: str) -> str:
    """Read the client secret, a SecureString, with the pod's AWS credentials.

    The region comes from the environment (AWS_REGION, set by IRSA).
    """
    import boto3

    response = boto3.client("ssm").get_parameter(Name=parameter, WithDecryption=True)
    secret = response["Parameter"]["Value"]
    if (
        len(secret) < MIN_SECRET_LENGTH
        or not secret.isascii()
        or secret != secret.strip()
    ):
        raise ValueError(
            f"SSM parameter {parameter!r} must hold an ASCII client secret of at least "
            f"{MIN_SECRET_LENGTH} characters, without surrounding whitespace"
        )
    return secret


def build_portal_client(
    settings: PortalSettings, client_secret: str, scope: str
) -> ProxyDCRClient:
    """The fixed client a profile's provider returns for the portal's client_id.

    Basic only: the SDK then compares the Authorization header with this secret.
    Redirect URIs must match exactly (no patterns, no loopback port changes).
    """
    return ProxyDCRClient(
        client_id=settings.client_id,
        client_secret=client_secret,
        client_name="Cloudflare MCP portal",
        redirect_uris=[AnyUrl(uri) for uri in settings.redirect_uris],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
        token_endpoint_auth_method="client_secret_basic",
        allowed_redirect_uri_patterns=None,
    )
