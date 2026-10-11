"""Supported rewinds: the saved state and its files change together, what is approved and built stays."""

import json

import pytest

from agentic_sdlc import main, rewind
from agentic_sdlc.registry.profiles import Profile
from agentic_sdlc.state import ItemProgress, MilestoneProgress, ProjectState
from agentic_sdlc.workspace import Workspace

WORKDIRS = {"backend": "server", "frontend": "app", "infra": ".", "shared": "."}


def state(wbs, delivery_plan):
    s = ProjectState(run_id="rw", profile="flutter_nestjs_ecommerce", status="completed")
    s.wbs, s.plan = wbs, delivery_plan
    s.build.items = {"WI-001": ItemProgress(status="done", attempts=1),
                     "WI-002": ItemProgress(status="failed", attempts=3, reason="still failing")}
    s.build.milestones = {"M1": MilestoneProgress(status="failed", qa_rounds=3)}
    return s


def test_build_retries_only_failed_and_blocked_tasks(tmp_path, wbs, delivery_plan):
    s = state(wbs, delivery_plan)
    rewind.apply(s, tmp_path, Profile.load("flutter_nestjs_ecommerce"), "build")
    assert s.build.items["WI-001"].status == "done" and s.build.items["WI-001"].attempts == 1
    assert s.build.items["WI-002"].status == "todo" and s.build.items["WI-002"].attempts == 0
    assert s.build.milestones["M1"].status == "todo" and s.status == "stopped" and "rewound to 'build'" in s.stop_reason


def test_plan_clears_the_delivery_plan_but_keeps_the_wbs(tmp_path, wbs, delivery_plan):
    s = state(wbs, delivery_plan)
    rewind.apply(s, tmp_path, Profile.load("flutter_nestjs_ecommerce"), "plan")
    assert s.plan is None and s.backlog is None and s.wbs is wbs


def test_release_starts_verification_and_acceptance_over(tmp_path, wbs, delivery_plan):
    s = state(wbs, delivery_plan)
    s.release.rounds, s.release.verified, s.release.production = 3, True, "ready"
    s.release.acceptance_met, s.release.apk_key = ["AC-01"], "abc"
    rewind.apply(s, tmp_path, Profile.load("flutter_nestjs_ecommerce"), "release")
    r = s.release
    assert (r.rounds, r.verified, r.production, r.acceptance_met, r.apk_key) == (0, False, "todo", [], "")


def test_the_suite_rewinds_delete_only_the_suite_files(tmp_path, wbs, delivery_plan):
    profile = Profile.load("flutter_nestjs_ecommerce")
    (tmp_path / "app/integration_test").mkdir(parents=True)
    (tmp_path / "app/integration_test/journeys_test.dart").write_text("x")
    (tmp_path / "app/lib").mkdir(parents=True)
    (tmp_path / "app/lib/main.dart").write_text("keep")
    (tmp_path / "server/test/smoke").mkdir(parents=True)
    (tmp_path / "server/test/smoke/critical.smoke.ts").write_text("x")
    (tmp_path / "server/test/health.e2e-spec.ts").write_text("keep")
    s = state(wbs, delivery_plan)
    rewind.apply(s, tmp_path, profile, "device-suite")
    rewind.apply(s, tmp_path, profile, "smoke-suite")
    assert not (tmp_path / "app/integration_test/journeys_test.dart").exists()
    assert not (tmp_path / "server/test/smoke/critical.smoke.ts").exists()
    assert (tmp_path / "app/lib/main.dart").read_text() == "keep" and (tmp_path / "server/test/health.e2e-spec.ts").exists()
    with pytest.raises(ValueError, match="unknown step"):
        rewind.apply(s, tmp_path, profile, "nope")


def test_widening_a_task_updates_wbs_backlog_and_document_and_resets_it(tmp_path, wbs, delivery_plan):
    from agentic_sdlc.artifacts.plan import to_backlog

    s = state(wbs, delivery_plan)
    s.backlog = to_backlog(wbs, delivery_plan)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/wbs.md").write_text("---\nagent: architect\nwritten_at: 2026\n---\nold")
    assert rewind.widen(s, tmp_path, "WI-002", ["app/lib/catalog/shared/**"], WORKDIRS) == []
    assert "app/lib/catalog/shared/**" in s.wbs.task("WI-002").owns
    assert "app/lib/catalog/shared/**" in next(w for w in s.backlog.work_items if w.id == "WI-002").owns
    doc = (tmp_path / "docs/wbs.md").read_text()
    assert "amended: WI-002 also owns app/lib/catalog/shared/**" in doc and "agent: architect" in doc and "app/lib/catalog/shared/**" in doc
    assert s.build.items["WI-002"].status == "todo" and wbs.task("WI-002").owns != s.wbs.task("WI-002").owns   # original untouched


def test_widening_across_components_is_refused(tmp_path, wbs, delivery_plan):
    s = state(wbs, delivery_plan)
    problems = rewind.widen(s, tmp_path, "WI-002", ["server/src/catalog/**"], WORKDIRS)   # frontend task asking for backend files
    assert problems and "W1" in problems[0] and s.wbs.task("WI-002").owns == wbs.task("WI-002").owns
    assert rewind.widen(s, tmp_path, "WI-999", ["app/x/**"], WORKDIRS) == ["no task WI-999"]


def test_the_rewind_command_refuses_a_running_run_and_writes_the_state(tmp_path, wbs, delivery_plan, monkeypatch):
    ws = Workspace.create("rw", runs_dir=tmp_path)
    s = state(wbs, delivery_plan)
    s.status = "running"
    ws.save_state(s)
    monkeypatch.setattr(Workspace, "open", classmethod(lambda cls, run_id, runs_dir=None: ws))
    with pytest.raises(SystemExit, match="says it is running"):
        main.rewind_cmd(["rw", "--to", "build"])
    s.status = "stopped"
    ws.save_state(s)
    main.rewind_cmd(["rw", "--to", "build"])
    saved = json.loads(ws.load_state_json())
    assert saved["build"]["items"]["WI-002"]["status"] == "todo" and saved["status"] == "stopped"
    with pytest.raises(SystemExit, match="exactly one"):
        main.rewind_cmd(["rw"])
