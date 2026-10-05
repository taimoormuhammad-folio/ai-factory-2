"""Specify: the Business analyst turns the Product Owner's intent into the spec (spec.md): user stories
with testable Given/When/Then acceptance criteria (AC-01, ...), edge cases, non-functional requirements,
scope limits and open questions. The customer is reached only through the Product Owner: open questions
are shown at the spec gate (G1) and the PO's answers come back as that gate's feedback."""

import re

from agentic_sdlc.artifacts.prd import PRD
from agentic_sdlc.crews.base import TaskResult, TaskRunner, artifact_guardrail
from agentic_sdlc.scope import Scope

PHASE = "discovery"
_AC_ID = re.compile(r"^AC-\d{2,3}$")


def prd_errors(prd: PRD) -> list[str]:
    errors = []
    ids = [s.id for s in prd.user_stories]
    if len(ids) != len(set(ids)):
        errors.append("User story ids must be unique")
    if not prd.must_have_ids():
        errors.append("At least one user story must have priority 'must'")
    for s in prd.user_stories:
        if len(s.acceptance_criteria) < 2:
            errors.append(f"{s.id} needs at least two acceptance criteria")
    ac_ids = [c.id for _, c in prd.criteria()]
    bad = [f"{sid}: '{c.id}'" for sid, c in prd.criteria() if not _AC_ID.match(c.id or "")]
    if bad:
        errors.append(f"Every acceptance criterion needs an id AC-01, AC-02, ... ({', '.join(bad[:5])})")
    dupes = sorted({a for a in ac_ids if a and ac_ids.count(a) > 1})
    if dupes:
        errors.append(f"Acceptance criterion ids must be unique across the spec: {', '.join(dupes)}")
    return errors


def write_spec(runner: TaskRunner, intent: str, stack: str, revision_notes: str,
               scope: Scope | None = None) -> TaskResult[PRD]:
    scope = scope or Scope()
    return runner.run(
        PHASE,
        "write_prd",
        {
            "intent": intent,
            "stack": stack,
            "revision_notes": revision_notes or "(none)",
            "scope_rules": scope.rules_text(),
        },
        PRD,
        guardrail=artifact_guardrail(PRD, lambda p: prd_errors(p) + scope.prd_errors(p)),
    )
