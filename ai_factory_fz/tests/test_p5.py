"""P5: the Acceptor's evidence, the manifest, and `uv run deploy`."""

import hashlib

import pytest

from agentic_sdlc.artifacts.tests import AcceptanceSuite, AcceptanceTest
from agentic_sdlc.gates import files as gate_files
from agentic_sdlc.release import deploy as deploying
from agentic_sdlc.release.acceptance import Acceptor, verify_manifest
from agentic_sdlc.state import GateDecision
from agentic_sdlc.tools.sandbox_exec import SandboxResult
from test_release import ScriptedWorker, releaser

TEST_FILE = "server/test/acceptance/ac-01.spec.ts"


def acceptor(tmp_path, prd, backlog, output, exit_code=0, tamper=False):
    r, s, ws = releaser(tmp_path, prd, backlog, ScriptedWorker())
    ws.write_text(TEST_FILE, "test('AC-01 lists products')")
    s.build.locked_tests = {TEST_FILE: hashlib.sha256(b"test('AC-01 lists products')").hexdigest()}
    if tamper:
        ws.write_text(TEST_FILE, "test('AC-01 always passes')")
    s.build.acceptance = {"M1/backend": AcceptanceSuite(tests=[
        AcceptanceTest(ac_id="AC-01", file=TEST_FILE, test_name="AC-01 lists products")])}
    profile = r.profile
    from agentic_sdlc.registry.profiles import AcceptanceTests
    profile.components["backend"].acceptance = AcceptanceTests(dir="server/test/acceptance", command="npm run test:acceptance")
    return Acceptor(s, ws, profile, lambda comp, acc: SandboxResult(exit_code=exit_code, output=output)), s, ws


def test_a_criterion_is_met_only_with_a_passing_run_that_names_it(tmp_path, prd, backlog):
    a, s, ws = acceptor(tmp_path, prd, backlog, "  ✓ AC-01 lists products (12 ms)\n")
    a.accept()
    assert s.release.acceptance_met == ["AC-01"]
    assert "AC-02" in s.release.acceptance_unmet                      # no test mapped to it
    assert "AC-01 lists products" in (ws.root / "evidence/AC-01/result.md").read_text()
    assert (ws.root / "evidence/AC-01/tests/ac-01.spec.ts").is_file()
    report = (ws.root / "docs/acceptance.md").read_text()
    assert "| AC-01 | US-001 | MET |" in report and "no acceptance test is mapped" in report
    assert "- [ ] I tried it and it is right" in (ws.root / "docs/uat-guide.md").read_text()


def test_a_failing_suite_or_a_run_that_never_shows_the_file_proves_nothing(tmp_path, prd, backlog):
    a, s, _ = acceptor(tmp_path, prd, backlog, "  ✕ AC-01 lists products\n", exit_code=1)
    a.accept()
    assert "AC-01" in s.release.acceptance_unmet
    a, s, _ = acceptor(tmp_path / "b", prd, backlog, "Tests: 3 passed\n")
    a.accept()
    assert "AC-01" in s.release.acceptance_unmet


def test_runners_that_print_files_not_test_names_still_prove_the_criterion(tmp_path, prd, backlog):
    """vitest's default output: ' ✓ test/acceptance/ac-01.spec.ts (3 tests)'; no per-test names."""
    a, s, ws = acceptor(tmp_path, prd, backlog, " ✓ test/acceptance/ac-01.spec.ts (3 tests) 231ms\n")
    a.accept()
    assert s.release.acceptance_met == ["AC-01"]
    assert "ac-01.spec.ts" in (ws.root / "evidence/AC-01/result.md").read_text()


def test_a_skipped_or_undeclared_test_is_not_proof(tmp_path, prd, backlog):
    a, s, ws = acceptor(tmp_path, prd, backlog, " ✓ test/acceptance/ac-01.spec.ts (3 tests)\n")
    code = "it.skip('AC-01 lists products', () => {})"
    ws.write_text(TEST_FILE, code)
    s.build.locked_tests = {TEST_FILE: hashlib.sha256(code.encode()).hexdigest()}
    a.accept()
    assert "AC-01" in s.release.acceptance_unmet
    assert "no test declaring AC-01" in (ws.root / "evidence/AC-01/result.md").read_text()
    assert Acceptor._declared("testWidgets('AC-03 shows total', (t) async {})", "AC-03")
    assert not Acceptor._declared("test('AC-04 x', () {}, skip: true);", "AC-04")


def test_a_changed_locked_test_is_never_proof(tmp_path, prd, backlog):
    a, s, ws = acceptor(tmp_path, prd, backlog, "  ✓ AC-01 lists products\n", tamper=True)
    a.accept()
    assert "AC-01" in s.release.acceptance_unmet
    assert "missing or was changed" in (ws.root / "evidence/AC-01/result.md").read_text()


