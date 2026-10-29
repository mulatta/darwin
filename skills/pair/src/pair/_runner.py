"""Clean-process entry point.

python _runner.py <notebook.py> <result.json> <label> <name>...

Runs the marimo app with no state from the caller, stores each named
definition as the artifact `<label>/<name>`, and writes their refs as JSON.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import artifact


def main(argv: list[str]) -> int:
    path, result, label, *names = argv
    # SourceFileLoader accepts any filename, so stored notebook objects run without a .py suffix.
    loader = importlib.machinery.SourceFileLoader("_notebook_under_run", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    _, defs = module.app.run()
    missing = [name for name in names if name not in defs]
    if missing:
        print(f"not defined by the notebook: {', '.join(missing)}", file=sys.stderr)
        return 2
    refs = {}
    for name in names:
        try:
            refs[name] = artifact.put(defs[name], name=f"{label}/{name}")
        except (TypeError, ValueError) as error:
            kind = type(defs[name]).__name__
            print(
                f"output {name!r} ({kind}) cannot be stored as an artifact: {error}\n"
                f"convert it in its cell first: .tolist() or a dict for JSON, a pandas DataFrame for tables, str, bytes, or a Path",
                file=sys.stderr,
            )
            return 3
    data = {
        name: {"hash": r.hash, "kind": r.kind, "name": r.name, "version": r.version}
        for name, r in refs.items()
    }
    Path(result).write_text(json.dumps(data), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
