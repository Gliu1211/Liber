"""Offloaded originals: the blob store and the content cache (ARCHITECTURE.md §4 offload, §7, §8)."""

import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from liber.cache import content
from liber.cache.blobs import BlobStore
from liber.model.anthropic import parse
from liber.model.items import Kind, Operation, OperationKind
from liber.plan import versions
from liber.session import chain
from liber.session.store import Store
from tests.helpers import FIXTURES, recorded


def test_blobs_are_stored_by_hash_once_and_readable_only_by_the_owner(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    data = b'{"text":"a long build log","type":"text"}'
    digest = hashlib.sha256(data).hexdigest()

    path = blobs.put(digest, data)
    again = blobs.put(digest, data)

    assert path == again == tmp_path / "blobs" / digest[:2] / digest
    assert blobs.get(digest) == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [digest]  # no temporary files left behind


def test_a_blob_must_match_its_hash_and_names_cannot_escape_the_store(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs")

    with pytest.raises(ValueError, match="match"):
        blobs.put("0" * 64, b"not that")
    with pytest.raises(ValueError, match="SHA-256"):
        blobs.path_for("../../etc/passwd")
    assert blobs.get("f" * 64) is None


async def test_offloading_keeps_the_original_where_the_content_cache_says(
    app: FastAPI, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    request = json.loads((FIXTURES / "requests" / "long_session_101.json").read_text(encoding="utf-8"))
    headers = {"content-type": "application/json"}
    await client.post("/v1/messages", content=json.dumps(request).encode(), headers=headers)
    [first] = await recorded(app)
    assert first.session_id is not None
    tool_result = next(i for i in parse(request).items if i.kind is Kind.TOOL_RESULT)
    store: Store = app.state.store
    await versions.create(store, first.session_id, (Operation(kind=OperationKind.OFFLOAD, item_ids=(tool_result.id,)),))
    await versions.apply(store, first.session_id)

    await client.post("/v1/messages", content=json.dumps(request).encode(), headers=headers)
    await recorded(app)  # waits for the background writes
    entry = await store.run(lambda db: content.lookup(db.connection(), tool_result.content_hash))

    assert entry is not None and entry.blob_path is not None
    stored = app.state.blobs.get(tool_result.content_hash)
    assert entry.blob_path == str(app.state.blobs.path_for(tool_result.content_hash))
    assert stored == chain.canonical_block(tool_result.block).encode("utf-8")
    assert hashlib.sha256(stored).hexdigest() == tool_result.content_hash
