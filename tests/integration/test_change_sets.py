"""Applying a change set regenerates the design and reports changed verdicts (A4)."""

from __future__ import annotations

import json
from pathlib import Path

from syntera.changes import DIFF_FILE, REVERT_FILE
from syntera.cli import main
from syntera.design import DESIGN_FILE, design_bytes, design_from_yaml, load_design

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"
EXAMPLE_CHANGES = PROJECT / "examples" / "demo_skid.changes.json"


def test_change_cli_writes_design_revert_and_diff_that_round_trip(tmp_path):
    output = tmp_path / "changed"
    args = ["change", "--design", str(FIXED), "--changes", str(EXAMPLE_CHANGES)]
    assert main([*args, "--output", str(output)]) == 0
    diff = json.loads((output / DIFF_FILE).read_text(encoding="utf-8"))
    assert [change["path"] for change in diff["values"]] == [
        "equipment.auxiliary-pump.geometry.center",
        "lines.main.tube.minimum_clearance",
    ]
    assert diff["regenerated"] is False and diff["verdicts"] == []

    restored = tmp_path / "restored"
    revert = ["change", "--design", str(output / DESIGN_FILE)]
    revert += ["--changes", str(output / REVERT_FILE), "--output", str(restored)]
    assert main(revert) == 0
    assert (restored / DESIGN_FILE).read_bytes() == design_bytes(design_from_yaml(FIXED))


def test_regeneration_reports_the_checks_that_changed_verdict(tmp_path):
    changes = tmp_path / "too_wide.json"
    changes.write_text(
        json.dumps(
            {
                "description": "clearance no route can meet",
                "edits": [
                    {
                        "op": "set_parameter",
                        "path": "lines.main.tube.minimum_clearance",
                        "value": 400,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    args = ["change", "--design", str(FIXED), "--changes", str(changes)]
    assert main([*args, "--output", str(output), "--regenerate"]) == 2
    diff = json.loads((output / DIFF_FILE).read_text(encoding="utf-8"))
    assert diff["regenerated"] is True
    flipped = {item["check"]: (item["before"], item["after"]) for item in diff["verdicts"]}
    assert flipped == {"overall": (True, False), "line:main": (True, False)}
    assert load_design(output / DESIGN_FILE).line("main").tube.minimum_clearance == 400
    # the change set only changed inputs; the verdicts come from the regenerated checks
    assert "overall_pass" not in (output / DESIGN_FILE).read_text(encoding="utf-8")
