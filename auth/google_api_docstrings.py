"""
Skip googleapiclient's schema pretty-printing for generated method docstrings.

When a discovery-based service exposes a resource (``service.spreadsheets()``,
``service.documents()``, ...), googleapiclient builds a docstring for every
method by pretty-printing its request and response schemas. The result is
memoised per service instance, and we build a service for every tool call, so
each call regenerates it. For Sheets' ``batchUpdate`` this takes ~74 MiB at peak
and holds ~35 MiB until the call returns: 20 concurrent Sheets calls were enough
to exhaust a 575 MiB pod. The docstrings only serve interactive ``help()``.
"""

from googleapiclient import schema as _schema

_ORIGINALS = {
    "prettyPrintByName": _schema.Schemas.prettyPrintByName,
    "prettyPrintSchema": _schema.Schemas.prettyPrintSchema,
}


def _schema_name_only(self, name):
    return f"object ({name})"


def _schema_placeholder(self, schema):
    return "object"


def disable_schema_docstrings() -> None:
    """Replace the schema pretty-printers with constant placeholders (idempotent)."""
    _schema.Schemas.prettyPrintByName = _schema_name_only
    _schema.Schemas.prettyPrintSchema = _schema_placeholder


def restore_schema_docstrings() -> None:
    """Restore googleapiclient's original pretty-printers (used by tests)."""
    for name, method in _ORIGINALS.items():
        setattr(_schema.Schemas, name, method)
