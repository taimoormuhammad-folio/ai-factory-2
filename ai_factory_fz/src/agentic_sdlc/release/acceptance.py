"""The Acceptor: proof per acceptance criterion. Deterministic on purpose: an agent that writes "AC-03 met" proves
nothing, so every criterion is run against its locked test and kept with the output that shows it.

For each criterion of the spec (AC-xx) the Acceptor
  - finds the locked tests that prove it (docs/test-plan.md, from the Test Writer);
  - runs the component's acceptance suite once and keeps its full output;
  - writes evidence/AC-xx/: the test file(s), the lines of the run that name the criterion, the suite verdict,
    and the device screenshots when the proof is an app test;
  - marks it MET only when the suite passed, the test file still has its locked hash and the run names the
    criterion; anything else is NOT MET with the reason.
It then writes docs/acceptance.md, docs/uat-guide.md and evidence-manifest.sha256: the hash of every evidence file,
report and release document plus a fingerprint of the factory itself (config, profile, framework commit), so the
approvers can tell later that nothing changed after they signed.
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from agentic_sdlc.build.services import ServiceError, environment_failure
from agentic_sdlc.settings import CONFIG_DIR

MANIFEST = "evidence-manifest.sha256"
EVIDENCE = "evidence"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def factory_fingerprint(profile_root: Path) -> dict:
    """What produced this run: config files, the profile, and the framework's git commit."""
    files = sorted(CONFIG_DIR.glob("*.yaml")) + sorted(profile_root.rglob("*.yaml")) + sorted(profile_root.rglob("*.md"))
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=CONFIG_DIR, capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return {"framework_commit": commit, "files": {f.name if f.parent == CONFIG_DIR else f"{f.parent.name}/{f.name}": _sha(f)
                                                  for f in files}}


