"""Release phase: contract diff, real local staging process handling, Releaser loop, flow + Gate 3."""

import json
import socket
import subprocess
import sys

import pytest

from agentic_sdlc.artifacts.reports import Bug, QAReport, WorkItemResult
from agentic_sdlc.registry.profiles import Profile, ReleaseConfig, Runtime
from agentic_sdlc.release.contract import contract_diff
from agentic_sdlc.release.releaser import Releaser
from agentic_sdlc.release.staging import Staging, StagingError
from agentic_sdlc.state import ItemProgress, ProjectState
from agentic_sdlc.tools.sandbox_exec import SandboxMode, SandboxResult, SandboxRunner
from agentic_sdlc.workspace import Workspace
from test_build_loop import ScriptedWorker

CONTRACT = """
openapi: 3.1.0
info: {title: t, version: '1'}
servers: [{url: 'http://localhost:3000/api/v1'}]
paths:
  /products: {get: {operationId: a, responses: {'200': {description: ok}}}}
  /products/{productId}: {get: {operationId: b, responses: {'200': {description: ok}}}}
  /cart: {post: {operationId: c, responses: {'200': {description: ok}}}}
"""


def test_contract_diff_normalises_prefix_and_param_names():
    served = {"openapi": "3.0.0", "paths": {
        "/api/v1/products": {"get": {}}, "/api/v1/products/{id}": {"get": {}}, "/api/v1/debug": {"get": {}}}}
    issues = contract_diff(CONTRACT, json.dumps(served), "/api/v1")
    assert issues == ["Missing in the API: POST /cart", "Not in the contract: GET /debug"]
    assert contract_diff(CONTRACT, "not json", "/api/v1") == ["The API does not serve a valid OpenAPI JSON document"]


# ---------- real local staging with a stand-in API ----------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def local_staging(tmp_path, start_cmd, monkeypatch, db_url="postgresql://x"):
    if db_url:
        monkeypatch.setenv("SDLC_STAGING_DATABASE_URL", db_url)
    else:
        monkeypatch.delenv("SDLC_STAGING_DATABASE_URL", raising=False)
    base = Profile.load("flutter_nestjs_ecommerce")
    profile = base.model_copy(update={"release": ReleaseConfig(api_component="backend", health_path="/", local_start=start_cmd)})
    profile.components["backend"] = profile.components["backend"].model_copy(update={"workdir": "server", "runtime": "py"})
    ws = Workspace.create("r", runs_dir=tmp_path)
    (ws.root / "server").mkdir(exist_ok=True)
    sandbox = SandboxRunner(ws, profile.sandbox.model_copy(update={"runtimes": {"py": Runtime(image="-", local_binary=sys.executable)}}),
                            SandboxMode.LOCAL)
    port = free_port()
    return Staging(ws, profile, sandbox, port=port, startup_timeout_s=15), port


def test_local_staging_starts_waits_for_health_and_stops(tmp_path, monkeypatch):
    staging, port = local_staging(tmp_path, f"{sys.executable} -m http.server PORT_PLACEHOLDER", monkeypatch)
    staging.rel.local_start = f"{sys.executable} -m http.server {port}"
    staging.start()
    try:
        assert staging._proc.poll() is None
    finally:
        staging.stop()
    assert staging._proc is None


def test_local_staging_reports_crash_with_logs(tmp_path, monkeypatch):
    staging, _ = local_staging(tmp_path, f"{sys.executable} -c \"print('Config validation error: JWT_SECRET is required'); raise SystemExit(1)\"", monkeypatch)
    with pytest.raises(StagingError, match="JWT_SECRET is required"):
        staging.start()


def test_local_staging_needs_a_database_url(tmp_path, monkeypatch):
    staging, _ = local_staging(tmp_path, "true", monkeypatch, db_url=None)
    assert "SDLC_STAGING_DATABASE_URL" in staging.unavailable_reason()


# ---------- Releaser with fakes ----------

