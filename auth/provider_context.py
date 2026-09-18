"""Select the OAuth provider for one HTTP request, including nested tool calls."""

from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send


_UNSET = object()
_request_provider: ContextVar = ContextVar("workspace_oauth_provider", default=_UNSET)


def get_request_auth_provider(default=None):
    provider = _request_provider.get()
    return default if provider is _UNSET else provider


class AuthProviderContextMiddleware:
    """Keep token recovery on the same provider that authenticated the request."""

    def __init__(self, app: ASGIApp, provider):
        self.app = app
        self.provider = provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = _request_provider.set(self.provider)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_provider.reset(token)
