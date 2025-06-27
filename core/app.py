from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from .server import MCPSessionMiddleware, server

def create_app() -> Starlette:
    """
    Creates and configures the Starlette application instance,
    including the MCP server and any necessary middleware.
    """
    # Get the underlying FastAPI app from the FastMCP server
    app = server.streamable_http_app()

    # Add the session middleware to the app
    app.add_middleware(MCPSessionMiddleware)

    return app

# Create a single app instance to be imported by other modules
http_app = create_app()