"""Gate files: the audit trail of human decisions, kept in the run folder.

  gates/<G>.pending.json   what the run is waiting for: gate, summary, documents and their sha256
  gates/<G>.gate           the decision (approver, approve/reject, feedback, the hashes approved)
  gates/history/           consumed decisions (a rejection is moved here once the run acts on it)

An approval only counts while every approved document still has the hash it had when approved; if one
changes, the gate is asked again. Decisions are written by people through `uv run approve` (or the
console prompt of a running pipeline), never by agents.
"""

import hashlib
import json
import re
import shutil
from pathlib import Path

from agentic_sdlc.state import GateDecision

# Pipeline gate name -> gate id (G1 spec … G7 production release). More gates are added with their steps.
GATE_IDS = {"prd": "G1", "architecture": "G2", "estimate": "G3", "ui": "G4", "merge": "G5",
            "release": "G6", "production": "G7"}
GATES_DIR = "gates"

# Names an approval may not be recorded under: approvals belong to named people.
_AGENT_LIKE = re.compile(r"\b(claude|codex|cursor|gpt|llm|ai|bot|agent|assistant|crew|automation|system|auto)\b", re.I)


def gate_id(gate: str) -> str:
    return GATE_IDS.get(gate, gate)


def file_hashes(root: Path, docs: list[str]) -> dict[str, str]:
    """sha256 of each existing document (relative path -> hex)."""
    out = {}
    for rel in docs:
        p = root / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def changed_since(root: Path, hashes: dict[str, str]) -> list[str]:
    """Documents whose content differs from (or that disappeared since) the approved hashes."""
    current = file_hashes(root, list(hashes))
    return [rel for rel, h in hashes.items() if current.get(rel) != h]


def approver_problem(name: str) -> str | None:
    """Why this name cannot approve a gate, or None."""
    name = (name or "").strip()
    if len(name) < 2:
        return "Give your name (who is approving)."
    if _AGENT_LIKE.search(name):
        return f"'{name}' looks like an agent or a system; gates are approved by named people."
    return None


def _path(root: Path, gid: str, suffix: str) -> Path:
    return root / GATES_DIR / f"{gid}{suffix}"


def write_pending(root: Path, gate: str, summary: str, docs: list[str], hashes: dict[str, str]) -> Path:
    gid = gate_id(gate)
    p = _path(root, gid, ".pending.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"gate": gate, "gate_id": gid, "summary": summary, "documents": docs,
                             "artifact_hashes": hashes}, indent=2), encoding="utf-8")
    return p


def read_pending(root: Path, gid: str) -> dict | None:
    p = _path(root, gid, ".pending.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def write_decision(root: Path, decision: GateDecision) -> Path:
    p = _path(root, decision.gate_id or gate_id(decision.gate), ".gate")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
    pending = _path(root, decision.gate_id or gate_id(decision.gate), ".pending.json")
    pending.unlink(missing_ok=True)
    return p


def read_decision(root: Path, gid: str) -> GateDecision | None:
    p = _path(root, gid, ".gate")
    return GateDecision.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else None


def archive_decision(root: Path, gid: str) -> None:
    """Move the current decision file to gates/history/ (so a revised artifact is asked about again)."""
    p = _path(root, gid, ".gate")
    if not p.exists():
        return
    history = root / GATES_DIR / "history"
    history.mkdir(parents=True, exist_ok=True)
    n = len(list(history.glob(f"{gid}.*.gate"))) + 1
    shutil.move(str(p), history / f"{gid}.{n}.gate")


def version_documents(root: Path, docs: list[str], gate: str) -> list[str]:
    """Keep the rejected drafts: docs/history/<stem>.v<n><suffix>. Returns the copies written."""
    copies = []
    for rel in docs:
        src = root / rel
        if not src.is_file():
            continue
        history = root / "docs" / "history"
        history.mkdir(parents=True, exist_ok=True)
        n = len(list(history.glob(f"{src.stem}.v*{src.suffix}"))) + 1
        dst = history / f"{src.stem}.v{n}{src.suffix}"
        shutil.copy2(src, dst)
        copies.append(str(dst.relative_to(root)))
    return copies
