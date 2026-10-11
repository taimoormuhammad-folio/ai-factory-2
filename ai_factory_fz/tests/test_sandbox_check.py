"""The real-site sandbox check: read-only, order submission forced off, never blocks when it cannot run."""

import yaml

from agentic_sdlc.registry.profiles import Profile, SandboxCheckConfig, SandboxRequest
from agentic_sdlc.release.sandbox_check import OVERRIDE, SandboxCheck

CONTRACT = {"servers": [{"url": "http://localhost:3000/api/v1"}], "paths": {
    "/products": {"get": {}}, "/products/{id}": {"get": {}}, "/cart": {"get": {}, "post": {}},
    "/search": {"get": {"parameters": [{"in": "query", "name": "q", "required": True}]}}}}


def project(tmp_path, env=True):
    (tmp_path / "infra").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "infra/docker-compose.sandbox.yml").write_text("services: {api: {}}")
    (tmp_path / "docs/api-contract.yaml").write_text(yaml.safe_dump(CONTRACT))
    if env:
        (tmp_path / "infra/sandbox.env").write_text("SUITECOMMERCE_BASE_URL=https://x\n")
    return tmp_path


class Recorder:
    def __init__(self, tracked=False, up=0, statuses=None):
        self.calls, self.tracked, self.up, self.statuses = [], tracked, up, statuses or {}

    def run(self, cmd, cwd, timeout=600):
        self.calls.append(cmd)
        if cmd[:2] == ["git", "ls-files"]:
            return (0 if self.tracked else 1), ""
        if "up" in cmd:
            return self.up, "up output"
        return 0, ""

    def get(self, url, timeout=5.0):
        path = url.split("3200", 1)[1]
        return self.statuses.get(path, 200), "body"


def check(root, rec, **kw):
    cfg = SandboxCheckConfig(checks=[SandboxRequest(path="/api/v1/health")], **kw)
    return SandboxCheck(root, cfg, run=rec.run, get=rec.get, sleep=lambda s: None)


def test_it_is_not_run_without_its_git_ignored_env_file_and_never_blocks(tmp_path):
    res = check(project(tmp_path, env=False), Recorder()).check()
    assert not res.ran and res.passed is None and res.problems == [] and "infra/sandbox.env does not exist" in res.note


def test_a_credentials_file_tracked_by_git_is_not_used(tmp_path):
    rec = Recorder(tracked=True)
    res = check(project(tmp_path), rec).check()
    assert not res.ran and "tracked by git" in res.note and not any("up" in c for c in rec.calls)


def test_order_submission_is_forced_off_and_containers_always_removed(tmp_path):
    rec = Recorder()
    root = project(tmp_path)
    seen = {}
    real_run = rec.run

    def run(cmd, cwd, timeout=600):
        if "up" in cmd:
            seen["override"] = (root / OVERRIDE).read_text()
        return real_run(cmd, cwd, timeout)

    rec.run = run
    res = check(root, rec).check()
    assert res.ran and res.passed and 'CHECKOUT_SUBMIT_ENABLED: "false"' in seen["override"]
    assert any("down" in c for c in rec.calls) and not (root / OVERRIDE).exists()


def test_contract_reads_without_parameters_are_checked_and_5xx_fails(tmp_path):
    rec = Recorder(statuses={"/api/v1/products": 502, "/api/v1/cart": 401})
    res = check(project(tmp_path), rec).check()
    assert not res.passed and len(res.problems) == 1 and "GET /api/v1/products returned 502" in res.problems[0]
    assert "GET /api/v1/cart -> 401 (ok)" in res.output                          # reached and handled
    assert "/api/v1/products/{id}" not in res.output and "/api/v1/search" not in res.output   # needs parameters


def test_a_stack_that_does_not_start_fails_with_the_output(tmp_path):
    res = check(project(tmp_path), Recorder(up=1)).check()
    assert res.ran and res.passed is False and "did not start" in res.problems[0]


def test_only_the_netsuite_profile_asks_for_it():
    assert Profile.load("flutter_nestjs_netsuite").release.sandbox_check is not None
    assert Profile.load("flutter_nestjs_ecommerce").release.sandbox_check is None


def test_the_release_round_runs_it_only_when_profile_and_pipeline_both_ask(tmp_path, prd, backlog, monkeypatch):
    from agentic_sdlc.release import sandbox_check
    from test_release import ScriptedWorker, releaser

    r, s, ws = releaser(tmp_path, prd, backlog, ScriptedWorker())
    r.profile.release.sandbox_check = SandboxCheckConfig()
    assert r._sandbox_round() == [] and s.release.sandbox_passed is None            # pipeline did not ask

    r.cfg["sandbox_check"] = True
    monkeypatch.setattr(sandbox_check.SandboxCheck, "check", lambda self: sandbox_check.SandboxResult(
        ran=True, passed=False, problems=["GET /x returned 502"], output="GET /x -> 502", note="sandbox check failed"))
    problems = r._sandbox_round()
    assert problems == [("backend", "GET /x returned 502")] and s.release.sandbox_passed is False
    assert (ws.root / "reports" / "sandbox_round0.txt").read_text() == "GET /x -> 502"
    monkeypatch.setattr(sandbox_check.SandboxCheck, "check", lambda self: sandbox_check.SandboxResult(
        ran=False, note="sandbox check not run: no env"))
    assert r._sandbox_round() == [] and s.release.sandbox_passed is None and "not run" in s.release.sandbox_note
    assert "Real-site sandbox: sandbox check not run" in r.gate_summary()