class FakeStaging:
    def __init__(self, fail_start=None):
        self.base_url, self.port, self.mode = "http://localhost:3999", 3999, "local"
        self.fail_start = list(fail_start or [])
        self.started = self.stopped = 0

    def start(self):
        self.started += 1
        if self.fail_start and self.fail_start.pop(0):
            raise StagingError("boot failed: missing env JWT_SECRET")

    def stop(self):
        self.stopped += 1

    def logs(self, chars=4000):
        return "log line"


class FakeSandbox:
    def __init__(self, smoke=None):
        self.smoke = list(smoke or [])
        self.calls = []

    def run_trusted(self, runtime, workdir, command, env=None, host_network=False, timeout_s=None):
        self.calls.append((command, env, host_network))
        if command == "npm run test:smoke" and self.smoke:
            return self.smoke.pop(0)
        return SandboxResult(exit_code=0, output="ok")


def releaser(tmp_path, prd, backlog, worker, staging=None, sandbox=None, cfg=None, openapi=False):
    state = ProjectState(run_id="r", prd=prd, backlog=backlog)
    state.build.items = {"WI-001": ItemProgress(status="done", summary="api")}
    ws = Workspace.create("r", runs_dir=tmp_path)
    ws.write_text("docs/api-contract.yaml", CONTRACT)
    profile = Profile.load("flutter_nestjs_ecommerce")
    if not openapi:
        profile = profile.model_copy(update={"release": profile.release.model_copy(update={"openapi_json_path": ""})})

    def stop(reason):
        state.status, state.stop_reason = "stopped", reason

    r = Releaser(state, ws, profile, sandbox or FakeSandbox(), lambda agent: worker, staging or FakeStaging(),
                 cfg or {"fix_rounds": 1}, record=lambda res: res.artifact, checkpoint=lambda m: None,
                 can_continue=lambda: state.status == "running", stop=stop)
    return r, state, ws


def passing_integration():
    return QAReport(milestone_id="integration", passed=True, summary="wired")


def test_verify_happy_path(tmp_path, prd, backlog):
    worker = ScriptedWorker({"integration_review": [passing_integration()]})
    sandbox = FakeSandbox()
    staging = FakeStaging()
    r, s, ws = releaser(tmp_path, prd, backlog, worker, staging=staging, sandbox=sandbox)
    r.verify()
    assert s.release.verified and s.release.smoke_passed
    assert [j.task_key for j in worker.jobs] == ["deploy_staging", "write_smoke_tests", "integration_review"]
    assert [j.agent_key for j in worker.jobs] == ["deployment_engineer", "smoke_tester", "integration_pass"]
    smoke = [c for c in sandbox.calls if c[0] == "npm run test:smoke"][0]
    assert smoke[1] == {"SMOKE_BASE_URL": "http://localhost:3999/api/v1"} and smoke[2] is True
    assert staging.started == staging.stopped == 1
    assert (ws.root / "reports" / "release_round1.md").exists()


def test_smoke_failure_goes_to_developer_then_passes(tmp_path, prd, backlog):
    worker = ScriptedWorker({"integration_review": [passing_integration(), passing_integration()]})
    sandbox = FakeSandbox(smoke=[SandboxResult(exit_code=1, output="POST /auth/signup expected 201 got 500")])
    r, s, _ = releaser(tmp_path, prd, backlog, worker, sandbox=sandbox)
    r.verify()
    assert s.release.verified and s.release.rounds == 2
    fix = [j for j in worker.jobs if j.task_key == "fix_work_item"][0]
    import re

    from agentic_sdlc.settings import load_config
    needed = set(re.findall(r"\{(\w+)\}", load_config("tasks")["fix_work_item"]["description"])) - {"tooling"}   # tooling: the worker's
    assert needed <= set(fix.inputs), f"fix_work_item prompt needs {sorted(needed - set(fix.inputs))} from the release phase"
    assert fix.agent_key == "backend_developer" and "expected 201 got 500" in fix.inputs["problems"]


