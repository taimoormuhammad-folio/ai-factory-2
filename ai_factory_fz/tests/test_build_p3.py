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


# ---------- environment failures and the throwaway database ----------

def test_environment_failures_are_told_apart_from_missing_features():
    from agentic_sdlc.build.services import environment_failure

    assert "Can't reach database server" in environment_failure(
        "PrismaClientInitializationError:\nCan't reach database server at `localhost:5432`")
    assert environment_failure("connect ECONNREFUSED 127.0.0.1:5432")
    assert "No devices are connected" in environment_failure(          # Flutter's integration_test/ needs a device
        "No supported devices connected.\nNo devices are connected. Ensure that `flutter doctor` shows one")
    assert environment_failure("AssertionError: expected 404 to be 200") is None
    assert environment_failure("") is None


def test_a_suite_that_fails_for_the_environment_is_sent_back_not_locked(tmp_path, prd, profile):
    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    broken = SandboxResult(exit_code=1, output="Can't reach database server at `localhost:5432`")
    sandbox.results_for["npm run test:acceptance"] = [broken, broken]
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True))
    worker = FileWorker(b.ws.root, ACCEPTANCE_FILE, suite_for)
    b.worker_for = lambda agent: worker
    b.run()
    assert s.status == "stopped" and not s.build.locked_tests
    assert "fails because of the environment, not because the feature is missing" in worker.jobs[1].feedback


def test_the_suite_runs_inside_the_throwaway_database(tmp_path, prd, profile, monkeypatch):
    import contextlib

    from agentic_sdlc.build import loop
    from agentic_sdlc.registry.profiles import AcceptanceDatabase

    seen = []

    @contextlib.contextmanager
    def fake_db(sandbox, db):
        seen.append(db)
        yield {"DATABASE_URL": "postgresql://app:app@localhost:40000/app"}

    from agentic_sdlc.build import services

    monkeypatch.setattr(services, "acceptance_database", fake_db)

    class Sandbox(FakeSandbox):
        def run_trusted(self, runtime, workdir, command, env=None, host_network=False, timeout_s=None):
            self.commands.append((command, dict(env or {}), host_network))
            return SandboxResult(exit_code=0, output="ok")

    profile = with_acceptance(profile)
    comps = dict(profile.components)
    comps["backend"] = comps["backend"].model_copy(update={"acceptance": comps["backend"].acceptance.model_copy(
        update={"database": AcceptanceDatabase(prepare=["npx prisma db push"])})})
    profile = profile.model_copy(update={"components": comps})
    sandbox = Sandbox()
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")], M1[:0] or
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox)
    b.s.build.locked_tests["server/test/acceptance/ac-01.spec.ts"] = "x"
    b.ws.write_text("server/test/acceptance/ac-01.spec.ts", "x")
    result = b.run_acceptance(b.s.backlog.milestones[0])
    ran = [c for c in sandbox.commands if c[0] in ("npx prisma db push", "npm run test:acceptance")]
    assert [c[0] for c in ran] == ["npx prisma db push", "npm run test:acceptance"]
    assert all(c[1] == {"DATABASE_URL": "postgresql://app:app@localhost:40000/app"} and c[2] is True for c in ran)
    assert "backend: `npm run test:acceptance` FAILED" in result or "PASSED" in result


def test_test_paths_listed_from_the_component_folder_are_accepted(tmp_path, prd, profile):
    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    sandbox.results_for["npm run test:acceptance"] = [SandboxResult(exit_code=1, output="expected 404 to be 201")]
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True))
    from_server = AcceptanceSuite(tests=[AcceptanceTest(ac_id="AC-01", file="test/acceptance/ac-01.spec.ts",
                                                        test_name="AC-01 lists products")])
    worker = FileWorker(b.ws.root, ACCEPTANCE_FILE, from_server)       # the file sits in server/test/acceptance/
    b.worker_for = lambda agent: worker
    b.run()
    assert s.build.acceptance["M1/backend"].tests[0].file == "server/test/acceptance/ac-01.spec.ts"
    assert list(s.build.locked_tests) == ["server/test/acceptance/ac-01.spec.ts"]


