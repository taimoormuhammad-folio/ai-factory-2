"""Faster release rounds: the device only runs against a working API, an unchanged app keeps its APK, and the
tests that failed last round run first."""

from agentic_sdlc.artifacts.reports import Bug, QAReport, WorkItemResult
from agentic_sdlc.release.device import failed_test_files, subset_test_command
from agentic_sdlc.tools.sandbox_exec import SandboxResult
from test_device import FakeEmulator, device_releaser
from test_release import ScriptedWorker

OUTPUT = """01:17 +3 -2: /workspace/app/integration_test/cart_test.dart: add to cart shows the line [E]
  Expected: at least one matching candidate
01:18 +3 -3: /workspace/app/integration_test/checkout_test.dart: place order [E]
01:18 +3 -3: /workspace/app/integration_test/cart_test.dart: remove a line [E]
01:18 +3 -3: Some tests failed.
"""
CMD = "flutter test integration_test -d emulator-5554 --dart-define=API_BASE_URL=http://10.0.2.2:3999/api/v1"
OK = QAReport(milestone_id="integration", passed=True, summary="ok")


def test_failing_test_files_are_read_from_the_flutter_output():
    assert failed_test_files(OUTPUT, "app", "integration_test") == ["integration_test/cart_test.dart",
                                                                    "integration_test/checkout_test.dart"]
    assert failed_test_files("All tests passed!", "app", "integration_test") == []


def test_the_failing_files_replace_the_test_folder_in_the_command():
    assert subset_test_command(CMD, "integration_test", ["integration_test/cart_test.dart"]) == \
        "flutter test integration_test/cart_test.dart -d emulator-5554 --dart-define=API_BASE_URL=http://10.0.2.2:3999/api/v1"
    assert subset_test_command("flutter test --dart-define=A=integration_test", "integration_test", ["x.dart"]) is None
    assert subset_test_command(CMD, "integration_test", []) is None


def test_the_device_waits_for_a_working_api(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    bad = QAReport(milestone_id="integration", passed=False, summary="x", bugs=[Bug(
        id="B1", work_item_id="WI-001", title="cart total wrong", severity="major", steps="s", expected="e", actual="a")])
    worker = ScriptedWorker({"integration_review": [bad, OK]})
    em = FakeEmulator()
    r, s, _ = device_releaser(tmp_path, prd, backlog, worker, em)
    r.verify()
    assert s.release.rounds == 2 and s.release.verified
    # Round 1 had an API problem: no APK build, no emulator. Round 2 (API fixed) ran the device checks once.
    assert em.events.count("build") == 1 and em.started == 1
    assert "skipped this round" not in s.release.device_note                # cleared by the successful round


def test_a_round_with_api_problems_says_why_the_device_did_not_run(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    bad = QAReport(milestone_id="integration", passed=False, summary="x", bugs=[Bug(
        id="B1", work_item_id="WI-001", title="cart total wrong", severity="major", steps="s", expected="e", actual="a")])
    worker = ScriptedWorker({"integration_review": [bad, bad]})
    em = FakeEmulator()
    r, s, ws = device_releaser(tmp_path, prd, backlog, worker, em)
    r.verify()
    assert s.release.failed and "build" not in em.events and em.started == 0   # never built, never booted
    assert "fix the API problems first" in (ws.root / "reports" / "release_round1.md").read_text()


def test_the_device_can_be_forced_to_run_every_round(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    bad = QAReport(milestone_id="integration", passed=False, summary="x", bugs=[Bug(
        id="B1", work_item_id="WI-001", title="t", severity="major", steps="s", expected="e", actual="a")])
    worker = ScriptedWorker({"integration_review": [bad, bad]})
    em = FakeEmulator()
    r, s, _ = device_releaser(tmp_path, prd, backlog, worker, em)
    r.cfg["device"]["only_when_api_green"] = False
    r.verify()
    assert em.events.count("build") >= 1


class TwoStageEmulator(FakeEmulator):
    """Device tests fail with named files in the first full run, then behave as scripted."""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def run(self, command, workdir, timeout_s=None):
        if command.startswith("flutter build"):
            return super().run(command, workdir, timeout_s)
        self.commands.append(command)
        ok, text = self.results.pop(0)
        return SandboxResult(exit_code=0 if ok else 1, output=text)


def _device_round(tmp_path, prd, backlog, em):
    worker = ScriptedWorker({"integration_review": [OK]})
    r, s, ws = device_releaser(tmp_path, prd, backlog, worker, em)
    s.release.device_suite = WorkItemResult(summary="suite", checks_passed=True)
    s.release.rounds = 1
    return r, s, ws


def test_last_rounds_failing_tests_run_first_and_stop_a_hopeless_round_early(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    em = TwoStageEmulator([(False, OUTPUT),                          # round 1, full suite
                           (False, "01:20 +0 -1: /workspace/app/integration_test/cart_test.dart: add [E]"),   # round 2, subset
                           (True, "subset ok"), (True, "full ok")])   # round 3: subset, then the whole suite
    r, s, _ = _device_round(tmp_path, prd, backlog, em)
    r._device_round()
    assert s.release.device_failed_files == ["integration_test/cart_test.dart", "integration_test/checkout_test.dart"]
    problems = r._device_round()                                                         # round 2
    tests = lambda: [c for c in em.commands if c.startswith("flutter test")]
    assert problems and "re-run of the previously failing tests only" in problems[0][1]
    assert len(tests()) == 2 and tests()[1].startswith(
        "flutter test integration_test/cart_test.dart integration_test/checkout_test.dart -d")   # the full suite did not run
    assert s.release.device_failed_files == ["integration_test/cart_test.dart"]         # narrowed to what still fails
    problems = r._device_round()                                                         # round 3: subset passes, full runs
    assert problems == [] and s.release.device_passed is True and s.release.device_failed_files == []
    assert tests()[-2].startswith("flutter test integration_test/cart_test.dart -d") and tests()[-1] == CMD


def test_an_unchanged_app_reuses_its_apk(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    em = TwoStageEmulator([(True, "ok")] * 4)
    r, s, ws = _device_round(tmp_path, prd, backlog, em)
    ws.write_text("app/lib/main.dart", "void main() {}\n")
    ws.commit("app")
    apk = ws.root / "app/build/app/outputs/flutter-apk/app-debug.apk"
    apk.parent.mkdir(parents=True, exist_ok=True)
    apk.write_bytes(b"apk")
    r._device_round()
    assert em.events.count("build") == 1 and s.release.apk_key
    r._device_round()                                              # nothing changed: no second build
    assert em.events.count("build") == 1
    ws.write_text("app/lib/main.dart", "void main() { print(1); }\n")
    r._device_round()                                              # uncommitted change: build again
    assert em.events.count("build") == 2
    ws.commit("fix")
    r._device_round()                                              # committed change: new tree, build again
    assert em.events.count("build") == 3
