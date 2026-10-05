"""Step 5-6: locked acceptance tests (Test Writer), owned paths, verify commands, build notes, parallel
lanes and the Claude Code hook guard."""

import json
import threading

from git import Repo

from agentic_sdlc.artifacts.backlog import Milestone, WorkItem
from agentic_sdlc.artifacts.reports import WorkItemResult
from agentic_sdlc.artifacts.tests import AcceptanceSuite, AcceptanceTest
from agentic_sdlc.build.loop import BuildConfig
from agentic_sdlc.crews.base import TaskResult
from agentic_sdlc.guardrails import code as cg
from agentic_sdlc.hooks.guard import decide
from agentic_sdlc.registry.profiles import AcceptanceTests, Component
from agentic_sdlc.state import UsageRecord
from agentic_sdlc.tools.sandbox_exec import SandboxResult
from test_build_loop import FakeSandbox, make_builder, profile  # noqa: F401  (profile is a fixture)

M1 = [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001", "WI-002"])]


def task(wid, component="backend", owns=("server/src/catalog/**",), verify="npm test", ac=("AC-01",), deps=()):
    return WorkItem(id=wid, title=f"title {wid}", description="d", epic_id="E-01", component=component,
                    story_ids=["US-001"], depends_on=list(deps), estimate_points=2, owns=list(owns), verify=verify,
                    ac_ids=list(ac))


class FileWorker:
    """Writes files per task key (callable(job) -> {path: text}), then reports success."""

    def __init__(self, ws_root, writes, suite=None):
        self.root, self.writes, self.suite, self.jobs = ws_root, writes, suite, []
        self.lock = threading.Lock()

    def run(self, job):
        with self.lock:
            self.jobs.append(job)
        for path, text in (self.writes.get(job.task_key, lambda j: {})(job) or {}).items():
            p = self.root / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        if job.task_key == "write_acceptance_tests":
            out = self.suite(job) if callable(self.suite) else self.suite
        elif job.task_key == "qa_milestone":
            from agentic_sdlc.artifacts.reports import QAReport
            out = QAReport(milestone_id=job.inputs["milestone_id"], passed=True, summary="ok")
        else:
            out = WorkItemResult(summary=f"did {job.inputs.get('item_id')}", checks_passed=True)
        return TaskResult(artifact=out, usage=UsageRecord(phase="build", agent=job.agent_key, model="fake", total_tokens=5))


def with_acceptance(profile):
    comps = dict(profile.components)
    comps["backend"] = comps["backend"].model_copy(update={"acceptance": AcceptanceTests(
        dir="server/test/acceptance", command="npm run test:acceptance")})
    return profile.model_copy(update={"components": comps})


def suite_for(job):
    return AcceptanceSuite(tests=[AcceptanceTest(ac_id="AC-01", file="server/test/acceptance/ac-01.spec.ts",
                                                 test_name="AC-01 lists products")])


ACCEPTANCE_FILE = {"write_acceptance_tests": lambda j: {"server/test/acceptance/ac-01.spec.ts": "test('AC-01 lists products')"}}


# ---------- Test Writer ----------

def test_acceptance_tests_must_fail_first_then_are_locked(tmp_path, prd, profile):
    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    sandbox.results_for["npm run test:acceptance"] = [SandboxResult(exit_code=1, output="1 failing"),   # before build
                                                      SandboxResult(exit_code=0, output="1 passing")]   # after build
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True, guard_rules={"DV1", "DV2", "DV3"}))
    worker = FileWorker(b.ws.root, {**ACCEPTANCE_FILE, "implement_work_item": lambda j: {"server/src/catalog/a.ts": "x"}},
                        suite_for)
    b.worker_for = lambda agent: worker
    b.run()
    assert s.status == "running" and s.build.item("WI-001").status == "done"
    assert list(s.build.locked_tests) == ["server/test/acceptance/ac-01.spec.ts"]
    assert json.loads((b.ws.root / "tests.lock").read_text()) == s.build.locked_tests
    assert "| AC-01 | M1/backend | server/test/acceptance/ac-01.spec.ts |" in (b.ws.root / "docs/test-plan.md").read_text()
    tw = worker.jobs[0]
    assert tw.agent_key == "test_writer" and "AC-01" in tw.inputs["criteria"] and tw.policy["owns"] == ["server/test/acceptance"]
    assert worker.jobs[1].policy["locked"] == ["server/test/acceptance/ac-01.spec.ts"]          # builder: locked
    qa = [j for j in worker.jobs if j.task_key == "qa_milestone"][0]
    assert "backend: `npm run test:acceptance` PASSED" in qa.inputs["acceptance"]
    assert "Tests: 1 locked acceptance test file(s) for M1/backend" in Repo(b.ws.root).git.log("--format=%s")


