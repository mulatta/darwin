"""Content-addressed artifact store shared by the notebook and record skills.

Artifacts are immutable bytes keyed by sha256. Names are versioned pointers
(`name@3`) to artifact hashes. Metadata and names live in one SQLite index
next to the objects, so the notebook and record skills share the same store.
"""

from __future__ import annotations

import builtins
import getpass
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KINDS = ("json", "table", "text", "bytes", "file")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL,
    meta TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS names (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    hash TEXT NOT NULL REFERENCES artifacts(hash),
    created_at REAL NOT NULL,
    PRIMARY KEY (name, version)
);
"""


def root() -> Path:
    """Store root: `$DARWIN_DATA_DIR`, else `$XDG_DATA_HOME/darwin`."""
    override = os.environ.get("DARWIN_DATA_DIR")
    if override:
        return Path(override)
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "darwin"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open the shared index; commits on success, rolls back on error."""
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(base / "index.sqlite")
    try:
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class Ref:
    """A stored artifact. Pickles as its fields only, so kernel snapshots stay small."""

    hash: str
    kind: str
    name: str | None = None
    version: int | None = None

    @property
    def path(self) -> Path:
        """Read-only object file inside the store."""
        return _object_path(self.hash)

    def load(self) -> Any:
        """Decode the artifact: json -> object, table -> DataFrame, text -> str,
        bytes -> bytes, file -> Path of the stored object."""
        if self.kind == "json":
            return json.loads(self.path.read_text(encoding="utf-8"))
        if self.kind == "table":
            import pandas as pd

            return pd.read_parquet(self.path)
        if self.kind == "text":
            return self.path.read_text(encoding="utf-8")
        if self.kind == "bytes":
            return self.path.read_bytes()
        return self.path

    def meta(self) -> dict[str, Any]:
        """Provenance metadata recorded on first put: note, parents, env, author, session."""
        with connect() as conn:
            row = conn.execute(
                "SELECT meta FROM artifacts WHERE hash = ?", (self.hash,)
            ).fetchone()
        if row is None:
            raise KeyError(self.hash)
        meta: dict[str, Any] = json.loads(row[0])
        return meta


def put(
    obj: Any,
    name: str | None = None,
    *,
    kind: str | None = None,
    note: str = "",
    parents: Iterable[Ref | str] = (),
) -> Ref:
    """Store `obj` and return its Ref; storing identical content again is a no-op.

    `kind` is inferred: Path -> file, bytes -> bytes, str -> text,
    DataFrame -> table, anything else -> json. With `name`, a new version is
    added only when the content differs from the latest version.
    """
    kind = kind or _infer_kind(obj)
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    if name is not None and (not name or "@" in name):
        raise ValueError(
            f"artifact name {name!r} must be non-empty and must not contain '@' (the version separator)"
        )
    digest, size = _write_object(obj, kind)
    meta = {
        "note": note,
        "parents": [p.hash if isinstance(p, Ref) else p for p in parents],
        "env": sys.prefix,
        "author": getpass.getuser(),
        "session": os.environ.get("RLM_SESSION_ID"),
    }
    if kind == "file":
        meta["filename"] = Path(obj).name
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO artifacts (hash, kind, size, created_at, meta) VALUES (?, ?, ?, ?, ?)",
            (digest, kind, size, now, json.dumps(meta)),
        )
        if name is None:
            return Ref(digest, kind)
        latest = conn.execute(
            "SELECT version, hash FROM names WHERE name = ? ORDER BY version DESC LIMIT 1",
            (name,),
        ).fetchone()
        if latest is not None and latest[1] == digest:
            return Ref(digest, kind, name, latest[0])
        version = 1 if latest is None else latest[0] + 1
        conn.execute(
            "INSERT INTO names (name, version, hash, created_at) VALUES (?, ?, ?, ?)",
            (name, version, digest, now),
        )
    return Ref(digest, kind, name, version)


def get(key: str) -> Ref:
    """Look up `sha256:<hex>`, `name@<version>`, or `name` (latest version)."""
    with connect() as conn:
        if key.startswith("sha256:"):
            row = conn.execute(
                "SELECT kind FROM artifacts WHERE hash = ?", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            return Ref(key, row[0])
        name, _, version = key.rpartition("@") if "@" in key else (key, "", "")
        query = "SELECT n.hash, a.kind, n.version FROM names n JOIN artifacts a ON a.hash = n.hash WHERE n.name = ?"
        if version and not version.isdigit():
            raise ValueError(f"version in {key!r} must be an integer, e.g. {name}@1")
        if version:
            row = conn.execute(
                query + " AND n.version = ?", (name, int(version))
            ).fetchone()
        else:
            row = conn.execute(
                query + " ORDER BY n.version DESC LIMIT 1", (name,)
            ).fetchone()
    if row is None:
        raise KeyError(key)
    return Ref(row[0], row[1], name, row[2])


# Named `list` so the API reads artifact.put/get/list; `builtins.list` is the builtin here.
def list(kind: str | None = None) -> builtins.list[dict[str, Any]]:
    """Latest version of every named artifact: name, version, hash, kind, size, note."""
    query = """
        SELECT n.name, n.version, n.hash, a.kind, a.size, a.meta
        FROM names n JOIN artifacts a ON a.hash = n.hash
        WHERE n.version = (SELECT MAX(version) FROM names WHERE name = n.name)
    """
    params: tuple[str, ...] = ()
    if kind is not None:
        query += " AND a.kind = ?"
        params = (kind,)
    with connect() as conn:
        rows = conn.execute(query + " ORDER BY n.name", params).fetchall()
    return [
        {
            "name": r[0],
            "version": r[1],
            "hash": r[2],
            "kind": r[3],
            "size": r[4],
            "note": json.loads(r[5])["note"],
        }
        for r in rows
    ]


def digest(obj: Any, kind: str | None = None) -> str:
    """Hash `obj` the way `put` would, without storing it."""
    kind = kind or _infer_kind(obj)
    if kind == "file":
        return _hash_file(Path(obj))
    return "sha256:" + hashlib.sha256(_encode(obj, kind)).hexdigest()


def _infer_kind(obj: Any) -> str:
    if isinstance(obj, Path):
        return "file"
    if isinstance(obj, bytes):
        return "bytes"
    if isinstance(obj, str):
        return "text"
    if type(obj).__name__ == "DataFrame" and type(obj).__module__.startswith("pandas"):
        return "table"
    return "json"


def _encode(obj: Any, kind: str) -> bytes:
    if kind == "json":
        return json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    if kind == "table":
        buf = io.BytesIO()
        obj.to_parquet(buf)
        return buf.getvalue()
    if kind == "text":
        return str(obj).encode()
    if kind == "bytes":
        return bytes(obj)
    raise ValueError(f"kind {kind!r} is not encoded in memory")


def _object_path(digest: str) -> Path:
    hexdigest = digest.removeprefix("sha256:")
    return root() / "objects" / hexdigest[:2] / hexdigest[2:]


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _write_object(obj: Any, kind: str) -> tuple[str, int]:
    if kind == "file":
        source = Path(obj)
        digest = _hash_file(source)
        size = source.stat().st_size
    else:
        data = _encode(obj, kind)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        size = len(data)
    target = _object_path(digest)
    if target.exists():
        return digest, size
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a crash never leaves a partial object under its hash.
    fd, tmp = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            if kind == "file":
                with Path(obj).open("rb") as src:
                    shutil.copyfileobj(src, f)
            else:
                f.write(data)
        os.chmod(tmp, 0o444)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return digest, size
