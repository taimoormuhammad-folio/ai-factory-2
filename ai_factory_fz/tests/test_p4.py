"""Steps 7-9: Integrator and scans (deterministic), read-only QA, the Code Reviewer, and the merge gate (G5)."""

from agentic_sdlc.artifacts.backlog import Milestone
from agentic_sdlc.artifacts.reports import QAReport
from agentic_sdlc.artifacts.review import Finding, ReviewReport, package_markdown
from agentic_sdlc.build import verify
from agentic_sdlc.build.coders import ClaudeCodeWorker, Job
from agentic_sdlc.build.loop import BuildConfig
from agentic_sdlc.hooks.guard import decide
from agentic_sdlc.registry.profiles import ScanStep
from agentic_sdlc.tools.sandbox_exec import SandboxResult
from test_build_loop import FakeSandbox, ScriptedWorker, item, make_builder, profile  # noqa: F401  (profile: fixture)

M1 = [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])]


def finding(fid="REV-001", severity="major", confidence="high", lens="security", item_id="WI-001"):
    return Finding(id=fid, lens=lens, severity=severity, confidence=confidence, work_item_id=item_id,
                   file="server/src/a.ts:10", title="Missing auth check", detail="Anyone can read any cart", fix="check the owner")


def review(*findings, lenses=("spec", "security", "design", "quality", "operability")):
    return ReviewReport(milestone_id="M1", lenses_covered=list(lenses), findings=list(findings), summary="reviewed")


def with_scans(profile, *scans):
    comps = dict(profile.components)
    comps["backend"] = comps["backend"].model_copy(update={"scans": list(scans)})
    return profile.model_copy(update={"components": comps})


# ---------- Integrator ----------

def test_integrator_passes_a_clean_milestone_and_keeps_raw_results(tmp_path, prd, profile):
    b, s, sandbox, _ = make_builder(tmp_path, prd, profile, [item("WI-001")], M1)
    s.build.item("WI-001").status = "done"
    res = verify.integrate(b, M1[0], [item("WI-001")], 1)
    assert res.bugs == []
    assert "- Tasks: 1 of 1 done" in res.text() and "ALL UNCHANGED" in res.text()
    assert any("`check build` PASSED" in line for line in res.lines)
    assert all((b.ws.root / e).is_file() for e in res.evidence) and len(res.evidence) == 2       # build + test, raw output


