"""Work directly in a live marimo notebook kernel and verify it with clean runs.

`open()` starts a headless marimo server for one notebook file (or reattaches
to it) and holds an editor session, so code runs in the notebook's own kernel:
`scratch()` for throwaway exploration, `cell()` for code that becomes part of
the notebook file (written as a plain function whose body is the cell), and
`define()` for functions other cells call. `verify()` re-runs the saved file
in a fresh process with the kernel's interpreter and checks it reproduces the
live values.
"""

from __future__ import annotations

import ast
import asyncio
import html
import inspect
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import artifact
import httpx

_PINNED = re.compile(
    r"""^\s*(\w+) = artifact\.get\(["'](sha256:[0-9a-f]{64})["']\)\.load\(\)$""",
    re.MULTILINE,
)
_VAR = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SESSION = "s_pair"
_TEMPLATE = """\
# /// script
# [tool.marimo.venv]
# path = {venv}
# ///

import marimo

app = marimo.App()


if __name__ == "__main__":
    app.run()
"""


class PairError(RuntimeError):
    """Code sent to the notebook kernel failed; the message carries its traceback."""


class ReproductionError(RuntimeError):
    """A clean run produced values that differ from the live kernel."""


@dataclass(frozen=True)
class RunResult:
    """Provenance of one clean run: the exact notebook file, its pinned inputs, its outputs."""

    notebook: artifact.Ref
    inputs: dict[str, str]
    outputs: dict[str, artifact.Ref]
    env: str


