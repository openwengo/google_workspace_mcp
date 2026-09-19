"""Keep profile authorization within the scopes of its enabled tools."""

from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull


class ProfileGoogleProvider(GoogleProvider):
    """Apply the current scope limit to persisted and CIMD client registrations."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = await super().get_client(client_id)
        if client is None:
            return None
        # Registrations created before profile-specific scopes may contain the
        # full workspace scope set. CIMD documents can also declare broader scopes.
        # Narrow the request-local copy without rewriting shared client records.
        allowed = set(self.client_registration_options.valid_scopes)
        scopes = set((client.scope or self._default_scope_str).split()) & allowed
        return client.model_copy(update={"scope": " ".join(sorted(scopes))})

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        allowed = set(self.client_registration_options.valid_scopes)
        if params.scopes is None:
            # The SDK leaves an omitted scope unset, and the upstream proxy then
            # falls back to protocol identity scopes only. Use this client's
            # permitted profile scopes so its tools work after authorization.
            scopes = set((client.scope or self._default_scope_str).split()) & allowed
            params = params.model_copy(update={"scopes": sorted(scopes)})
        if set(params.scopes or []) - allowed:
            raise AuthorizeError(
                error="invalid_scope",
                error_description="Requested scopes are not available on this tool profile",
            )
        return await super().authorize(client, params)
