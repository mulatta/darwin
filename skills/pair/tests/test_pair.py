import asyncio
import linecache
import subprocess
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import artifact
import pair
import pytest

LAYOUT = "layout = [f'{s}-r{r}' for s in samples for r in range(reps)]"


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("DARWIN_DATA_DIR", str(tmp_path / "store"))
    yield tmp_path / "store"
    for registry in (tmp_path / "store" / "pair").glob("*.json"):
        nb = pair.Notebook(
            Path("unused.py"), __import__("json").loads(registry.read_text())
        )
        asyncio.run(nb.close())


async def _plate(path: Path) -> pair.Notebook:
    nb = await pair.open(path)
    await nb.input("samples", artifact.put(["s1", "s2"], name="samples"))
    await nb.cell("params", "reps = 3")
    await nb.cell("layout", LAYOUT)
    return nb


def test_cells_are_saved_and_rerun_their_dependents(tmp_path: Path) -> None:
    async def scenario() -> None:
        nb = await _plate(tmp_path / "qpcr.py")
        assert await nb.scratch("len(layout)") == "6\n"
        await nb.cell("params", "reps = 2")
        assert await nb.scratch("len(layout)") == "4\n"
        source = nb.path.read_text()
        assert LAYOUT in source and "reps = 2" in source
        await nb.delete("layout")
        with pytest.raises(pair.PairError, match="NameError"):
            await nb.scratch("layout")

    asyncio.run(scenario())


def test_scratch_code_is_not_saved(tmp_path: Path) -> None:
    async def scenario() -> None:
        nb = await pair.open(tmp_path / "explore.py")
        assert (
            await nb.scratch("probe_value = 41\nprint('seen')\nprobe_value + 1")
            == "seen\n42\n"
        )
        assert "probe_value" not in nb.path.read_text()

    asyncio.run(scenario())


def test_failing_cell_is_saved_and_explains_the_fix(tmp_path: Path) -> None:
    async def scenario() -> None:
        nb = await pair.open(tmp_path / "broken.py")
        with pytest.raises(pair.PairError, match=r"NameError(.|\n)*delete\('bad'\)"):
            await nb.cell("bad", "y = undefined_name")
        await nb.delete("bad")
        assert "undefined_name" not in nb.path.read_text()

    asyncio.run(scenario())


def test_function_bodies_become_cells_and_definitions_stay_whole(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        nb = await pair.open(tmp_path / "plate.py")

        def plate_types() -> object:
            from dataclasses import dataclass

            @dataclass
            class Well:
                row: str
                col: int

            label = """plate map"""
            return Well, label

        def dilute(c_mM: float, target_mM: float, final_ul: float = 50) -> float:
            return final_ul * target_mM / c_mM

        def wells(Well: type, dilute: Callable[..., float]) -> object:
            plate = [Well(r, c) for r in "AB" for c in (1, 2)]
            stock_ul = dilute(100, 10)
            return plate, stock_ul

        types = await nb.cell(plate_types)
        assert (types.name, types.status) == ("plate_types", "idle")
        defined = await nb.define(dilute)
        await nb.cell(wells)
        assert await nb.scratch("print(plate[-1], stock_ul, label)") == (
            "Well(row='B', col=2) 5.0 plate map\n"
        )
        with pytest.raises(TypeError, match="call it from a cell"):
            defined(100, 10)

        def early(x: int) -> int:
            if x:
                return 1
            return 2

        with pytest.raises(ValueError, match="return"):
            await nb.cell(early)
        with pytest.raises(TypeError, match="cell wrapper"):
            await nb.define(pair.Cell)
        with pytest.raises(TypeError, match="def"):
            await nb.cell(lambda: 1)

    asyncio.run(scenario())


def test_cell_source_is_read_from_repl_cells() -> None:
    # The rlm REPL registers each executed cell in linecache under <cell-N>.
    code = 'def probe():\n    seen = "x"\n    return seen\n'
    linecache.cache["<cell-7>"] = (len(code), None, code.splitlines(True), "<cell-7>")
    module = compile(code, "<cell-7>", "exec")
    body = next(c for c in module.co_consts if isinstance(c, types.CodeType))
    assert pair._cell_code(types.FunctionType(body, {})) == 'seen = "x"'


def test_verify_pins_inputs_and_detects_hidden_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        nb = await _plate(tmp_path / "qpcr.py")
        result = await nb.verify(["layout", "reps"])
        assert result.outputs["layout"].load() == [
            f"{s}-r{r}" for s in ["s1", "s2"] for r in range(3)
        ]
        assert result.outputs["layout"].name == "qpcr/layout"
        assert result.inputs == {"samples": artifact.get("samples").hash}
        assert result.notebook.load().read_text() == nb.path.read_text()
        await nb.scratch("layout.append('extra')")  # mutation outside any cell
        with pytest.raises(pair.ReproductionError, match="layout"):
            await nb.verify(["layout"])

    asyncio.run(scenario())


def test_reopen_reattaches_and_a_new_server_reruns_saved_cells(tmp_path: Path) -> None:
    async def scenario() -> None:
        nb = await _plate(tmp_path / "qpcr.py")
        again = await pair.open(nb.path)
        assert again.url == nb.url
        await nb.close()
        fresh = await pair.open(nb.path)
        assert fresh.url != nb.url
        assert await fresh.scratch("len(layout)") == "6\n"

    asyncio.run(scenario())


def test_project_venv_kernel_sees_artifact_but_not_darwin_packages(
    tmp_path: Path,
) -> None:
    venv = tmp_path / "proj" / ".venv"
    subprocess.run(
        [
            Path(sys.base_prefix) / "bin" / "python3",
            "-m",
            "venv",
            "--without-pip",
            venv,
        ],
        check=True,
    )

    async def scenario() -> None:
        nb = await pair.open(tmp_path / "proj" / "analysis.py", venv=venv)
        out = await nb.scratch(
            "import importlib.util, sys\n"
            "print(sys.prefix)\n"
            "print([m for m in ['numpy', 'pandas', 'rlm'] if importlib.util.find_spec(m)])\n"
            "print(importlib.util.find_spec('artifact') is not None)"
        )
        assert out.splitlines() == [str(venv), "[]", "True"]
        with pytest.raises(ValueError, match="file header"):
            await pair.open(nb.path, venv=venv)

    asyncio.run(scenario())
