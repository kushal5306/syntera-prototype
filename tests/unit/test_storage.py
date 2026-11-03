"""Storage interface and run keys (I2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from syntera.cache import ROUTE_DESIGN_KIND, run_key
from syntera.changes import ChangeSet, SetParameter, apply_change_set
from syntera.design import design_from_yaml
from syntera.storage import LocalStorage, StorageError

KEY = "ab" * 32
PROJECT = Path(__file__).parents[2]


def test_local_storage_round_trips_bundles(tmp_path):
    storage = LocalStorage(tmp_path)
    assert storage.get(KEY) is None and not storage.exists(KEY)
    files = {"report.json": b"{}", "lines/main/route.json": b"[1, 2]"}
    storage.put(KEY, files)
    assert storage.get(KEY) == files and storage.exists(KEY)
    assert (tmp_path / "objects" / "ab" / KEY / "lines" / "main" / "route.json").is_file()


def test_tampered_bundles_read_as_missing(tmp_path):
    storage = LocalStorage(tmp_path)
    storage.put(KEY, {"report.json": b"{}"})
    (tmp_path / "objects" / "ab" / KEY / "report.json").write_bytes(b'{"overall_pass": true}')
    assert storage.get(KEY) is None
    (tmp_path / "objects" / "ab" / KEY / "manifest.json").write_text("not json")
    assert storage.get(KEY) is None


@pytest.mark.parametrize("name", ["../escape.json", "/abs.json", "manifest.json", ""])
def test_bad_names_and_keys_are_refused(tmp_path, name):
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).put(KEY, {name: b"x"})
    with pytest.raises(StorageError, match="invalid storage key"):
        LocalStorage(tmp_path).get("../../etc")


def test_run_keys_follow_the_design_content():
    design = design_from_yaml(PROJECT / "examples" / "demo_skid.yaml")
    again = design_from_yaml(PROJECT / "examples" / "demo_skid.yaml")
    assert run_key(ROUTE_DESIGN_KIND, design) == run_key(ROUTE_DESIGN_KIND, again)
    assert run_key("other/v1", design) != run_key(ROUTE_DESIGN_KIND, design)
    changed, _ = apply_change_set(design, ChangeSet(description="x", edits=[
        SetParameter(path="parameters.voxel_resolution", value=10)]))  # fmt: skip
    assert run_key(ROUTE_DESIGN_KIND, changed) != run_key(ROUTE_DESIGN_KIND, design)