def test_the_manifest_detects_any_change_after_signing(tmp_path, prd, backlog):
    a, s, ws = acceptor(tmp_path, prd, backlog, "  ✓ AC-01 lists products\n")
    a.accept()
    assert verify_manifest(ws.root) == [] and s.release.evidence_manifest
    manifest = (ws.root / "evidence-manifest.sha256").read_text()
    assert "evidence/AC-01/result.md" in manifest and "evidence/_factory.json" in manifest and "docs/acceptance.md" in manifest
    (ws.root / "evidence/AC-01/result.md").write_text("# AC-01: MET (edited)")
    assert verify_manifest(ws.root) == ["evidence/AC-01/result.md"]
    assert verify_manifest(tmp_path) == ["evidence-manifest.sha256 (missing)"]


# ---------- deploy ----------

def ready(tmp_path, prd, backlog):
    a, s, ws = acceptor(tmp_path, prd, backlog, "  ✓ AC-01 lists products\n")
    a.accept()
    ws.write_text("docs/release.md", "# plan")
    for gate, who in (("release", "Pat Owner"), ("production", "Dana Approver")):
        docs = ["docs/acceptance.md"] if gate == "release" else ["docs/release.md"]
        s.gate_history.append(GateDecision(gate=gate, gate_id=gate_files.gate_id(gate), approved=True, decided_by="human",
                                           approver=who, artifact_hashes=gate_files.file_hashes(ws.root, docs)))
    s.release.production = "ready"
    return s, ws


def test_deploy_goes_ahead_only_when_everything_signed_is_intact(tmp_path, prd, backlog):
    s, ws = ready(tmp_path, prd, backlog)
    assert deploying.problems(s, ws.root, "Dana Approver", "./deploy.sh") == []
    assert deploying.problems(s, ws.root, "dana approver", "./deploy.sh") == []                # case does not matter


def test_deploy_refuses_a_different_person_a_missing_command_or_an_unready_run(tmp_path, prd, backlog):
    s, ws = ready(tmp_path, prd, backlog)
    assert "only that person deploys" in deploying.problems(s, ws.root, "Someone Else", "./deploy.sh")[0]
    assert "No production command" in deploying.problems(s, ws.root, "Dana Approver", " ")[0]
    s.release.production = "packaged"
    assert "not ready to deploy" in deploying.problems(s, ws.root, "Dana Approver", "./d.sh")[0]
    s.release.production = "deployed"
    assert deploying.problems(s, ws.root, "Dana Approver", "./d.sh") == ["This release is already deployed."]


def test_deploy_refuses_after_a_document_or_the_evidence_changed(tmp_path, prd, backlog):
    s, ws = ready(tmp_path, prd, backlog)
    ws.write_text("docs/release.md", "# plan, edited after G7")
    found = " ".join(deploying.problems(s, ws.root, "Dana Approver", "./d.sh"))
    assert "G7 (go-live) was approved, but these documents changed afterwards: docs/release.md" in found
    s, ws = ready(tmp_path / "x", prd, backlog)
    (ws.root / "evidence/AC-01/result.md").write_text("tampered")
    assert "no longer matches evidence-manifest.sha256" in " ".join(deploying.problems(s, ws.root, "Dana Approver", "./d.sh"))


def test_deploy_needs_both_gates(tmp_path, prd, backlog):
    s, ws = ready(tmp_path, prd, backlog)
    s.gate_history = [g for g in s.gate_history if g.gate != "production"]
    assert "G7 (go-live) is not approved." in deploying.problems(s, ws.root, "Dana Approver", "./d.sh")


def test_the_deploy_command_refuses_agents_and_non_terminals(tmp_path, monkeypatch):
    from agentic_sdlc import main

    with pytest.raises(SystemExit, match="interactive terminal"):
        main.deploy(["r1", "--as", "Dana Approver"], is_tty=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(SystemExit, match="agent session"):
        main.deploy(["r1", "--as", "Dana Approver"], is_tty=True)
    monkeypatch.delenv("CLAUDECODE")
    with pytest.raises(SystemExit):
        main.deploy(["r1", "--as", "Claude"], is_tty=True)           # agent-like names are not people


def test_deploy_runs_the_command_after_the_typed_confirmation_and_records_it(tmp_path, prd, backlog, monkeypatch):
    from agentic_sdlc import main
    from agentic_sdlc.workspace import Workspace

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("SDLC_AGENT", raising=False)
    s, ws = ready(tmp_path, prd, backlog)
    ws.save_state(s)
    monkeypatch.setattr(Workspace, "open", classmethod(lambda cls, run_id, runs_dir=None: ws))
    monkeypatch.setattr("agentic_sdlc.settings.load_config",
                        lambda name, config_dir=None: {"release": {"production_command": "sh -c 'echo shipped'"}})
    with pytest.raises(SystemExit, match="Cancelled"):
        main.deploy([s.run_id or "r", "--as", "Dana Approver"], input_fn=lambda _p: "no", is_tty=True)
    assert not (ws.root / "reports/production_deploy.log").exists()
    main.deploy([s.run_id or "r", "--as", "Dana Approver"], input_fn=lambda _p: "DEPLOY", is_tty=True)
    assert "shipped" in (ws.root / "reports/production_deploy.log").read_text()
    assert '"production": "deployed"' in ws.load_state_json()
