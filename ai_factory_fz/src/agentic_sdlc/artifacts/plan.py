"""Delivery plan, written by the Project manager from the Architect's WBS: milestones that sequence the
WBS tasks, and an estimate per task. The PM may not add, drop or change tasks; scope changes go back to
the Architect. `to_backlog` merges WBS and plan into the Backlog the build loop works from."""

from pydantic import BaseModel, Field

from agentic_sdlc.artifacts.backlog import Backlog, Epic, Level, Milestone, WorkItem
from agentic_sdlc.artifacts.wbs import Wbs


class TaskEstimate(BaseModel):
    task_id: str
    points: int = Field(ge=1, le=8)
    complexity: Level = "medium"
    risk: Level = "medium"
    confidence: Level = "medium"
    rationale: str = Field(description="One line: what drives the size")


class PlannedMilestone(BaseModel):
    id: str = Field(description="M1, M2, ...")
    name: str
    goal: str = Field(description="The testable slice this milestone delivers")
    task_ids: list[str]


class DeliveryPlan(BaseModel):
    milestones: list[PlannedMilestone]
    estimates: list[TaskEstimate]
    risks: list[str] = Field(default_factory=list, description="Delivery risks and how the plan handles them")
    schedule_notes: str = Field(default="", description="Sequencing and timeline in a few sentences")

    def errors(self, wbs: Wbs) -> list[str]:
        errors: list[str] = []
        task_ids = [t.id for t in wbs.tasks]
        planned = [tid for m in self.milestones for tid in m.task_ids]
        errors += [f"Task {tid} is not a WBS task; the WBS is the Architect's (send scope changes back)"
                   for tid in planned if tid not in task_ids]
        errors += [f"WBS task {tid} is in no milestone" for tid in task_ids if tid not in planned]
        errors += [f"Task {tid} is in more than one milestone" for tid in set(planned) if planned.count(tid) > 1]
        estimated = {e.task_id for e in self.estimates}
        errors += [f"WBS task {tid} has no estimate" for tid in task_ids if tid not in estimated]
        errors += [f"Estimate for unknown task {e.task_id}" for e in self.estimates if e.task_id not in task_ids]
        position = {tid: i for i, m in enumerate(self.milestones) for tid in m.task_ids}
        for t in wbs.tasks:
            for dep in t.depends_on:
                if t.id in position and dep in position and position[dep] > position[t.id]:
                    errors.append(f"{t.id} ({self.milestones[position[t.id]].id}) depends on {dep}, which is in a "
                                  f"later milestone ({self.milestones[position[dep]].id})")
        return errors

    def to_markdown(self, wbs: Wbs) -> str:
        est = {e.task_id: e for e in self.estimates}
        total = sum(e.points for e in self.estimates)
        lines = ["# Delivery plan", "", f"{len(self.milestones)} milestones, {len(wbs.tasks)} tasks, {total} points.",
                 "", self.schedule_notes or "", ""]
        for m in self.milestones:
            pts = sum(est[t].points for t in m.task_ids if t in est)
            lines += [f"## {m.id}: {m.name} ({pts} pts)", m.goal, ""]
            for tid in m.task_ids:
                t = wbs.task(tid)
                lines.append(f"- {tid} {t.title if t else ''} ({t.component if t else '?'}, "
                             f"{est[tid].points if tid in est else '?'} pts)")
            lines.append("")
        lines += ["## Risks"] + [f"- {r}" for r in self.risks or ["(none listed)"]]
        return "\n".join(lines) + "\n"

    def estimates_markdown(self, wbs: Wbs) -> str:
        lines = ["# Estimates", "", "| Task | Points | Complexity | Risk | Confidence | Why this size |",
                 "|---|---|---|---|---|---|"]
        for e in self.estimates:
            t = wbs.task(e.task_id)
            lines.append(f"| {e.task_id} {t.title if t else ''} | {e.points} | {e.complexity} | {e.risk} | "
                         f"{e.confidence} | {e.rationale} |")
        lines += ["", f"Total: {sum(e.points for e in self.estimates)} points."]
        return "\n".join(lines) + "\n"


def to_backlog(wbs: Wbs, plan: DeliveryPlan) -> Backlog:
    """The build loop's view: WBS tasks with their estimates, grouped into the planned milestones."""
    est = {e.task_id: e for e in plan.estimates}
    items = []
    for t in wbs.tasks:
        e = est.get(t.id)
        items.append(WorkItem(
            id=t.id, title=t.title, description=t.description, epic_id=t.package_id, feature=t.feature,
            component=t.component, story_ids=t.story_ids, depends_on=t.depends_on, modules=t.modules,
            api_operations=t.api_operations, data_models=t.data_models, owns=t.owns, verify=t.verify,
            ac_ids=t.ac_ids, estimate_points=e.points if e else 1, complexity=e.complexity if e else "medium",
            risk=e.risk if e else "medium", confidence=e.confidence if e else "medium",
            estimate_rationale=e.rationale if e else ""))
    return Backlog(
        epics=[Epic(id=p.id, title=p.title, story_ids=p.story_ids) for p in wbs.packages],
        work_items=items,
        milestones=[Milestone(id=m.id, name=m.name, goal=m.goal, work_item_ids=m.task_ids) for m in plan.milestones],
    )
