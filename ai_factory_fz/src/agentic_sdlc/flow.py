"""SDLCFlow: runs the SDLC phases in order, with human gates, revision loops and checkpoints.

Every phase is idempotent: it skips work whose artifact is already in the state.
That is what makes resume work: reload state.json and kick off the flow again.
Rejecting a gate clears that artifact, so the next pass rewrites it using the feedback.
"""

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from crewai.flow.flow import Flow, listen, or_, router, start
from pydantic import PrivateAttr

from agentic_sdlc.build.coders import Worker, make_worker
from agentic_sdlc.build.coverage import markdown as coverage_markdown, uncovered
from agentic_sdlc.build.loop import BuildConfig, Builder
from agentic_sdlc.build.sync import sync_build_with_backlog
from agentic_sdlc import preflight
from agentic_sdlc import intake
from agentic_sdlc.artifacts.architecture import CONTRACT_PATH
from agentic_sdlc.artifacts.plan import to_backlog
from agentic_sdlc.artifacts.review import package_markdown
from agentic_sdlc.crews import design, discovery, planning
from agentic_sdlc.design import mockups as mockup_kit
from agentic_sdlc.guardrails import agents as agent_guardrails
from agentic_sdlc.guardrails import architecture as architecture_guardrails
from agentic_sdlc.crews.base import TaskResult, TaskRunner
from agentic_sdlc.gates import files as gate_files
from agentic_sdlc.gates.human import InputFn, gate_mode, request_approval
from agentic_sdlc.registry.agents import AgentRegistry
from agentic_sdlc.registry.profiles import Profile
from agentic_sdlc.release.device import AndroidToolchain, Emulator
from agentic_sdlc.release.releaser import Releaser
from agentic_sdlc.release.staging import Staging
from agentic_sdlc.scope import Scope
from agentic_sdlc.settings import load_config
from agentic_sdlc.state import GateDecision, ProjectState
from agentic_sdlc.tools.registry import build_tool_resolver
from agentic_sdlc.env_toolchain import apply_toolchain_path
from agentic_sdlc.tools.sandbox_exec import SandboxRunner, sandbox_mode
from agentic_sdlc.workspace import Workspace

log = logging.getLogger(__name__)


@dataclass
class Deps:
    workspace: Workspace
    profile: Profile
    runner: TaskRunner
    pipeline: dict[str, Any]
    input_fn: InputFn = input
    sandbox: SandboxRunner | None = None
    worker_for: Callable[[str], Worker] | None = None
    staging: Staging | None = None
    emulator: Emulator | None = None
    preflight: Callable[[], list[str]] | None = None   # machine checks; problems stop the run early
    extra: dict[str, Any] = field(default_factory=dict)


def default_deps(state: ProjectState) -> Deps:
    apply_toolchain_path()
    profile = Profile.load(state.profile)
    workspace = Workspace.create(state.run_id)
    pipeline = load_config(state.pipeline)
    build_cfg = pipeline.get("build", {}) or {}
    sandbox = SandboxRunner(workspace, profile.sandbox, sandbox_mode(build_cfg.get("sandbox", "docker")))
    agents = AgentRegistry.from_config(profile, build_tool_resolver(workspace, sandbox), pipeline.get("models"))
    runner = TaskRunner(agents, workspace=workspace)
    timeout = build_cfg.get("agent_timeout_s", 3600)
    release_cfg = pipeline.get("release", {}) or {}
    staging = Staging(workspace, profile, sandbox, port=release_cfg.get("staging_port", 3100),
                      startup_timeout_s=release_cfg.get("startup_timeout_s", 90))
    emulator = None
    if profile.device and (release_cfg.get("device") or {}).get("enabled"):
        device_rt = profile.components[profile.device.app_component].runtime
        emulator = Emulator(AndroidToolchain(profile.device), sandbox, workspace, runtime=device_rt,
                            boot_timeout_s=(release_cfg.get("device") or {}).get("boot_timeout_s", 300))
    return Deps(
        workspace=workspace, profile=profile, runner=runner, pipeline=pipeline, sandbox=sandbox, staging=staging,
        emulator=emulator,
        worker_for=lambda agent_key: make_worker(agent_key, agents, runner.tasks, workspace, sandbox, timeout),
        preflight=lambda: preflight.check(pipeline, profile, sandbox, agents.models),
    )


