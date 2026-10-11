"""The canary's verdict: finished smoothly, finished roughly, or failed."""

from agentic_sdlc import canary
from agentic_sdlc.state import GateDecision, ItemProgress, MilestoneProgress, ProjectState


def finished(**over) -> ProjectState:
    s = ProjectState(run_id="c", status="completed")
    s.build.items = {"WI-001": ItemProgress(status="done", attempts=1), "WI-002": ItemProgress(status="done", attempts=1)}
    s.build.milestones = {"M1": MilestoneProgress(status="done", qa_rounds=1)}
    s.release.production, s.release.verified, s.release.rounds = "ready", True, 1
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_a_clean_run_is_smooth():
    v = canary.judge(finished())
    assert v.exit_code == 0 and not v.failures and not v.rough
    assert v.numbers["tasks"] == 2 and "PASSED" in canary.report("c", v)


def test_a_run_that_did_not_finish_or_lacks_proof_fails():
    s = finished(status="stopped", stop_reason="Smoke tester could not write the smoke suite")
    s.release.acceptance_unmet = ["AC-02"]
    s.build.items["WI-002"].status = "failed"
    v = canary.judge(s)
    assert v.exit_code == 1
    text = " ".join(v.failures)
    assert "ended as 'stopped'" in text and "WI-002 (failed)" in text and "AC-02" in text
    assert "FAILED" in canary.report("c", v)


def test_retries_beyond_the_budget_make_it_not_smooth_but_not_failed():
    s = finished()
    s.build.items["WI-001"].attempts = 3
    s.release.rounds, s.release.suite_rewrites = 4, 1
    s.gate_history.append(GateDecision(gate="merge", approved=False, decided_by="human"))
    v = canary.judge(s)
    assert v.exit_code == 2 and not v.failures
    text = " ".join(v.rough)
    assert "build attempts" in text and "release verification rounds" in text and "suite(s) had to be rewritten" in text
    assert "gate rejection" in text and "NOT SMOOTH" in canary.report("c", v)


def test_the_canary_pipeline_is_tiny_and_uses_auto_gates_at_tier_l():
    from agentic_sdlc.gates.human import gate_mode
    from agentic_sdlc.settings import load_config

    p = load_config("pipeline.canary")
    assert gate_mode(p["gate_mode"], demo=p["demo"], risk_tier=p["risk_tier"]) == "auto"
    assert p["scope"]["max_work_items"] <= 4 and p["scope"]["max_milestones"] == 1
    assert not p["release"].get("production_command")                    # it never deploys
