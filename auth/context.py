"""
Context management for MCP session IDs and OAuth credentials.

This module provides ContextVar-based storage for request-scoped data that needs
to be accessible throughout the MCP request lifecycle without explicit parameter passing.
"""

import logging
from contextvars import ContextVar
from typing import Optional, Any

logger = logging.getLogger(__name__)

# ContextVar for storing the current MCP session ID
_current_mcp_session_id: ContextVar[Optional[str]] = ContextVar(
    'current_mcp_session_id', 
    default=None
)

# ContextVar for storing injected OAuth credentials
_injected_oauth_credentials: ContextVar[Optional[Any]] = ContextVar(
    'injected_oauth_credentials', 
    default=None
)


def get_current_mcp_session_id() -> Optional[str]:
    """
    Retrieves the MCP session ID from the current context.
    
    Returns:
        Optional[str]: The MCP session ID if set, None otherwise.
    """
    session_id = _current_mcp_session_id.get()
    logger.debug(f"Retrieved MCP session ID from context: {session_id}")
    return session_id


def set_current_mcp_session_id(session_id: Optional[str]) -> None:
    """
    Sets the MCP session ID for the current context.
    
    Args:
        session_id: The MCP session ID to store in the context.
    """
    _current_mcp_session_id.set(session_id)
    logger.debug(f"Set MCP session ID in context: {session_id}")


def get_injected_oauth_credentials() -> Optional[Any]:
    """
    Retrieves the injected OAuth credentials from the current context.
    
    Returns:
        Optional[Any]: The OAuth credentials if set, None otherwise.
    """
    credentials = _injected_oauth_credentials.get()
    if credentials:
        logger.debug("Retrieved OAuth credentials from context")
    else:
        logger.debug("No OAuth credentials found in context")
    return credentials


def set_injected_oauth_credentials(credentials: Any) -> None:
    """
    Sets the OAuth credentials for the current context.
    
    Args:
        credentials: The OAuth credentials to store in the context.
    """
    _injected_oauth_credentials.set(credentials)
    logger.debug("Set OAuth credentials in context")


def reset_injected_oauth_credentials() -> None:
    """
    Resets the injected OAuth credentials for the current context.
    """
    _injected_oauth_credentials.set(None)
    logger.debug("Reset OAuth credentials in context")


def clear_context() -> None:
    """
    Clears all context variables. Useful for cleanup or testing.
    """
    _current_mcp_session_id.set(None)
    _injected_oauth_credentials.set(None)
    logger.debug("Cleared all context variables")