def test_an_agent_that_reports_denied_shell_access_is_not_treated_as_blocked():
    from agentic_sdlc.build.loop import _agent_sandbox_block_is_retryable as retry

    assert retry("Bash permission is denied in this session (don't-ask mode). Nothing was implemented.")
    assert retry("Command execution is blocked")
    assert not retry("The spec does not say which currency to use")


def test_builders_are_told_which_locked_tests_to_read_first(tmp_path, prd, profile):
    profile = with_acceptance(profile)
    b, s, _, worker = make_builder(tmp_path, prd, profile, [task("WI-001")],
                                   [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])])
    s.build.locked_tests["server/test/acceptance/ac-01.spec.ts"] = "x"
    s.build.locked_tests["app/acceptance_test/ac_01_test.dart"] = "y"
    b.run()
    given = worker.jobs[0].inputs["acceptance_files"]
    assert given == "- server/test/acceptance/ac-01.spec.ts"                 # only this component's tests
    from agentic_sdlc.settings import load_config
    import re
    for key in ("implement_work_item", "fix_work_item"):
        needed = set(re.findall(r"\{(\w+)\}", load_config("tasks")[key]["description"]))
        assert "acceptance_files" in needed and needed - {"tooling"} <= set(given_inputs := worker.jobs[0].inputs), key


def test_flutter_containers_keep_the_android_sdk_and_gradle_cache_between_runs(tmp_path):
    from agentic_sdlc.registry.profiles import Profile
    from agentic_sdlc.tools.sandbox_exec import SandboxMode, SandboxRunner
    from agentic_sdlc.workspace import Workspace

    for name in ("flutter_nestjs_ecommerce", "flutter_nestjs_netsuite"):
        profile = Profile.load(name)
        ws = Workspace.create("r", runs_dir=tmp_path)
        cmd, _ = SandboxRunner(ws, profile.sandbox, SandboxMode.DOCKER).build_command("flutter", "app", ["flutter", "build", "apk"])
        volumes = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        assert "sdlc-android-sdk:/opt/android-sdk-linux" in volumes and "sdlc-gradle:/cache/gradle" in volumes
        assert "GRADLE_USER_HOME=/cache/gradle" in cmd
        node, _ = SandboxRunner(ws, profile.sandbox, SandboxMode.DOCKER).build_command("node", "server", ["npm", "test"])
        assert not any(v.startswith("sdlc-") for v in [node[i + 1] for i, a in enumerate(node) if a == "-v"])   # only Flutter


# ---------- acceptance after every task ----------

def test_failing_lines_are_matched_to_the_tasks_criteria():
    from agentic_sdlc.build.loop import failing_for

    out = "  ✓ AC-02 lists carts\n  ✕ AC-01 lists products (12 ms)\n● Catalog › AC-03 detail 404\nAC-01 mentioned in a passing line\n"
    assert failing_for(out, ["AC-01"]) == ["✕ AC-01 lists products (12 ms)"]
    assert failing_for(out, ["AC-03"]) == ["● Catalog › AC-03 detail 404"]
    assert failing_for(out, ["AC-09"]) == []


def _own_acceptance(tmp_path, prd, profile, results):
    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    sandbox.results_for["npm run test:acceptance"] = results
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True, guard_rules={"DV1", "DV2", "DV3"}))
    s.build.locked_tests = {"server/test/acceptance/ac-01.spec.ts": "h"}
    return b, s


