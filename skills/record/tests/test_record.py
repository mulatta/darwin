import asyncio
from pathlib import Path

import artifact
import pair
import pytest
import record


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DARWIN_DATA_DIR", str(tmp_path / "store"))
    return tmp_path / "store"


def _run(tmp_path: Path) -> pair.RunResult:
    async def scenario() -> pair.RunResult:
        nb = await pair.open(tmp_path / "dilution.py")
        try:
            await nb.input("stock", artifact.put({"conc_mM": 100.0}, name="stock"))
            await nb.cell(
                "volumes",
                "target_mM = 10.0\nfinal_ul = 50.0\nstock_ul = final_ul * target_mM / stock['conc_mM']",
            )
            return await nb.verify(["stock_ul"])
        finally:
            await nb.close()

    return asyncio.run(scenario())


def test_commit_records_provenance(tmp_path: Path) -> None:
    run = _run(tmp_path)
    rec = record.commit(
        "experiment_design",
        {"title": "dilution", "stock_ul": 5.0},
        run,
        rationale="1:10",
    )
    assert rec.notebook == run.notebook.hash
    assert rec.outputs == {"stock_ul": run.outputs["stock_ul"].hash}
    assert rec.inputs == {"stock": artifact.get("stock").hash}
    assert rec.load_spec()["stock_ul"] == 5.0
    assert record.show(rec.id) == rec
    assert [r.id for r in record.list(kind="experiment_design")] == [rec.id]
    assert record.list(kind="other") == []


def test_reproduce_matches_recorded_outputs(tmp_path: Path) -> None:
    rec = record.commit("experiment_design", {"title": "dilution"}, _run(tmp_path))
    assert asyncio.run(record.reproduce(rec.id)) == {}


def test_commit_refuses_fabricated_run(tmp_path: Path) -> None:
    run = _run(tmp_path)
    fake = pair.RunResult(
        artifact.Ref("sha256:" + "0" * 64, "file"), run.inputs, run.outputs, run.env
    )
    with pytest.raises(KeyError):
        record.commit("experiment_design", {"title": "x"}, fake)
    with pytest.raises(ValueError):
        record.commit("Bad Kind", {"title": "x"}, run)


def test_commit_explains_a_missing_await(tmp_path: Path) -> None:
    async def verify() -> pair.RunResult:
        raise AssertionError("never awaited")

    pending = verify()
    try:
        with pytest.raises(TypeError, match="await"):
            record.commit("experiment_design", {"title": "x"}, pending)  # type: ignore[arg-type]
    finally:
        pending.close()