class SDLCFlow(Flow[ProjectState]):
    _deps_factory: Callable[[ProjectState], Deps] | None = PrivateAttr(default=None)
    _deps: Deps | None = PrivateAttr(default=None)
    _restore_json: str | None = PrivateAttr(default=None)

    def __init__(self, deps_factory: Callable[[ProjectState], Deps] | None = None, restore_json: str | None = None, **data: Any):
        super().__init__(**data)
        self._deps_factory = deps_factory
        self._restore_json = restore_json

    # ---------- helpers ----------

    @property
    def deps(self) -> Deps:
        assert self._deps is not None, "init_run has not run"
        return self._deps

    def _checkpoint(self, message: str, paths: list[str] | tuple[str, ...] | None = None) -> None:
        """Save state and status, and commit (only `paths` while builders work in parallel)."""
        self.deps.workspace.save_state(self.state)
        self.deps.workspace.write_text("status.md", self._status_markdown(message))
        self.deps.workspace.commit(message, paths)

    def _status_markdown(self, last_step: str) -> str:
        """status.md: the run at a glance, rewritten at every checkpoint."""
        s = self.state
        lines = [f"# Run {s.run_id}", "", f"Status: **{s.status}**" + (f" ({s.stop_reason})" if s.stop_reason else ""),
                 f"Risk tier: {(s.risk_tier or self.deps.pipeline.get('risk_tier') or 'M').upper()}",
                 f"Last step: {last_step}", "", "## Gates", "| Gate | Decision | By | When |", "|---|---|---|---|"]
        latest: dict[str, GateDecision] = {}
        for d in s.gate_history:
            latest[d.gate] = d
        for gate, d in latest.items():
            verdict = "approved" if d.approved else ("reopened" if d.decided_by == "system" else "rejected")
            lines.append(f"| {d.gate_id or gate_files.gate_id(gate)} {gate} | {verdict} | "
                         f"{d.approver or d.decided_by} | {d.decided_at} |")
        items = s.build.items.values()
        if items:
            counts: dict[str, int] = {}
            for p in items:
                counts[p.status] = counts.get(p.status, 0) + 1
            lines += ["", "## Build", ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))]
        if (self.deps.workspace.root / "blocked.md").exists():
            lines += ["", "Blocked work needs a person: see blocked.md"]
        return "\n".join(lines) + "\n"

    def _write_blocked(self) -> bool:
        """blocked.md lists work agents could not do (missing input, toolchain, ambiguity). True if any."""
        blocked = [(wid, p.reason) for wid, p in self.state.build.items.items() if p.status == "blocked"]
        path = self.deps.workspace.root / "blocked.md"
        if not blocked:
            path.unlink(missing_ok=True)
            return False
        lines = ["# Blocked", "", "These work items could not be done. Resolve the cause (answer the question, "
                 "provide the input or toolchain), then `uv run resume " + self.state.run_id + "`.", ""]
        lines += [f"- **{wid}**: {reason or 'blocked'}" for wid, reason in blocked]
        self.deps.workspace.write_text("blocked.md", "\n".join(lines) + "\n")
        return True

    def _record(self, result: TaskResult) -> Any:
        self.state.usage.append(result.usage)
        return result.artifact

    @property
    def _scope(self) -> Scope:
        from agentic_sdlc.release.contract import strip_prefix

        rel = self.deps.profile.release
        infra = tuple(strip_prefix(p, rel.api_prefix) for p in (rel.health_path, rel.openapi_json_path) if p)
        return Scope.from_pipeline(self.deps.pipeline, infra_paths=infra)

    def _phase_enabled(self, phase: str) -> bool:
        return bool(self.deps.pipeline.get("phases", {}).get(phase, False))

    def _stop(self, reason: str) -> None:
        if self.state.status == "stopped":
            log.warning("Also: %s", reason)  # keep the first (root-cause) reason
            return
        self.state.status = "stopped"
        self.state.stop_reason = reason
        log.warning("Run stopped: %s", reason)

    def _can_continue(self) -> bool:
        return self.state.status == "running"

    @property
    def _risk_tier(self) -> str:
        return (self.state.risk_tier or self.deps.pipeline.get("risk_tier") or "M").upper()

    def _latest_approval(self, gate: str) -> GateDecision | None:
        latest = [d for d in self.state.gate_history if d.gate == gate]
        return latest[-1] if latest and latest[-1].approved else None

    def _gate(self, gate: str, summary: str, docs: list[str], discard: Callable[[], None]) -> str:
        """Returns '<gate>_approved', '<gate>_rejected' or 'stopped'.

        An approval holds only while the approved documents keep their hashes; a change asks again.
        Every decision is written to gates/<G>.gate. In async mode the run stops and waits for
        `uv run approve`. On rejection the drafts are kept in docs/history/ and `discard` drops the
        rejected artifact, so the next pass (or a resume) rewrites it with the feedback.
        """
        if self.state.status != "running":
            return "stopped"
        root, gid = self.deps.workspace.root, gate_files.gate_id(gate)
        approved = self._latest_approval(gate)
        if approved:
            changed = gate_files.changed_since(root, approved.artifact_hashes)
            if not changed:
                return f"{gate}_approved"
            self.state.gate_history.append(GateDecision(
                gate=gate, gate_id=gid, approved=False, decided_by="system",
                feedback=f"Changed after approval: {', '.join(changed)}"))
        hashes = gate_files.file_hashes(root, docs)
        cfg = self.deps.pipeline.get("gates", {}) or {}
        if not cfg.get(gate, True):
            decision = GateDecision(gate=gate, approved=True, decided_by="config")
        elif gate == "architecture" and self._risk_tier == "L" and cfg.get("architecture_waiver_at_tier_l"):
            decision = GateDecision(gate=gate, approved=True, decided_by="waiver",
                                    feedback="Design gate waived at risk tier L (pipeline gates.architecture_waiver_at_tier_l)")
        else:
            mode = gate_mode(self.deps.pipeline.get("gate_mode", "console"),
                             demo=bool(self.deps.pipeline.get("demo")), risk_tier=self._risk_tier)
            if mode == "async":
                decision = gate_files.read_decision(root, gid)
                if decision is None or decision.artifact_hashes != hashes:
                    gate_files.write_pending(root, gate, summary, docs, hashes)
                    self._stop(f"Waiting for {gid} ({gate}): a person approves with "
                               f"`uv run approve {self.state.run_id} {gid} --as \"Your Name\"`, "
                               f"then `uv run resume {self.state.run_id}`")
                    self._checkpoint(f"Gate {gid}: waiting for approval")
                    return "stopped"
                gate_files.archive_decision(root, gid)
            else:
                decision = request_approval(gate, summary, [root / d for d in docs], mode, self.deps.input_fn,
                                            need_risk_note=gate == "merge")
        decision.gate_id, decision.artifact_hashes = gid, hashes
        gate_files.write_decision(root, decision)        # gates/<G>.gate holds the latest decision
        self.state.gate_history.append(decision)
        if decision.approved:
            self._checkpoint(f"Gate {gid} {gate}: approved")
            return f"{gate}_approved"
        gate_files.version_documents(root, docs, gate)
        discard()
        limit = self.deps.pipeline.get("limits", {}).get("gate_rejections", 3)
        if len(self.state.rejections(gate)) >= limit:
            self._stop(f"Gate '{gate}' rejected {limit} times")
            self._checkpoint(f"Gate {gid} {gate}: rejected, run stopped")
            return "stopped"
        self._checkpoint(f"Gate {gid} {gate}: rejected")
        return f"{gate}_rejected"

    # ---------- flow ----------

    @start()
    def init_run(self) -> None:
        if self._restore_json:
            restored = ProjectState.model_validate_json(self._restore_json)
            for name in ProjectState.model_fields:
                if name != "id":
                    setattr(self.state, name, getattr(restored, name))
            # Resuming continues from where the run stopped, or runs phases enabled since it completed.
            self.state.status, self.state.stop_reason = "running", ""
        if not self.state.run_id:
            raise ValueError("run_id is required")
        self._deps = (self._deps_factory or default_deps)(self.state)
        self._checkpoint("Run started")
        if self.deps.preflight:
            problems = self.deps.preflight()
            print(preflight.report(problems), flush=True)
            if problems:
                self._stop(preflight.report(problems) + f"\nThen run: uv run resume {self.state.run_id}")

    def _intent(self) -> tuple[dict, str]:
        return intake.parse(self.state.brief)

    def _write_intent(self) -> None:
        """Step 0: the Product Owner's request (brief + front matter) becomes docs/intent.md."""
        meta, body = self._intent()
        if not self.state.risk_tier and meta.get("risk_tier"):
            self.state.risk_tier = str(meta["risk_tier"]).upper()
        if not self.state.product_owner and meta.get("product_owner"):
            self.state.product_owner = str(meta["product_owner"])
        ws = self.deps.workspace
        if not (ws.root / "docs/intent.md").exists():
            ws.write_text("docs/intent.md", intake.intent_markdown(meta, body, self._risk_tier))

    @listen(init_run)
    def discovery_phase(self) -> None:
        """Specify: the Business analyst writes the spec from the intent."""
        if self.state.status == "running":
            self._write_intent()
        if not self._can_continue() or not self._phase_enabled("discovery"):
            return
        self._write_prd()

    def _write_prd(self) -> None:
        if self.state.prd is not None or not self._can_continue():
            return
        _, body = self._intent()
        owner = f"\n\nProduct Owner: {self.state.product_owner}" if self.state.product_owner else ""
        self.state.prd = self._record(discovery.write_spec(
            self.deps.runner, f"{body.strip()}{owner}\nRisk tier: {self._risk_tier}",
            self.deps.profile.stack_summary(), self.state.revision_notes("prd"), self._scope))
        self.deps.workspace.save_artifact("spec", self.state.prd, agent="business_analyst", inputs=["docs/intent.md"])
        self._checkpoint("Specify: spec")

    @router(or_(discovery_phase, "revise_prd"))
    def prd_gate(self) -> Literal["prd_approved", "prd_rejected", "stopped"]:
        """G1 spec approval (customer, through the Product Owner)."""
        if self.state.prd is None:
            if self.state.status == "running":
                self._stop("No spec was produced (is the discovery phase disabled?)")
            return "stopped"
        prd = self.state.prd
        summary = (f"Spec '{prd.title}': {len(prd.user_stories)} user stories ({len(prd.must_have_ids())} must-have), "
                   f"{len(prd.criteria())} acceptance criteria.")
        if prd.open_questions:
            summary += ("\nOpen questions for the Product Owner (answer them as your feedback to get a revised spec):\n"
                        + "\n".join(f"  - {q}" for q in prd.open_questions))
        return self._gate("prd", summary, ["docs/spec.md", "docs/intent.md"], lambda: setattr(self.state, "prd", None))

    @listen("prd_rejected")
    def revise_prd(self) -> None:
        self._write_prd()

    @listen("prd_approved")
    def solution_phase(self) -> None:
        """Design: the Architect writes the design (options, contract, ADRs), then breaks it into the WBS."""
        self._write_solution()

    def _write_solution(self) -> None:
        ws, notes = self.deps.workspace, self.state.revision_notes("architecture")
        if not self._phase_enabled("planning"):
            return
        if self.state.architecture is None and self._can_continue():
            arch = self._record(planning.design_architecture(
                self.deps.runner, self.state.prd, self.deps.profile.stack_summary(),
                self.deps.profile.domain_entities, notes, self._scope,
                guardrails=architecture_guardrails.checker(self.deps.profile, self.deps.pipeline),
                database=self.deps.profile.database, layout=self.deps.profile.layout_summary(),
            ))
            self.state.architecture = arch
            ws.save_artifact("design", arch, agent="architect", inputs=["docs/spec.md"])
            ws.write_text(CONTRACT_PATH, arch.openapi_yaml)
            if self.deps.profile.database:
                ws.write_text("docs/schema.prisma", arch.prisma_schema)
            for old in ws.root.glob("docs/adr-*.md"):
                old.unlink()
            for adr in arch.adrs:
                ws.write_text(f"docs/{adr.id.lower()}.md", ws.doc_header("architect", ["docs/spec.md"]) +
                              f"# {adr.id} {adr.title}\n\n## Context\n{adr.context}\n\n## Decision\n{adr.decision}\n\n"
                              f"## Consequences\n{adr.consequences}\n")
            self._checkpoint("Design: design, API contract, ADRs")
        if self.state.architecture is not None and self.state.wbs is None and self._can_continue():
            profile = self.deps.profile
            workdirs = {name: c.workdir for name, c in profile.components.items()}
            allowed = {name: profile.sandbox.allowed_commands.get(c.runtime, []) if c.runtime else []
                       for name, c in profile.components.items()}
            self.state.wbs = self._record(planning.design_wbs(
                self.deps.runner, self.state.prd, self.state.architecture, workdirs, profile.stack_summary(),
                notes, layout=profile.layout_summary(), allowed=allowed, entry_points=profile.entry_points(), scope=self._scope))
            ws.save_artifact("wbs", self.state.wbs, agent="architect", inputs=["docs/spec.md", "docs/design.md"])
            self._checkpoint("Design: work breakdown structure")

    @router(or_(solution_phase, "revise_solution"))
    def architecture_gate(self) -> Literal["architecture_approved", "architecture_rejected", "stopped"]:
        """G2 design approval (human architect; may be waived at risk tier L)."""
        if self.state.architecture is None or self.state.wbs is None:
            if self.state.status == "running":
                self._stop("No design or WBS was produced (is the planning phase disabled?)")
            return "stopped"
        a, w = self.state.architecture, self.state.wbs
        summary = (
            f"Design: option '{a.recommended_option or '-'}' of {len(a.options)}; {len(a.backend_modules)} backend modules, "
            f"{len(a.operations())} API operations, {len(a.data_models())} data models, {len(a.adrs)} ADRs. "
            f"WBS: {len(w.tasks)} tasks in {len(w.packages)} work packages."
        )
        docs = ["docs/design.md", CONTRACT_PATH, "docs/wbs.md"]
        if self.deps.profile.database:
            docs.append("docs/schema.prisma")
        docs += sorted(str(p.relative_to(self.deps.workspace.root)) for p in self.deps.workspace.root.glob("docs/adr-*.md"))

        def discard() -> None:
            # A change to the design changes everything planned on it.
            self.state.architecture = self.state.wbs = self.state.plan = self.state.backlog = self.state.design = None
            self.state.mockups = []

        return self._gate("architecture", summary, docs, discard)

    @listen("architecture_rejected")
    def revise_solution(self) -> None:
        self._write_solution()

    @listen("architecture_approved")
    def plan_phase(self) -> None:
        """Plan: the Project manager sequences and estimates the WBS."""
        self._write_plan()

    def _write_plan(self) -> None:
        if (not self._phase_enabled("planning") or self.state.plan is not None or self.state.wbs is None
                or not self._can_continue()):
            return
        ws, wbs = self.deps.workspace, self.state.wbs
        self.state.plan = self._record(planning.plan_delivery(
            self.deps.runner, self.state.prd, wbs, self.state.revision_notes("estimate"), self._scope,
            entry_points=self.deps.profile.entry_points()))
        self.state.backlog = to_backlog(wbs, self.state.plan)
        header = ws.doc_header("project_manager", ["docs/spec.md", "docs/wbs.md"])
        ws.write_text("docs/plan.md", header + self.state.plan.to_markdown(wbs))
        ws.write_text("docs/estimates.md", header + self.state.plan.estimates_markdown(wbs))
        ws.write_text("docs/plan.json", self.state.plan.model_dump_json(indent=2))
        ws.write_text("docs/backlog.json", self.state.backlog.model_dump_json(indent=2))
        for component, paths in wbs.ownership().items():
            ws.write_text(f"docs/ownership/{component}.txt", "\n".join(paths) + "\n")
        self._checkpoint("Plan: delivery plan, estimates and ownership")

    @router(or_(plan_phase, "revise_plan"))
    def estimate_gate(self) -> Literal["estimate_approved", "estimate_rejected", "stopped"]:
        """G3 estimate approval (customer, through the Product Owner)."""
        if self.state.plan is None or self.state.backlog is None:
            if self.state.status == "running":
                self._stop("No delivery plan was produced (is the planning phase disabled?)")
            return "stopped"
        b = self.state.backlog
        summary = (f"Plan: {len(b.milestones)} milestones, {len(b.work_items)} tasks, {b.total_points()} points; "
                   f"critical path {' → '.join(b.critical_path())}.")

        def discard() -> None:
            self.state.plan = self.state.backlog = None

        return self._gate("estimate", summary, ["docs/plan.md", "docs/estimates.md"], discard)

    @listen("estimate_rejected")
    def revise_plan(self) -> None:
        self._write_plan()

    @listen("estimate_approved")
    def ui_phase(self) -> None:
        """UI/UX: screens, states and prototypes (mockups) from the spec, design and plan."""
        self._write_ui()

    def _write_ui(self) -> None:
        ws, notes = self.deps.workspace, self.state.revision_notes("ui")
        if (self._phase_enabled("design") and self.state.design is None and self.state.architecture is not None
                and self._can_continue()):
            self.state.design = self._record(design.design_ui(
                self.deps.runner, self.state.prd, self.state.architecture, self._scope, notes,
                agent_guardrails.enabled(self.deps.pipeline)))
            ws.save_artifact("ui-design", self.state.design, agent="ui_ux_designer",
                             inputs=["docs/spec.md", "docs/design.md", "docs/plan.md"])
            self._checkpoint("UI/UX: design system and screen specs")
        self._write_mockups(notes)

    def _write_mockups(self, notes: str) -> None:
        """One designer call per screen (resumable), then the HTML pages, PNGs and gallery."""
        cfg = self.deps.pipeline.get("design") or {}
        spec = self.state.design
        if not cfg.get("mockups") or spec is None or not self._phase_enabled("design"):
            return
        wanted = cfg.get("mockup_states", mockup_kit.DEFAULT_STATES)
        viewport = tuple(cfg.get("viewport", mockup_kit.DEFAULT_VIEWPORT))
        limit = cfg.get("max_states_per_screen", 4)
        done = {m.screen_id for m in self.state.mockups}
        drew = False
        for screen in spec.screens:
            if screen.id in done or not self._can_continue():
                continue
            drew = True
            states = mockup_kit.states_to_draw(screen, wanted, limit)
            self.state.mockups.append(self._record(design.design_mockups(self.deps.runner, spec, screen, states, notes,
                                                                         prd=self.state.prd)))
            self._checkpoint(f"Design: mockups {screen.id}")
        ws = self.deps.workspace
        if {m.screen_id for m in self.state.mockups} >= {s.id for s in spec.screens} \
                and (drew or not (ws.root / mockup_kit.MOCKUP_DIR / "index.html").exists()):
            shutil.rmtree(ws.root / mockup_kit.MOCKUP_DIR, ignore_errors=True)   # drop pages of a rejected design
            counts = mockup_kit.write_mockups(ws, spec, self.state.mockups, viewport)
            if counts["pages"] and not counts["pngs"]:
                log.warning("Mockup PNGs were not rendered (no Chrome/Chromium found); the HTML pages are in " + mockup_kit.MOCKUP_DIR + "")
            self._checkpoint(f"Design: {counts['pages']} mockup pages, {counts['pngs']} PNGs")

    @router(or_(ui_phase, "revise_ui"))
    def ui_gate(self) -> Literal["ui_approved", "ui_rejected", "stopped"]:
        """G4 UI design approval (customer, through the Product Owner)."""
        if self.state.status != "running":
            return "stopped"
        if not self._phase_enabled("design"):
            return "ui_approved"
        if self.state.design is None:
            self._stop("No UI design was produced")
            return "stopped"
        d = self.state.design
        states = sum(len(m.mockups) for m in self.state.mockups)
        summary = (f"UI: {len(d.screens)} screens, {len(d.colors)} colour tokens"
                   + (f", {states} screen-state prototypes" if states else "") + ".")
        docs = ["docs/ui-design.md"] + ([f"{mockup_kit.MOCKUP_DIR}/index.html"] if self.state.mockups else [])

        def discard() -> None:
            self.state.design = None
            self.state.mockups = []

        return self._gate("ui", summary, docs, discard)

    @listen("ui_rejected")
    def revise_ui(self) -> None:
        self._write_ui()

    @router("ui_approved")
    def after_solution(self) -> Literal["build_requested", "release_requested", "run_finished", "stopped"]:
        if self.state.status != "running":
            return "stopped"
        if self._phase_enabled("build"):
            return "build_requested"
        return self._after_build()

    @router("build_requested")
    def build_phase(self) -> Literal["build_done", "stopped"]:
        sync_build_with_backlog(self.state)
        done_before = {w for w, p in self.state.build.items.items() if p.status == "done"}
        if self._can_continue():
            self._builder().run()
        done_now = {w for w, p in self.state.build.items.items() if p.status == "done"}
        if done_now - done_before and (self.state.release.deployment or self.state.release.rounds):
            # New features since the last release verification: verify again, with a smoke
            # suite that covers them.
            self.state.reopen_release("New build work since the last release verification")
            self.state.release.smoke_suite = None
            self.state.release.device_suite = None
            self._checkpoint("Release: re-verification needed after new build work")
        if self._write_blocked() and self.state.status == "running" \
                and (self.deps.pipeline.get("limits", {}) or {}).get("stop_on_blocked", True):
            self._stop("Blocked work needs a person: see blocked.md")
            self._checkpoint("Build: blocked work, run stopped")
        if self.state.status != "running":
            return "stopped"
        return "build_done"

    def _builder(self) -> Builder:
        return Builder(
            state=self.state, workspace=self.deps.workspace, profile=self.deps.profile, sandbox=self.deps.sandbox,
            worker_for=self.deps.worker_for, config=BuildConfig.from_pipeline(self.deps.pipeline),
            record=self._record, checkpoint=self._checkpoint, can_continue=self._can_continue, stop=self._stop)

    def _write_package(self) -> None:
        """docs/package.md and docs/review.md: what the developer of record reads at the merge gate (G5)."""
        ws, b = self.deps.workspace, self.state.backlog
        milestones = []
        for m in b.milestones:
            mp = self.state.build.milestones.get(m.id)
            if mp is None:
                continue
            evidence = [f"reports/qa_{m.id}_round{mp.qa_rounds}.md"] + list(mp.evidence)
            evidence += [f"reports/review_{m.id}_round{mp.qa_rounds}.md"] if mp.reviews else []
            evidence += [p for p in sorted(str(x.relative_to(ws.root)) for x in (ws.root / "reports").glob(f"acceptance_{m.id}_*"))]
            milestones.append({
                "id": m.id, "name": m.name, "status": mp.status, "qa_rounds": mp.qa_rounds, "evidence": evidence,
                "open": mp.open_findings,
                "tasks": [{"id": w, "title": next((i.title for i in b.work_items if i.id == w), ""),
                           "status": self.state.build.item(w).status, "commit": (self.state.build.item(w).commit or "")[:8]}
                          for w in m.work_item_ids]})
        ws.write_text("docs/package.md", ws.doc_header("code_reviewer", ["docs/review.md"]) + package_markdown(
            self.state.run_id, milestones, coverage_markdown(uncovered(self.state.prd, self.state.build.acceptance))
            + ("\n### Locked tests repaired by the Test Writer during the build\n\n"
               + "\n".join(f"- {r}" for r in self.state.build.test_repairs) + "\n" if self.state.build.test_repairs else "")))

    @router(or_("build_done", "revise_merge"))
    def merge_gate(self) -> Literal["merge_approved", "merge_rejected", "stopped"]:
        """G5 merge approval: the developer of record reads the package and writes a risk note in their own words."""
        if self.state.status != "running":
            return "stopped"
        if not (self.deps.pipeline.get("gates") or {}).get("merge", False):
            return "merge_approved"
        self._write_package()
        built = sum(1 for p in self.state.build.items.values() if p.status == "done")
        gaps = uncovered(self.state.prd, self.state.build.acceptance)
        summary = f"Merge: {built} of {len(self.state.build.items)} tasks built, every milestone QA'd and code-reviewed."
        if gaps:
            summary += (f"\nWARNING: {len(gaps)} acceptance criteria have no locked test "
                        f"({', '.join(a for a, _, _ in gaps)}); they cannot be proven at release.")
        docs = ["docs/package.md"] + (["docs/review.md"] if (self.deps.workspace.root / "docs/review.md").exists() else [])
        docs += ["docs/integration-report.md"] if (self.deps.workspace.root / "docs/integration-report.md").exists() else []
        docs += ["docs/test-plan.md"] if (self.deps.workspace.root / "docs/test-plan.md").exists() else []
        return self._gate("merge", summary, docs, lambda: None)

    @listen("merge_rejected")
    def revise_merge(self) -> None:
        """The developer of record sent the code back: builders fix it, then the milestone is verified again."""
        feedback = self.state.revision_notes("merge")
        self._builder().feedback_round(feedback)

    @router("merge_approved")
    def after_merge(self) -> Literal["release_requested", "run_finished", "stopped"]:
        if self.state.status != "running":
            return "stopped"
        return self._after_build()

    def _after_build(self) -> Literal["release_requested", "run_finished", "stopped"]:
        if self._phase_enabled("release"):
            if not any(p.status == "done" for p in self.state.build.items.values()):
                if self.state.build.items:
                    self._stop("Nothing could be built, so there is nothing to release: every work item is "
                               "blocked or failed. See the Build table in reports/run_summary.md for the reasons, "
                               "fix the cause and resume.")
                else:
                    self._stop("The release phase needs built work items; enable the build phase and resume")
                return "stopped"
            return "release_requested"
        return "run_finished"

    def _releaser(self) -> Releaser:
        return Releaser(
            state=self.state,
            workspace=self.deps.workspace,
            profile=self.deps.profile,
            sandbox=self.deps.sandbox,
            worker_for=self.deps.worker_for,
            staging=self.deps.staging,
            config=self.deps.pipeline.get("release", {}) or {},
            record=self._record,
            checkpoint=self._checkpoint,
            can_continue=self._can_continue,
            stop=self._stop,
            emulator=self.deps.emulator,
            guard_rules=agent_guardrails.enabled(self.deps.pipeline),
        )

    @router(or_("release_requested", "revise_release"))
    def release_phase(self) -> Literal["release_ready", "stopped"]:
        if self._can_continue():
            self._releaser().verify()
        r = self.state.release
        if self.state.status != "running" or not (r.verified or r.failed):
            return "stopped"
        return "release_ready"   # verified, or failed after all fix rounds: the human decides

    @router("release_ready")
    def acceptance_phase(self) -> Literal["acceptance_ready", "stopped"]:
        """The Acceptor: run every criterion against its locked test and keep the proof (evidence/AC-xx/)."""
        if self._can_continue():
            self._releaser().accept()
        return "acceptance_ready" if self._can_continue() else "stopped"

    @router("acceptance_ready")
    def release_gate(self) -> Literal["release_approved", "release_rejected", "stopped"]:
        """G6: user acceptance, signed by the customer through the Product Owner."""
        n = self.state.release.rounds
        r = self.state.release
        docs = ["docs/acceptance.md", "docs/uat-guide.md", f"reports/release_round{n}.md", "infra/README.md",
                "evidence-manifest.sha256"]
        summary = (f"{self._releaser().gate_summary()}\nAcceptance: {len(r.acceptance_met)} criteria met with evidence, "
                   f"{len(r.acceptance_unmet)} not met ({', '.join(r.acceptance_unmet) or 'none'}).")
        return self._gate("release", summary, docs, lambda: self.state.release.reset_verification())

    @listen("release_rejected")
    def revise_release(self) -> None:
        # The reviewer's feedback (with any open problems) goes to each affected component's
        # developer; then staging is verified again.
        if self._can_continue():
            self._releaser().fix_after_rejection(self.state.revision_notes("release"))

    @router("release_approved")
    def packaging_phase(self) -> Literal["release_packaged", "stopped"]:
        """Artifacts, release notes and docs/release.md (deploy steps, rollback), tagged. Never deploys."""
        if self._can_continue():
            self._releaser().package()
        return "release_packaged" if self.state.status == "running" else "stopped"

    @router("release_packaged")
    def golive_gate(self) -> Literal["production_approved", "production_rejected", "stopped"]:
        """G7: the named approver signs the release. The pipeline ends 'ready to deploy'; `uv run deploy` deploys."""
        r = self.state.release
        summary = (f"Release {r.production_notes.split('.')[0].removeprefix('Tagged ')} is packaged. "
                   f"{len(r.acceptance_met)} criteria met, {len(r.acceptance_unmet)} not met. Read docs/release.md "
                   f"(deploy steps and rollback). Approving does not deploy: you run `uv run deploy {self.state.run_id}`.")
        return self._gate("production", summary, ["docs/release.md", "reports/release_notes.md", "evidence-manifest.sha256"],
                          lambda: None)

    @listen("production_rejected")
    def golive_rejected(self) -> None:
        self._stop(f"G7 (go-live) was rejected: {self.state.revision_notes('production') or 'no reason given'}. "
                   f"Fix what it names, then `uv run resume {self.state.run_id}`")

    @router("production_approved")
    def ready_to_deploy(self) -> Literal["run_finished", "stopped"]:
        if self._can_continue():
            self.state.release.production = "ready"
            self._checkpoint("Release: ready to deploy (G6 and G7 approved)")
        return "run_finished" if self.state.status == "running" else "stopped"

    @listen(or_("run_finished", "stopped"))
    def finish(self) -> ProjectState:
        if self.state.status == "running":
            self.state.status = "completed"
        self.deps.workspace.write_text("reports/run_summary.md", self._summary_markdown())
        self._checkpoint(f"Run {self.state.status}")
        return self.state

    def _summary_markdown(self) -> str:
        s = self.state
        lines = [f"# Run {s.run_id}", "", f"Status: **{s.status}**"]
        lines.append("Architect guardrails enforced: "
                     + ", ".join(architecture_guardrails.enabled_rules(self.deps.pipeline)))
        if s.stop_reason:
            lines.append(f"Stop reason: {s.stop_reason}")
        lines += ["", "## Gates", "| Gate | Approved | By | Feedback | At |", "|---|---|---|---|---|"]
        lines += [f"| {g.gate} | {g.approved} | {g.decided_by} | {g.feedback or '-'} | {g.decided_at} |" for g in s.gate_history]
        r = s.release
        if r.rounds or r.deployment:
            lines += ["", "## Release", f"Staging rounds: {r.rounds}; verified: {r.verified}; "
                      f"smoke: {r.smoke_passed}; production: {r.production}"]
            if r.production_notes:
                lines.append(r.production_notes)
        if s.build.items or s.build.milestones:
            lines += ["", "## Build", "| Milestone | Status | QA rounds |", "|---|---|---|"]
            lines += [f"| {mid} | {mp.status} | {mp.qa_rounds} |" for mid, mp in s.build.milestones.items()]
            lines += ["", "| Work item | Status | Attempts | Notes |", "|---|---|---|---|"]
            for wid, ip in s.build.items.items():
                note = (ip.reason or ip.summary).replace("|", "/").replace("\n", " ")[:160]
                lines.append(f"| {wid} | {ip.status} | {ip.attempts} | {note} |")
        lines += ["", "## Token usage by agent", "| Agent | Model | Calls | Tokens | Uncached |", "|---|---|---|---|---|"]
        totals: dict[tuple[str, str], list[int]] = {}
        for u in s.usage:
            t = totals.setdefault((u.agent, u.model), [0, 0, 0])
            t[0] += 1
            t[1] += u.total_tokens
            t[2] += u.uncached_tokens
        lines += [f"| {a} | {m} | {c} | {n:,} | {un:,} |" for (a, m), (c, n, un) in totals.items()]
        lines += ["", f"Total tokens: {s.total_tokens():,} ({s.uncached_tokens():,} uncached; the budget counts uncached)", ""]
        return "\n".join(lines)
