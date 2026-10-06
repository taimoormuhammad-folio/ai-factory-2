"""Flow state for one SDLC run. Checkpointed to runs/<run_id>/state.json after every phase."""

from datetime import datetime, timezone
from typing import Literal

from crewai.flow.flow import FlowState
from pydantic import BaseModel, Field

from agentic_sdlc.artifacts.architecture import ArchitectureDoc
from agentic_sdlc.artifacts.backlog import Backlog
from agentic_sdlc.artifacts.design import DesignSystem, ScreenMockups
from agentic_sdlc.artifacts.plan import DeliveryPlan
from agentic_sdlc.artifacts.wbs import Wbs
from agentic_sdlc.artifacts.prd import PRD, ProductBrief, QAPair
from agentic_sdlc.artifacts.reports import QAReport, WorkItemResult
from agentic_sdlc.artifacts.review import ReviewReport
from agentic_sdlc.artifacts.tests import AcceptanceSuite


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GateDecision(BaseModel):
    gate: str
    approved: bool
    feedback: str = ""
    decided_by: str = "human"          # human | auto | config | waiver | system (a reopen, not a rejection)
    decided_at: str = Field(default_factory=utcnow)
    gate_id: str = ""                  # G1 … G7
    approver: str = ""                 # the named person (human decisions)
    role: str = ""                     # e.g. product owner, architect, developer of record
    risk_note: str = ""                # the approver's own words (merge gate)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)   # what was approved


class UsageRecord(BaseModel):
    phase: str
    agent: str
    model: str
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0  # included in prompt_tokens; cheap cache reads
    completion_tokens: int = 0
    total_tokens: int = 0
    task_key: str = ""
    duration_ms: int = 0
    started_at: str = ""
    ended_at: str = ""

    @property
    def uncached_tokens(self) -> int:
        return self.total_tokens - self.cached_prompt_tokens


class ItemProgress(BaseModel):
    status: Literal["todo", "done", "blocked", "failed"] = "todo"
    attempts: int = 0
    summary: str = ""
    reason: str = ""  # why blocked or failed
    commit: str | None = None


class MilestoneProgress(BaseModel):
    status: Literal["todo", "done", "partial", "failed"] = "todo"
    qa_rounds: int = 0
    qa_reports: list[QAReport] = Field(default_factory=list)
    reviews: list[ReviewReport] = Field(default_factory=list)       # Code Reviewer, one per round that reached review
    evidence: list[str] = Field(default_factory=list)               # report files of the last round (integration, scans)
    open_findings: list[str] = Field(default_factory=list)          # minor or low-confidence items shown at the merge gate


class BuildState(BaseModel):
    scaffolded: list[str] = Field(default_factory=list)  # components whose project exists
    locked_tests: dict[str, str] = Field(default_factory=dict)        # acceptance test file -> sha256
    acceptance: dict[str, AcceptanceSuite] = Field(default_factory=dict)   # "<milestone>/<component>" -> suite
    items: dict[str, ItemProgress] = Field(default_factory=dict)
    milestones: dict[str, MilestoneProgress] = Field(default_factory=dict)

    def item(self, wid: str) -> ItemProgress:
        return self.items.setdefault(wid, ItemProgress())

    def milestone(self, mid: str) -> MilestoneProgress:
        return self.milestones.setdefault(mid, MilestoneProgress())


