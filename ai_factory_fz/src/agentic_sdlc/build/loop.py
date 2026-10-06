"""The build loop: for each milestone, implement its work items, verify them, then QA.

Per milestone (build.acceptance_tests): the Test Writer writes acceptance tests for the milestone's
criteria before any code; they must fail, then they are locked (tests.lock).

Per work item: scaffold the component if needed -> the developer agent implements it, writing only the
paths the task owns (Claude Code hooks block anything else live) -> guardrails (no weakened or changed
locked tests, no secrets, ownership) -> the component's checks and the task's verify command -> on
failure the agent gets the output and fixes it (limited attempts) -> commit + build notes. Items whose
toolchain is missing, or whose dependencies are blocked, are marked blocked with a reason.
With build.parallel the backend lane and the frontend lane build at the same time (app tasks work
against the contract, so they do not wait for backend tasks); git access is serialised.

Then the locked acceptance tests run, and the QA agent verifies the milestone and reports bugs ->
developers fix blocking bugs -> checks -> QA again, up to qa_fix_rounds. If bugs remain, the run
stops for a human to look.

All progress lives in ProjectState.build and is checkpointed after every step, so a resumed
run continues where it stopped.
"""

import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from agentic_sdlc.artifacts.backlog import Milestone, WorkItem
from agentic_sdlc.artifacts.reports import Bug, QAReport, WorkItemResult
from agentic_sdlc.artifacts.review import ReviewReport
from agentic_sdlc.artifacts.tests import AcceptanceSuite, test_plan_markdown
from agentic_sdlc.artifacts.wbs import _prefix as owned_prefix
from agentic_sdlc.build.coders import Job, Worker
from agentic_sdlc.build.scaffold import ScaffoldError, required_runtimes, scaffold
from agentic_sdlc.build import verify
from agentic_sdlc.build.services import ServiceError, acceptance_database, environment_failure
from agentic_sdlc.guardrails import agents as agent_guardrails
from agentic_sdlc.guardrails import code as code_guardrails
from agentic_sdlc.crews.base import PhaseError, TaskResult, UsageLimitError
from agentic_sdlc.registry.profiles import Component, Profile
from agentic_sdlc.state import ProjectState
from agentic_sdlc.tools.sandbox_exec import SandboxRejected, SandboxResult, SandboxRunner
from agentic_sdlc.workspace import Workspace
from agentic_sdlc.workspace_layout import component_workdir

log = logging.getLogger(__name__)
PHASE = "build"

_SANDBOX_BLOCK_MARKERS = (
    "command execution is blocked",
    "shell/sandbox command invocations are rejected",
    "could not be run to verify",
    "permission is denied",
    "permission was denied",
    "bash is denied",
    "don't-ask mode",
    "dontask mode",
    "not allowed to run",
)


def _agent_sandbox_block_is_retryable(reason: str) -> bool:
    """CrewAI + Cursor cannot run sandbox_exec; the pipeline still runs component checks."""
    text = (reason or "").lower()
    return any(m in text for m in _SANDBOX_BLOCK_MARKERS)


@dataclass
class BuildConfig:
    milestones: list[str]          # empty = all, in backlog order
    check_fix_attempts: int = 2
    qa_fix_rounds: int = 3
    guard_rules: set[str] = field(default_factory=set)   # agent guardrails on (DV*, QA*)
    stop_on_failed: bool = True      # a work item that failed for good stops the run (a person must look)
    verify: bool = False             # Integrator checks + the profile's scans run every QA round
    review: bool = False             # Code Reviewer reviews each milestone that is clean otherwise
    acceptance_tests: bool = False   # Test Writer writes locked acceptance tests before each milestone
    parallel: bool = False           # backend and frontend lanes build at the same time

    @classmethod
    def from_pipeline(cls, pipeline: dict[str, Any]) -> "BuildConfig":
        b = pipeline.get("build", {}) or {}
        override = os.environ.get("SDLC_BUILD_MILESTONES")
        milestones = [m.strip() for m in override.split(",") if m.strip()] if override else list(b.get("milestones") or [])
        return cls(
            milestones=milestones,
            check_fix_attempts=b.get("check_fix_attempts", 2),
            qa_fix_rounds=pipeline.get("limits", {}).get("qa_fix_rounds", 3),
            guard_rules=agent_guardrails.enabled(pipeline),
            stop_on_failed=bool(b.get("stop_on_failed", True)),
            verify=bool(b.get("verify", False)),
            review=bool(b.get("review", False)),
            acceptance_tests=bool(b.get("acceptance_tests", False)),
            parallel=bool(b.get("parallel", False)),
        )