def test_tests_that_pass_before_the_build_are_sent_back_then_stop_the_run(tmp_path, prd, profile):
    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    sandbox.results_for["npm run test:acceptance"] = [SandboxResult(exit_code=0, output="pass")] * 2
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True))
    worker = FileWorker(b.ws.root, ACCEPTANCE_FILE, suite_for)
    b.worker_for = lambda agent: worker
    b.run()
    assert s.status == "stopped" and "not usable after 2 attempts" in s.stop_reason
    assert "passes before the feature is built" in worker.jobs[1].feedback
    assert not s.build.locked_tests and s.build.item("WI-001").status != "done"


def test_a_builder_changing_a_locked_test_is_rejected(tmp_path, prd, profile):
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001", owns=("server/**",))], M1[:0] or
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])],
                              cfg=BuildConfig(milestones=[], guard_rules={"DV1"}, check_fix_attempts=0))
    b.ws.write_text("server/test/acceptance/ac-01.spec.ts", "locked")
    b.ws.commit("tests")
    b._lock("server/test/acceptance")
    worker = FileWorker(b.ws.root, {"implement_work_item": lambda j: {"server/test/acceptance/ac-01.spec.ts": "weakened"}})
    b.worker_for = lambda agent: worker
    b.run()
    assert s.build.item("WI-001").status == "failed"
    assert (b.ws.root / "server/test/acceptance/ac-01.spec.ts").read_text() == "locked"     # discarded


# ---------- owned paths, verify, build notes ----------

def test_changes_outside_the_owned_paths_are_rejected(tmp_path, prd, profile):
    worker = None
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])],
                              cfg=BuildConfig(milestones=[], guard_rules={"DV3"}))
    writes = iter([{"server/src/catalog/a.ts": "x", "server/src/users/b.ts": "y"}, {"server/src/catalog/a.ts": "x2"}])
    worker = FileWorker(b.ws.root, {"implement_work_item": lambda j: next(writes),
                                    "fix_work_item": lambda j: (b.ws.root.joinpath("server/src/users/b.ts").unlink()
                                                                or next(writes))})
    b.worker_for = lambda agent: worker
    b.run()
    fix = [j for j in worker.jobs if j.task_key == "fix_work_item"][0]
    assert "server/src/users/b.ts is outside the paths this task owns (server/src/catalog/**)" in fix.inputs["problems"]
    assert s.build.item("WI-001").status == "done"
    assert worker.jobs[0].policy["owns"] == ["server/src/catalog/**"]


def test_manifests_are_always_allowed_and_globs_match():
    comp = Component(agent="backend_developer", workdir="server", runtime="node")
    from agentic_sdlc.registry.profiles import Profile
    profile = Profile.load("flutter_nestjs_ecommerce")
    files = ["server/package.json", "server/src/cart/cart.service.ts", "server/src/app.module.ts", "app/lib/x.dart"]
    errors = cg.dv3_ownership(comp, profile, files, ["server/src/cart/**", "server/src/app.module.ts"])
    assert errors == ["DV3: app/lib/x.dart is outside the paths this task owns (server/src/cart/**, "
                      "server/src/app.module.ts); undo that change"]
    assert cg.dv3_ownership(comp, profile, ["app/lib/x.dart"], ["server/**"], ignore_prefixes=("app/",)) == []


def test_verify_command_runs_after_the_checks_and_failures_go_back(tmp_path, prd, profile):
    sandbox = FakeSandbox()
    sandbox.results_for["npm test -- catalog"] = [SandboxResult(exit_code=1, output="1 failing")]
    b, s, _, worker = make_builder(tmp_path, prd, profile, [task("WI-001", verify="npm test -- catalog")],
                                   [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox)
    b.run()
    assert s.build.item("WI-001").status == "done" and s.build.item("WI-001").attempts == 2
    fix = [j for j in worker.jobs if j.task_key == "fix_work_item"][0]
    assert "verify command `npm test -- catalog` failed" in fix.inputs["problems"]
    note = (b.ws.root / "docs/build-notes-backend.md").read_text()
    assert note.startswith("# Build notes: backend") and "## WI-001 title WI-001" in note
    assert "verify: `npm test -- catalog`: passed" in note and "Criteria: AC-01" in note


# ---------- parallel lanes ----------

def test_backend_and_frontend_lanes_build_in_parallel_with_separate_commits(tmp_path, prd, profile):
    items = [task("WI-001"), task("WI-002", component="frontend", owns=("app/lib/catalog/**",), verify="flutter test",
                                  deps=["WI-001"])]
    b, s, _, _ = make_builder(tmp_path, prd, profile, items, M1, cfg=BuildConfig(milestones=[], parallel=True,
                                                                                  guard_rules={"DV3"}))
    both_running = threading.Barrier(2, timeout=10)

    def write(path):
        def go(job):
            both_running.wait()                 # proves both lanes are in flight at the same time
            return {path: "x"}
        return go

    worker = FileWorker(b.ws.root, {"implement_work_item": lambda j: write(
        "server/src/catalog/a.ts" if j.inputs["item_id"] == "WI-001" else "app/lib/catalog/b.dart")(j)})
    b.worker_for = lambda agent: worker
    b.run()
    assert s.build.item("WI-001").status == "done" and s.build.item("WI-002").status == "done"
    repo = Repo(b.ws.root)
    files = {msg: [f for f in repo.git.show("--name-only", "--format=", sha).split() if not f.startswith("reports/")]
             for sha, msg in (line.split(" ", 1) for line in repo.git.log("--format=%H %s").splitlines())}
    assert files["WI-001: title WI-001 (backend_developer)"] == ["docs/build-notes-backend.md", "server/src/catalog/a.ts"]
    assert files["WI-002: title WI-002 (frontend_developer)"] == ["app/lib/catalog/b.dart", "docs/build-notes-frontend.md"]


# ---------- hook guard and worker settings ----------

def test_hook_guard_blocks_what_the_task_may_not_touch(tmp_path):
    policy = {"root": str(tmp_path), "owns": ["server/src/cart/**"], "workdir": "server",
              "locked": ["server/test/acceptance/ac-01.spec.ts"], "also_allowed": ["reports/"]}

    def write(path):
        return decide(policy, {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / path)}})

    assert write("server/src/cart/cart.service.ts") is None
    assert write("server/package.json") is None and write("reports/x.log") is None
    assert "outside the paths this task owns" in write("server/src/users/u.ts")
    assert "locked acceptance test" in write("server/test/acceptance/ac-01.spec.ts")
    assert "managed by the pipeline" in write("gates/G2.gate") and "managed by the pipeline" in write("docs/spec.md")
    assert "outside the run folder" in decide(policy, {"tool_name": "Edit", "tool_input": {"file_path": "/etc/passwd"}})
    assert "never deploy" in decide(policy, {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})
    assert decide(policy, {"tool_name": "Bash", "tool_input": {"command": "npm test"}}) is None
    assert decide(policy, {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}) is None


def test_claude_code_worker_runs_the_guard_as_a_pretooluse_hook(tmp_path):
    from agentic_sdlc.build.coders import ClaudeCodeWorker, Job
    from agentic_sdlc.registry.agents import AgentRegistry
    from agentic_sdlc.registry.models import ModelRegistry
    from agentic_sdlc.registry.profiles import Profile
    from agentic_sdlc.settings import load_config
    from agentic_sdlc.tools.sandbox_exec import SandboxMode, SandboxRunner
    from agentic_sdlc.workspace import Workspace

    profile = Profile.load("flutter_nestjs_ecommerce")
    ws = Workspace.create("r", runs_dir=tmp_path)
    worker = ClaudeCodeWorker(AgentRegistry(load_config("agents"), ModelRegistry.from_config(), profile),
                              load_config("tasks"), ws, SandboxRunner(ws, profile.sandbox, SandboxMode.LOCAL))
    job = Job("build", "backend_developer", "implement_work_item", {}, WorkItemResult, "server", "node",
              policy={"owns": ["server/src/cart/**"]})
    cmd = worker.build_command(job, "claude-sonnet-5-5", "sys", "prompt")
    settings = json.loads(open(cmd[cmd.index("--settings") + 1]).read())
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Write|Edit|MultiEdit|NotebookEdit|Bash"
    assert "-m agentic_sdlc.hooks.guard" in hook["hooks"][0]["command"]
    policy_file = hook["hooks"][0]["command"].rsplit('"', 2)[-2]
    assert json.loads(open(policy_file).read())["owns"] == ["server/src/cart/**"]
    no_policy = worker.build_command(Job("build", "backend_developer", "t", {}, WorkItemResult, "server", "node"),
                                     "m", "s", "p")
    assert "--settings" not in no_policy
