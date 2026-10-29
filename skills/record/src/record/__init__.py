"""Commit research records with provenance, and reproduce them from their notebook.

A record ties a validated spec (for example an experiment design) to the clean
notebook run that produced its numbers: notebook file hash, pinned input
hashes, output hashes, and the kernel interpreter.
"""

from __future__ import annotations

import builtins
import getpass
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import artifact
from pair import RunResult, clean_run

_KIND = re.compile(r"^[a-z][a-z0-9_]*$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    spec TEXT NOT NULL,
    notebook TEXT NOT NULL,
    inputs TEXT NOT NULL,
    outputs TEXT NOT NULL,
    env TEXT NOT NULL,
    author TEXT NOT NULL,
    session TEXT,
    rationale TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Record:
    id: int
    kind: str
    spec: str
    notebook: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    env: str
    author: str
    session: str | None
    rationale: str
    created_at: float

    def load_spec(self) -> Any:
        return artifact.get(self.spec).load()


def commit(
    kind: str, spec: Mapping[str, Any], run: RunResult, *, rationale: str = ""
) -> Record:
    """Store `spec` as a record backed by the clean notebook `run`.

    Refuses runs whose notebook or outputs are missing from the store; a run
    result only exists after `await nb.verify(...)` succeeded.
    """
    if not isinstance(run, RunResult):
        raise TypeError(
            f"run must be the result of `await nb.verify(...)`, got {type(run).__name__}; did you forget `await`?"
        )
    if not _KIND.match(kind):
        raise ValueError(f"record kind {kind!r} must match {_KIND.pattern}")
    for ref in (run.notebook, *run.outputs.values()):
        artifact.get(ref.hash)  # raises KeyError for a fabricated run
    spec_ref = artifact.put(
        dict(spec), kind="json", parents=[run.notebook, *run.outputs.values()]
    )
    outputs = {name: ref.hash for name, ref in run.outputs.items()}
    row = (
        kind,
        spec_ref.hash,
        run.notebook.hash,
        json.dumps(run.inputs),
        json.dumps(outputs),
        run.env,
        getpass.getuser(),
        os.environ.get("RLM_SESSION_ID"),
        rationale,
        time.time(),
    )
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO records (kind, spec, notebook, inputs, outputs, env, author, session, rationale, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        assert cur.lastrowid is not None
        record_id = cur.lastrowid
    return show(record_id)


def show(record_id: int) -> Record:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ?", (record_id,)
        ).fetchone()
    if row is None:
        raise KeyError(record_id)
    return _from_row(row)


# Named `list` so the API reads record.commit/show/list; `builtins.list` is the builtin here.
def list(kind: str | None = None) -> builtins.list[Record]:
    query = "SELECT * FROM records"
    params: tuple[str, ...] = ()
    if kind is not None:
        query, params = query + " WHERE kind = ?", (kind,)
    with _connect() as conn:
        rows = conn.execute(query + " ORDER BY id", params).fetchall()
    return [_from_row(r) for r in rows]


async def reproduce(record_id: int) -> dict[str, tuple[str, str]]:
    """Re-run the record's exact notebook in a fresh process.

    Returns mismatching outputs as `{name: (recorded_hash, new_hash)}`; empty means reproduced.
    """
    rec = show(record_id)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"record_{record_id}.py"
        shutil.copyfile(artifact.get(rec.notebook).path, path)
        path.chmod(0o644)
        refs = await clean_run(
            path,
            f"reproduce/{record_id}",
            builtins.list(rec.outputs),
            cwd=Path(tmp),
            env=json.loads(rec.env),
        )
    return {
        name: (expected, refs[name].hash)
        for name, expected in rec.outputs.items()
        if refs[name].hash != expected
    }


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    with artifact.connect() as conn:
        conn.executescript(_SCHEMA)
        yield conn


def _from_row(row: tuple[Any, ...]) -> Record:
    return Record(
        id=row[0],
        kind=row[1],
        spec=row[2],
        notebook=row[3],
        inputs=json.loads(row[4]),
        outputs=json.loads(row[5]),
        env=row[6],
        author=row[7],
        session=row[8],
        rationale=row[9],
        created_at=row[10],
    )
