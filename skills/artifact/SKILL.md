---
name: artifact
description: Store and load research results as immutable, content-addressed artifacts with versioned names and provenance metadata. Use when saving a table, JSON value, text, or file that later steps, notebooks, or records must reference exactly, or when loading such a result by name or hash.
---

# Artifact

Synchronous API, callable from the kernel and from marimo notebook cells:

    ref = artifact.put(df, name="samples", note="cleaned sample sheet")   # -> Ref(hash, kind, name, version)
    ref = artifact.get("samples")        # latest version; also "samples@2" or "sha256:<hex>"
    df = ref.load()                      # json -> object, table -> DataFrame, text -> str, bytes, file -> Path
    artifact.list(kind="table")          # latest version of each named artifact, with notes
    artifact.digest(df)                  # hash without storing

Rules:

- Kinds: `json` (dict, list, numbers, strings), `table` (pandas DataFrame, stored as parquet), `text`, `bytes`, `file` (a `Path`). Kind is inferred; other objects (numpy arrays, models) must be converted first.
- Putting identical content again returns the same hash and does not add a version.
- Fetched external data (database queries, downloads) should be stored as an artifact before it is used, so notebooks can pin it by hash.
- `ref.load()` for a `file` returns the read-only stored object; its original name is in `ref.meta()["filename"]`.
- The store lives in `$DARWIN_DATA_DIR`, else `$XDG_DATA_HOME/darwin`.
