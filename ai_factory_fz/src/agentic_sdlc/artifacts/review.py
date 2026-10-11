"""Code review of a milestone, by an agent that wrote none of the code: findings through five lenses
(spec fidelity, security, design conformance, quality, operability), each with a severity and a
confidence. Blocker/major findings the reviewer is at least moderately sure of go back to the builders.
The merge package (package.md) collects the reviews, QA and scan results for the developer of record."""

from typing import Literal

from pydantic import BaseModel, Field

from agentic_sdlc.artifacts.reports import Bug

Lens = Literal["spec", "security", "design", "quality", "operability"]
LENSES: tuple[str, ...] = ("spec", "security", "design", "quality", "operability")


class Finding(BaseModel):
    id: str = Field(description="REV-001, ...")
    lens: Lens
    severity: Literal["blocker", "major", "minor"]
    confidence: Literal["low", "medium", "high"]
    work_item_id: str = Field(description="The work item the finding belongs to")
    file: str = Field(default="", description="Path (and line) where the problem is")
    title: str
    detail: str = Field(description="What is wrong, and why it matters")
    fix: str = Field(default="", description="What would fix it")


class ReviewReport(BaseModel):
    milestone_id: str
    lenses_covered: list[Lens] = Field(description="The lenses you actually reviewed; all five unless the diff is empty")
    findings: list[Finding] = Field(default_factory=list)
    summary: str

    def actionable(self) -> list[Finding]:
        """Blocker/major findings the reviewer is at least moderately sure of: builders must fix these."""
        return [f for f in self.findings if f.severity in ("blocker", "major") and f.confidence != "low"]

    def errors(self, item_ids: list[str]) -> list[str]:
        missing = [lens for lens in LENSES if lens not in self.lenses_covered]
        errors = [f"Review every lens; not covered: {', '.join(missing)}"] if missing else []
        errors += [f"{f.id} names work item '{f.work_item_id}', which is not one of {sorted(item_ids)}"
                   for f in self.findings if f.work_item_id not in item_ids]
        errors += [f"{f.id} has no detail" for f in self.findings if not f.detail.strip()]
        return errors

    def bugs(self) -> list[Bug]:
        """The actionable findings as bugs, for the builders' fix round."""
        return [Bug(id=f.id, work_item_id=f.work_item_id, title=f"[{f.lens}] {f.title}", severity=f.severity,
                    steps=f.file or "see the finding", expected=f.fix or "the problem is fixed", actual=f.detail)
                for f in self.actionable()]

    def to_markdown(self) -> str:
        lines = [f"# Code review: {self.milestone_id}", "", f"Lenses covered: {', '.join(self.lenses_covered)}", "",
                 self.summary, ""]
        for lens in LENSES:
            found = [f for f in self.findings if f.lens == lens]
            lines += [f"## {lens.capitalize()}", ""]
            for f in found:
                lines += [f"### {f.id} [{f.severity}, confidence {f.confidence}] {f.title} ({f.work_item_id})",
                          f"{f.file}" if f.file else "", f.detail, f"Fix: {f.fix}" if f.fix else "", ""]
            if not found:
                lines += ["No findings.", ""]
        return "\n".join(lines)


def package_markdown(run_id: str, milestones: list[dict], coverage: str = "") -> str:
    """docs/package.md for the merge gate (G5): per milestone what was built, verified and reviewed, with the
    evidence files, so the developer of record reads facts and not a summary."""
    lines = [f"# Merge package: {run_id}", "",
             "For the developer of record. Read the evidence files; write the risk note in your own words.", ""]
    for m in milestones:
        lines += [f"## {m['id']} {m['name']}", f"Status: **{m['status']}**; QA rounds: {m['qa_rounds']}", "",
                  "| Task | Status | Commit |", "|---|---|---|"]
        lines += [f"| {t['id']} {t['title']} | {t['status']} | {t['commit'] or '-'} |" for t in m["tasks"]]
        lines += ["", "Evidence:"] + [f"- {e}" for e in m["evidence"]] + [""]
        if m.get("open"):
            lines += ["Open findings (minor, or low confidence):"] + [f"- {o}" for o in m["open"]] + [""]
    if coverage:
        lines += ["## Test coverage of the acceptance criteria", "", coverage]
    return "\n".join(lines)