def test_a_tasks_own_failing_acceptance_test_goes_back_to_the_builder_in_the_same_loop(tmp_path, prd, profile):
    fail = SandboxResult(exit_code=1, output="  ✕ AC-01 lists products\n  ✕ AC-07 not built yet\n")
    b, s = _own_acceptance(tmp_path, prd, profile, [fail, SandboxResult(exit_code=0, output="ok")])
    ok, text = b.run_own_acceptance(b.items["WI-001"])
    assert not ok and "AC-01 lists products" in text and "AC-07" not in text.split("Last output")[0]
    assert "never the tests" in text
    assert b.run_own_acceptance(b.items["WI-001"]) == (True, "")


def test_tests_of_criteria_other_tasks_serve_are_ignored_and_machine_problems_are_not_blamed(tmp_path, prd, profile):
    other = SandboxResult(exit_code=1, output="  ✕ AC-07 not built yet\n")
    env = SandboxResult(exit_code=1, output="  ✕ AC-01 lists products\nError: connect ECONNREFUSED 127.0.0.1:5432\n")
    b, s = _own_acceptance(tmp_path, prd, profile, [other, env])
    assert b.run_own_acceptance(b.items["WI-001"]) == (True, "")
    assert b.run_own_acceptance(b.items["WI-001"]) == (True, "")
    b.cfg.acceptance_each_task = False
    assert b.run_own_acceptance(b.items["WI-001"]) == (True, "")


def test_a_failing_check_with_a_network_error_is_retried_before_the_agent_sees_it(tmp_path, prd, profile, monkeypatch):
    from agentic_sdlc.build import loop

    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    sandbox = FakeSandbox()
    key = profile.components["backend"].checks[0]
    sandbox.results_for[key] = [SandboxResult(exit_code=1, output="npm ERR! code EAI_AGAIN"), SandboxResult(exit_code=0, output="ok")]
    b, s, _, worker = make_builder(tmp_path, prd, profile, [task("WI-001")],
                                   [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox)
    b.run()
    assert s.build.item("WI-001").status == "done" and s.build.item("WI-001").attempts == 1
    assert not [j for j in worker.jobs if j.task_key == "fix_work_item"]


def test_a_task_refused_the_same_files_twice_stops_with_an_ownership_message(tmp_path, prd, profile):
    writes = {"implement_work_item": lambda j: {"server/src/orders/other.ts": "x"},
              "fix_work_item": lambda j: {"server/src/orders/other.ts": "x"}}
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])],
                              cfg=BuildConfig(milestones=[], guard_rules={"DV1", "DV2", "DV3"}, check_fix_attempts=5))
    worker = FileWorker(b.ws.root, writes)
    b.worker_for = lambda agent: worker
    b.run()
    p = s.build.item("WI-001")
    assert p.status == "blocked" and p.attempts == 2          # not 6
    assert "needs an ownership change" in p.reason and "server/src/orders/other.ts" in p.reason


def test_every_agent_that_writes_files_has_the_write_tool():
    """On the API path an agent can only write files through fs_write (the Claude Code path hid its absence)."""
    from agentic_sdlc.settings import load_config

    agents, tasks = load_config("agents"), load_config("tasks")
    writers = {"implement_work_item", "fix_work_item", "write_acceptance_tests", "write_smoke_tests",
               "write_device_tests", "deploy_staging"}
    for key in writers:
        agent = tasks[key]["agent"]
        assert "fs_write" in agents[agent]["tools"], f"{agent} runs {key} but has no fs_write"


def test_device_suites_are_short_and_use_keys_not_guessed_widget_types(tmp_path, profile):
    from agentic_sdlc.guardrails import agents as ag
    from agentic_sdlc.registry.profiles import Profile
    from agentic_sdlc.workspace import Workspace

    profile = Profile.load("flutter_nestjs_ecommerce")
    ws = Workspace.create("d", runs_dir=tmp_path)
    ws.write_text("app/integration_test/a_test.dart", "testWidgets('a', (t) async { await t.tap(find.byKey(Key('x'))); });\n")
    assert ag.st1_device_suite(ws, profile) == []
    ws.write_text("app/integration_test/a_test.dart",
                  "testWidgets('a', (t) async { await t.tap(find.byType(ElevatedButton)); });\n" * 9)
    errors = " ".join(ag.st1_device_suite(ws, profile))
    assert "9 on-device journeys; write at most 8" in errors and "guessed type (ElevatedButton)" in errors


