"""Design phase: UI/UX designer produces the design system and screen specs, then (optionally) a mockup
of each screen state, drawn with the mockup kit (see design/mockups.py)."""

from agentic_sdlc.artifacts.architecture import ArchitectureDoc
from agentic_sdlc.artifacts.design import DesignSystem, ScreenMockups, ScreenSpec
from agentic_sdlc.artifacts.prd import PRD, UserStory
from agentic_sdlc.crews.base import TaskResult, TaskRunner, artifact_guardrail
from agentic_sdlc.design import mockups as kit
from agentic_sdlc.scope import Scope

PHASE = "design"


def design_ui(runner: TaskRunner, prd: PRD, architecture: ArchitectureDoc, scope: Scope | None = None,
              revision_notes: str = "", guard_rules: set[str] | frozenset = frozenset()) -> TaskResult[DesignSystem]:
    scope = scope or Scope()
    must_haves = prd.must_have_ids()
    return runner.run(
        PHASE,
        "design_ui",
        {"prd": prd.to_markdown(), "app_features": architecture.app_features_summary(), "scope_rules": scope.rules_text(),
         "revision_notes": revision_notes or "(none)"},
        DesignSystem,
        guardrail=artifact_guardrail(
            DesignSystem,
            lambda d: [f"Must-have story {sid} is not served by any screen" for sid in d.uncovered_stories(must_haves)]
            + scope.design_errors(d),
        ),
    )


def screen_spec_text(screen: ScreenSpec) -> str:
    return (f"{screen.id} {screen.name} ({screen.route})\n{screen.purpose}\n"
            f"Components: {'; '.join(screen.components)}\nAll states: {'; '.join(screen.states)}")


def stories_text(prd: PRD | None, story_ids: list[str]) -> str:
    """The screen's user stories with their acceptance criteria: the rules the mockup must show."""
    stories: list[UserStory] = [s for s in (prd.user_stories if prd else []) if s.id in story_ids]
    if not stories:
        return "(none)"
    lines = []
    for s in stories:
        lines.append(f"{s.id} {s.title}: as {s.as_a}, I want {s.i_want}, so that {s.so_that}.")
        lines += [f"  - Given {c.given}, when {c.when}, then {c.then}." for c in s.acceptance_criteria]
    return "\n".join(lines)


def design_mockups(runner: TaskRunner, design: DesignSystem, screen: ScreenSpec, states: list[str],
                   revision_notes: str = "", prd: PRD | None = None) -> TaskResult[ScreenMockups]:
    """Mockups of one screen: one body-markup page per state in `states` (names as in the spec). `prd` gives
    the product and the screen's stories, so sample content and rules (e.g. what stock may show) are right."""
    tokens = set(kit.color_tokens(design))

    def check(m: ScreenMockups) -> list[str]:
        wrong_id = [f"screen_id must be {screen.id}, not {m.screen_id}"] if m.screen_id != screen.id else []
        return wrong_id + m.problems(tokens, states)

    return runner.run(
        PHASE,
        "design_mockups",
        {
            "screen": screen_spec_text(screen),
            "product": f"{prd.title}: {prd.summary}" if prd else "(not given)",
            "stories": stories_text(prd, screen.story_ids),
            "screen_id": screen.id,
            "states": "\n".join(f"- {s}" for s in states),
            "kit": kit.kit_reference(design),
            "accessibility": "; ".join(design.accessibility) or "(none)",
            "revision_notes": revision_notes or "(none)",
        },
        ScreenMockups,
        guardrail=artifact_guardrail(ScreenMockups, check),
    )
