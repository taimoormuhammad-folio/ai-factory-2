"""Release phase: staging deployment, integration pass, smoke test, then production.

verify():
  1. Deployment engineer writes the Dockerfile, staging compose file, staging env (test values)
     and CI workflow.                                                    (once)
  2. Smoke tester writes the smoke suite for the journeys that are built. (once)
  3. Round: start staging -> contract check (served OpenAPI vs docs/api-contract.yaml) ->
     Integration pass reviews wiring/config and reports bugs -> smoke suite runs against
     staging -> stop staging. Problems go to the developer of their component, then the next
     round. Rounds are limited; if problems remain, the release gate (G6) shows them and the human decides.
     Machine problems (port taken, Docker unusable) stop the run: no developer can fix those.
production(): package the release (build, notes, git tag) and, if a production command is
  configured, run it.
"""

import logging
import os
import shlex
import subprocess
import time
from datetime import datetime
from typing import Any, Callable

from agentic_sdlc.artifacts.architecture import CONTRACT_PATH
from agentic_sdlc.artifacts.reports import QAReport, WorkItemResult
from agentic_sdlc.build.coders import Job, Worker
from agentic_sdlc.build import facts
from agentic_sdlc.build.services import transient_failure
from agentic_sdlc.crews.base import PhaseError, TaskResult, UsageLimitError
from agentic_sdlc.guardrails import agents as agent_guardrails
from agentic_sdlc.guardrails import code as code_guardrails
from agentic_sdlc.registry.profiles import Profile
from agentic_sdlc.release.device import (DeviceError, Emulator, failed_test_files, harness_failure, subset_test_command,
                                         suspect_suite)
from agentic_sdlc.release.contract import contract_diff
from agentic_sdlc.release.staging import Staging, StagingError, http_get
from agentic_sdlc.state import ProjectState
from agentic_sdlc.tools.sandbox_exec import SandboxRunner
from agentic_sdlc.workspace import Workspace

log = logging.getLogger(__name__)

SMOKE_SUITE, DEVICE_SUITE = "smoke-suite", "device-suite"   # pseudo-components: problems for the suite's writer
PHASE = "release"