class Builder:
    def __init__(
        self,
        state: ProjectState,
        workspace: Workspace,
        profile: Profile,
        sandbox: SandboxRunner,
        worker_for: Callable[[str], Worker],
        config: BuildConfig,
        record: Callable[[TaskResult], Any],
        checkpoint: Callable[[str], None],
        can_continue: Callable[[], bool],
        stop: Callable[[str], None],
    ):
        self.s = state
        self.ws = workspace
        self.profile = profile
        self.sandbox = sandbox
        self.worker_for = worker_for
        self.cfg = config
        self.record = record
        self.checkpoint = checkpoint
        self.can_continue = can_continue
        self.stop = stop
        self.items: dict[str, WorkItem] = {w.id: w for w in state.backlog.work_items}
        self._unavailable: dict[str, str] = {}  # component -> reason
        self._git = threading.RLock()            # one git/state writer at a time (parallel lanes)
        self._parallel_now = False
        self._lane_prefixes: dict[str, tuple[str, ...]] = {}   # lane -> folders it writes (parallel)
        self._soft_deps: set[str] = set()        # other-lane items a lane does not wait for (parallel)
        self._acceptance_text: dict[str, str] = {}

    # ---------- git / state (shared by the lanes) ----------

    def _save(self, message: str, paths: list[str] | None = None) -> None:
        """Checkpoint; while lanes run in parallel, only bookkeeping (and `paths`) is committed."""
        with self._git:
            if self._parallel_now:
                self.checkpoint(message, [*Workspace.BOOKKEEPING, *(paths or [])])
            elif paths is not None:
                self.checkpoint(message, [*Workspace.BOOKKEEPING, *paths])
            else:
                self.checkpoint(message)

    @staticmethod
    def lane(item: WorkItem) -> str:
        return "frontend" if item.component == "frontend" else "main"

    def _write_paths(self, item: WorkItem, comp: Component) -> list[str]:
        """Folders/files this item's commit and discard cover."""
        if comp.workdir not in (".", ""):
            return [comp.workdir]
        return [p for p in (owned_prefix(g) for g in item.owns) if p] or ["."]

    def _ignore_for(self, item: WorkItem) -> tuple[str, ...]:
        """While lanes run in parallel: the other lane's folders, which this item's checks skip."""
        if not self._parallel_now:
            return ()
        mine = self.lane(item)
        return tuple(p.rstrip("/") + "/" for lane, ps in self._lane_prefixes.items() if lane != mine for p in ps)

    def policy(self, item: WorkItem | None, comp: Component) -> dict[str, Any]:
        """Hook policy for a coding job (hooks/guard.py)."""
        extra = [*(self.profile.guardrails.get("scope_also_allowed") or []), "reports/"]
        return {"owns": list(item.owns) if item else [], "workdir": comp.workdir,
                "locked": sorted(self.s.build.locked_tests), "also_allowed": extra}

    # ---------- selection ----------

    def selected_milestones(self) -> list[Milestone]:
        ms = self.s.backlog.milestones
        if not self.cfg.milestones:
            return ms
        unknown = set(self.cfg.milestones) - {m.id for m in ms}
        if unknown:
            raise ValueError(f"build.milestones lists unknown milestones: {sorted(unknown)}")
        return [m for m in ms if m.id in self.cfg.milestones]

    def ordered_items(self, m: Milestone) -> list[WorkItem]:
        """Milestone items in dependency order (dependencies inside the milestone first)."""
        ids = [wid for wid in m.work_item_ids if wid in self.items]
        done: list[str] = []
        pending = list(ids)
        while pending:
            ready = [w for w in pending if all(d not in pending or d in done for d in self.items[w].depends_on)]
            nxt = ready[0] if ready else pending[0]  # cycles are rejected at planning; be safe anyway
            done.append(nxt)
            pending.remove(nxt)
        return [self.items[w] for w in done]

    def _milestone_has_pending_items(self, m: Milestone) -> bool:
        return any(
            self.s.build.item(wid).status != "done"
            for wid in m.work_item_ids
            if wid in self.items
        )

    # ---------- run ----------

    def run(self) -> None:
        milestones = self.selected_milestones()
        self.prepare_toolchains(milestones)
        for m in milestones:
            mp = self.s.build.milestone(m.id)
            if mp.status == "done" and not self._milestone_has_pending_items(m):
                continue
            if mp.status == "done" and self._milestone_has_pending_items(m):
                mp.status = "todo"
                log.info("Reopened milestone %s for incomplete work items", m.id)
            # A partial milestone is retried: blocked/failed items get another go, since the
            # cause (a missing toolchain, a setup error) may have been fixed since.
            if self.cfg.acceptance_tests:
                self.write_acceptance_tests(m)
                if not self.can_continue():
                    return
            items = self.ordered_items(m)
            before = {i.id for i in items if self.s.build.item(i.id).status == "done"}
            if self.cfg.parallel:
                self.build_lanes(m, items)
            else:
                for item in items:
                    if not self.can_continue():
                        return
                    self.build_item(m, item)
            newly_done = any(self.s.build.item(i.id).status == "done" and i.id not in before for i in items)
            if not self.can_continue():
                return
            failed = [i for i in items if self.s.build.item(i.id).status == "failed"]
            if failed and self.cfg.stop_on_failed:
                # A task that failed after its fix attempts needs a person: building on it only produces blocked work.
                self._save(f"Build: {m.id} has failed work")
                self.stop(f"{m.id}: " + "; ".join(f"{i.id} failed ({self.s.build.item(i.id).reason[:160]})" for i in failed)
                          + ". Fix the cause (see reports/ and the task's attempts) and resume.")
                return
            if mp.status == "todo" or newly_done or mp.status == "failed":
                self._acceptance_text[m.id] = self.run_acceptance(m)
                self.qa_milestone(m)
            if self.s.status != "running":
                return

    def build_lanes(self, m: Milestone, items: list[WorkItem]) -> None:
        """Backend (+infra) and frontend lanes at the same time; each lane builds its items in order."""
        lanes: dict[str, list[WorkItem]] = {}
        for item in items:
            lanes.setdefault(self.lane(item), []).append(item)
        if len(lanes) < 2:
            for item in items:
                if not self.can_continue():
                    return
                self.build_item(m, item)
            return
        for item in items:                       # set up projects first, one at a time
            self.ensure_scaffold(item.component, self.component(item))
        self._lane_prefixes = {lane: tuple(dict.fromkeys(p for i in its for p in self._write_paths(i, self.component(i))))
                               for lane, its in lanes.items()}
        self._soft_deps = {i.id for i in items}
        self._parallel_now = True

        def run_lane(lane_items: list[WorkItem]) -> None:
            for item in lane_items:
                if not self.can_continue():
                    return
                self.build_item(m, item)

        try:
            with ThreadPoolExecutor(max_workers=len(lanes), thread_name_prefix="lane") as pool:
                for future in [pool.submit(run_lane, its) for its in lanes.values()]:
                    future.result()
        finally:
            self._parallel_now, self._soft_deps, self._lane_prefixes = False, set(), {}

    def prepare_toolchains(self, milestones: list[Milestone]) -> None:
        """Make the toolchains for the components still to build ready (e.g. pull Docker images)."""
        components = {self.items[w].component for m in milestones for w in m.work_item_ids
                      if w in self.items and self.s.build.item(w).status != "done"}
        runtimes = [rt for c in sorted(components) if c in self.profile.components
                    for rt in required_runtimes(self.profile.components[c])]
        for runtime, error in self.sandbox.prepare(runtimes).items():
            log.warning("Toolchain %s unavailable: %s", runtime, error)

    # ---------- work items ----------

    def component(self, item: WorkItem) -> Component:
        comp = self.profile.components.get(item.component)
        if comp is None:
            raise ValueError(f"Profile has no component '{item.component}' (needed by {item.id})")
        return comp

    def effective_component(self, item: WorkItem) -> Component:
        comp = self.component(item)
        wd = component_workdir(self.ws, comp)
        if wd == comp.workdir:
            return comp
        return comp.model_copy(update={"workdir": wd})

    def effective_component_for(self, name: str) -> Component:
        comp = self.profile.components[name]
        wd = component_workdir(self.ws, comp)
        return comp if wd == comp.workdir else comp.model_copy(update={"workdir": wd})

    def block_reason(self, item: WorkItem) -> str | None:
        for dep in item.depends_on:
            if dep in self._soft_deps and self.lane(self.items[dep]) != self.lane(item):
                continue        # parallel: the other lane builds it; this task works against the contract
            dp = self.s.build.items.get(dep)
            if dp is None or dp.status != "done":
                state = dp.status if dp else "not built (outside the selected milestones)"
                return f"depends on {dep}, which is {state}"
        comp = self.component(item)
        if item.component in self._unavailable:
            return self._unavailable[item.component]
        for rt in required_runtimes(comp):
            reason = self.sandbox.unavailable_reason(rt)
            if reason:
                self._unavailable[item.component] = reason
                return reason
        return None

    def ensure_scaffold(self, name: str, comp: Component) -> str | None:
        if name in self.s.build.scaffolded or not comp.scaffold:
            return None
        try:
            steps = scaffold(name, comp, self.ws, self.sandbox, self.profile.root / "templates")
        except ScaffoldError as e:
            self._unavailable[name] = f"project setup failed: {str(e)[:500]}"
            return self._unavailable[name]
        self.s.build.scaffolded.append(name)
        self.ws.write_text(f"reports/scaffold_{name}.log", "\n".join(steps) + "\n")
        self._save(f"Build: scaffold {name}")
        return None

    def run_checks(self, comp: Component) -> tuple[bool, str]:
        if not comp.runtime:
            return True, ""
        for cmd in comp.checks:
            result = self.sandbox.run_trusted(comp.runtime, comp.workdir, cmd)
            if not result.ok:
                return False, f"`{cmd}` failed (exit {result.exit_code}):\n{result.output[-6000:]}"
        return True, ""

    def stories_text(self, story_ids: list[str]) -> str:
        stories = {s.id: s for s in self.s.prd.user_stories}
        out = []
        for sid in story_ids:
            s = stories.get(sid)
            if s:
                out.append(f"{s.id} {s.title}: As a {s.as_a}, I want {s.i_want}, so that {s.so_that}.")
                out += [f"  - Given {c.given} when {c.when} then {c.then}" for c in s.acceptance_criteria]
        return "\n".join(out) or "(no user stories: technical item)"

    @staticmethod
    def links_text(item: WorkItem) -> str:
        """What the plan says this item builds, so the developer knows exactly what it owns."""
        parts = [(label, values) for label, values in (
            ("API operations", item.api_operations), ("data models", item.data_models),
            ("screens", item.screens), ("modules", item.modules)) if values]
        text = ("\nPlanned scope: " + "; ".join(f"{label}: {', '.join(v)}" for label, v in parts)) if parts else ""
        if item.owns:
            text += (f"\nYou may write only these paths (the Architect's WBS; anything else is blocked): "
                     f"{', '.join(item.owns)} (dependency manifests are always allowed).")
        if item.verify and item.verify.strip() != "-":
            text += f"\nDone means the checks pass and this verify command passes: `{item.verify}`."
        if item.ac_ids:
            text += f"\nAcceptance criteria this task makes pass: {', '.join(item.ac_ids)} (locked acceptance tests prove them)."
        return text

    def done_summary(self) -> str:
        lines = [f"- {wid} {self.items[wid].title}: {p.summary[:200]}" for wid, p in self.s.build.items.items()
                 if p.status == "done" and wid in self.items]
        return "\n".join(lines) or "(none yet)"

    def item_inputs(self, m: Milestone, item: WorkItem, comp: Component, problems: str) -> dict[str, Any]:
        return {
            "item_id": item.id,
            "item_title": item.title,
            "item_description": item.description + self.links_text(item),
            "component": item.component,
            "milestone": f"{m.id} {m.name}: {m.goal}",
            "stories": self.stories_text(item.story_ids),
            "done_items": self.done_summary(),
            "docs_dir": str(self.ws.root / "docs"),
            "checks": "; ".join(comp.checks) or "(none)",
            "problems": problems or "(none)",
            "acceptance_files": self.acceptance_files(item.component),
        }

    def acceptance_files(self, component: str) -> str:
        """The locked acceptance test files of a component (builders must read them: they fix the names)."""
        comp = self.profile.components.get(component)
        folder = (comp.acceptance.dir.rstrip("/") + "/") if comp and comp.acceptance else None
        files = sorted(p for p in self.s.build.locked_tests if folder and p.startswith(folder))
        return "\n".join(f"- {p}" for p in files) or "(none for this component yet)"

    def build_item(self, m: Milestone, item: WorkItem) -> None:
        p = self.s.build.item(item.id)
        if p.status == "done":
            return
        comp = self.effective_component(item)
        reason = self.block_reason(item) or self.ensure_scaffold(item.component, self.component(item))
        if reason:
            p.status, p.reason = "blocked", reason
            self._save(f"Build: {item.id} blocked")
            return

        outcome, result, detail = self._work_until_green(m, item, comp, "implement_work_item", "")
        if outcome == "done":
            p.status, p.summary, p.reason = "done", result.summary, ""
            with self._git:
                files = [f for f in code_guardrails.changed_files(self.ws) if not f.startswith(self._ignore_for(item))]
                self.write_build_note(item, comp, result, files)
                p.commit = self.ws.commit(f"{item.id}: {item.title} ({comp.agent})",
                                          [*self._write_paths(item, comp), "docs", "reports"])
        elif outcome == "blocked":
            p.status, p.reason, p.summary = "blocked", detail, result.summary if result else ""
        else:
            p.status, p.reason = "failed", detail
        self._save(f"Build: {item.id} {p.status}")

    def write_build_note(self, item: WorkItem, comp: Component, result: WorkItemResult, files: list[str]) -> None:
        """docs/build-notes-<component>.md: what each task did, how it was verified, which files changed."""
        path = self.ws.root / "docs" / f"build-notes-{item.component}.md"
        head = "" if path.exists() else f"# Build notes: {item.component}\n\n"
        verify = item.verify if item.verify and item.verify.strip() != "-" else "(none)"
        note = (f"## {item.id} {item.title}\n\n{result.summary}\n\n- Agent: {comp.agent}\n"
                f"- Checks: {'; '.join(comp.checks) or '(none)'}; verify: `{verify}`: passed\n"
                f"- Criteria: {', '.join(item.ac_ids) or '-'}\n- Files: {', '.join(files[:40]) or '-'}\n\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(head + note)

    def _work_until_green(self, m: Milestone, item: WorkItem, comp: Component, first_task: str,
                          problems: str) -> tuple[str, WorkItemResult | None, str]:
        """Run the agent, then the checks; feed failing check output back to the agent until the
        checks pass or attempts run out. Returns (outcome, last result, reason): outcome is
        done, blocked or failed. Used for new work items and for QA bug fixes alike."""
        p = self.s.build.item(item.id)
        task_key = first_task
        result = None
        code_rules = self.cfg.guard_rules & set(code_guardrails.CODE_RULES)
        for attempt in range(1, self.cfg.check_fix_attempts + 2):
            p.attempts += 1
            result = self._work(comp, task_key, self.item_inputs(m, item, comp, problems), self.policy(item, comp))
            if result is None:
                self._discard(item, comp)
                return "failed", None, "the developer agent failed (see reports/agent_failures and logs)"
            if result.blocked:
                if _agent_sandbox_block_is_retryable(result.blocked_reason):
                    log.warning(
                        "Ignoring agent-reported sandbox/shell block for %s; pipeline will run checks",
                        item.id,
                    )
                    result.blocked = False
                    result.blocked_reason = ""
                else:
                    return "blocked", result, result.blocked_reason or "agent reported blocked"
            task_key = "fix_work_item"
            profile_comp = self.component(item)
            with self._git:
                violations = (
                    code_guardrails.check_changes(self.ws, profile_comp, self.profile, code_rules, owns=item.owns,
                                                  locked=self.s.build.locked_tests, ignore_prefixes=self._ignore_for(item))
                    if code_rules else []
                )
            if violations:
                output = "\n".join(violations)
                problems = f"Guardrails rejected your change. Fix all of these:\n{output}"
                continue
            ok, output = self.run_checks(comp)
            if ok:
                ok, output = self.run_verify(item, comp)
            if ok:
                return "done", result, ""
            problems = f"The checks failed after your change. Fix the cause.\n{output}"
        # Nothing half-done may slip into the next item's commit.
        self._discard(item, comp)
        return "failed", result, f"still failing after {attempt} attempt(s); changes discarded. Last output:\n{output[-1500:]}"

    def _discard(self, item: WorkItem, comp: Component) -> None:
        with self._git:
            for path in self._write_paths(item, comp):
                code_guardrails.discard_changes(self.ws, path)

    def run_verify(self, item: WorkItem, comp: Component) -> tuple[bool, str]:
        """The task's own verify command (from the WBS), run on the sandbox allow-list."""
        cmd = (item.verify or "").strip()
        if not cmd or cmd == "-" or not comp.runtime:
            return True, ""
        try:
            result = self.sandbox.run(comp.runtime, comp.workdir, cmd)
        except SandboxRejected as e:
            log.warning("Verify command of %s not run: %s", item.id, e)
            return True, ""
        if not result.ok:
            return False, f"The task's verify command `{cmd}` failed (exit {result.exit_code}):\n{result.output[-6000:]}"
        return True, ""

    def _work(self, comp: Component, task_key: str, inputs: dict[str, Any],
              policy: dict[str, Any] | None = None) -> WorkItemResult | None:
        job = Job(PHASE, comp.agent, task_key, inputs, WorkItemResult, comp.workdir, comp.runtime, policy=policy)
        try:
            return self.record(self.worker_for(comp.agent).run(job))
        except UsageLimitError as e:
            self.stop(f"{e}. Resume the run after the limit resets (uv run resume <run_id>).")
            return None
        except PhaseError as e:
            log.error("%s", e)
            return None

    # ---------- acceptance tests (Test Writer) ----------

    def _criteria(self, ac_ids: list[str]) -> str:
        by_id = {c.id: (sid, c) for sid, c in self.s.prd.criteria()}
        stories = {st.id: st for st in self.s.prd.user_stories}
        lines = []
        for ac in ac_ids:
            if ac in by_id:
                sid, c = by_id[ac]
                lines.append(f"- {ac} ({sid} {stories[sid].title}): Given {c.given}, when {c.when}, then {c.then}")
        return "\n".join(lines) or "(none)"

    def _lock(self, folder: str) -> list[str]:
        """Hash every acceptance test file under `folder` into tests.lock (run root)."""
        locked = []
        for f in sorted((self.ws.root / folder).rglob("*")):
            if f.is_file():
                rel = f.relative_to(self.ws.root).as_posix()
                self.s.build.locked_tests[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
                locked.append(rel)
        self.ws.write_text("tests.lock", json.dumps(self.s.build.locked_tests, indent=2, sort_keys=True) + "\n")
        return locked

    def write_acceptance_tests(self, m: Milestone) -> None:
        """Before the milestone is built: tests per acceptance criterion, which must fail, then locked."""
        by_comp: dict[str, list[str]] = {}
        for wid in m.work_item_ids:
            item = self.items.get(wid)
            comp = self.profile.components.get(item.component) if item else None
            if item and comp and comp.acceptance and self.s.build.item(wid).status != "done":
                by_comp.setdefault(item.component, [])
                by_comp[item.component] += [a for a in item.ac_ids if a not in by_comp[item.component]]
        for name, ac_ids in by_comp.items():
            key = f"{m.id}/{name}"
            if key in self.s.build.acceptance or not ac_ids or not self.can_continue():
                continue
            comp = self.profile.components[name]
            acc = comp.acceptance
            if self.ensure_scaffold(name, comp) or any(self.sandbox.unavailable_reason(rt) for rt in required_runtimes(comp)):
                log.warning("Acceptance tests for %s skipped: %s cannot be set up here", key, name)
                continue
            inputs = {"milestone": f"{m.id} {m.name}: {m.goal}", "component": name, "criteria": self._criteria(ac_ids),
                      "docs_dir": str(self.ws.root / "docs"), "folder": acc.dir, "command": acc.command,
                      "runner_note": acc.note}
            policy = {"owns": [acc.dir], "workdir": comp.workdir, "locked": sorted(self.s.build.locked_tests),
                      "also_allowed": ["reports/"]}
            feedback = ""
            code_guardrails.discard_changes(self.ws, acc.dir)       # no leftovers of an interrupted earlier attempt
            for attempt in (1, 2):
                job = Job(PHASE, "test_writer", "write_acceptance_tests", inputs, AcceptanceSuite, comp.workdir,
                          comp.runtime, feedback=feedback, policy=policy)
                try:
                    suite = self.record(self.worker_for("test_writer").run(job))
                except UsageLimitError as e:
                    self.stop(f"{e}. Resume the run after the limit resets (uv run resume <run_id>).")
                    return
                except PhaseError as e:
                    self.stop(f"Test Writer failed on {key}: {e}")
                    return
                if suite.blocked:
                    self.stop(f"Test Writer blocked on {key}: {suite.blocked_reason or 'no reason given'} "
                              "(answer it, then resume)")
                    return
                for t in suite.tests:           # agents work inside the component folder and often list paths from there
                    if not (self.ws.root / t.file).is_file() and (self.ws.root / comp.workdir / t.file).is_file():
                        t.file = f"{comp.workdir.rstrip('/')}/{t.file}"
                problems = suite.errors(ac_ids, acc.dir)
                problems += [f"{t.file} does not exist" for t in suite.tests if not (self.ws.root / t.file).is_file()]
                with self._git:
                    problems += code_guardrails.check_changes(self.ws, comp, self.profile, {"DV2", "DV3"}, owns=[acc.dir],
                                                              locked=self.s.build.locked_tests)
                if not problems:
                    try:
                        run = self.run_suite(comp, acc)
                    except ServiceError as e:
                        self.stop(f"Acceptance setup for {key} failed: {e}")
                        return
                    broken = environment_failure(run.output) if not run.ok else None
                    if run.ok:
                        problems = [f"`{acc.command}` passes before the feature is built, so the tests prove nothing; "
                                    "make each test check the criterion's real behaviour"]
                    elif broken:
                        problems = [f"`{acc.command}` fails because of the environment, not because the feature is "
                                    f"missing ({broken}). The tests must fail only for missing behaviour: fix the test "
                                    "setup (imports, helpers, scripts) so they reach the app"]
                    else:
                        self.ws.write_text(f"reports/acceptance_{m.id}_{name}_before.txt", run.output[-20000:])
                if not problems:
                    break
                feedback = "\n".join(problems)
                self.ws.write_text(f"reports/acceptance_{m.id}_{name}_attempt{attempt}_rejected.txt", feedback + "\n")
                code_guardrails.discard_changes(self.ws, acc.dir)
            else:
                self.stop(f"Acceptance tests for {key} are not usable after 2 attempts: {feedback[:500]}")
                return
            locked = self._lock(acc.dir)
            self.s.build.acceptance[key] = suite
            self.ws.write_text("docs/test-plan.md", test_plan_markdown(self.s.build.acceptance))
            self.ws.commit(f"Tests: {len(locked)} locked acceptance test file(s) for {key} (test_writer)",
                           [comp.workdir, "tests.lock", "docs", "reports"])
            self._save(f"Tests: {key} written and locked")

    def run_suite(self, comp: Component, acc) -> SandboxResult:
        """Run a component's acceptance suite, inside its throwaway services (e.g. a PostgreSQL)."""
        with acceptance_database(self.sandbox, acc.database) as env:
            host = bool(env)             # the database listens on the host's localhost
            for command in (acc.database.prepare if acc.database else []):
                prep = self.sandbox.run_trusted(comp.runtime, comp.workdir, command, env=env, host_network=host)
                if not prep.ok:
                    raise ServiceError(f"`{command}` failed: {prep.output[-400:]}")
            return self.sandbox.run_trusted(comp.runtime, comp.workdir, acc.command, env=env, host_network=host)

    def run_acceptance(self, m: Milestone) -> str:
        """Run every locked acceptance suite of the milestone's components; the result goes to QA."""
        lines = []
        names = {self.items[w].component for w in m.work_item_ids if w in self.items}
        for name in sorted(names):
            comp = self.profile.components.get(name)
            acc = comp.acceptance if comp else None
            if not acc or not any(p.startswith(acc.dir.rstrip("/") + "/") for p in self.s.build.locked_tests):
                continue
            changed = [p for p, h in self.s.build.locked_tests.items() if p.startswith(acc.dir.rstrip("/") + "/")
                       and (not (self.ws.root / p).is_file()
                            or hashlib.sha256((self.ws.root / p).read_bytes()).hexdigest() != h)]
            try:
                run = self.run_suite(comp, acc)
            except ServiceError as e:
                lines.append(f"- {name}: the acceptance suite could not run: {e}")
                continue
            report = f"reports/acceptance_{m.id}_{name}.txt"
            self.ws.write_text(report, f"$ {acc.command}\nexit {run.exit_code}\n\n{run.output[-20000:]}")
            verdict = "PASSED" if run.ok else f"FAILED (exit {run.exit_code})"
            lines.append(f"- {name}: `{acc.command}` {verdict}; full output in {report}")
            if changed:
                lines.append(f"  LOCKED TESTS CHANGED (blocker): {', '.join(changed)}")
            if not run.ok:
                lines.append("  Last output:\n" + "\n".join("    " + ln for ln in run.output[-1500:].splitlines()))
        return "\n".join(lines) or "(no locked acceptance tests for this milestone)"

    # ---------- merge gate feedback (G5) ----------

    def feedback_round(self, feedback: str) -> None:
        """The developer of record rejected the merge: builders get the feedback, then the last milestone is
        verified (integration, scans, QA, review) again."""
        milestones = [m for m in self.selected_milestones() if any(self.s.build.item(w).status == "done"
                                                                    for w in m.work_item_ids if w in self.items)]
        if not milestones or not feedback.strip():
            return
        m = milestones[-1]
        owner = next(w for w in reversed(m.work_item_ids) if w in self.items and self.s.build.item(w).status == "done")
        bug = Bug(id="G5-001", work_item_id=owner, title="Merge review feedback", severity="major",
                  steps="the developer of record's review of the merge package", expected="the feedback is addressed",
                  actual=feedback)
        self.fix_bugs(m, [bug])
        if self.can_continue():
            self.qa_milestone(m)

    # ---------- QA ----------

    def qa_milestone(self, m: Milestone) -> None:
        mp = self.s.build.milestone(m.id)
        items = [self.items[w] for w in m.work_item_ids if w in self.items]
        done = [i for i in items if self.s.build.item(i.id).status == "done"]
        not_done = [i for i in items if i not in done]
        if not done:
            mp.status = "partial"
            self._save(f"Build: {m.id} nothing buildable, QA skipped")
            return

        session_rounds = 0  # the fix-round limit applies per run, so a resume gets fresh rounds
        previous: frozenset[str] | None = None
        while True:
            mp.qa_rounds += 1
            session_rounds += 1
            # Integrate (7) and Verify (8): deterministic checks first, then the QA agent reads their results.
            ver = verify.VerifyResult()
            if self.cfg.verify:
                ver.extend(verify.integrate(self, m, items, mp.qa_rounds))
                ver.extend(verify.scans(self, m, items, mp.qa_rounds))
                self.ws.write_text("docs/integration-report.md", self.integration_markdown(m, mp.qa_rounds, ver))
                mp.evidence = ver.evidence
            report = self._qa(m, done, not_done, ver.text())
            if report is None:
                self.stop(f"QA agent failed on {m.id}")
                break
            blocking_ver = [b for b in ver.bugs if b.severity in ("blocker", "major")]
            report.bugs += [b for b in ver.bugs if b.id not in {x.id for x in report.bugs}]
            report.passed = report.passed and not blocking_ver            # "passed" needs the reports behind it
            mp.qa_reports.append(report)
            self.ws.write_text(f"reports/qa_{m.id}_round{mp.qa_rounds}.md", report.to_markdown())
            self.ws.commit(f"QA {m.id} round {mp.qa_rounds}: {'passed' if report.passed else 'failed'}")
            bugs = report.blocking_bugs()
            if not bugs and self.cfg.review:                               # Review (9): only clean code is reviewed
                review = self._review(m, done, ver.text(), report)
                if review is None:
                    self.stop(f"Code Reviewer failed on {m.id}")
                    break
                mp.reviews.append(review)
                self.ws.write_text(f"reports/review_{m.id}_round{mp.qa_rounds}.md", review.to_markdown())
                self.ws.write_text("docs/review.md", self.review_markdown())
                self.ws.commit(f"Review {m.id} round {mp.qa_rounds}: {len(review.actionable())} blocking finding(s)")
                bugs = review.bugs()
                mp.open_findings = [f"{f.id} [{f.lens}/{f.severity}/{f.confidence}] {f.title} ({f.work_item_id})"
                                    for f in review.findings if f not in review.actionable()]
            if not bugs:
                mp.status = "done" if not not_done else "partial"
                break
            now = verify.signature(bugs)
            if previous is not None and now == previous:
                mp.status = "failed"
                self.stop(f"{m.id}: the last fix round changed nothing ({len(bugs)} blocking finding(s) are the same); "
                          f"a person needs to look: see reports/qa_{m.id}_round{mp.qa_rounds}.md")
                break
            previous = now
            if session_rounds > self.cfg.qa_fix_rounds:
                mp.status = "failed"
                self.stop(f"{m.id}: {len(bugs)} blocking bug(s) remain after {self.cfg.qa_fix_rounds} fix rounds; "
                          f"see reports/qa_{m.id}_round{mp.qa_rounds}.md")
                break
            self.fix_bugs(m, bugs)
            if not self.can_continue():
                break
        self._save(f"Build: {m.id} {mp.status}")

    def integration_markdown(self, m: Milestone, round_no: int, ver: "verify.VerifyResult") -> str:
        head = self.ws.doc_header("pipeline (deterministic checks)", [])
        bugs = "\n".join(f"- {b.id} [{b.severity}] {b.title} ({b.work_item_id})" for b in ver.bugs) or "None."
        files = "\n".join(f"- {e}" for e in ver.evidence) or "- (none)"
        return (f"{head}# Integration report: {m.id}, round {round_no}\n\n{ver.text()}\n\n## Findings\n{bugs}\n\n"
                f"## Raw results\n{files}\n")

    def review_markdown(self) -> str:
        parts = [f"{self.ws.doc_header('code_reviewer', [])}# Code review\n"]
        for mid, mp in self.s.build.milestones.items():
            if mp.reviews:
                parts.append(f"\n---\n\n{mp.reviews[-1].to_markdown()}")
        return "\n".join(parts) + "\n"

    def _review(self, m: Milestone, done: list[WorkItem], verification: str, qa: QAReport) -> ReviewReport | None:
        diff = self.milestone_diff(m)
        inputs = {
            "milestone": f"{m.id} {m.name}: {m.goal}",
            "diff_stat": diff,
            "items": "\n".join(f"- {i.id} [{i.component}] {i.title}; owns: {', '.join(i.owns) or '-'}" for i in done),
            "stories": self.stories_text(sorted({sid for i in done for sid in i.story_ids})),
            "docs_dir": str(self.ws.root / "docs"),
            "verification": f"{verification}\nQA: {qa.summary}",
            "revision_notes": "",
        }
        ids = [i.id for i in done]
        runtimes = sorted({self.component(i).runtime for i in done if self.component(i).runtime})
        job = Job(PHASE, "code_reviewer", "review_milestone", inputs, ReviewReport, ".", runtimes[0] if runtimes else None,
                  runtimes[1:], policy={"read_only": True})
        try:
            review = self.record(self.worker_for("code_reviewer").run(job))
            errors = review.errors(ids)
            if errors:                                       # one retry with the reasons
                job.feedback = "\n".join(errors)
                review = self.record(self.worker_for("code_reviewer").run(job))
            return review
        except UsageLimitError as e:
            self.stop(f"{e}. Resume the run after the limit resets (uv run resume <run_id>).")
            return None
        except PhaseError as e:
            log.error("%s", e)
            return None

    def milestone_diff(self, m: Milestone) -> str:
        """The milestone's changed files (commits of its tasks), for the reviewer to read."""
        from git import Repo

        shas = [self.s.build.item(w).commit for w in m.work_item_ids if w in self.items and self.s.build.item(w).commit]
        if not shas:
            return "(no task commits recorded)"
        repo = Repo(self.ws.root)
        files: dict[str, str] = {}
        for sha in shas:
            for line in repo.git.show("--numstat", "--format=", sha).splitlines():
                add, rem, path = (line.split("\t") + ["", "", ""])[:3]
                if path and not path.startswith(("docs/", "reports/", "gates/")) and path != "tests.lock":   # code only
                    files[path] = f"+{add} -{rem}"
        return "\n".join(f"- {p} ({stat})" for p, stat in sorted(files.items())) or "(no changes)"

    def _qa(self, m: Milestone, done: list[WorkItem], not_done: list[WorkItem], verification: str = "") -> QAReport | None:
        runtimes = sorted({self.component(i).runtime for i in done if self.component(i).runtime})
        inputs = {
            "milestone": f"{m.id} {m.name}: {m.goal}",
            "milestone_id": m.id,
            "items": "\n".join(f"- {i.id} [{i.component}] {i.title}: {self.s.build.item(i.id).summary[:300]}" for i in done),
            "not_built": "\n".join(f"- {i.id} {i.title}: {self.s.build.item(i.id).reason}" for i in not_done) or "(none)",
            "stories": self.stories_text(sorted({sid for i in done for sid in i.story_ids})),
            "docs_dir": str(self.ws.root / "docs"),
            "acceptance": self._acceptance_text.get(m.id, "(no locked acceptance tests for this milestone)"),
            "verification": verification or "(not run: build.verify is off)",
        }
        job = Job(PHASE, "qa_engineer", "qa_milestone", inputs, QAReport, ".", runtimes[0] if runtimes else None,
                  runtimes[1:], policy={"read_only": True})
        try:
            report = self.record(self.worker_for("qa_engineer").run(job))
            errors = agent_guardrails.report_errors(report, [i.id for i in done], self.cfg.guard_rules)
            if errors:  # one retry with the reasons
                job.feedback = "\n".join(errors)
                report = self.record(self.worker_for("qa_engineer").run(job))
                if agent_guardrails.report_errors(report, [i.id for i in done], self.cfg.guard_rules):
                    log.warning("QA report still inconsistent; deriving the verdict from its bugs")
                    report.passed = not report.blocking_bugs()
            return report
        except UsageLimitError as e:
            self.stop(f"{e}. Resume the run after the limit resets (uv run resume <run_id>).")
            return None
        except PhaseError as e:
            log.error("%s", e)
            return None

    def fix_bugs(self, m: Milestone, bugs: list[Bug]) -> None:
        by_item: dict[str, list[Bug]] = {}
        for b in bugs:
            by_item.setdefault(b.work_item_id if b.work_item_id in self.items else m.work_item_ids[0], []).append(b)
        for wid, item_bugs in by_item.items():
            if not self.can_continue():
                return
            item = self.items[wid]
            comp = self.component(item)
            text = "QA found these bugs. Fix them and add or update tests that prove the fix:\n" + "\n".join(
                f"- {b.id} [{b.severity}] {b.title}\n  Steps: {b.steps}\n  Expected: {b.expected}\n  Actual: {b.actual}"
                for b in item_bugs
            )
            outcome, _, detail = self._work_until_green(m, item, comp, "fix_work_item", text)
            if outcome == "done":
                self.ws.commit(f"{wid}: fix {', '.join(b.id for b in item_bugs)}")
            else:
                log.warning("Bug fix on %s did not complete: %s", wid, detail[:300])
            self._save(f"Build: {wid} bug fixes")