# ---------- coverage: every criterion gets a locked test ----------

def test_uncovered_criteria_are_listed_with_must_haves_marked(prd):
    from agentic_sdlc.artifacts.tests import AcceptanceSuite, AcceptanceTest
    from agentic_sdlc.build.coverage import markdown, uncovered

    suites = {"M1/backend": AcceptanceSuite(tests=[AcceptanceTest(ac_id="AC-01", file="f", test_name="AC-01 x")])}
    gaps = uncovered(prd, suites)
    assert "AC-01" not in [g[0] for g in gaps] and gaps, "AC-02 and the rest have no test"
    assert "AC-02" in markdown(gaps) and "WITHOUT a locked acceptance test" in markdown(gaps)
    assert markdown([]).startswith("All acceptance criteria")


def test_criteria_of_built_tasks_without_tests_get_catch_up_tests_that_must_pass(tmp_path, prd, profile):
    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    sandbox.results_for["npm run test:acceptance"] = [SandboxResult(exit_code=0, output="1 passing")]   # code exists: passes
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True, guard_rules={"DV1", "DV2", "DV3"}))
    s.build.item("WI-001").status = "done"                       # built before any test existed (a resumed run)
    worker = FileWorker(b.ws.root, ACCEPTANCE_FILE, suite_for)
    b.worker_for = lambda agent: worker
    b.write_acceptance_tests(b.s.backlog.milestones[0])
    tw = [j for j in worker.jobs if j.task_key == "write_acceptance_tests"]
    assert len(tw) == 1 and "CATCH-UP" in tw[0].inputs["mode"] and "PASS" in tw[0].inputs["mode"]
    assert list(s.build.locked_tests) == ["server/test/acceptance/ac-01.spec.ts"]
    assert b.covered_ac_ids("backend") == {"AC-01"}
    b.write_acceptance_tests(b.s.backlog.milestones[0])           # nothing missing now: no second job
    assert len([j for j in worker.jobs if j.task_key == "write_acceptance_tests"]) == 1


def test_catch_up_tests_that_fail_against_existing_code_are_sent_back(tmp_path, prd, profile):
    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    sandbox.results_for["npm run test:acceptance"] = [SandboxResult(exit_code=1, output="1 failing"),
                                                      SandboxResult(exit_code=0, output="1 passing")]
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True, guard_rules={"DV1", "DV2", "DV3"}))
    s.build.item("WI-001").status = "done"
    worker = FileWorker(b.ws.root, ACCEPTANCE_FILE, suite_for)
    b.worker_for = lambda agent: worker
    b.write_acceptance_tests(b.s.backlog.milestones[0])
    tw = [j for j in worker.jobs if j.task_key == "write_acceptance_tests"]
    assert len(tw) == 2 and "fails although the code exists" in tw[1].feedback


def test_a_block_after_files_were_written_goes_back_to_the_writer_with_the_file_list(tmp_path, prd, profile):
    from agentic_sdlc.artifacts.tests import AcceptanceSuite

    profile = with_acceptance(profile)
    sandbox = FakeSandbox()
    sandbox.results_for["npm run test:acceptance"] = [SandboxResult(exit_code=1, output="1 failing")]
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])], sandbox=sandbox,
                              cfg=BuildConfig(milestones=[], acceptance_tests=True, guard_rules={"DV1", "DV2", "DV3"}))
    answers = [AcceptanceSuite(tests=[], blocked=True, blocked_reason="no input given"), suite_for(None)]
    worker = FileWorker(b.ws.root, ACCEPTANCE_FILE, lambda job: answers.pop(0))
    b.worker_for = lambda agent: worker
    b.write_acceptance_tests(b.s.backlog.milestones[0])
    tw = [j for j in worker.jobs if j.task_key == "write_acceptance_tests"]
    assert len(tw) == 2 and "Do not block" in tw[1].feedback and "server/test/acceptance/ac-01.spec.ts" in tw[1].feedback
    assert s.status != "stopped" and list(s.build.locked_tests) == ["server/test/acceptance/ac-01.spec.ts"]