class Releaser:
    def __init__(
        self,
        state: ProjectState,
        workspace: Workspace,
        profile: Profile,
        sandbox: SandboxRunner,
        worker_for: Callable[[str], Worker],
        staging: Staging,
        config: dict[str, Any],
        record: Callable[[TaskResult], Any],
        checkpoint: Callable[[str], None],
        can_continue: Callable[[], bool],
        stop: Callable[[str], None],
        emulator: Emulator | None = None,
        guard_rules: set[str] | None = None,
    ):
        self.emulator = emulator
        self.guard_rules = guard_rules or set()
        self.s = state
        self.r = state.release
        self.ws = workspace
        self.profile = profile
        self.api = profile.components[profile.release.api_component]
        self.sandbox = sandbox
        self.worker_for = worker_for
        self.staging = staging
        self.cfg = config
        self.record = record
        self.checkpoint = checkpoint
        self.can_continue = can_continue
        self.stop = stop

    # ---------- helpers ----------

    @property
    def api_base_url(self) -> str:
        """Base URL clients use, including the API prefix, e.g. http://localhost:3100/api/v1."""
        return self.staging.base_url + self.profile.release.api_prefix

    def built_summary(self) -> str:
        items = {w.id: w for w in self.s.backlog.work_items}
        lines = [f"- {wid} [{items[wid].component}] {items[wid].title}" for wid, p in self.s.build.items.items()
                 if p.status == "done" and wid in items]
        return "\n".join(lines) or "(nothing built)"

    def not_built_summary(self) -> str:
        items = {w.id: w for w in self.s.backlog.work_items}
        lines = [f"- {wid} {items[wid].title}: {p.status} ({p.reason[:120]})" for wid, p in self.s.build.items.items()
                 if p.status != "done" and wid in items]
        return "\n".join(lines) or "(none)"

    def _job(self, agent: str, task_key: str, inputs: dict[str, Any], model, workdir: str,
             runtime: str | None = "api", feedback: str = "") -> Any:
        rt = self.api.runtime if runtime == "api" else runtime
        job = Job(PHASE, agent, task_key, {**self._common(), **inputs}, model, workdir, rt, feedback=feedback)
        try:
            return self.record(self.worker_for(agent).run(job))
        except UsageLimitError as e:
            self.stop(f"{e}. Resume the run after the limit resets (uv run resume <run_id>).")
            return None
        except PhaseError as e:
            log.error("%s", e)
            return None

    def _common(self) -> dict[str, Any]:
        rel = self.profile.release
        return {
            "docs_dir": str(self.ws.root / "docs"),
            "built": self.built_summary(),
            "not_built": self.not_built_summary(),
            "api_dir": self.api.workdir,
            "health_path": rel.health_path,
            "openapi_json_path": rel.openapi_json_path or "(not required)",
            "compose_file": rel.compose_file,
            "env_file": rel.env_file,
            "smoke_command": rel.smoke_command,
            "port": self.staging.port,
            "api_base_example": f"http://localhost:{self.staging.port}{self.profile.release.api_prefix}",
            "toolchain_image": self.profile.sandbox.runtimes[self.api.runtime].image if self.api.runtime else "(none)",
            **self._data_store_rules(),
            "staging_notes": rel.staging_notes.strip(),
        }

    def _data_store_rules(self) -> dict[str, str]:
        if self.profile.database:
            return {
                "start_rule": ("It must run database migrations on start (before the server) and listen on the PORT "
                               "environment variable."),
                "compose_services": " plus PostgreSQL 16",
                "env_rule": " DATABASE_URL must point at the compose database.",
                "data_check": "database migrations, ",
            }
        return {"start_rule": "It listens on the PORT environment variable (the API has no database).",
                "compose_services": "", "env_rule": "", "data_check": ""}

    # ---------- guardrails ----------

    def _guarded(self, agent: str, task_key: str, inputs: dict[str, Any], model, workdir: str,
                 runtime: str | None, check: Callable[[Any], list[str]], what: str) -> Any:
        """Run a job; if its guardrail check fails, re-run it once with the reasons. If it still
        fails, discard its uncommitted changes and stop the run. Returns the result or None."""
        if task_key in ("write_smoke_tests", "write_device_tests"):
            prev = inputs.get("previous_failures") or ""
            inputs = {**inputs, "facts": facts.build(self.ws.root, self.profile, self.s.wbs),
                      "previous_failures": ("YOUR PREVIOUS VERSION OF THIS SUITE WAS REJECTED: most journeys failed on its first "
                                            "run, which means the tests were wrong (keys, ids, waits), not the app. Rewrite "
                                            "it from the FACTS below. Failures:\n" + prev) if prev else ""}
        result = self._job(agent, task_key, inputs, model, workdir, runtime)
        if result is None or result.blocked:
            return result
        problems = check(result)
        if problems:
            result = self._job(agent, task_key, inputs, model, workdir, runtime, feedback="\n".join(problems))
            problems = check(result) if result is not None and not result.blocked else problems
        if problems:
            code_guardrails.discard_changes(self.ws, workdir)
            self.stop(f"{what} was rejected by guardrails:\n- " + "\n- ".join(problems))
            return None
        return result

    def _code_rules(self) -> set[str]:
        return self.guard_rules & set(code_guardrails.CODE_RULES)

    def _deployment_problems(self, _result: Any) -> list[str]:
        problems = agent_guardrails.de1_container(self.ws, self.profile) if "DE1" in self.guard_rules else []
        if "DV2" in self.guard_rules:
            problems += code_guardrails.dv2_secrets(self.ws, self.profile, code_guardrails.changed_files(self.ws))
        return problems

    def _smoke_problems(self, _result: Any) -> list[str]:
        if "ST1" not in self.guard_rules:
            return []
        return agent_guardrails.st1_smoke_suite(self.ws, self.profile, self.cfg.get("min_smoke_tests", 3))

    def _device_suite_problems(self, _result: Any) -> list[str]:
        return agent_guardrails.st1_device_suite(self.ws, self.profile) if "ST1" in self.guard_rules else []

    # ---------- device ----------

    @property
    def device_enabled(self) -> bool:
        return bool(self.profile.device and self.emulator and (self.cfg.get("device") or {}).get("enabled"))

    @property
    def app(self):
        return self.profile.components[self.profile.device.app_component]

    def app_built(self) -> bool:
        items = {w.id: w for w in self.s.backlog.work_items}
        return any(p.status == "done" and wid in items and items[wid].component == self.profile.device.app_component
                   for wid, p in self.s.build.items.items())

    @property
    def device_api_base(self) -> str:
        return f"http://{self.profile.device.host_alias}:{self.staging.port}{self.profile.release.api_prefix}"

    def device_env(self) -> dict[str, str]:
        dev = self.profile.device
        return {k: v.format(host=dev.host_alias, port=self.staging.port) for k, v in dev.asset_env.items()}

    def _device_round(self) -> list[tuple[str, str]]:
        """Build, install and launch the app on the emulator against the running staging; run the
        on-device journey tests. Returns problems for the app component."""
        dev, app, r = self.profile.device, self.app, self.r
        if not self.app_built():
            r.device_note = "no app work items are built yet"
            return []
        reason = self.emulator.tc.not_ready_reason()
        if reason:
            r.device_note = reason
            return []
        fmt = {"api_base": self.device_api_base, "serial": self.emulator.serial}
        # Build before booting: a Gradle build next to a booting emulator starves it (it hangs).
        tree = self._app_tree()
        key = f"{tree}|{self.device_api_base}" if tree else ""
        if key and key == r.apk_key and self.ws.resolve(f"{app.workdir}/{dev.apk_path}").is_file():
            log.info("The app is unchanged since the last build: reusing its APK")
        else:
            build = self.emulator.build(dev.build_command.format(**fmt), app.workdir, timeout_s=dev.build_timeout_s)
            for pause in (30, 90):        # Gradle/pub downloads fail now and then: retry before blaming the app
                if build.ok or not transient_failure(build.output):
                    break
                log.warning("App build hit a network problem (%s); retrying in %ss", transient_failure(build.output), pause)
                time.sleep(pause)
                build = self.emulator.build(dev.build_command.format(**fmt), app.workdir, timeout_s=dev.build_timeout_s)
            if not build.ok and transient_failure(build.output):
                r.apk_key = ""
                r.device_note = f"the app build kept failing on the network (a machine problem, not the app): {transient_failure(build.output)}"
                return []
            if not build.ok:
                r.apk_key = ""
                r.device_passed, r.device_output = False, build.output[-6000:]
                return [(dev.app_component, f"Building the app for the device failed:\n{build.output[-3000:]}")]
            r.apk_key = key
        window = not (self.cfg.get("device") or {}).get("headless", True)
        try:
            self.emulator.start(window=window)
        except DeviceError as e:
            r.device_note = f"emulator did not start (a machine problem, not the app): {str(e)[:500]}"
            return []
        installed = self.emulator.install(dev.apk_path, app.workdir, dev.app_id)
        if not installed.ok and not self.emulator.responsive():
            # The emulator hung or crashed (a machine problem): restart it once and try again.
            log.warning("Emulator stopped responding; restarting it")
            self.emulator.stop()
            try:
                self.emulator.start(window=window)
                installed = self.emulator.install(dev.apk_path, app.workdir, dev.app_id)
            except DeviceError as e:
                installed = None
                log.warning("Emulator restart failed: %s", e)
        if installed is None or (not installed.ok and not self.emulator.responsive()):
            r.device_note = ("the emulator stopped responding (a machine problem, not the app), "
                             "also after a restart; see reports/device/emulator.log")
            return []
        if not installed.ok:
            r.device_passed, r.device_output = False, installed.output[-3000:]
            return [(dev.app_component, f"Installing the app on the emulator failed:\n{installed.output[-2000:]}")]
        self.emulator.launch(dev.app_id)
        time.sleep(8)  # first frame and first API calls
        shot = self.emulator.screenshot(f"reports/device/round{r.rounds}/launch.png")
        if shot:
            r.device_screenshots.append(shot)
        if r.device_suite is None or not self.ws.resolve(f"{app.workdir}/{dev.test_dir}").exists():
            r.device_note = "no on-device test suite yet; only launched the app"
            r.device_passed = True
            return []
        command = dev.test_command.format(**fmt)
        again = subset_test_command(command, dev.test_dir, r.device_failed_files) if r.device_failed_files else None
        if again:        # last round's failures first: a fix that did not work shows in seconds, not after the whole suite
            quick = self.emulator.run(again, app.workdir, timeout_s=dev.build_timeout_s)
            if not quick.ok:
                r.device_passed, r.device_output = False, quick.output[-6000:]
                r.device_failed_files = failed_test_files(quick.output, app.workdir, dev.test_dir) or r.device_failed_files
                return [(dev.app_component, f"On-device journey tests failed again against staging "
                                            f"({self.device_api_base}; re-run of the previously failing tests "
                                            f"only: {', '.join(r.device_failed_files)}):\n{quick.output[-3000:]}")]
        test = self.emulator.run(command, app.workdir, timeout_s=dev.build_timeout_s)
        for restart in (False, True):      # the driver could not attach: try again, then on a fresh emulator
            if test.ok or not harness_failure(test.output):
                break
            log.warning("Device test driver could not start the app (%s); retrying%s", harness_failure(test.output),
                        " on a restarted emulator" if restart else "")
            if restart:
                self.emulator.stop()
                try:
                    self.emulator.start(window=window)
                    if not self.emulator.install(dev.apk_path, app.workdir, dev.app_id).ok:
                        break
                except DeviceError as e:
                    log.warning("Emulator restart failed: %s", e)
                    break
            test = self.emulator.run(command, app.workdir, timeout_s=dev.build_timeout_s)
        if not test.ok and harness_failure(test.output):
            r.device_passed, r.device_output = None, test.output[-6000:]
            r.device_note = (f"the on-device test driver could not attach to the app after retries and an emulator restart "
                             f"(a machine/emulator problem, not the app: it launched, see {shot or 'the launch screenshot'}): "
                             f"{harness_failure(test.output)}")
            return []
        r.device_passed, r.device_output = test.ok, test.output[-6000:]
        r.device_passed_once = r.device_passed_once or test.ok
        r.device_failed_files = [] if test.ok else failed_test_files(test.output, app.workdir, dev.test_dir)
        if not test.ok and r.suite_rewrites < 2 and suspect_suite(test.output, r.device_passed_once):
            return [(DEVICE_SUITE, f"The on-device suite fails most of its journeys on its first run, so the TESTS "
                                   f"are suspect (wrong keys, ids or waits), not the app:\n{test.output[-3000:]}")]
        after = self.emulator.screenshot(f"reports/device/round{r.rounds}/after_tests.png")
        if after:
            r.device_screenshots.append(after)
        if not test.ok:
            return [(dev.app_component, f"On-device journey tests failed against staging "
                                        f"({self.device_api_base}):\n{test.output[-3000:]}")]
        return []

    def _app_tree(self) -> str:
        """Git tree id of the committed app folder, or '' when it has uncommitted changes (no reuse then)."""
        from git import Repo

        folder = self.app.workdir
        try:
            repo = Repo(self.ws.root)
            if repo.git.status("--porcelain", "--", folder).strip():
                return ""
            return repo.git.rev_parse(f"HEAD:{folder}").strip()
        except Exception:        # no commit yet, no such folder: just build
            return ""

    def _rewrite_suite(self, which: str, failures: str) -> None:
        """A suspect suite goes back to its writer with the failures; the app's developer is not involved."""
        self.r.suite_rewrites += 1
        if which == DEVICE_SUITE:
            folder = self.ws.root / self.app.workdir / self.profile.device.test_dir
            for f in folder.rglob("*.dart") if folder.exists() else []:
                f.unlink()
            self.r.device_suite, self.r.device_failed_files = None, []
            self._write_device_suite(previous_failures=failures)
        else:
            self.r.smoke_suite = None
            for f in self._smoke_files():
                f.unlink()
            result = self._guarded("smoke_tester", "write_smoke_tests", {"previous_failures": failures}, WorkItemResult,
                                   self.api.workdir, "api", self._smoke_problems, "The smoke test suite")
            if result is None or result.blocked:
                self.stop(f"Smoke tester could not rewrite the smoke suite: {result.blocked_reason if result else 'agent failed'}")
                return
            self.r.smoke_suite = result
            self.ws.commit("Release: smoke test suite rewritten (smoke_tester)")

    def _smoke_files(self) -> list:
        root = self.ws.root / self.api.workdir
        return [p for p in root.rglob("*") if p.is_file() and "node_modules" not in p.parts
                and "smoke" in str(p.relative_to(root)).lower() and p.suffix in (".ts", ".js")]

    def _write_device_suite(self, previous_failures: str = "") -> bool:
        dev, app = self.profile.device, self.app
        result = self._guarded("smoke_tester", "write_device_tests", {
            "app_dir": app.workdir, "test_dir": dev.test_dir, "device_api_base": self.device_api_base,
            "test_command": dev.test_command.format(api_base=self.device_api_base, serial=self.emulator.serial),
            "host_alias": dev.host_alias, "previous_failures": previous_failures,
        }, WorkItemResult, app.workdir, app.runtime, self._device_suite_problems, "The on-device test suite")
        if result is None or result.blocked:
            self.stop(f"Smoke tester could not write the device tests: {result.blocked_reason if result else 'agent failed'}")
            return False
        self.r.device_suite = result
        self.ws.commit("Release: on-device journey tests (smoke_tester)")
        self.checkpoint("Release: device test suite")
        return True

    # ---------- verification ----------

    def verify(self) -> None:
        try:
            self._verify()
        finally:
            if self.device_enabled:
                self.emulator.stop()

    def _verify(self) -> None:
        if self.r.verified and self.device_enabled and self.app_built() and self.r.device_passed is None \
                and not self.r.device_note:
            # Device checks were enabled after this release was verified: verify it again.
            self.s.reopen_release("On-device checks enabled since the last verification")
            self.checkpoint("Release: reopened for on-device checks")
        if self.r.verified or self.r.failed:   # failed: waiting for the human at the release gate (G6)
            return
        if self.r.deployment is None:
            result = self._guarded(self.profile.components.get("infra", self.api).agent, "deploy_staging", {},
                                   WorkItemResult, ".", "api", self._deployment_problems, "The staging deployment")
            if result is None or result.blocked:
                self.stop(f"Deployment engineer could not prepare staging: {result.blocked_reason if result else 'agent failed'}")
                return
            self.r.deployment = result
            self.ws.commit("Release: staging deployment files (deployment_engineer)")
            self.checkpoint("Release: deployment files")
        if self.r.smoke_suite is None and self.profile.release.smoke_command:
            result = self._guarded("smoke_tester", "write_smoke_tests", {}, WorkItemResult, self.api.workdir, "api",
                                   self._smoke_problems, "The smoke test suite")
            if result is None or result.blocked:
                self.stop(f"Smoke tester could not write the smoke suite: {result.blocked_reason if result else 'agent failed'}")
                return
            self.r.smoke_suite = result
            self.ws.commit("Release: smoke test suite (smoke_tester)")
            self.checkpoint("Release: smoke suite")
        if self.device_enabled and self.app_built() and self.r.device_suite is None:
            if not self._write_device_suite():
                return

        max_rounds = self.cfg.get("fix_rounds", 2) + 1
        for session_round in range(1, max_rounds + 1):
            if not self.can_continue():
                return
            self.r.rounds += 1
            self.r.reset_verification()
            problems = self._verify_round(first_in_session=session_round == 1)
            self.ws.write_text(f"reports/release_round{self.r.rounds}.md", self.verification_markdown(problems))
            outcome = "staging did not start" if problems is None else ("passed" if not problems else "failed")
            self.ws.commit(f"Release verification round {self.r.rounds}: {outcome}")
            if problems is None:  # staging could not start; reason already recorded
                return
            self.r.open_problems = [[c, p] for c, p in problems]
            if not problems:
                self.r.verified = True
                self.checkpoint("Release: staging verified")
                return
            if session_round == max_rounds:
                # Out of fix rounds: the human decides at the release gate (G6) (ship with known problems, or
                # reject with feedback for another fix-and-verify cycle).
                self.r.failed = True
                self.checkpoint(f"Release: verification still failing after {max_rounds} round(s)")
                return
            for component in dict.fromkeys(c for c, _ in problems):
                texts = [t for c, t in problems if c == component]
                if component in (SMOKE_SUITE, DEVICE_SUITE):
                    self._rewrite_suite(component, "\n".join(texts))
                else:
                    self.fix("Staging verification found these problems. Fix them:\n" + "\n".join(texts), component)

    def _verify_round(self, first_in_session: bool = True) -> list[tuple[str, str]] | None:
        """One staging round. Returns (component, problem) pairs, or None if staging could not start."""
        api_c = self.profile.release.api_component
        if self.device_enabled:
            self.staging.extra_env = self.device_env()
        else:
            self.r.device_note = "device checks are disabled in the pipeline config"
        try:
            self.staging.start()
        except StagingError as e:
            logs = str(e)
            self.checkpoint("Release: staging failed to start")
            if self.device_enabled:
                self.r.device_note = "staging did not start, so the app was not run on the device"
            if e.environment:  # the machine, not the code: no developer can fix it
                self.stop(f"Staging could not start because of a machine problem (not the code): {logs[:900]}")
                return None
            # A start failure may be a code/config problem the developer can fix; hand it over once.
            if first_in_session:
                return [(api_c, f"Staging failed to start:\n{logs[-3000:]}")]
            self.stop(f"Staging could not start: {logs[:600]}")
            return None
        try:
            rel = self.profile.release
            if rel.openapi_json_path:
                status, body = http_get(self.staging.base_url + rel.openapi_json_path)
                self.r.contract_issues = (
                    contract_diff(self.ws.read_text(CONTRACT_PATH), body, rel.api_prefix,
                                  ignore_paths=[rel.health_path, rel.openapi_json_path])
                    if status == 200 else [f"GET {rel.openapi_json_path} returned {status or 'no response'}"]
                )
                self.r.contract_issues = self._built_scope(self.r.contract_issues)
            inputs = {
                "base_url": self.api_base_url,
                "contract_issues": "\n".join(self.r.contract_issues) or "(none)",
                "staging_logs": self.staging.logs(3000),
            }
            item_ids = [w.id for w in self.s.backlog.work_items]
            report = self._job("integration_pass", "integration_review", inputs, QAReport, ".")
            errors = agent_guardrails.report_errors(report, item_ids, self.guard_rules, ("RELEASE",)) if report else []
            if errors:  # one retry with the reasons, then derive the verdict from the bugs
                report = self._job("integration_pass", "integration_review", inputs, QAReport, ".", feedback="\n".join(errors))
                if report and agent_guardrails.report_errors(report, item_ids, self.guard_rules, ("RELEASE",)):
                    report.passed = not report.blocking_bugs()
            self.r.integration = report
            if self.profile.release.smoke_command:
                res = self.sandbox.run_trusted(self.api.runtime, self.api.workdir, rel.smoke_command,
                                               env={"SMOKE_BASE_URL": self.api_base_url}, host_network=True)
                self.r.smoke_passed, self.r.smoke_output = res.ok, res.output[-6000:]
            sandbox_problems = self._sandbox_round()
            device_problems = []
            if self.device_enabled:
                api_green = (self.r.integration is not None and not self.r.integration.blocking_bugs()
                             and self.r.smoke_passed is not False)
                if api_green or not (self.cfg.get("device") or {}).get("only_when_api_green", True):
                    device_problems = self._device_round()
                else:
                    # The expensive part (APK build, emulator, journeys) only pays off against a working API: the
                    # fixes for these API problems would change what the app talks to anyway.
                    self.r.device_note = "skipped this round: fix the API problems first (device tests need a working API)"
        finally:
            self.staging.stop()
        problems: list[tuple[str, str]] = []
        if self.r.integration is None:
            problems.append((api_c, "The integration pass agent failed to produce a report"))
        else:
            problems += [(api_c, f"[{b.severity}] {b.title}: {b.actual} (expected: {b.expected})")
                         for b in self.r.integration.blocking_bugs()]
        self.r.smoke_passed_once = self.r.smoke_passed_once or bool(self.r.smoke_passed)
        if self.r.smoke_passed is False:
            if self.r.suite_rewrites < 2 and suspect_suite(self.r.smoke_output, self.r.smoke_passed_once):
                problems.append((SMOKE_SUITE, f"The smoke suite fails most of its journeys on its first run, so the TESTS "
                                              f"are suspect, not the API:\n{self.r.smoke_output[-3000:]}"))
            else:
                problems.append((api_c, f"Smoke tests failed against staging:\n{self.r.smoke_output[-3000:]}"))
        return problems + sandbox_problems + device_problems

    def _sandbox_round(self) -> list[tuple[str, str]]:
        """The built API against the real third-party sandbox (read-only), when the profile and pipeline ask for it."""
        cfg = self.profile.release.sandbox_check
        if not (cfg and self.cfg.get("sandbox_check")):
            return []
        from agentic_sdlc.release.sandbox_check import SandboxCheck

        result = SandboxCheck(self.ws.root, cfg, self.profile.release.compose_api_service).check()
        self.r.sandbox_passed, self.r.sandbox_note = (result.passed if result.ran else None), result.note
        if result.ran:
            self.ws.write_text(f"reports/sandbox_round{self.r.rounds}.txt", result.output)
        return [(self.profile.release.api_component, p) for p in result.problems]

    def _built_scope(self, issues: list[str]) -> list[str]:
        """Endpoints of unbuilt work are expected to be missing; only report extra endpoints and
        missing ones when everything was built."""
        if any(p.status != "done" for p in self.s.build.items.values()):
            return [i for i in issues if not i.startswith("Missing in the API")]
        return issues

    def fix_after_rejection(self, feedback: str) -> None:
        """the release gate (G6) rejected: send the reviewer's feedback, with the open verification problems of each
        component, to that component's developer (the API's developer if nothing is open)."""
        open_by_component: dict[str, list[str]] = {}
        for c, p in self.r.open_problems:
            open_by_component.setdefault(c, []).append(p)
        if not open_by_component:
            open_by_component[self.profile.release.api_component] = []
        for component, texts in open_by_component.items():
            if not self.can_continue():
                return
            message = "The release reviewer rejected this release:\n" + (feedback or "(no feedback given)")
            if texts:
                message += "\n\nStaging verification also found these problems in your part:\n" + "\n".join(texts)
            self.fix(message, component)
        self.r.open_problems = []

    def acceptance_files(self, comp) -> str:
        """The component's locked acceptance tests (read-only for the fixer)."""
        folder = (comp.acceptance.dir.rstrip("/") + "/") if comp.acceptance else None
        files = sorted(p for p in self.s.build.locked_tests if folder and p.startswith(folder))
        return "\n".join(f"- {p}" for p in files) or "(none for this component)"

    def fix(self, problems: str, component: str | None = None) -> None:
        """Send problems to the developer of the component they belong to (default: the API)."""
        component = component or self.profile.release.api_component
        comp = self.profile.components[component]
        inputs = {
            "item_id": "RELEASE", "item_title": "Release verification fixes", "component": component,
            "item_description": "Fix problems found while running the system on staging.",
            "milestone": "Release", "stories": "(see the problems)", "checks": "; ".join(comp.checks) or "(none)",
            "problems": problems, "done_items": self.built_summary(), "acceptance_files": self.acceptance_files(comp),
        }
        code_rules = self._code_rules()
        locked = self.s.build.locked_tests
        result = self._guarded(comp.agent, "fix_work_item", inputs, WorkItemResult, comp.workdir, comp.runtime,
                               lambda _r: code_guardrails.check_changes(self.ws, comp, self.profile, code_rules, locked=locked)
                               if code_rules else [], f"The {component} fix")
        if result is not None:
            self.ws.commit(f"Release: fixes from staging verification ({comp.agent})")
        self.checkpoint("Release: fixes applied")

    def verification_markdown(self, problems: list[str] | None) -> str:
        r = self.r
        lines = [f"# Release verification, round {r.rounds}", "",
                 f"Staging: {self.staging.mode} at {self.staging.base_url}", "", "## Contract check"]
        lines += [f"- {i}" for i in r.contract_issues] or ["- matches the contract for the built scope"]
        lines += ["", "## Integration pass", r.integration.to_markdown() if r.integration else "(no report)",
                  "", "## Smoke tests", f"Result: {'passed' if r.smoke_passed else 'failed' if r.smoke_passed is False else 'not run'}",
                  "", "```", r.smoke_output[-3000:], "```", "", "## On-device (Android emulator)"]
        if r.device_passed is None:
            lines.append(f"Not run: {r.device_note or 'no reason recorded'}")
        else:
            lines.append(f"Result: {'passed' if r.device_passed else 'failed'}" + (f" ({r.device_note})" if r.device_note else ""))
            lines += [f"![{p.rsplit('/', 1)[-1]}](../{p.removeprefix('reports/')})" for p in r.device_screenshots]
            if r.device_output:
                lines += ["", "```", r.device_output[-3000:], "```"]
        lines += ["", "## Problems"]
        if problems is None:
            lines.append(f"- {self.s.stop_reason[:1500] or 'Staging could not start'}")
        else:
            lines += [f"- [{c}] {p}" for c, p in problems] or ["- none"]
        return "\n".join(lines)

    def gate_summary(self) -> str:
        r = self.r
        if r.failed and not r.verified:
            problems = "\n".join(f"  - [{c}] {p.splitlines()[0][:200]}" for c, p in r.open_problems) or "  (none recorded)"
            return (f"Release verification FAILED: problems remain after round {r.rounds} (fix rounds used up).\n"
                    f"{problems}\nApproving ships the release WITH these problems. Rejecting sends your feedback and "
                    f"these problems to the developers, then verifies staging again.")
        return (f"Staging verified in round {r.rounds}: contract ok, integration "
                f"{'passed' if r.integration and r.integration.passed else 'n/a'}, smoke "
                f"{'passed' if r.smoke_passed else 'n/a'}, on device "
                f"{'passed' if r.device_passed else ('not run: ' + r.device_note if r.device_passed is None else 'failed')}"
                f"{' (screenshots: ' + ', '.join(r.device_screenshots) + ')' if r.device_screenshots else ''}"
                f"{'. Real-site sandbox: ' + ('passed' if r.sandbox_passed else r.sandbox_note or 'failed') if r.sandbox_passed is not None or r.sandbox_note else ''}. Built: "
                f"{sum(p.status == 'done' for p in self.s.build.items.values())} work items; not built: "
                f"{sum(p.status != 'done' for p in self.s.build.items.values())}.")

    # ---------- acceptance, packaging and the release plan (G6, G7) ----------

    def accept(self) -> None:
        """Proof per acceptance criterion (evidence/AC-xx/), the acceptance report, the UAT guide and the manifest."""
        from agentic_sdlc.build.services import run_acceptance_suite
        from agentic_sdlc.release.acceptance import Acceptor

        Acceptor(self.s, self.ws, self.profile, lambda comp, acc: run_acceptance_suite(self.sandbox, comp, acc),
                 checkpoint=self.checkpoint).accept()

    def package(self) -> None:
        """Build the release artifacts, write release.md (what ships, how to deploy, how to roll back) and tag.
        Never deploys: `uv run deploy` does, run by the named approver after G6 and G7."""
        if self.r.production in ("packaged", "ready", "deployed"):
            return
        notes = []
        for cmd in self.profile.release.package_commands:
            res = self.sandbox.run_trusted(self.api.runtime, self.api.workdir, cmd)
            if not res.ok:
                self.r.production, self.r.production_notes = "failed", f"`{cmd}` failed:\n{res.output[-3000:]}"
                self.stop(f"Packaging failed: `{cmd}`")
                return
            notes.append(f"ran `{cmd}`")
        tag = "release-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        self.ws.write_text("reports/release_notes.md", self.release_notes(tag))
        self.ws.write_text("docs/release.md", self.release_plan(tag))
        self.ws.commit(f"Release plan and notes for {tag}")
        self.ws.tag(tag)
        self.r.production, self.r.production_notes = "packaged", f"Tagged {tag}. " + "; ".join(notes)
        self.checkpoint(f"Release: packaged as {tag}")

    def release_plan(self, tag: str) -> str:
        """docs/release.md, read by the approver at G7: what ships, the exact steps, and how to undo it."""
        rel, r = self.profile.release, self.r
        command = (self.cfg.get("production_command") or "").strip()
        rollback = rel.rollback_steps
        met = f"{len(r.acceptance_met)} met, {len(r.acceptance_unmet)} not met"
        return "\n".join([
            f"# Release plan {tag}", "", f"Product: {self.s.prd.title}", "",
            "## What ships", self.built_summary(), "", "## Not included", self.not_built_summary(), "",
            "## Evidence", f"Acceptance criteria: {met}. See docs/acceptance.md and evidence-manifest.sha256 "
            f"(manifest hash {r.evidence_manifest[:16] or 'n/a'}).", "",
            "## Deploy", *(["The approver runs, from the project root:", "", f"    uv run deploy {self.s.run_id} --as \"<your name>\"",
                           "", f"which executes `{command}` with the approver's own environment."]
                          if command else ["**No production command is configured** (`release.production_command` in the pipeline "
                                           "config). Deployment is then done by hand from the packaged artifacts."]),
            "", "## Roll back",
            *([f"{i}. {step}" for i, step in enumerate(rollback, 1)] if rollback else
              ["**No rollback steps are defined for this profile** (`release.rollback_steps`). The approver must agree "
               "how to undo this release before approving G7."]),
            "", "## Production settings", rel.staging_notes or "(none listed)", ""])

    def release_notes(self, tag: str) -> str:
        r = self.r
        return "\n".join([
            f"# Release {tag}", "", f"Product: {self.s.prd.title}", "",
            "## Included", self.built_summary(), "",
            "## Not included", self.not_built_summary(), "",
            "## Verification", f"Staging rounds: {r.rounds}. Smoke: {'passed' if r.smoke_passed else 'n/a'}.",
            *(["**Verification failed; approved at the release gate (G6) with these known problems:**",
               *[f"- [{c}] {p.splitlines()[0][:200]}" for c, p in r.open_problems], ""] if r.failed and not r.verified else []),
            f"Integration: {r.integration.summary if r.integration else 'n/a'}", "",
            "## Deployment", r.deployment.summary if r.deployment else "", "",
        ])
