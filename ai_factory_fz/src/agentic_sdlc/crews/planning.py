"""Design and Plan: the Architect designs the solution (options, contract, ADRs) and breaks it into the
WBS (tasks with owned paths, verify commands and acceptance criteria); the Project manager then
sequences and estimates the WBS into the delivery plan without changing it."""

from typing import Callable

from agentic_sdlc.artifacts.architecture import ArchitectureDoc
from agentic_sdlc.artifacts.backlog import Backlog
from agentic_sdlc.artifacts.plan import DeliveryPlan, to_backlog
from agentic_sdlc.artifacts.wbs import Wbs
from agentic_sdlc.artifacts.design import DesignSystem
from agentic_sdlc.artifacts.prd import PRD
from agentic_sdlc.crews.base import TaskResult, TaskRunner, artifact_guardrail
from agentic_sdlc.scope import Scope

PHASE = "planning"

DATA_LAYER = "A complete Prisma schema (prisma_schema) for PostgreSQL."
NO_DATA_LAYER = ("No database: the API keeps no data of its own, so leave prisma_schema empty. Module "
                 "entities name the records of the external system the API calls.")
NO_LAYOUT = "(not fixed by the profile)"


def design_architecture(
    runner: TaskRunner,
    prd: PRD,
    stack: str,
    domain_entities: list[str],
    revision_notes: str,
    scope: Scope | None = None,
    guardrails: Callable[[ArchitectureDoc], list[str]] | None = None,
    database: bool = True,
    layout: str = "",
) -> TaskResult[ArchitectureDoc]:
    """`guardrails`: extra checks on the design (see guardrails/architecture.py).
    `database`: False for stacks whose API keeps no data (no Prisma schema).
    `layout`: the repository folders the build tooling creates (Profile.layout_summary)."""
    scope = scope or Scope()
    extra = guardrails or (lambda a: [])
    data_errors = ArchitectureDoc.prisma_errors if database else ArchitectureDoc.no_database_errors
    return runner.run(
        PHASE,
        "design_architecture",
        {
            "prd": prd.to_markdown(),
            "stack": stack,
            "domain_entities": ", ".join(domain_entities),
            "revision_notes": revision_notes or "(none)",
            "scope_rules": scope.rules_text(),
            "data_layer": DATA_LAYER if database else NO_DATA_LAYER,
            "layout": layout or NO_LAYOUT,
        },
        ArchitectureDoc,
        guardrail=artifact_guardrail(
            ArchitectureDoc,
            lambda a: a.openapi_errors() + data_errors(a) + scope.architecture_errors(a) + extra(a),
        ),
    )


def screens_summary(design: DesignSystem | None) -> str:
    if design is None:
        return "(no screen specs: the design phase is disabled)"
    return "\n".join(f"- {s.id} {s.name} ({s.route}): stories {', '.join(s.story_ids)}" for s in design.screens)


def wbs_errors(wbs: Wbs, prd: PRD, architecture: ArchitectureDoc, workdirs: dict[str, str],
               allowed: dict[str, list[str]] | None = None) -> list[str]:
    known_acs = {c.id for _, c in prd.criteria()}
    return (wbs.structure_errors() + wbs.w1_ownership(workdirs) + wbs.w2_verify(allowed)
            + wbs.w3_coverage(prd.must_have_ac_ids(), known_acs, architecture.operations(), architecture.data_models()))


def design_wbs(runner: TaskRunner, prd: PRD, architecture: ArchitectureDoc, workdirs: dict[str, str],
               stack: str, revision_notes: str = "", layout: str = "",
               allowed: dict[str, list[str]] | None = None) -> TaskResult[Wbs]:
    """The Architect's work breakdown: packages -> small tasks, each owning paths with a verify command.
    `allowed`: component -> the command prefixes its verify command may use."""
    return runner.run(
        PHASE,
        "design_wbs",
        {
            "prd": prd.to_markdown(),
            "solution": architecture.solution_summary(),
            "stack": stack,
            "layout": layout or NO_LAYOUT,
            "workdirs": "\n".join(
                f"- {c}: {w}/ (verify with: {', '.join((allowed or {}).get(c, [])) or 'any listed command'})"
                for c, w in workdirs.items()),
            "revision_notes": revision_notes or "(none)",
        },
        Wbs,
        guardrail=artifact_guardrail(Wbs, lambda w: wbs_errors(w, prd, architecture, workdirs, allowed)),
    )


def plan_delivery(runner: TaskRunner, prd: PRD, wbs: Wbs, revision_notes: str = "",
                  scope: Scope | None = None) -> TaskResult[DeliveryPlan]:
    """The Project manager sequences the WBS into milestones and estimates every task."""
    scope = scope or Scope()
    must_haves = prd.must_have_ids()

    def check(plan: DeliveryPlan) -> list[str]:
        errors = plan.errors(wbs)
        if not errors:
            backlog = to_backlog(wbs, plan)
            errors = backlog.validation_errors(must_haves) + scope.backlog_errors(backlog)
        return errors

    return runner.run(
        PHASE,
        "plan_delivery",
        {
            "prd": prd.to_markdown(),
            "wbs": wbs.to_markdown(),
            "revision_notes": revision_notes or "(none)",
            "scope_rules": scope.rules_text(),
        },
        DeliveryPlan,
        guardrail=artifact_guardrail(DeliveryPlan, check),
    )