@dataclass(frozen=True)
class Cell:
    name: str
    status: str
    errors: list[str]
    output: str

    def __call__(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError(
            f"{self.name} lives in the notebook; call it from a cell (nb.cell(fn) whose body uses {self.name})"
        )


class Notebook:
    def __init__(self, path: Path, state: Mapping[str, Any]) -> None:
        self.path = path
        self.url: str = state["url"]
        self._state = dict(state)
        self._headers = {
            "Marimo-Session-Id": _SESSION,
            "Authorization": f"Bearer {state['token']}",
        }

    def __repr__(self) -> str:
        return f"<pair.Notebook {self.path} at {self.url}>"

    async def scratch(self, code: str) -> str:
        """Run `code` in the kernel without saving it; returns printed output and the last expression."""
        return await self._execute(code)

    async def cell(
        self, cell: Callable[..., Any] | str, code: str | None = None
    ) -> Cell:
        """Create or replace a cell, run it and its dependents, and save the file.

        `cell(fn)` saves the body of the plain function `fn` as the cell named
        `fn.__name__`: its parameters document the names it reads, and a final
        `return` (the names it exports) is dropped. `cell(name, code)` takes the
        code as a string, for cells that are not Python (markdown, SQL).
        The cell is saved even when it fails; the PairError says how to fix or drop it.
        """
        if isinstance(cell, str):
            if code is None:
                raise TypeError(f"cell({cell!r}) needs code: cell({cell!r}, code)")
            name = cell
        else:
            name, code = cell.__name__, _cell_code(cell)
        try:
            await self._run_cell(name, code)
        except PairError as error:
            raise PairError(
                f"{error}\ncell {name!r} is saved with this error: fix it with cell({name!r}, ...) or remove it with delete({name!r})"
            ) from None
        return await self.status(name)

    async def define(self, fn: Callable[..., Any]) -> Cell:
        """Save the whole function `fn` as its own cell so other cells can call it.

        Rebind the name to the result (`dilute = await nb.define(dilute)`): the
        function runs in the notebook, not here.
        """
        return await self.cell(fn.__name__, _source(fn)[1])

    async def _run_cell(self, name: str, code: str) -> None:
        await self._execute(
            "import marimo._code_mode as _cm\n"
            "async with _cm.get_context() as _ctx:\n"
            f"    if {name!r} in _ctx.cells:\n"
            f"        _ctx.edit_cell({name!r}, {code!r}, hide_code=False)\n"
            f"        _cid = {name!r}\n"
            "    else:\n"
            f"        _cid = _ctx.create_cell({code!r}, name={name!r}, hide_code=False)\n"
            "    _ctx.run_cell(_cid)\n"
        )

    async def delete(self, name: str) -> None:
        """Delete the cell `name`; the variables it defined leave the kernel."""
        await self._execute(
            "import marimo._code_mode as _cm\n"
            "async with _cm.get_context() as _ctx:\n"
            f"    _ctx.delete_cell({name!r})\n"
        )

    async def status(self, name: str) -> Cell:
        out = await self._execute(
            "import json as _json\n"
            "import marimo._code_mode as _cm\n"
            "async with _cm.get_context() as _ctx:\n"
            f"    _c = _ctx.cells[{name!r}]\n"
            "    _o = _c.output\n"
            "    print(_json.dumps({'status': str(_c.status), 'errors': [str(e) for e in _c.errors],"
            " 'output': '' if _o is None else str(_o.data)}))\n"
        )
        data = json.loads(out.splitlines()[-1])
        return Cell(name, data["status"], data["errors"], _text(data["output"]))

    async def input(self, var: str, ref: artifact.Ref | str) -> None:
        """Pin an artifact into the `inputs` cell by hash, so clean runs read exactly this version."""
        if not _VAR.match(var) or var == "artifact":
            raise ValueError(
                f"input name {var!r} must be an identifier other than 'artifact'"
            )
        ref = ref if isinstance(ref, artifact.Ref) else artifact.get(ref)
        current = await self._execute(
            "import marimo._code_mode as _cm\n"
            "async with _cm.get_context() as _ctx:\n"
            "    print(_ctx.cells['inputs'].code if 'inputs' in _ctx.cells else '')\n"
        )
        lines = [
            line
            for line in current.splitlines()
            if line.strip()
            and line != "import artifact"
            and not line.startswith(f"{var} = ")
        ]
        lines.append(f"{var} = artifact.get({ref.hash!r}).load()")
        await self.cell("inputs", "\n".join(["import artifact", *lines]))

    async def verify(
        self, outputs: Sequence[str], label: str | None = None
    ) -> RunResult:
        """Re-run the saved notebook in a fresh process and store `outputs` as `<label>/<name>` artifacts.

        Raises ReproductionError when a clean value differs from the live kernel
        value, which means the kernel holds state the notebook does not produce.
        """
        label = label or self.path.stem
        probe = await self._execute(
            "import json as _json, os as _os, sys as _sys\n"
            "import artifact as _artifact\n"
            "import marimo._code_mode as _cm\n"
            "async with _cm.get_context() as _ctx:\n"
            "    _g = _ctx.globals\n"
            f"    _names = {list(outputs)!r}\n"
            "    print(_json.dumps({'python': _sys.executable, 'pythonpath': _os.environ.get('PYTHONPATH', ''),"
            " 'digests': {n: _artifact.digest(_g[n]) for n in _names if n in _g}}))\n"
        )
        kernel = json.loads(probe.splitlines()[-1])
        missing = [name for name in outputs if name not in kernel["digests"]]
        if missing:
            raise ValueError(f"not defined in the live kernel: {', '.join(missing)}")
        env = {"python": kernel["python"], "pythonpath": kernel["pythonpath"]}
        notebook_ref = artifact.put(self.path, name=f"notebook/{label}", kind="file")
        refs = await clean_run(self.path, label, outputs, cwd=self.path.parent, env=env)
        mismatches = [
            f"{name}: kernel={kernel['digests'][name]} clean={ref.hash}"
            for name, ref in refs.items()
            if kernel["digests"][name] != ref.hash
        ]
        if mismatches:
            raise ReproductionError(
                f"clean run of {self.path.name} differs from the live kernel "
                "(a value was changed outside its cell, or depends on unsaved state):\n"
                + "\n".join(mismatches)
            )
        source = self.path.read_text(encoding="utf-8")
        inputs = dict(_PINNED.findall(source))
        return RunResult(notebook_ref, inputs, refs, json.dumps(env))

    async def close(self) -> None:
        """Stop the server and its kernel."""
        _stop(self._state)
        _registry_path(self.path).unlink(missing_ok=True)

    async def _execute(self, code: str) -> str:
        stdout: list[str] = []
        stderr: list[str] = []
        done: dict[str, Any] = {}
        async with (
            httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client,
            client.stream(
                "POST",
                f"{self.url}/api/kernel/execute",
                json={"code": code},
                headers=self._headers,
            ) as response,
        ):
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")
                raise PairError(
                    f"marimo server answered {response.status_code}: {body}"
                )
            event = "message"
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line.partition(":")[2])
                    if event == "stdout":
                        stdout.append(payload["data"])
                    elif event == "stderr":
                        stderr.append(payload["data"])
                    elif event == "done":
                        done = payload
        if not done.get("success"):
            raise PairError("".join(stderr) or "execution failed without a traceback")
        output = _text(str((done.get("output") or {}).get("data") or ""))
        return "".join(stdout) + (output + "\n" if output else "")


