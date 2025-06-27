from typing import List, Set
from auth.scopes import BASE_SCOPES

_tool_scopes: Set[str] = set()

def register_tool_scopes(scopes: List[str]):
    """Adds a list of scopes to the global tool scope set."""
    for scope in scopes:
        _tool_scopes.add(scope)

def get_required_scopes() -> List[str]:
    """
    Returns a list of all unique scopes required by the enabled tools,
    plus the base scopes required for authentication.
    """
    # Combine base scopes with scopes from all registered tools
    all_scopes = set(BASE_SCOPES)
    all_scopes.update(_tool_scopes)
    return list(all_scopes)

def get_registered_tool_scopes() -> List[str]:
    """Returns only the scopes registered by tools, excluding base scopes."""
    return list(_tool_scopes)