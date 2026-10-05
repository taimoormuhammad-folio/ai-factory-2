"""Gates: named approvers, gate files bound to artifact hashes, async approval, waivers, status/blocked files."""

import json

import pytest

from agentic_sdlc.gates import files as gate_files
from agentic_sdlc.gates.human import gate_mode
from test_flow import PIPELINE, FakeRunner, make_flow


@pytest.fixture(autouse=True)
def person_at_the_terminal(monkeypatch):
    monkeypatch.delenv("SDLC_GATE_MODE", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("SDLC_APPROVER", "Pat Owner")


def test_only_named_people_can_approve():
    assert gate_files.approver_problem("Pat Owner") is None
    for name in ("", "x", "Claude", "release-bot", "AI reviewer", "auto"):
        assert gate_files.approver_problem(name), name


def test_auto_mode_is_only_for_demo_pipelines_at_tier_l():
    assert gate_mode("auto", demo=True, risk_tier="L") == "auto"
    assert gate_mode("async") == "async"
    for demo, tier in ((False, "L"), (True, "M"), (True, "H")):
        with pytest.raises(ValueError, match="demo"):
            gate_mode("auto", demo=demo, risk_tier=tier)


def test_decisions_are_written_to_gate_files_with_the_approved_hashes(tmp_path, canned):
    flow = make_flow(tmp_path, FakeRunner(canned), ["y", "", "y", ""])
    flow.kickoff(inputs={"run_id": "g1", "brief": "shop"})
    root = tmp_path / "g1"
    g1 = json.loads((root / "gates" / "G1.gate").read_text())
    assert g1["approved"] and g1["approver"] == "Pat Owner" and g1["gate"] == "prd"
    assert set(g1["artifact_hashes"]) == {"docs/prd.md", "docs/clarifications.md"}
    assert (root / "gates" / "G2.gate").exists()
    status = (root / "status.md").read_text()
    assert "| G1 prd | approved | Pat Owner |" in status and "Status: **completed**" in status
    assert (root / "docs" / "prd.md").read_text().startswith("---\nagent: business_analyst\nstatus: draft")


def test_changing_an_approved_document_asks_again(tmp_path, canned):
    flow = make_flow(tmp_path, FakeRunner(canned), ["y", "", "y", ""])
    flow.kickoff(inputs={"run_id": "g2", "brief": "shop"})
    root = tmp_path / "g2"
    (root / "docs" / "prd.md").write_text("edited by someone after approval\n")
    answers = []
    resumed = make_flow(tmp_path, FakeRunner(canned), ["y", "", "y", ""], restore_json=(root / "state.json").read_text())
    resumed._deps_factory = (lambda f: (lambda st: _recording(f(st), answers)))(resumed._deps_factory)
    resumed.kickoff(inputs={"run_id": "g2"})
    reopened = [d for d in resumed.state.gate_history if d.decided_by == "system"]
    assert reopened and "docs/prd.md" in reopened[0].feedback
    assert answers[0].startswith("Approve?")          # G1 was asked again
    assert resumed.state.gate_approved("prd")


def _recording(deps, prompts):
    inner = deps.input_fn
    deps.input_fn = lambda prompt: prompts.append(prompt) or inner(prompt)
    return deps


def test_rejected_drafts_are_kept_in_history(tmp_path, canned):
    flow = make_flow(tmp_path, FakeRunner(canned), ["n", "Add guest checkout", "y", "", "y", ""])
    flow.kickoff(inputs={"run_id": "g3", "brief": "shop"})
    history = tmp_path / "g3" / "docs" / "history"
    assert (history / "prd.v1.md").exists() and (history / "clarifications.v1.md").exists()


def test_async_gate_waits_for_uv_run_approve_then_resumes(tmp_path, canned, monkeypatch):
    from agentic_sdlc import main, workspace

    monkeypatch.setattr(workspace, "RUNS_DIR", tmp_path)
    pipeline = {**PIPELINE, "gate_mode": "async", "phases": {**PIPELINE["phases"], "design": False}}
    flow = make_flow(tmp_path, FakeRunner(canned), [], pipeline=pipeline)
    flow.kickoff(inputs={"run_id": "g4", "brief": "shop"})
    root = tmp_path / "g4"
    assert flow.state.status == "stopped" and "uv run approve g4 G1" in flow.state.stop_reason
    pending = json.loads((root / "gates" / "G1.pending.json").read_text())
    assert pending["gate"] == "prd" and "docs/prd.md" in pending["artifact_hashes"]

    with pytest.raises(SystemExit, match="interactive"):
        main.approve(["g4", "G1", "--as", "Pat Owner"], input_fn=lambda _p: "y", is_tty=False)
    with pytest.raises(SystemExit, match="named people"):
        main.approve(["g4", "G1", "--as", "Claude"], input_fn=lambda _p: "y", is_tty=True)
    answers = iter(["y", ""])
    main.approve(["g4", "G1", "--as", "Pat Owner", "--role", "product owner"],
                 input_fn=lambda _p: next(answers), is_tty=True)
    assert not (root / "gates" / "G1.pending.json").exists()

    resumed = make_flow(tmp_path, FakeRunner(canned), [], pipeline=pipeline, restore_json=(root / "state.json").read_text())
    resumed.kickoff(inputs={"run_id": "g4"})
    g1 = [d for d in resumed.state.gate_history if d.gate == "prd"][-1]
    assert g1.approved and g1.approver == "Pat Owner" and g1.role == "product owner"
    assert "uv run approve g4 G2" in resumed.state.stop_reason      # now waiting for the design gate


def test_approve_refuses_documents_changed_after_the_request(tmp_path, canned, monkeypatch):
    from agentic_sdlc import main, workspace

    monkeypatch.setattr(workspace, "RUNS_DIR", tmp_path)
    flow = make_flow(tmp_path, FakeRunner(canned), [], pipeline={**PIPELINE, "gate_mode": "async"})
    flow.kickoff(inputs={"run_id": "g5", "brief": "shop"})
    (tmp_path / "g5" / "docs" / "prd.md").write_text("changed\n")
    with pytest.raises(SystemExit, match="changed since the gate was requested"):
        main.approve(["g5", "G1", "--as", "Pat Owner"], input_fn=lambda _p: "y", is_tty=True)


def test_design_gate_waiver_at_tier_l(tmp_path, canned):
    pipeline = {**PIPELINE, "risk_tier": "L", "gates": {**PIPELINE["gates"], "architecture_waiver_at_tier_l": True}}
    flow = make_flow(tmp_path, FakeRunner(canned), ["y", ""], pipeline=pipeline)   # only G1 is asked
    flow.kickoff(inputs={"run_id": "g6", "brief": "shop"})
    g2 = [d for d in flow.state.gate_history if d.gate == "architecture"][-1]
    assert flow.state.status == "completed" and g2.decided_by == "waiver"
    assert json.loads((tmp_path / "g6" / "gates" / "G2.gate").read_text())["decided_by"] == "waiver"