# Named `open` so the API reads pair.open(path); nothing here uses the builtin.
async def open(
    path: str | os.PathLike[str], *, venv: str | os.PathLike[str] | None = None
) -> Notebook:
    """Open the marimo notebook at `path`, creating it if needed, and attach to its live kernel.

    A new file pins its kernel environment in the script header: `venv` (a
    directory with `bin/python`, e.g. a project's `.venv` or a conda env), or
    darwin's own Python environment by default. An existing file keeps its own
    configuration (`[tool.marimo.venv]` in the header or pyproject.toml).
    """
    nb_path = Path(path).resolve()
    if nb_path.suffix != ".py":
        raise ValueError(f"marimo notebooks are .py files: {nb_path}")
    if not nb_path.exists():
        nb_path.parent.mkdir(parents=True, exist_ok=True)
        env_dir = Path(venv).resolve() if venv is not None else Path(sys.prefix)
        nb_path.write_text(
            _TEMPLATE.format(venv=json.dumps(str(env_dir))), encoding="utf-8"
        )
    elif venv is not None:
        raise ValueError(
            f"{nb_path.name} exists; set its kernel in the file header ([tool.marimo.venv] path) instead of venv="
        )
    nb = await _attach(nb_path)
    return nb or Notebook(nb_path, await _start(nb_path))


