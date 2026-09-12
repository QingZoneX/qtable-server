from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_dev_data.py"
spec = importlib.util.spec_from_file_location("qtable_seed_dev_data", SCRIPT)
seed_dev_data = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = seed_dev_data
spec.loader.exec_module(seed_dev_data)


def test_realistic_default_scale_matches_product_plan():
    plans = seed_dev_data.plans_for("realistic", 300)
    assert [(p.template_id, p.records) for p in plans] == [
        ("project_management", 300),
        ("sprint_planning", 500),
        ("bug_tracking", 400),
        ("milestone_tracker", 40),
        ("sales_crm", 300),
        ("content_calendar", 200),
        ("recruiting_pipeline", 150),
        ("asset_inventory", 300),
    ]


def test_generation_is_deterministic_and_uses_real_member_ids():
    template = seed_dev_data.template_file("project_management")
    kwargs = dict(
        template_id="project_management",
        fields=template["fields"],
        count=12,
        member_ids=[101, 202, 303],
        seed=20260910,
        coverage=True,
    )
    first = seed_dev_data.generate_records(**kwargs)
    second = seed_dev_data.generate_records(**kwargs)
    assert first == second
    assignees = {row["f3"] for row in first if row["f3"] is not None}
    assert assignees <= {"101", "202", "303"}


def test_project_status_progress_are_consistent():
    template = seed_dev_data.template_file("project_management")
    rows = seed_dev_data.generate_records(
        template_id="project_management",
        fields=template["fields"],
        count=9,
        member_ids=[1, 2],
        seed=20260910,
        coverage=True,
    )
    for row in rows:
        if row["f2"] == "opt1":
            assert row["f4"] == 0
        elif row["f2"] == "opt3":
            assert row["f4"] == 100
        else:
            assert 10 <= row["f4"] <= 90


def test_seed_date_anchor_is_reproducible():
    assert seed_dev_data.anchor_date(20260910).isoformat() == "2026-09-10"
    assert seed_dev_data.anchor_date(20260910) == seed_dev_data.anchor_date(20260910)


def test_write_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("QTABLE_ALLOW_DEV_SEED", raising=False)
    with pytest.raises(seed_dev_data.SeedSafetyError):
        seed_dev_data.require_write_opt_in(dry_run=False, profile="realistic")
    seed_dev_data.require_write_opt_in(dry_run=True, profile="realistic")


def test_seed_workspace_markers_are_fixed():
    assert seed_dev_data.workspace_id("coverage").startswith("wkb_seed_")
    assert seed_dev_data.workspace_name("coverage").startswith("[SEED]")


def test_seed_workspace_has_one_folder_root_contract():
    wid = seed_dev_data.workspace_id("realistic")
    name = seed_dev_data.workspace_name("realistic")
    root = seed_dev_data.build_seed_root_item(wid, name)

    assert root.id == seed_dev_data.seed_root_id(wid)
    assert root.id.startswith("fld_seed_")
    assert root.workspace_id == wid
    assert root.type == "folder"
    assert root.name == name
    assert root.parent_id is None


def test_seed_children_require_workspace_root_parent():
    create_table_parameters = inspect.signature(seed_dev_data.create_table).parameters
    coverage_parameters = inspect.signature(seed_dev_data.coverage_extras).parameters

    assert "parent_id" in create_table_parameters
    assert "parent_id" in coverage_parameters


def test_seed_root_id_is_deterministic_and_workspace_scoped():
    realistic = seed_dev_data.seed_root_id("wkb_seed_realistic")
    assert realistic == seed_dev_data.seed_root_id("wkb_seed_realistic")
    assert realistic != seed_dev_data.seed_root_id("wkb_seed_coverage")
