"""Work breakdown structure (WBS), written by the Architect: work packages broken into small tasks, each
with the files it owns, a verify command and the acceptance criteria it serves. The Project manager
sequences and estimates these tasks (artifacts/plan.py) but may not change them.

WBS checks (sent back to the Architect before the design gate):
  W1 ownership   every task owns paths inside its component's folder; frontend, backend and infra tasks
                 never own the same paths (they may be built in parallel)
  W2 verify      every task has a verify command the build may run (an allowed command of its component)
  W3 coverage    every must-have acceptance criterion, API operation and data model has a task
"""

from typing import Literal

from pydantic import BaseModel, Field


class WbsTask(BaseModel):
    id: str = Field(description="WI-001, WI-002, ...")
    title: str
    description: str
    package_id: str = Field(description="Work package id (WP-01, ...)")
    feature: str = Field(default="", description="Feature within the package, e.g. 'Cart API' or 'Cart screen'")
    component: Literal["backend", "frontend", "infra", "shared"]
    story_ids: list[str]
    ac_ids: list[str] = Field(description="Acceptance criteria (AC-..) this task makes pass")
    depends_on: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list, description="Backend module names it builds or changes")
    api_operations: list[str] = Field(default_factory=list,
                                      description="OpenAPI operationIds it implements (backend) or calls (app)")
    data_models: list[str] = Field(default_factory=list, description="Prisma model names it creates or changes")
    owns: list[str] = Field(description="Paths or globs this task may write, e.g. server/src/cart/**")
    verify: str = Field(description="Command that proves the task works, e.g. npm test -- cart")


class WorkPackage(BaseModel):
    id: str = Field(description="WP-01, WP-02, ...")
    title: str
    story_ids: list[str]


class Wbs(BaseModel):
    packages: list[WorkPackage]
    tasks: list[WbsTask]

    def task(self, tid: str) -> WbsTask | None:
        return next((t for t in self.tasks if t.id == tid), None)

    def structure_errors(self) -> list[str]:
        errors: list[str] = []
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            errors.append("Task ids must be unique")
        known, packages = set(ids), {p.id for p in self.packages}
        for t in self.tasks:
            if t.package_id not in packages:
                errors.append(f"{t.id} is in unknown work package {t.package_id}")
            errors += [f"{t.id} depends on unknown task {d}" for d in t.depends_on if d not in known]
        if _has_cycle({t.id: t.depends_on for t in self.tasks}):
            errors.append("Task dependencies contain a cycle")
        return errors

    def w1_ownership(self, workdirs: dict[str, str]) -> list[str]:
        """`workdirs`: component -> folder (from the profile); '.' means the whole repository."""
        errors: list[str] = []
        owner_paths: dict[str, list[tuple[str, str]]] = {}
        for t in self.tasks:
            if not t.owns:
                errors.append(f"W1: {t.id} owns no paths; list the files or folders it writes")
            folder = (workdirs.get(t.component) or ".").rstrip("/")
            for path in t.owns:
                base = _prefix(path)
                if folder not in (".", "") and not (base + "/").startswith(folder + "/"):
                    errors.append(f"W1: {t.id} ({t.component}) owns {path}, outside its folder {folder}/")
                owner_paths.setdefault(t.component, []).append((t.id, base))
        comps = sorted(owner_paths)
        for i, a in enumerate(comps):
            for b in comps[i + 1:]:
                for ta, pa in owner_paths[a]:
                    for tb, pb in owner_paths[b]:
                        if _overlaps(pa, pb):
                            errors.append(f"W1: {ta} ({a}) and {tb} ({b}) both own {pa or '.'} / {pb or '.'}; "
                                          f"give each path one owner")
        return errors

    def w2_verify(self, allowed: dict[str, list[str]] | None = None) -> list[str]:
        """`allowed`: component -> command prefixes its sandbox runs (no entry: any command)."""
        errors = []
        for t in self.tasks:
            if not t.verify.strip():
                errors.append(f"W2: {t.id} has no verify command")
                continue
            prefixes = (allowed or {}).get(t.component)
            if prefixes == [] and t.verify.strip() == "-":
                continue                       # a component that runs no commands (e.g. infra files)
            if prefixes is not None and not any(t.verify.split()[:len(p.split())] == p.split() for p in prefixes):
                errors.append(f"W2: {t.id} verify command `{t.verify}` is not allowed for {t.component}; "
                              f"start it with one of: {', '.join(prefixes) or '(none: this component runs no commands)'}")
        return errors

    def w3_coverage(self, must_ac_ids: list[str], known_ac_ids: set[str], operations: dict[str, str],
                    data_models: list[str]) -> list[str]:
        errors: list[str] = []
        covered_acs = {a for t in self.tasks for a in t.ac_ids}
        errors += [f"W3: {t.id} serves unknown acceptance criterion {a}" for t in self.tasks
                   for a in t.ac_ids if a not in known_ac_ids]
        errors += [f"W3: must-have acceptance criterion {a} has no task" for a in must_ac_ids if a not in covered_acs]
        implemented = {op for t in self.tasks if t.component == "backend" for op in t.api_operations}
        errors += [f"W3: {t.id} links unknown API operation {op}" for t in self.tasks
                   for op in t.api_operations if op not in operations]
        errors += [f"W3: no backend task implements API operation {op} ({route})"
                   for op, route in operations.items() if op not in implemented]
        covered_models = {m for t in self.tasks for m in t.data_models}
        errors += [f"W3: no task covers data model {m}" for m in data_models if m not in covered_models]
        return errors

    def to_markdown(self) -> str:
        lines = ["# Work breakdown structure", "",
                 f"{len(self.tasks)} tasks in {len(self.packages)} work packages. Each task owns its paths, "
                 "has a verify command and lists the acceptance criteria it makes pass.", ""]
        for p in self.packages:
            lines += [f"## {p.id} {p.title}", f"Stories: {', '.join(p.story_ids) or '-'}", "",
                      "| Task | Component | Owns | Verify | Criteria | Depends on |", "|---|---|---|---|---|---|"]
            for t in (t for t in self.tasks if t.package_id == p.id):
                lines.append(f"| {t.id} {t.title} | {t.component} | {', '.join(t.owns) or '-'} | `{t.verify}` | "
                             f"{', '.join(t.ac_ids) or '-'} | {', '.join(t.depends_on) or '-'} |")
            lines.append("")
        return "\n".join(lines)

    def ownership(self) -> dict[str, list[str]]:
        """component -> the paths its builder may write (the PM's ownership files)."""
        out: dict[str, list[str]] = {}
        for t in self.tasks:
            out.setdefault(t.component, [])
            out[t.component] += [p for p in t.owns if p not in out[t.component]]
        return out


def _prefix(path: str) -> str:
    """'server/src/cart/**' -> 'server/src/cart'; 'app/lib/main.dart' -> 'app/lib/main.dart'."""
    parts = []
    for part in path.strip().strip("/").split("/"):
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def _overlaps(a: str, b: str) -> bool:
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting, done = set(), set()

    def visit(n: str) -> bool:
        if n in done:
            return False
        if n in visiting:
            return True
        visiting.add(n)
        if any(visit(d) for d in graph.get(n, []) if d in graph):
            return True
        visiting.discard(n)
        done.add(n)
        return False

    return any(visit(n) for n in graph)