def test_a_block_with_no_files_written_still_stops_the_run_for_a_person(tmp_path, prd, profile):
    from agentic_sdlc.artifacts.tests import AcceptanceSuite

    profile = with_acceptance(profile)
    b, s, _, _ = make_builder(tmp_path, prd, profile, [task("WI-001")],
                              [Milestone(id="M1", name="m", goal="g", work_item_ids=["WI-001"])],
                              cfg=BuildConfig(milestones=[], acceptance_tests=True))
    worker = FileWorker(b.ws.root, {}, lambda job: AcceptanceSuite(tests=[], blocked=True, blocked_reason="AC-03 is untestable"))
    b.worker_for = lambda agent: worker
    b.write_acceptance_tests(b.s.backlog.milestones[0])
    assert s.status == "stopped" and "AC-03 is untestable" in s.stop_reason


def test_an_empty_api_credit_balance_is_a_usage_limit_not_a_code_failure():
    from agentic_sdlc.crews.base import is_usage_limit

    assert is_usage_limit("Error code: 400 - {'message': 'Your credit balance is too low to access the Anthropic API.'}")
    assert is_usage_limit("You exceeded your current quota: insufficient_quota")
    assert not is_usage_limit("Error code: 400 - invalid_request_error: messages.0.content: Field required")


def test_the_secret_scan_skips_what_the_pipeline_writes_but_not_what_agents_write(tmp_path, profile):
    from agentic_sdlc.guardrails import code as cg
    from agentic_sdlc.workspace import Workspace

    ws = Workspace.create("sec", runs_dir=tmp_path)
    leak = "DATABASE_URL=postgresql://app:hunter2secret@db:5432/app\n"
    ws.write_text("reports/agent_transcript.jsonl", leak)       # a prompt quoting the profile's example URL
    ws.write_text("evidence/_suites/backend.txt", leak)
    ws.write_text("server/src/config.ts", leak)                 # an agent hard-coding it: still caught
    errors = cg.dv2_secrets(ws, profile, ["reports/agent_transcript.jsonl", "evidence/_suites/backend.txt", "server/src/config.ts"])
    assert len(errors) == 1 and "server/src/config.ts" in errors[0]
    assert cg.is_pipeline_file("docs/spec.md") and cg.is_pipeline_file("gates/G1.gate") and not cg.is_pipeline_file("app/lib/main.dart")


def test_scaffold_steps_can_be_skipped_by_a_glob_and_the_profiles_build_the_api_client(tmp_path):
    from agentic_sdlc.build.scaffold import scaffold
    from agentic_sdlc.registry.profiles import Component, Profile, ScaffoldStep
    from agentic_sdlc.workspace import Workspace

    ws = Workspace.create("sc", runs_dir=tmp_path)
    ws.write_text("app/lib/model.g.dart", "// generated")
    comp = Component(agent="a", workdir="app", runtime="flutter",
                     scaffold=[ScaffoldStep(run="dart run build_runner build", creates="app/lib/**/*.g.dart")])
    assert scaffold("frontend", comp, ws, FakeSandbox()) == ["skip (exists): app/lib/**/*.g.dart"]
    for name in ("flutter_nestjs_ecommerce", "flutter_nestjs_netsuite"):
        steps = [s.run for s in Profile.load(name).components["frontend"].scaffold]
        assert "dart run build_runner build --delete-conflicting-outputs" in steps
        assert steps.index("dart run build_runner build --delete-conflicting-outputs") > steps.index("flutter pub get")
