from pathlib import Path

import artifact
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DARWIN_DATA_DIR", str(tmp_path / "store"))
    return tmp_path / "store"


def test_json_roundtrip_and_versions() -> None:
    first = artifact.put({"reps": 3}, name="params", note="initial")
    again = artifact.put({"reps": 3}, name="params")
    second = artifact.put({"reps": 4}, name="params")
    assert (first.version, again.version, second.version) == (1, 1, 2)
    assert again.hash == first.hash
    assert artifact.get("params").load() == {"reps": 4}
    assert artifact.get("params@1").load() == {"reps": 3}
    assert artifact.get(first.hash).kind == "json"
    assert first.meta()["note"] == "initial"


def test_table_hash_is_stable_and_matches_digest() -> None:
    df = pd.DataFrame({"well": ["A1", "A2"], "volume_ul": [10.0, 12.5]})
    ref = artifact.put(df, name="layout")
    assert ref.kind == "table"
    assert artifact.put(df.copy()).hash == ref.hash
    assert artifact.digest(df) == ref.hash
    pd.testing.assert_frame_equal(ref.load(), df)


def test_file_kind_keeps_filename_and_is_read_only(tmp_path: Path) -> None:
    src = tmp_path / "primers.fasta"
    src.write_text(">p1\nACGT\n")
    ref = artifact.put(src, name="primers")
    assert ref.kind == "file"
    assert ref.load().read_text() == ">p1\nACGT\n"
    assert ref.meta()["filename"] == "primers.fasta"
    assert not ref.path.stat().st_mode & 0o222


def test_list_shows_latest_named_versions() -> None:
    artifact.put("v1", name="notes")
    artifact.put("v2", name="notes")
    artifact.put([1, 2], name="counts")
    rows = artifact.list()
    assert [(r["name"], r["version"]) for r in rows] == [("counts", 1), ("notes", 2)]
    assert [r["name"] for r in artifact.list(kind="text")] == ["notes"]


def test_unknown_keys_and_kinds_fail() -> None:
    with pytest.raises(KeyError):
        artifact.get("missing")
    with pytest.raises(KeyError):
        artifact.get("sha256:" + "0" * 64)
    with pytest.raises(ValueError):
        artifact.put(1, kind="pickle")


def test_names_with_version_separator_are_rejected() -> None:
    with pytest.raises(ValueError, match="@"):
        artifact.put(1, name="plate@96")
    artifact.put(1, name="plate")
    with pytest.raises(ValueError, match="version"):
        artifact.get("plate@latest")
