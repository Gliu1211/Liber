"""The on-disk blob store: offloaded content, kept by its hash (ARCHITECTURE.md §4 offload, §8).

When a plan offloads an item, the model gets a pointer note instead of the content, and the
original is kept here so the user can see it and restore it (undo). Layout:

    <data dir>/blobs/<first 2 hex digits of the hash>/<full SHA-256 hex>

Content-addressed: a blob's name is the SHA-256 of what's in it (the item's canonical JSON,
session/chain.py), so the same content is stored once, a blob is never rewritten, and a
corrupted or swapped file can be detected. Files are readable by the owner only (§11).
Writes happen off the request path (liber/app.py runs them as background tasks).
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

HASH = re.compile(r"[0-9a-f]{64}")


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, content_hash: str) -> Path:
        # Only a real SHA-256 hex name: anything else could point outside the store ("../..").
        if not HASH.fullmatch(content_hash):
            raise ValueError("not a SHA-256 hex digest")
        return self.root / content_hash[:2] / content_hash

    def put(self, content_hash: str, data: bytes) -> Path:
        """Store `data` under its hash; does nothing if it's already there. Returns its path."""
        path = self.path_for(content_hash)
        if hashlib.sha256(data).hexdigest() != content_hash:
            raise ValueError("the data doesn't match its hash")
        if self.get(content_hash) is not None:
            return path  # already there and intact (a damaged file is rewritten below)
        # mkdir's mode only applies to the directory it creates last, so make the root first.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        # Write to a temporary file in the same directory, then rename: a reader (or a crash)
        # never sees a half-written blob. mkstemp creates the file with mode 0600.
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return path

    def get(self, content_hash: str) -> bytes | None:
        """The blob's bytes, or None if it's missing or damaged (its bytes don't match its name)."""
        path = self.path_for(content_hash)
        if not path.exists():
            return None
        data = path.read_bytes()
        return data if hashlib.sha256(data).hexdigest() == content_hash else None