class ReleaseState(BaseModel):
    deployment: WorkItemResult | None = None      # Deployment engineer: Dockerfile, compose, CI, staging env
    smoke_suite: WorkItemResult | None = None     # Smoke tester: the smoke test suite
    rounds: int = 0                               # staging verification rounds, all runs
    contract_issues: list[str] = Field(default_factory=list)
    integration: QAReport | None = None
    smoke_passed: bool | None = None
    smoke_output: str = ""
    device_suite: WorkItemResult | None = None    # Smoke tester: on-device journey tests
    device_passed: bool | None = None             # None: not run (see device_note)
    device_output: str = ""
    device_note: str = ""                         # why device checks were skipped, if they were
    device_screenshots: list[str] = Field(default_factory=list)
    apk_key: str = ""                             # app tree + API base of the last APK build: unchanged -> reuse the APK
    device_failed_files: list[str] = Field(default_factory=list)   # failing device test files, run first next round
    verified: bool = False                        # staging, integration, smoke (and device) passed
    failed: bool = False                          # fix rounds used up with problems left: the release gate (G6) decides
    open_problems: list[list[str]] = Field(default_factory=list)  # [component, problem] from the last round
    production: Literal["todo", "packaged", "ready", "deployed", "failed"] = "todo"   # ready: G6 and G7 approved
    production_notes: str = ""
    acceptance_met: list[str] = Field(default_factory=list)       # criteria with evidence (evidence/AC-xx/)
    acceptance_unmet: list[str] = Field(default_factory=list)     # criteria without a passing test or evidence
    evidence_manifest: str = ""                                   # sha256 of evidence-manifest.sha256 itself

    def reset_verification(self) -> None:
        self.contract_issues, self.integration = [], None
        self.smoke_passed, self.smoke_output, self.verified, self.failed = None, "", False, False
        self.device_passed, self.device_output, self.device_note, self.device_screenshots = None, "", "", []
        self.acceptance_met, self.acceptance_unmet, self.evidence_manifest = [], [], ""


class ProjectState(FlowState):
    run_id: str = ""
    profile: str = "flutter_nestjs_ecommerce"
    pipeline: str = "pipeline"          # config/<pipeline>.yaml, fixed for the run
    brief: str = ""

    product_brief: ProductBrief | None = None
    clarifications: list[QAPair] = Field(default_factory=list)
    prd: PRD | None = None
    backlog: Backlog | None = None
    architecture: ArchitectureDoc | None = None
    risk_tier: str = ""                 # L | M | H (from the intake); empty = the pipeline's risk_tier
    product_owner: str = ""             # from the intake
    wbs: Wbs | None = None              # the Architect's work breakdown
    plan: DeliveryPlan | None = None    # the Project manager's sequencing and estimates (merged into backlog)
    design: DesignSystem | None = None
    mockups: list[ScreenMockups] = Field(default_factory=list)   # per screen, when design.mockups is on
    build: BuildState = Field(default_factory=BuildState)
    release: ReleaseState = Field(default_factory=ReleaseState)

    gate_history: list[GateDecision] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)

    status: Literal["running", "completed", "stopped"] = "running"
    stop_reason: str = ""

    def gate_approved(self, gate: str) -> bool:
        """True if the latest decision for this gate is an approval."""
        latest = [d for d in self.gate_history if d.gate == gate]
        return bool(latest) and latest[-1].approved

    def rejections(self, gate: str) -> list[GateDecision]:
        """Human rejections (a system reopen is not a rejection)."""
        return [d for d in self.gate_history if d.gate == gate and not d.approved and d.decided_by != "system"]

    def reopen_release(self, reason: str) -> None:
        """Something changed after release verification/approval: verify again and ask again."""
        self.release.reset_verification()
        self.release.production, self.release.production_notes = "todo", ""
        for gate in ("release", "production"):
            if self.gate_approved(gate):
                self.gate_history.append(GateDecision(gate=gate, approved=False, decided_by="system", feedback=reason))

    def revision_notes(self, gate: str) -> str:
        """Feedback from the latest human rejection of this gate, if it came after the last approval."""
        for d in reversed(self.gate_history):
            if d.gate == gate and d.decided_by != "system":
                return "" if d.approved else d.feedback
        return ""

    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.usage)

    def uncached_tokens(self) -> int:
        """Tokens that count against the budget: cache reads are excluded, since agentic
        coding sessions re-read the same context many times at a small fraction of the cost."""
        return sum(u.uncached_tokens for u in self.usage)