async def clean_run(
    path: Path,
    label: str,
    outputs: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> dict[str, artifact.Ref]:
    """Run the marimo file `path` in a fresh process and store `outputs` as `<label>/<name>` artifacts.

    `env` names the interpreter and PYTHONPATH of the kernel the notebook was
    written against, so the clean run sees the same packages.
    """
    with tempfile.TemporaryDirectory() as tmp:
        result = Path(tmp) / "result.json"
        cmd = [
            env["python"],
            # -P keeps the script directory off sys.path, so pair's modules cannot shadow the notebook's imports.
            "-P",
            str(Path(__file__).with_name("_runner.py")),
            str(path),
            str(result),
            label,
            *outputs,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env={
                **os.environ,
                "PYTHONPATH": env["pythonpath"],
                "DARWIN_DATA_DIR": str(artifact.root()),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"clean run of {label!r} failed:\n{out.decode(errors='replace')}"
            )
        data = json.loads(result.read_text(encoding="utf-8"))
    return {
        name: artifact.Ref(v["hash"], v["kind"], v["name"], v["version"])
        for name, v in data.items()
    }


async def _start(nb_path: Path) -> dict[str, Any]:
    server_python = os.environ.get("DARWIN_MARIMO_PYTHON")
    if not server_python:
        raise RuntimeError(
            "DARWIN_MARIMO_PYTHON is not set; run through the darwin prime-agent wrapper"
        )
    state_dir = _registry_path(nb_path).parent
    state_dir.mkdir(parents=True, exist_ok=True)
    key = _registry_path(nb_path).stem
    token = secrets.token_urlsafe(24)
    token_file = state_dir / f"{key}.token"
    _write_private(token_file, token)
    port = _free_port()
    # Only artifact's own site-packages, not the caller's PYTHONPATH: the kernel env must not see darwin's packages.
    artifact_path = str(Path(artifact.__file__).resolve().parent.parent)
    env = {
        **os.environ,
        "PYTHONPATH": artifact_path,
        "DARWIN_DATA_DIR": str(artifact.root()),
    }
    server = _spawn(
        [server_python, "-m", "marimo", "edit", str(nb_path),
         "--headless", "--no-sandbox", "--skip-update-check",
         "--host", "127.0.0.1", "-p", str(port), "--token-password-file", str(token_file)],
        env, cwd=nb_path.parent, log=state_dir / f"{key}.log",
    )  # fmt: skip
    state: dict[str, Any] = {
        "url": f"http://127.0.0.1:{port}",
        "token": token,
        "server": server,
    }
    try:
        await _wait(
            lambda: _healthy(state["url"]), "marimo server", state_dir / f"{key}.log"
        )
        url = f"ws://127.0.0.1:{port}/ws?session_id={_SESSION}&file={nb_path.name}&access_token={token}"
        # The token goes through the environment, which other users cannot read, not argv.
        state["holder"] = _spawn(
            [server_python, "-P", str(Path(__file__).with_name("_hold.py"))],
            {**os.environ, "PAIR_WS_URL": url},
            cwd=nb_path.parent,
            log=state_dir / f"{key}.hold.log",
        )
        nb = Notebook(nb_path, state)
        await _wait(lambda: _ready(nb), "marimo kernel", state_dir / f"{key}.log")
        # A fresh kernel starts empty; run the saved cells so live state matches the file.
        await nb._execute(
            "import marimo._code_mode as _cm\n"
            "async with _cm.get_context() as _ctx:\n"
            "    for _c in _ctx.cells.values():\n"
            "        _ctx.run_cell(_c.id)\n"
        )
    except BaseException:
        _stop(state)
        raise
    _write_private(_registry_path(nb_path), json.dumps(state))
    return state


def _spawn(cmd: list[str], env: Mapping[str, str], *, cwd: Path, log: Path) -> int:
    """Start a detached process and return its pid.

    Popen, not asyncio: asyncio kills its children when the creating event loop
    closes, and the server must outlive the agent kernel so a later open() can reattach.
    """
    with log.open("wb") as out:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdout=out, stderr=out, start_new_session=True
        )
    return proc.pid


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


async def _healthy(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            return (await client.get(f"{url}/health")).status_code == 200
    except httpx.HTTPError:
        return False


async def _ready(nb: Notebook) -> bool:
    try:
        await nb._execute("pass")
        return True
    except (PairError, httpx.HTTPError):
        return False


async def _wait(check: Any, what: str, log: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.3)
    tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
    raise RuntimeError(
        f"{what} did not start within {timeout:.0f}s; server log:\n{tail}"
    )


async def _attach(nb_path: Path) -> Notebook | None:
    registry = _registry_path(nb_path)
    if not registry.exists():
        return None
    nb = Notebook(nb_path, json.loads(registry.read_text(encoding="utf-8")))
    if await _ready(nb):
        return nb
    _stop(nb._state)
    registry.unlink()
    return None


def _registry_path(nb_path: Path) -> Path:
    return (
        artifact.root()
        / "pair"
        / (artifact.digest(str(nb_path)).removeprefix("sha256:")[:16] + ".json")
    )


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stop(state: Mapping[str, Any]) -> None:
    for key in ("holder", "server"):
        pid = state.get(key)
        if _alive(pid):
            assert pid is not None
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _source(fn: object) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]:
    if inspect.isclass(fn):
        raise TypeError(
            f"define({fn.__name__}) takes a function; define classes inside a cell wrapper: nb.cell(fn) whose body defines the class"
        )
    if not inspect.isfunction(fn) or fn.__name__ == "<lambda>":
        raise TypeError(f"expected a function written with def, got {fn!r}")
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except OSError:
        raise ValueError(
            f"source of {fn.__name__} is not available; pass the code as a string: cell({fn.__name__!r}, code)"
        ) from None
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    return node, source


def _cell_code(fn: Callable[..., Any]) -> str:
    """The body of `fn` as cell code, without its final `return`."""
    node, source = _source(fn)
    body = node.body[:-1] if isinstance(node.body[-1], ast.Return) else node.body
    if not body:
        raise ValueError(f"{fn.__name__} has no statements besides its return")
    for stmt in body:
        if _returns(stmt):
            raise ValueError(
                f"{fn.__name__} returns before its last line; a cell body runs top to bottom, so only a final return (its exported names) is allowed"
            )
    first = body[0]
    decorators = getattr(first, "decorator_list", [])
    start = min([first.lineno, *(d.lineno for d in decorators)])
    lines = source.splitlines()[start - 1 : body[-1].end_lineno]
    if start == node.lineno:  # one-line def: drop the `def ...:` prefix
        lines[0] = " " * first.col_offset + lines[0][first.col_offset :]
    return textwrap.dedent("\n".join(lines)).strip("\n")


def _returns(node: ast.AST) -> bool:
    """Whether `node` returns from the enclosing function (nested scopes excluded)."""
    if isinstance(node, ast.Return):
        return True
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return False
    return any(_returns(child) for child in ast.iter_child_nodes(node))


def _text(data: str) -> str:
    """marimo renders values as HTML; agents read the text."""
    return html.unescape(re.sub(r"<[^>]+>", "", data)).strip()