def test_integrator_reports_each_kind_of_problem_without_fixing_anything(tmp_path, prd, profile):
    sandbox = FakeSandbox(check_results=[SandboxResult(exit_code=1, output="TS2322: type error")])
    items = [item("WI-001"), item("WI-002")]
    b, s, _, _ = make_builder(tmp_path, prd, profile, items,
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001", "WI-002"])], sandbox=sandbox)
    s.build.item("WI-001").status = "done"                                   # WI-002 was never built
    prof = profile.model_copy(update={"guardrails": {"contract_copies": {"docs/api-contract.yaml": ["server/openapi.yaml"]}}})
    b.profile = prof
    b.ws.write_text("docs/api-contract.yaml", "openapi: 3.1.0\n")
    b.ws.write_text("server/openapi.yaml", "openapi: 3.1.0\n# drifted\n")
    b.ws.write_text("server/test/acceptance/a.spec.ts", "original")
    b.ws.commit("tests")
    b._lock("server/test/acceptance")
    b.ws.write_text("server/test/acceptance/a.spec.ts", "weakened")
    b.ws.write_text("server/src/config.ts", 'const key = "AKIAABCDEFGHIJKLMNOP"\n')
    b.ws.commit("code")
    res = verify.integrate(b, b.s.backlog.milestones[0], items, 1)
    titles = {bug.title for bug in res.bugs}
    assert "WI-002 is not done (todo)" in titles
    assert "server/openapi.yaml differs from the approved contract" in titles
    assert "A locked acceptance test changed" in titles
    assert "Secret in the code" in titles
    assert "Integrated backend fails `check build`" in titles
    assert {bug.severity for bug in res.bugs if "locked" in bug.title or "Secret" in bug.title or "Integrated" in bug.title} == {"blocker"}
    assert (b.ws.root / "server/test/acceptance/a.spec.ts").read_text() == "weakened"          # the Integrator fixes nothing


# ---------- scans ----------

def test_scans_run_per_component_and_failures_become_bugs_with_the_right_severity(tmp_path, prd, profile):
    profile = with_scans(profile, ScanStep(name="lint", command="npm run lint", severity="major"),
                         ScanStep(name="dependency audit", command="npm audit", severity="minor", network=True),
                         ScanStep(name="coverage", command="npm run cov", severity="info"))
    sandbox = FakeSandbox()
    for cmd in ("npm run lint", "npm audit", "npm run cov"):
        sandbox.results_for[cmd] = [SandboxResult(exit_code=1, output=f"{cmd} says no")]
    b, s, _, _ = make_builder(tmp_path, prd, profile, [item("WI-001")], M1, sandbox=sandbox)
    s.build.item("WI-001").status = "done"
    res = verify.scans(b, M1[0], [item("WI-001")], 2)
    assert [(bug.title, bug.severity) for bug in res.bugs] == [("lint failed in backend", "major"),
                                                               ("dependency audit failed in backend", "minor")]   # info never fails
    assert "npm run lint says no" in (b.ws.root / "reports/scan_M1_r2_backend_lint.txt").read_text()
    assert len(res.evidence) == 3


def test_signature_ignores_wording_and_notices_an_unchanged_set(tmp_path, prd, profile):
    from agentic_sdlc.artifacts.reports import Bug
    one = Bug(id="A-1", work_item_id="WI-001", title="t", severity="major", steps="s", expected="e", actual="first words")
    same = one.model_copy(update={"id": "B-9", "actual": "other words"})
    assert verify.signature([one]) == verify.signature([same])
    assert verify.signature([one]) != verify.signature([one.model_copy(update={"title": "different"})])


# ---------- the QA loop with verification and review ----------

def test_a_verify_failure_overrules_a_passing_qa_report_and_goes_to_the_builders(tmp_path, prd, profile):
    profile = with_scans(profile, ScanStep(name="lint", command="npm run lint", severity="major"))
    sandbox = FakeSandbox()
    sandbox.results_for["npm run lint"] = [SandboxResult(exit_code=1, output="3 problems")]      # round 1 only
    b, s, _, worker = make_builder(tmp_path, prd, profile, [item("WI-001")], M1, sandbox=sandbox,
                                   cfg=BuildConfig(milestones=[], verify=True))
    b.run()
    qa = s.build.milestone("M1").qa_reports
    assert qa[0].passed is False and any(bug.title == "lint failed in backend" for bug in qa[0].bugs)   # QA said passed
    assert qa[1].passed is True and s.build.milestone("M1").status == "done"
    fix = [j for j in worker.jobs if j.task_key == "fix_work_item"][0]
    assert "lint failed in backend" in fix.inputs["problems"]
    assert "lint: FAILED" in [j for j in worker.jobs if j.task_key == "qa_milestone"][0].inputs["verification"]
    assert (b.ws.root / "docs/integration-report.md").read_text().startswith("---\nagent: pipeline")


def test_qa_and_review_are_read_only_and_review_only_sees_clean_milestones(tmp_path, prd, profile):
    worker = ScriptedWorker({"review_milestone": [review()]})
    b, s, _, _ = make_builder(tmp_path, prd, profile, [item("WI-001")], M1, worker=worker,
                              cfg=BuildConfig(milestones=[], review=True))
    b.run()
    qa = [j for j in worker.jobs if j.task_key == "qa_milestone"][0]
    rv = [j for j in worker.jobs if j.task_key == "review_milestone"][0]
    assert qa.policy == {"read_only": True} and rv.policy == {"read_only": True} and rv.agent_key == "code_reviewer"
    assert rv.inputs["diff_stat"] == "(no changes)"          # bookkeeping files (docs/, reports/) are not code to review
    assert s.build.milestone("M1").status == "done" and len(s.build.milestone("M1").reviews) == 1
    assert (b.ws.root / "reports/review_M1_round1.md").is_file() and (b.ws.root / "docs/review.md").is_file()


def test_the_reviewer_is_given_the_code_files_of_the_milestone(tmp_path, prd, profile):
    worker = ScriptedWorker({"review_milestone": [review()]})
    b, s, _, _ = make_builder(tmp_path, prd, profile, [item("WI-001")], M1, worker=worker,
                              cfg=BuildConfig(milestones=[], review=True))
    b.ws.commit("setup")
    b.ws.write_text("server/src/cart.ts", "export const x = 1;\n")
    s.build.item("WI-001").commit = b.ws.commit("WI-001")
    assert b.milestone_diff(M1[0]) == "- server/src/cart.ts (+1 -0)"


def test_actionable_review_findings_go_back_to_the_builders_and_the_rest_is_kept_for_the_gate(tmp_path, prd, profile):
    worker = ScriptedWorker({"review_milestone": [
        review(finding("REV-001"), finding("REV-002", severity="minor"), finding("REV-003", confidence="low")),
        review(finding("REV-004", severity="minor"))]})
    b, s, _, _ = make_builder(tmp_path, prd, profile, [item("WI-001")], M1, worker=worker,
                              cfg=BuildConfig(milestones=[], review=True))
    b.run()
    fix = [j for j in worker.jobs if j.task_key == "fix_work_item"]
    assert len(fix) == 1 and "[security] Missing auth check" in fix[0].inputs["problems"] and "REV-001" in fix[0].inputs["problems"]
    assert "REV-002" not in fix[0].inputs["problems"] and "REV-003" not in fix[0].inputs["problems"]
    mp = s.build.milestone("M1")
    assert mp.status == "done" and mp.qa_rounds == 2 and len(mp.reviews) == 2
    assert mp.open_findings == ["REV-004 [security/minor/high] Missing auth check (WI-001)"]


def test_a_review_that_skips_a_lens_or_names_a_wrong_task_is_sent_back_once(tmp_path, prd, profile):
    bad = review(finding(item_id="WI-999"), lenses=("spec", "security"))
    worker = ScriptedWorker({"review_milestone": [bad, review()]})
    b, s, _, _ = make_builder(tmp_path, prd, profile, [item("WI-001")], M1, worker=worker,
                              cfg=BuildConfig(milestones=[], review=True))
    b.run()
    jobs = [j for j in worker.jobs if j.task_key == "review_milestone"]
    assert len(jobs) == 2 and "not covered: design, quality, operability" in jobs[1].feedback and "WI-999" in jobs[1].feedback
    assert s.build.milestone("M1").status == "done"


def test_read_only_steps_cannot_write_through_the_hook_or_the_tool_list(tmp_path):
    policy = {"root": str(tmp_path), "read_only": True}
    assert "read-only" in decide(policy, {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "a.ts")}})
    assert decide(policy, {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "a.ts")}}) is None
    from agentic_sdlc.registry.agents import AgentRegistry
    from agentic_sdlc.registry.models import ModelRegistry
    from agentic_sdlc.registry.profiles import Profile
    from agentic_sdlc.settings import load_config
    from agentic_sdlc.tools.sandbox_exec import SandboxMode, SandboxRunner
    from agentic_sdlc.workspace import Workspace

    prof = Profile.load("flutter_nestjs_ecommerce")
    ws = Workspace.create("r", runs_dir=tmp_path)
    worker = ClaudeCodeWorker(AgentRegistry(load_config("agents"), ModelRegistry.from_config(), prof), load_config("tasks"), ws,
                              SandboxRunner(ws, prof.sandbox, SandboxMode.LOCAL))
    def allowed(read_only):
        cmd = worker.build_command(Job("build", "qa_engineer", "qa_milestone", {}, QAReport, ".", None,
                                       policy={"read_only": True} if read_only else None), "m", "s", "p")
        return cmd[cmd.index("--allowedTools") + 1: cmd.index("--disallowedTools")]
    assert "Edit" not in allowed(True) and "Write" not in allowed(True) and "Read" in allowed(True)
    assert "Edit" in allowed(False) and "Write" in allowed(False)