def test_exhausted_rounds_go_to_the_human_not_a_stop(tmp_path, prd, backlog):
    bug = Bug(id="BUG-1", work_item_id="RELEASE", title="wrong prefix", severity="blocker", steps="s", expected="/api/v1", actual="/")
    bad = QAReport(milestone_id="integration", passed=False, bugs=[bug], summary="bad")
    worker = ScriptedWorker({"integration_review": [bad, bad]})
    r, s, _ = releaser(tmp_path, prd, backlog, worker)
    r.verify()
    assert s.status == "running" and s.release.failed and not s.release.verified
    summary = r.gate_summary()
    assert "FAILED" in summary and "wrong prefix" in summary and "Approving ships the release WITH" in summary
    r.verify()                                   # resumed while waiting at Gate 3: no new rounds
    assert s.release.rounds == 2
    assert "known problems" in r.release_notes("t")


def test_gate3_rejection_sends_open_problems_to_each_component(tmp_path, prd, backlog):
    worker = ScriptedWorker()
    r, s, _ = releaser(tmp_path, prd, backlog, worker)
    s.release.open_problems = [["backend", "smoke failed"], ["frontend", "device test failed"]]
    r.fix_after_rejection("cart total is wrong")
    fixes = [j for j in worker.jobs if j.task_key == "fix_work_item"]
    assert [j.agent_key for j in fixes] == ["backend_developer", "frontend_developer"]
    assert "cart total is wrong" in fixes[1].inputs["problems"] and "device test failed" in fixes[1].inputs["problems"]
    assert "device test failed" not in fixes[0].inputs["problems"]
    assert s.release.open_problems == []


def test_machine_problems_stop_the_run_without_a_developer_fix(tmp_path, prd, backlog):
    class Busy(FakeStaging):
        def start(self):
            raise StagingError("Port 3999 is already in use by container other-app", environment=True)

    worker = ScriptedWorker()
    r, s, ws = releaser(tmp_path, prd, backlog, worker, staging=Busy())
    r.verify()
    assert s.status == "stopped" and "machine problem" in s.stop_reason and "other-app" in s.stop_reason
    assert not [j for j in worker.jobs if j.task_key == "fix_work_item"]
    assert "device checks are disabled" in (ws.root / "reports" / "release_round1.md").read_text()


# ---------- staging port ----------

def compose_staging(tmp_path, port_busy, containers, monkeypatch):
    from agentic_sdlc.release import staging as mod
    from agentic_sdlc.tools.sandbox_exec import SandboxMode, SandboxRunner
    profile = Profile.load("flutter_nestjs_ecommerce")
    ws = Workspace.create("r", runs_dir=tmp_path)
    st = mod.Staging(ws, profile, SandboxRunner(ws, profile.sandbox, SandboxMode.DOCKER), port=3999)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = ""
        if cmd[:2] == ["docker", "ps"] and "--format" in cmd:
            out = "\n".join("\t".join(c) for c in containers)
        elif cmd[:2] == ["docker", "ps"]:
            out = "abc123\n"
        elif cmd[:2] == ["docker", "stop"]:
            port_busy[:] = [False]
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(mod.docker_access, "run", run)
    monkeypatch.setattr(mod.docker_access, "problem", lambda: None)
    monkeypatch.setattr(mod, "port_in_use", lambda port: port_busy[0])
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return st, calls


def test_staging_stops_an_earlier_pipeline_staging_that_holds_the_port(tmp_path, monkeypatch):
    old = ("shopease-staging-api-1", "shopease-staging", "", "/x/runs/old/infra/c.yml,/x/runs/old/.sdlc/compose.override.yml")
    st, calls = compose_staging(tmp_path, [True], [old], monkeypatch)
    st._free_port()
    assert ["docker", "stop", "abc123"] in calls


def test_staging_refuses_a_port_held_by_something_else(tmp_path, monkeypatch):
    other = ("postgres-admin", "", "", "")
    st, calls = compose_staging(tmp_path, [True], [other], monkeypatch)
    with pytest.raises(StagingError) as e:
        st._free_port()
    assert e.value.environment and "postgres-admin" in str(e.value) and "staging_port" in str(e.value)
    assert not any(c[:2] == ["docker", "stop"] for c in calls)


