"""Which acceptance criteria have no locked test yet. A criterion without a test cannot be proven at release, so
this is shown early (the merge package, G5) and again before release, not discovered at the user-acceptance gate."""

from agentic_sdlc.artifacts.prd import PRD
from agentic_sdlc.artifacts.tests import AcceptanceSuite


def uncovered(prd: PRD, suites: dict[str, AcceptanceSuite]) -> list[tuple[str, str, bool]]:
    """(criterion id, story id, must-have) for every criterion with no acceptance test, in spec order."""
    covered = {t.ac_id for s in suites.values() for t in s.tests}
    must = set(prd.must_have_ac_ids())
    return [(c.id, story, c.id in must) for story, c in prd.criteria() if c.id and c.id not in covered]


def markdown(missing: list[tuple[str, str, bool]]) -> str:
    if not missing:
        return "All acceptance criteria have a locked test.\n"
    lines = ["Criteria WITHOUT a locked acceptance test (they cannot be proven at release):", ""]
    lines += [f"- {ac} ({story}){' MUST-HAVE' if must else ''}" for ac, story, must in missing]
    return "\n".join(lines) + "\n"
