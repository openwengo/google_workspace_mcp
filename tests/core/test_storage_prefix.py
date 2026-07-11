import pytest
from key_value.aio.stores.memory import MemoryStore

from core.server import _maybe_apply_storage_prefix


@pytest.mark.asyncio
async def test_storage_prefix_namespaces_collections():
    store = MemoryStore()
    prefixed_store = _maybe_apply_storage_prefix(store, "workspace-mcp-prod")

    await prefixed_store.put(
        key="client-123",
        value={"status": "ok"},
        collection="clients",
    )

    assert await store.get("client-123", collection="clients") is None
    assert await store.get(
        "client-123",
        collection="workspace-mcp-prod__clients",
    ) == {"status": "ok"}


def test_storage_prefix_noop_when_empty():
    store = MemoryStore()

    assert _maybe_apply_storage_prefix(store, "") is store
    assert _maybe_apply_storage_prefix(store, None) is store
