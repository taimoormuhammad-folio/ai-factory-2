"""Bridge sdlc-v2 async gates (gates/G*.pending.json) to the React console."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from config import AI_FACTORY_FZ_ROOT, FZ_RUNS_DIR

# Gate name → React carousel index + review kind (react-base GateReview).
UI_GATE_META: dict[str, dict[str, Any]] = {
    "prd": {"index": 1, "kind": "spec", "label": "Review specification (G1)"},
    "architecture": {"index": 2, "kind": "architecture", "label": "Review architecture (G2)"},
    "estimate": {"index": 4, "kind": "architecture", "label": "Review estimates (G3)"},
    "ui": {"index": 3, "kind": "design", "label": "Review UI mockups (G4)"},
    "merge": {"index": 7, "kind": "deploy", "label": "Review merge (G5)"},
    "release": {"index": 9, "kind": "smoke", "label": "Review release (G6)"},
    "production": {"index": 10, "kind": "smoke", "label": "Approve production (G7)"},
}

GATE_NAMES = set(UI_GATE_META)


def _ensure_fz_src() -> None:
    src = str(AI_FACTORY_FZ_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def list_pending_gate_files(run_id: str) -> list[Path]:
    gates_dir = FZ_RUNS_DIR / run_id / "gates"
    if not gates_dir.is_dir():
        return []
    return sorted(gates_dir.glob("*.pending.json"))


def read_pending_gate(run_id: str) -> dict[str, Any] | None:
    """Return the first pending gate payload shaped for the React console."""
    files = list_pending_gate_files(run_id)
    if not files:
        return None
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("gate"):
        return None
    gate = str(data["gate"])
    meta = UI_GATE_META.get(gate, {"index": 1, "kind": "spec", "label": f"Review {gate}"})
    docs = data.get("documents") or []
    return {
        "gate": gate,
        "gate_id": data.get("gate_id") or files[0].name.split(".")[0],
        "summary": data.get("summary") or "",
        "documents": docs,
        "artifact_hashes": data.get("artifact_hashes") or {},
        "requested_at": data.get("requested_at") or "",
        "ui_index": meta["index"],
        "kind": meta["kind"],
        "label": meta["label"],
        "needs_risk_note": gate == "merge",
    }


def ui_gate_meta(gate: str) -> dict[str, Any]:
    return UI_GATE_META.get(gate, {"index": 1, "kind": "spec", "label": f"Review {gate}"})


def record_gate_decision(
    run_id: str,
    gate: str,
    *,
    approved: bool,
    feedback: str = "",
    approver: str = "Console User",
    role: str = "",
    risk_note: str = "",
) -> dict[str, Any]:
    """Write gates/<G>.gate using sdlc-v2 gate_files, then the caller should resume the run."""
    _ensure_fz_src()
    from agentic_sdlc.gates import files as gate_files
    from agentic_sdlc.state import GateDecision

    if gate not in GATE_NAMES and not gate.upper().startswith("G"):
        raise ValueError(f"Unknown gate '{gate}'")

    run_dir = FZ_RUNS_DIR / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run {run_id} not found")

    # Accept either gate name (prd) or id (G1)
    gid = gate.upper() if gate.upper().startswith("G") and len(gate) <= 3 else gate_files.gate_id(gate)
    pending = gate_files.read_pending(run_dir, gid)
    if pending is None:
        raise FileNotFoundError(f"Run {run_id} is not waiting for {gid}")

    gate_name = str(pending.get("gate") or gate)
    if gate in GATE_NAMES and gate_name != gate:
        raise ValueError(f"Pending gate is '{gate_name}', not '{gate}'")

    problem = gate_files.approver_problem(approver)
    if problem:
        raise ValueError(problem)

    if not approved and not feedback.strip():
        raise ValueError("Feedback is required when rejecting a gate")
    if approved and gate_name == "merge" and len((risk_note or "").strip()) < 20:
        raise ValueError("Merge gate (G5) requires a risk note of at least 20 characters")

    hashes = pending.get("artifact_hashes") or {}
    changed = gate_files.changed_since(run_dir, hashes)
    if changed:
        raise ValueError(
            "These documents changed since the gate was requested: "
            + ", ".join(changed)
            + ". Resume the run so it asks again."
        )

    decision = GateDecision(
        gate=gate_name,
        approved=bool(approved),
        feedback=(feedback or "").strip(),
        decided_by="human",
        gate_id=gid,
        approver=approver.strip(),
        role=(role or "").strip(),
        risk_note=(risk_note or "").strip(),
        artifact_hashes=hashes,
    )
    gate_files.write_decision(run_dir, decision)
    return {
        "run_id": run_id,
        "gate": gate_name,
        "gate_id": gid,
        "approved": decision.approved,
        "approver": decision.approver,
        "status": "recorded",
    }