class Acceptor:
    def __init__(self, state, workspace, profile, run_suite: Callable, *, checkpoint: Callable[[str], None] = lambda m: None):
        self.s, self.ws, self.profile, self.run_suite, self.checkpoint = state, workspace, profile, run_suite, checkpoint

    def accept(self) -> None:
        r, root = self.s.release, self.ws.root
        shutil.rmtree(root / EVIDENCE, ignore_errors=True)
        suites = self._run_suites()
        met, unmet, rows = [], [], []
        for story_id, c in self.s.prd.criteria():
            if not c.id:
                continue
            verdict, why = self._prove(c.id, suites)
            (met if verdict else unmet).append(c.id)
            rows.append((c.id, story_id, c, verdict, why))
        r.acceptance_met, r.acceptance_unmet = met, unmet
        self.ws.write_text("docs/acceptance.md", self._acceptance_markdown(rows))
        self.ws.write_text("docs/uat-guide.md", self._uat_markdown(rows))
        r.evidence_manifest = self._write_manifest()
        self.checkpoint(f"Acceptance: {len(met)} met, {len(unmet)} not met")

    # ---------- running the suites ----------

    def _run_suites(self) -> dict[str, dict]:
        """component -> {command, exit, output, error, files{path: locked ok?}} for every component with locked tests."""
        out: dict[str, dict] = {}
        for name, comp in self.profile.components.items():
            acc = comp.acceptance
            if not acc:
                continue
            folder = acc.dir.rstrip("/") + "/"
            locked = {p: h for p, h in self.s.build.locked_tests.items() if p.startswith(folder)}
            if not locked:
                continue
            info = {"command": acc.command, "exit": None, "output": "", "error": "",
                    "intact": {p: (self.ws.root / p).is_file() and _sha(self.ws.root / p) == h for p, h in locked.items()}}
            try:
                run = self.run_suite(comp, acc)
                info["exit"], info["output"] = run.exit_code, run.output
            except ServiceError as e:
                info["error"] = str(e)
            out[name] = info
            self.ws.write_text(f"{EVIDENCE}/_suites/{name}.txt",
                               f"$ {acc.command}\nexit {info['exit']}\n{info['error']}\n\n{info['output'][-60000:]}")
        return out

    # ---------- one criterion ----------

    def _prove(self, ac_id: str, suites: dict[str, dict]) -> tuple[bool, str]:
        tests = [(key, t) for key, suite in sorted(self.s.build.acceptance.items()) for t in suite.tests if t.ac_id == ac_id]
        if not tests:
            return False, "no acceptance test is mapped to this criterion"
        reasons, proven = [], False
        folder = Path(EVIDENCE) / ac_id
        lines_out: list[str] = []
        for key, t in tests:
            comp = key.split("/", 1)[-1]
            info = suites.get(comp)
            if info is None:
                reasons.append(f"{t.file}: the {comp} suite did not run")
                continue
            if info["error"]:
                reasons.append(f"{t.file}: the {comp} suite could not run ({info['error'][:160]})")
                continue
            if not info["intact"].get(t.file, False):
                reasons.append(f"{t.file}: the locked test file is missing or was changed")
                continue
            if info["exit"] != 0:
                broken = environment_failure(info["output"])
                reasons.append(f"{t.file}: the {comp} suite FAILED (exit {info['exit']})" +
                               (f"; environment problem: {broken}" if broken else ""))
                continue
            named = [ln.strip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", info["output"]).splitlines() if ac_id.lower() in ln.lower()]
            if not named:
                reasons.append(f"{t.file}: the run's output does not name {ac_id}, so it is not proof")
                continue
            proven = True
            src = self.ws.root / t.file
            self.ws.write_text(str(folder / "tests" / Path(t.file).name), src.read_text(encoding="utf-8", errors="replace"))
            lines_out += [f"[{key}] {ln}" for ln in named[:20]]
        if proven:
            shots = self.s.release.device_screenshots if any(k.endswith("frontend") for k, _ in tests) else []
            for rel in shots:
                p = self.ws.root / rel
                if p.is_file():
                    dest = folder / "screens" / p.name
                    (self.ws.root / dest).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, self.ws.root / dest)
            self.ws.write_text(str(folder / "result.md"), "\n".join([
                f"# {ac_id}: MET", "", *(f"- {t.file} :: {t.test_name}" for _, t in tests), "",
                "## Lines of the run that name the criterion", "```", *lines_out, "```"]))
            return True, ""
        self.ws.write_text(str(folder / "result.md"), f"# {ac_id}: NOT MET\n\n" + "\n".join(f"- {x}" for x in reasons))
        return False, "; ".join(reasons)

    # ---------- documents ----------

    def _acceptance_markdown(self, rows) -> str:
        r = self.s.release
        lines = ["# Acceptance", "",
                 f"{len(r.acceptance_met)} of {len(rows)} criteria met with evidence; {len(r.acceptance_unmet)} not met.",
                 "A criterion is met only when its locked test passed in this acceptance run and the run names it.", "",
                 "| Criterion | Story | Result | Evidence / reason |", "|---|---|---|---|"]
        for ac_id, story_id, c, ok, why in rows:
            lines.append(f"| {ac_id} | {story_id} | {'MET' if ok else 'NOT MET'} | "
                         f"{'evidence/' + ac_id + '/result.md' if ok else why.replace('|', '/')} |")
        if r.device_note or r.device_passed is not None:
            lines += ["", "## Device", f"Passed: {r.device_passed}. {r.device_note}".strip()]
        return "\n".join(lines) + "\n"

    def _uat_markdown(self, rows) -> str:
        lines = ["# User acceptance guide", "",
                 "Try each criterion yourself on the staging build (or the APK) and mark it; the evidence folder shows",
                 "what the pipeline saw, this guide is what a person should see.", ""]
        for ac_id, story_id, c, ok, why in rows:
            lines += [f"## {ac_id} ({story_id}), pipeline result: {'MET' if ok else 'NOT MET'}",
                      f"- Given {c.given}", f"- When {c.when}", f"- Then {c.then}", "- [ ] I tried it and it is right", ""]
        return "\n".join(lines)

    # ---------- the manifest ----------

    def _write_manifest(self) -> str:
        root = self.ws.root
        self.ws.write_text(f"{EVIDENCE}/_factory.json", json.dumps(factory_fingerprint(self.profile.root), indent=2, sort_keys=True))
        paths = [p for p in sorted((root / EVIDENCE).rglob("*")) if p.is_file()]
        paths += [root / d for d in ("docs/acceptance.md", "docs/uat-guide.md", "docs/test-plan.md", "docs/spec.md", "tests.lock")
                  if (root / d).is_file()]
        paths += sorted((root / "reports").glob("release_round*.md")) + sorted((root / "reports").glob("acceptance_*.txt"))
        text = "".join(f"{_sha(p)}  {p.relative_to(root).as_posix()}\n" for p in paths)
        (root / MANIFEST).write_text(text, encoding="utf-8")
        return hashlib.sha256(text.encode()).hexdigest()


def verify_manifest(root: Path) -> list[str]:
    """Paths whose content no longer matches evidence-manifest.sha256 (empty: everything is as signed)."""
    f = root / MANIFEST
    if not f.is_file():
        return [MANIFEST + " (missing)"]
    bad = []
    for line in f.read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        p = root / rel
        if not p.is_file() or _sha(p) != digest:
            bad.append(rel)
    return bad