# ---------- package and the merge gate ----------

def test_package_lists_tasks_evidence_and_open_findings():
    md = package_markdown("run1", [{"id": "M1", "name": "Catalog", "status": "done", "qa_rounds": 2,
                                    "tasks": [{"id": "WI-001", "title": "API", "status": "done", "commit": "abc12345"}],
                                    "evidence": ["reports/qa_M1_round2.md"], "open": ["REV-004 minor"]}])
    assert "| WI-001 API | done | abc12345 |" in md and "- reports/qa_M1_round2.md" in md and "- REV-004 minor" in md
    assert "risk note" in md


def _merge_flow(tmp_path, canned, answers, worker, merge=True):
    from test_flow import PIPELINE, FakeRunner, Deps, Profile, SDLCFlow, Workspace
    pipeline = {**PIPELINE, "phases": {**PIPELINE["phases"], "build": True},
                "gates": {**PIPELINE["gates"], "merge": merge}, "build": {"milestones": ["M1"], "review": True},
                "limits": {**PIPELINE["limits"], "stop_on_blocked": False}}
    answers = iter(answers)
    prompts = []

    def deps_factory(state):
        def ask(prompt):
            prompts.append(prompt)
            return next(answers)
        return Deps(workspace=Workspace.create(state.run_id, runs_dir=tmp_path),
                    profile=Profile.load("flutter_nestjs_ecommerce"), runner=FakeRunner(canned), pipeline=pipeline,
                    input_fn=ask, sandbox=FakeSandbox(), worker_for=lambda agent: worker)

    return SDLCFlow(deps_factory=deps_factory), prompts


