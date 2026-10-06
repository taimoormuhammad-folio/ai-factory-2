"""`uv run rewind <run_id>`: supported ways to go back a step, so nobody edits state.json by hand.

Every operation changes the saved state and the files that belong to it together, keeps what was already
approved and built, and leaves the run stopped; `uv run resume <run_id>` then continues. Gates ask again by
themselves where a document they approved has changed.
"""

import json
from pathlib import Path

from agentic_sdlc.state import ProjectState

STEPS = {
    "plan": "re-plan: the PM plans milestones and estimates again (the WBS stays); G3 asks again",
    "scaffold": "run the project setup steps again (steps whose result exists are skipped)",
    "build": "retry failed and blocked tasks (done tasks stay done)",
    "release": "verify and accept the release again from the start",
    "device-suite": "the Smoke tester rewrites the on-device suite",
    "smoke-suite": "the Smoke tester rewrites the API smoke suite",
}


def _drop(root: Path, rels: list[str]) -> None:
    for rel in rels:
        p = root / rel
        if p.is_file():
            p.unlink()


def _files(root: Path, folder: str, suffixes: tuple[str, ...], contains: str = "") -> list[str]:
    base = root / folder
    if not base.exists():
        return []
    return [p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file() and p.suffix in suffixes
            and contains in p.relative_to(base).as_posix().lower() and "node_modules" not in p.parts
            and ".pub-cache" not in p.parts]


def apply(state: ProjectState, root: Path, profile, step: str) -> str:
    """Rewind `state` (and files under `root`) to `step`; returns what was done."""
    if step not in STEPS:
        raise ValueError(f"unknown step '{step}'; choose one of: {', '.join(STEPS)}")
    r, b = state.release, state.build
    if step == "plan":
        state.plan, state.backlog = None, None
    elif step == "scaffold":
        b.scaffolded = []
        for p in b.items.values():
            if p.status in ("failed", "blocked"):
                p.status, p.reason, p.attempts = "todo", "", 0
    elif step == "build":
        for p in b.items.values():
            if p.status in ("failed", "blocked"):
                p.status, p.reason, p.attempts = "todo", "", 0
        for m in b.milestones.values():
            if m.status != "done":
                m.status, m.qa_rounds = "todo", 0
    elif step == "release":
        r.reset_verification()
        r.rounds, r.apk_key, r.production, r.production_notes = 0, "", "todo", ""
        r.device_failed_files, r.suite_rewrites = [], 0
    elif step == "device-suite" and profile.device:
        comp = profile.components[profile.device.app_component]
        _drop(root, _files(root, f"{comp.workdir}/{profile.device.test_dir}", (".dart",)))
        r.device_suite, r.device_failed_files, r.apk_key, r.device_passed_once = None, [], "", False
    elif step == "smoke-suite":
        api = profile.components[profile.release.api_component]
        _drop(root, _files(root, api.workdir, (".ts", ".js"), contains="smoke"))
        r.smoke_suite, r.smoke_passed_once = None, False
    state.status, state.stop_reason = "stopped", f"rewound to '{step}'; resume to continue"
    return STEPS[step]


def widen(state: ProjectState, root: Path, task_id: str, paths: list[str], workdirs: dict[str, str]) -> list[str]:
    """Give a task more owned paths (the Architect's amendment, done by the pipeline instead of by hand): updates the
    WBS, the backlog, docs/wbs.md (with a note) and resets the task. Returns problems; empty means done. G2 asks again
    because docs/wbs.md changed."""
    if state.wbs is None:
        return ["this run has no WBS yet"]
    task = state.wbs.task(task_id)
    if task is None:
        return [f"no task {task_id}"]
    wbs = state.wbs.model_copy(deep=True)
    new = next(t for t in wbs.tasks if t.id == task_id)
    new.owns = list(dict.fromkeys([*new.owns, *paths]))
    errors = wbs.w1_ownership(workdirs)
    if errors:
        return errors
    state.wbs = wbs
    if state.backlog is not None:
        for w in state.backlog.work_items:
            if w.id == task_id:
                w.owns = list(new.owns)
    p = state.build.item(task_id)
    p.status, p.reason, p.attempts = "todo", "", 0
    old = (root / "docs" / "wbs.md")
    header = ""
    if old.is_file() and old.read_text(encoding="utf-8").startswith("---\n"):
        header = old.read_text(encoding="utf-8").split("---\n", 2)[1]
        header = "".join(ln + "\n" for ln in header.splitlines() if not ln.startswith(("amended:", "written_at:")))
    stamp = f"amended: {task_id} also owns {', '.join(paths)} (ownership refusal while building)\n"
    (root / "docs" / "wbs.md").write_text("---\n" + header + stamp + "---\n" + wbs.to_markdown(), encoding="utf-8")
    state.status, state.stop_reason = "stopped", f"{task_id} widened; G2 asks again, then resume"
    return []
