"""The Architect's WBS (W1-W3), the PM's delivery plan over it, and the intake."""

import pytest

from agentic_sdlc import intake
from agentic_sdlc.artifacts.plan import PlannedMilestone, TaskEstimate, to_backlog
from agentic_sdlc.artifacts.wbs import WbsTask
from agentic_sdlc.crews import planning

WORKDIRS = {"backend": "server", "frontend": "app", "infra": ".", "shared": "."}


def test_a_good_wbs_passes(wbs, prd, architecture):
    assert planning.wbs_errors(wbs, prd, architecture, WORKDIRS) == []


def test_w1_ownership_stays_in_the_component_and_is_exclusive(wbs):
    wbs.tasks[1].owns = ["server/src/catalog/screens/**"]          # frontend task writing into the backend
    errors = " ".join(wbs.w1_ownership(WORKDIRS))
    assert "WI-002 (frontend) owns server/src/catalog/screens/**, outside its folder app/" in errors
    assert "WI-001 (backend) and WI-002 (frontend) both own" in errors
    wbs.tasks[1].owns = []
    assert "W1: WI-002 owns no paths" in " ".join(wbs.w1_ownership(WORKDIRS))


def test_w1_allows_overlap_within_one_component(wbs):
    wbs.tasks.append(WbsTask(id="WI-003", title="Catalog filters", description="d", package_id="E-01",
                             component="backend", story_ids=["US-001"], ac_ids=["AC-02"], owns=["server/src/catalog/**"],
                             verify="npm test"))
    assert wbs.w1_ownership(WORKDIRS) == []        # same builder, built one after the other


def test_w2_every_task_has_a_verify_command(wbs):
    wbs.tasks[0].verify = " "
    assert wbs.w2_verify() == ["W2: WI-001 has no verify command"]


def test_w3_criteria_operations_and_models_are_covered(wbs, prd, architecture):
    wbs.tasks[0].ac_ids = ["AC-01", "AC-99"]
    wbs.tasks[0].api_operations = []
    wbs.tasks[0].data_models = []
    errors = " ".join(planning.wbs_errors(wbs, prd, architecture, WORKDIRS))
    assert "serves unknown acceptance criterion AC-99" in errors
    assert "must-have acceptance criterion AC-02 has no task" in errors
    assert "no backend task implements API operation listProducts" in errors
    assert "no task covers data model Product" in errors


def test_plan_may_not_change_the_wbs(wbs, delivery_plan):
    assert delivery_plan.errors(wbs) == []
    delivery_plan.milestones[0].task_ids = ["WI-001", "WI-777"]
    delivery_plan.estimates = [TaskEstimate(task_id="WI-001", points=3, rationale="r")]
    errors = " ".join(delivery_plan.errors(wbs))
    assert "Task WI-777 is not a WBS task" in errors
    assert "WBS task WI-002 is in no milestone" in errors and "WBS task WI-002 has no estimate" in errors


def test_plan_keeps_dependencies_in_milestone_order(wbs, delivery_plan):
    delivery_plan.milestones = [PlannedMilestone(id="M1", name="App", goal="g", task_ids=["WI-002"]),
                                PlannedMilestone(id="M2", name="API", goal="g", task_ids=["WI-001"])]
    assert any("WI-002 (M1) depends on WI-001, which is in a later milestone (M2)" in e
               for e in delivery_plan.errors(wbs))


def test_wbs_and_plan_merge_into_the_build_backlog(wbs, delivery_plan):
    backlog = to_backlog(wbs, delivery_plan)
    assert [w.id for w in backlog.work_items] == ["WI-001", "WI-002"]
    first = backlog.work_items[0]
    assert (first.owns, first.verify, first.ac_ids, first.estimate_points) == (
        ["server/src/catalog/**"], "npm test", ["AC-01", "AC-02"], 3)
    assert backlog.milestones[0].work_item_ids == ["WI-001", "WI-002"] and backlog.epics[0].id == "E-01"
    assert backlog.validation_errors(["US-001"]) == []
    assert "| WI-001 Products API | backend | server/src/catalog/** | `npm test` | AC-01, AC-02 |" in wbs.to_markdown()
    assert "## M1: Catalog (6 pts)" in delivery_plan.to_markdown(wbs)
    assert wbs.ownership() == {"backend": ["server/src/catalog/**"], "frontend": ["app/lib/catalog/**"]}


def test_intake_front_matter_and_interview(tmp_path):
    meta, body = intake.parse("---\ntitle: Store\nrisk_tier: l\n---\nBuild a store.\n")
    assert meta["title"] == "Store" and body == "Build a store.\n"
    assert intake.parse("Just a request.") == ({}, "Just a request.")
    with pytest.raises(ValueError, match="risk_tier"):
        intake.parse("---\nrisk_tier: X\n---\nx")
    answers = iter(["B2B store", "Pat Owner", "q", "m", "Sell lighting to businesses.", "."])
    text = intake.interview(lambda _p: next(answers))
    path = intake.write_brief(tmp_path, text)
    assert path.name == "b2b_store.md"
    meta, body = intake.parse(path.read_text())
    assert meta == {"title": "B2B store", "product_owner": "Pat Owner", "risk_tier": "M"}
    assert body.strip() == "Sell lighting to businesses."
