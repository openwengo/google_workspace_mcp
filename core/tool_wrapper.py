import inspect
import os
import re
import logging
from functools import wraps
from auth.context import get_user_email_from_header

logger = logging.getLogger(__name__)
EMAIL_IN_HEADER = os.getenv("EMAIL_IN_HEADER", "0") == "1"

def _remove_email_from_docstring(doc: str) -> str:
    """Removes the user_google_email line from the Args section of a docstring."""
    if not doc:
        return ""
    # This regex is designed to remove the specific user_google_email line.
    pattern = re.compile(r"^\s*user_google_email\s+\(str\):.*\n?", re.MULTILINE)
    return pattern.sub("", doc)

def tool(server_instance):
    """
    A wrapper for the MCP server's @tool decorator that dynamically modifies
    the tool's signature and docstring based on the EMAIL_IN_HEADER env var.
    """
    def decorator(func):
        try:
            original_sig = inspect.signature(func)
            has_email_param = 'user_google_email' in original_sig.parameters
        except (TypeError, ValueError):
            # If we can't inspect the signature, we can't modify it.
            return server_instance.tool()(func)

        if not (EMAIL_IN_HEADER and has_email_param):
            # If the feature is disabled or the tool doesn't have the email param,
            # register it as-is without modification.
            return server_instance.tool()(func)

        # --- Proceed with creating the modified tool ---
        logger.debug(f"Dynamically modifying tool '{func.__name__}' for header-based email.")

        # Create a new signature that omits the 'user_google_email' parameter.
        # This is the signature the LLM will see.
        new_params = [p for p in original_sig.parameters.values() if p.name != 'user_google_email']
        new_sig = original_sig.replace(parameters=new_params)

        # This wrapper is what the MCP server will actually call.
        @wraps(func)
        async def wrapper(*args, **kwargs):
            logger.debug(f"Executing wrapped tool '{func.__name__}' with args: {args}, kwargs: {kwargs}")
            
            # Bind the arguments that were passed to the wrapper using the NEW signature.
            bound_args = new_sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            
            # Get the email from the context (set by the middleware).
            email_from_header = get_user_email_from_header()
            if not email_from_header:
                raise ValueError("Header 'x-user-email' is required but was not found in the request context.")
            
            # Add the email to the arguments that will be passed to the ORIGINAL function.
            final_arguments = bound_args.arguments
            final_arguments['user_google_email'] = email_from_header
            
            logger.debug(f"Calling original function '{func.__name__}' with final arguments: {final_arguments}")
            
            # Call the original function with all arguments as keywords to avoid positional errors.
            return await func(**final_arguments)

        # Update the wrapper's metadata to reflect the changes.
        wrapper.__signature__ = new_sig
        
        original_doc = inspect.getdoc(func)
        if original_doc:
            wrapper.__doc__ = _remove_email_from_docstring(original_doc)
        
        # Register the fully-configured wrapper with the MCP server.
        return server_instance.tool()(wrapper)

    return decorator