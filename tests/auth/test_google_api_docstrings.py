import tracemalloc

import httplib2
import pytest
from googleapiclient import schema
from googleapiclient.discovery import build

import auth.service_decorator  # noqa: F401  (importing the auth stack applies the patch)
from auth.google_api_docstrings import disable_schema_docstrings

# Without the patch these resources cost ~56 MiB (Sheets) and ~36 MiB (Docs) to
# build, once per tool call.
BUDGET_MIB = 5


def test_auth_stack_disables_schema_docstrings():
    assert schema.Schemas.prettyPrintByName("unused", "Spreadsheet") == (
        "object (Spreadsheet)"
    )
    assert schema.Schemas.prettyPrintSchema("unused", {"type": "object"}) == "object"


@pytest.mark.parametrize(
    "api, version, resource",
    [("sheets", "v4", "spreadsheets"), ("docs", "v1", "documents")],
)
def test_building_a_resource_stays_within_memory_budget(api, version, resource):
    disable_schema_docstrings()
    tracemalloc.start()
    try:
        service = build(api, version, http=httplib2.Http(), static_discovery=True)
        getattr(service, resource)()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < BUDGET_MIB * 2**20, f"{api} {resource}: {peak / 2**20:.1f} MiB"
