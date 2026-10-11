"""The canary: the smallest run that still goes through every stage, judged by what it cost.

`uv run canary` starts a real run of briefs/canary.md with config/pipeline.canary.yaml (auto gates, tier L; it never
deploys) and then reads the finished state. Hard failures (the run did not finish, a criterion unproven, a task
failed) exit 1; a run that finished but needed retries beyond the budget exits 2 ("not smooth"), because retries are
how new framework defects show up: the first real run of a new feature should happen here, not on a client's project.
"""

from dataclasses import dataclass, field

from agentic_sdlc.state import ProjectState

# What a smooth run looks like (per run, for the canary's size).
BUDGET = {"task_attempts_extra": 1, "qa_rounds_extra": 1, "release_rounds": 2, "suite_rewrites": 0, "gate_rejections": 0}


@dataclass
class Verdict:
    failures: list[str] = field(default_factory=list)       # exit 1
    rough: list[str] = field(default_factory=list)          # exit 2: finished, but not smoothly
    numbers: dict[str, int] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else (2 if self.rough else 0)


def judge(state: ProjectState) -> Verdict:
    v = Verdict()
    b, r = state.build, state.release
    items = list(b.items.values())
    v.numbers = {
        "tasks": len(items), "task_attempts": sum(i.attempts for i in items),
        "qa_rounds": sum(m.qa_rounds for m in b.milestones.values()), "milestones": len(b.milestones),
        "release_rounds": r.rounds, "suite_rewrites": r.suite_rewrites,
        "gate_rejections": sum(1 for g in state.gate_history if not g.approved and g.decided_by != "system"),
        "tokens": state.total_tokens(),
    }
    if state.status != "completed":
        v.failures.append(f"the run ended as '{state.status}': {state.stop_reason[:300] or 'no reason recorded'}")
    bad = [f"{wid} ({i.status})" for wid, i in b.items.items() if i.status != "done"]
    if bad:
        v.failures.append("tasks not done: " + ", ".join(bad))
    if r.production != "ready":
        v.failures.append(f"the release did not reach 'ready to deploy' (state: {r.production})")
    if r.acceptance_unmet:
        v.failures.append("criteria without proof: " + ", ".join(r.acceptance_unmet))
    if not r.verified:
        v.failures.append("staging verification did not pass")
    n = v.numbers
    if n["task_attempts"] > n["tasks"] + BUDGET["task_attempts_extra"]:
        v.rough.append(f"{n['task_attempts']} build attempts for {n['tasks']} tasks (budget: +{BUDGET['task_attempts_extra']})")
    if n["qa_rounds"] > n["milestones"] + BUDGET["qa_rounds_extra"]:
        v.rough.append(f"{n['qa_rounds']} QA rounds for {n['milestones']} milestone(s)")
    if n["release_rounds"] > BUDGET["release_rounds"]:
        v.rough.append(f"{n['release_rounds']} release verification rounds (budget: {BUDGET['release_rounds']})")
    if n["suite_rewrites"] > BUDGET["suite_rewrites"]:
        v.rough.append(f"{n['suite_rewrites']} test suite(s) had to be rewritten")
    if n["gate_rejections"] > BUDGET["gate_rejections"]:
        v.rough.append(f"{n['gate_rejections']} gate rejection(s)")
    return v


def report(run_id: str, v: Verdict) -> str:
    n = v.numbers
    lines = [f"# Canary {run_id}", "", f"Result: **{'PASSED' if not v.exit_code else ('FAILED' if v.exit_code == 1 else 'NOT SMOOTH')}**", ""]
    lines += [f"- {k.replace('_', ' ')}: {val:,}" for k, val in n.items()]
    if v.failures:
        lines += ["", "## Failures"] + [f"- {x}" for x in v.failures]
    if v.rough:
        lines += ["", "## Retries beyond the budget (read reports/ and traces to find the cause)"] + [f"- {x}" for x in v.rough]
    return "\n".join(lines) + "\n"