def test_compose_failures_from_the_machine_are_environment_errors(tmp_path, monkeypatch):
    st, _ = compose_staging(tmp_path, [False], [], monkeypatch)
    (st.ws.root / "infra").mkdir(exist_ok=True)
    for f in (st.rel.compose_file, st.rel.env_file):
        st.ws.write_text(f, "x")
    monkeypatch.setattr(st, "_compose", lambda *a: subprocess.CompletedProcess(a, 1, "",
                        "Bind for 0.0.0.0:3999 failed: port is already allocated"))
    with pytest.raises(StagingError) as e:
        st._start_compose()
    assert e.value.environment
    monkeypatch.setattr(st, "_compose", lambda *a: subprocess.CompletedProcess(a, 1, "", "npm ERR! build failed"))
    with pytest.raises(StagingError) as e:
        st._start_compose()
    assert not e.value.environment


def test_staging_containers_are_labelled_for_the_pipeline(tmp_path, monkeypatch):
    st, _ = compose_staging(tmp_path, [False], [], monkeypatch)
    from agentic_sdlc.release import staging as mod
    monkeypatch.setattr(mod.docker_access, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    st._compose("ps")
    assert "agentic-sdlc.staging:" in (st.ws.root / ".sdlc/compose.override.yml").read_text()


def test_staging_start_failure_is_handed_to_developer_once(tmp_path, prd, backlog):
    worker = ScriptedWorker({"integration_review": [passing_integration()]})
    staging = FakeStaging(fail_start=[True])
    r, s, _ = releaser(tmp_path, prd, backlog, worker, staging=staging)
    r.verify()
    assert s.release.verified
    fix = [j for j in worker.jobs if j.task_key == "fix_work_item"][0]
    assert "missing env JWT_SECRET" in fix.inputs["problems"]


def test_production_packages_and_tags(tmp_path, prd, backlog):
    r, s, ws = releaser(tmp_path, prd, backlog, ScriptedWorker())
    r.production()
    assert s.release.production == "packaged"
    assert "release-" in s.release.production_notes
    from git import Repo
    assert any(t.name.startswith("release-") for t in Repo(ws.root).tags)
    assert "## Included" in (ws.root / "reports" / "release_notes.md").read_text()


def test_production_command_runs_and_failure_stops(tmp_path, prd, backlog):
    r, s, ws = releaser(tmp_path, prd, backlog, ScriptedWorker(), cfg={"production_command": "sh -c 'echo deployed; exit 3'"})
    r.production()
    assert s.release.production == "failed" and s.status == "stopped"
    assert "deployed" in (ws.root / "reports" / "production_deploy.log").read_text()


def test_contract_diff_ignores_infrastructure_endpoints():
    served = {"paths": {"/api/v1/products": {"get": {}}, "/api/v1/products/{id}": {"get": {}}, "/api/v1/cart": {"post": {}},
                        "/api/v1/health": {"get": {}}}}
    assert contract_diff(CONTRACT, json.dumps(served), "/api/v1") == ["Not in the contract: GET /health"]
    assert contract_diff(CONTRACT, json.dumps(served), "/api/v1", ignore_paths=["/api/v1/health", "/api/docs-json"]) == []


def test_staging_that_cannot_start_is_not_recorded_as_passed(tmp_path, prd, backlog):
    from git import Repo
    staging = FakeStaging(fail_start=[True, True])
    r, s, ws = releaser(tmp_path, prd, backlog, ScriptedWorker(), staging=staging, cfg={"fix_rounds": 1})
    s.release.rounds = 1  # an earlier session used round 1
    r.verify()           # round 2 fails -> developer; round 3 fails again -> stop
    assert s.status == "stopped" and not s.release.verified
    assert Repo(ws.root).head.commit.message.startswith("Release verification round 3: staging did not start")
    assert "Staging could not start" in (ws.root / "reports" / "release_round3.md").read_text()


def test_contract_diff_with_prefix_in_servers_and_paths():
    # NestJS: servers ['/api/v1'] and paths '/api/v1/...' at the same time.
    served = {"servers": [{"url": "/api/v1"}], "paths": {
        "/api/v1/products": {"get": {}}, "/api/v1/products/{id}": {"get": {}}, "/api/v1/cart": {"post": {}}, "/api/v1": {"get": {}}}}
    assert contract_diff(CONTRACT, json.dumps(served), "/api/v1") == ["Not in the contract: GET /"]
