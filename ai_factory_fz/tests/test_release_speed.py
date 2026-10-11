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


# ---------- network hiccups are retried, not handed to a developer ----------

def test_transient_failures_are_told_apart_from_code_failures():
    from agentic_sdlc.build.services import transient_failure

    assert "resolve host" in transient_failure("FAILURE\n> Could not resolve host: dl.google.com\n")
    assert transient_failure("Could not GET 'https://repo.maven.org/x'. Read timed out") is not None
    assert transient_failure("lib/main.dart:3: Error: Undefined name 'foo'") is None


class FlakyBuildEmulator(FakeEmulator):
    def __init__(self, outputs):
        super().__init__()
        self.outputs = list(outputs)

    def run(self, command, workdir, timeout_s=None):
        if command.startswith("flutter build"):
            ok, text = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
            return SandboxResult(exit_code=0 if ok else 1, output=text)
        return super().run(command, workdir, timeout_s)


def test_a_download_hiccup_in_the_app_build_is_retried_without_a_developer(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    em = FlakyBuildEmulator([(False, "Could not resolve host: dl.google.com"), (True, "built")])
    r, s, _ = _device_round(tmp_path, prd, backlog, em)
    assert r._device_round() == [] and em.events.count("build") == 2
    assert s.release.device_passed is True


def test_a_build_that_keeps_failing_on_the_network_is_a_machine_problem_not_an_app_bug(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    em = FlakyBuildEmulator([(False, "Could not resolve host: dl.google.com")])
    r, s, _ = _device_round(tmp_path, prd, backlog, em)
    assert r._device_round() == []                                   # nothing for the frontend developer
    assert em.events.count("build") == 3 and "machine problem" in s.release.device_note


# ---------- the test driver could not attach: a machine problem, not an app bug ----------

HARNESS = "00:00 +0 -1: loading /workspace/app/integration_test/smoke_test.dart [E]\n  Failed to load \"x\": Unable to start the app on the device.\n"


def test_harness_failures_are_told_apart_from_failing_journeys():
    from agentic_sdlc.release.device import harness_failure

    assert "Unable to start the app on the device" in harness_failure(HARNESS)
    assert harness_failure("00:12 +3 -1: cart_test.dart: add to cart [E]\n  Expected: 1 Actual: 0") is None


class DriverEmulator(TwoStageEmulator):
    pass


def test_a_driver_that_attaches_after_a_retry_passes(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    em = DriverEmulator([(False, HARNESS), (True, "All tests passed")])
    r, s, _ = _device_round(tmp_path, prd, backlog, em)
    assert r._device_round() == [] and s.release.device_passed is True and em.stopped == 0


def test_a_driver_that_never_attaches_is_a_machine_problem_and_not_sent_to_the_developer(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    em = DriverEmulator([(False, HARNESS)] * 3)
    r, s, _ = _device_round(tmp_path, prd, backlog, em)
    assert r._device_round() == []                                   # nothing for the frontend developer
    assert s.release.device_passed is None and "machine/emulator problem, not the app" in s.release.device_note
    assert em.stopped >= 1 and em.started >= 2                       # the second retry used a restarted emulator


def test_a_real_journey_failure_still_goes_to_the_developer(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    em = DriverEmulator([(False, "00:12 +3 -1: /workspace/app/integration_test/cart_test.dart: add to cart [E]")])
    r, s, _ = _device_round(tmp_path, prd, backlog, em)
    problems = r._device_round()
    assert problems and s.release.device_passed is False


# ---------- a suite that mostly fails on its first run is the tests' problem ----------

def test_test_counts_are_read_from_flutter_and_vitest_summaries():
    from agentic_sdlc.release.device import suspect_suite, test_counts

    assert test_counts("05:40 +3 -11: Some tests failed.") == (3, 11)
    assert test_counts("00:09 +8: All tests passed!") == (8, 0)
    assert test_counts(" Tests  2 failed | 5 passed (7)") == (5, 2)
    assert test_counts("no summary") is None
    assert suspect_suite("05:40 +3 -11: Some tests failed.", ever_passed=False)
    assert not suspect_suite("05:40 +3 -11: Some tests failed.", ever_passed=True)       # trusted suite: a real regression
    assert not suspect_suite("00:09 +8 -1: Some tests failed.", ever_passed=False)       # one failure: probably the app
    assert not suspect_suite("00:09 +0 -2: Some tests failed.", ever_passed=False)       # too few tests to judge


def test_a_mostly_failing_first_device_run_goes_back_to_the_test_writer_not_the_developer(tmp_path, prd, backlog, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.release.releaser.time.sleep", lambda s: None)
    bad = (False, "05:40 +3 -11: ShopEase journeys [E]\n05:41 +3 -11: Some tests failed.")
    em = TwoStageEmulator([bad, bad])
    r, s, _ = _device_round(tmp_path, prd, backlog, em)
    problems = r._device_round()
    assert problems and problems[0][0] == "device-suite" and "TESTS are suspect" in problems[0][1]
    s.release.device_passed_once = True                       # once trusted, the same output is a regression for the app
    s.release.device_failed_files = []
    problems = r._device_round()
    assert problems and problems[0][0] == "frontend"


def test_rewriting_a_suspect_device_suite_deletes_it_and_hands_the_writer_the_failures(tmp_path, prd, backlog):
    from test_release import ScriptedWorker, releaser
    from test_device import device_releaser

    worker = ScriptedWorker()
    r, s, ws = device_releaser(tmp_path, prd, backlog, worker, FakeEmulator())
    ws.write_text("app/integration_test/old_test.dart", "testWidgets('x', (t) async {});")
    s.release.device_suite = WorkItemResult(summary="old", checks_passed=True)
    r._rewrite_suite("device-suite", "05:40 +3 -11: failed")
    assert not (ws.root / "app/integration_test/old_test.dart").exists() and s.release.suite_rewrites == 1
    job = [j for j in worker.jobs if j.task_key == "write_device_tests"][0]
    assert "PREVIOUS VERSION OF THIS SUITE WAS REJECTED" in job.inputs["previous_failures"] and "FACTS" in job.inputs["facts"]