def test_merge_gate_needs_a_risk_note_and_shows_the_package(tmp_path, canned, capsys):
    worker = ScriptedWorker({"review_milestone": [review()]})
    flow, prompts = _merge_flow(tmp_path, canned, ["y", "", "y", "", "y", "Reviewed the cart totals myself; fine to merge.", ""], worker)
    flow.kickoff(inputs={"run_id": "g5", "brief": "shop"})
    assert flow.state.status == "completed", flow.state.stop_reason
    assert "Risk note, in your own words" in " ".join(prompts)
    out = capsys.readouterr().out.split("APPROVAL NEEDED: merge")[1]
    assert "docs/package.md" in out and "docs/review.md" in out
    decision = [d for d in flow.state.gate_history if d.gate == "merge"][-1]
    assert decision.approved and decision.gate_id == "G5" and "cart totals" in decision.risk_note
    assert (tmp_path / "g5" / "docs/package.md").read_text().count("| WI-001") == 1
    assert (tmp_path / "g5" / "gates" / "G5.gate").is_file()


def test_rejecting_the_merge_sends_feedback_to_the_builders_and_verifies_again(tmp_path, canned):
    worker = ScriptedWorker({"review_milestone": [review(), review()]})
    flow, _ = _merge_flow(tmp_path, canned, ["y", "", "y", "", "n", "The cart total ignores discounts",
                                             "y", "Fixed and checked the totals again myself.", ""], worker)
    flow.kickoff(inputs={"run_id": "g5r", "brief": "shop"})
    assert flow.state.status == "completed", flow.state.stop_reason
    fix = [j for j in worker.jobs if j.task_key == "fix_work_item"]
    assert len(fix) == 1 and "The cart total ignores discounts" in fix[0].inputs["problems"]
    assert [d.approved for d in flow.state.gate_history if d.gate == "merge"] == [False, True]
    assert flow.state.build.milestone("M1").qa_rounds == 2


def test_the_merge_gate_is_off_unless_the_pipeline_asks_for_it(tmp_path, canned):
    worker = ScriptedWorker({"review_milestone": [review()]})
    flow, prompts = _merge_flow(tmp_path, canned, ["y", "", "y", ""], worker, merge=False)
    flow.kickoff(inputs={"run_id": "g5o", "brief": "shop"})
    assert flow.state.status == "completed" and not [d for d in flow.state.gate_history if d.gate == "merge"]
