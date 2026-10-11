"""sdlc-v2 async gate bridge helpers."""

from __future__ import annotations

import json
from pathlib import Path


def test_read_pending_gate(tmp_path: Path, monkeypatch) -> None:
    import fz_gates as gates

    monkeypatch.setattr(gates, "FZ_RUNS_DIR", tmp_path)
    run_id = "run1"
    gdir = tmp_path / run_id / "gates"
    gdir.mkdir(parents=True)
    (gdir / "G1.pending.json").write_text(
        json.dumps(
            {
                "gate": "prd",
                "gate_id": "G1",
                "summary": "3 stories",
                "documents": ["docs/prd.md"],
                "artifact_hashes": {"docs/prd.md": "abc"},
            }
        ),
        encoding="utf-8",
    )
    pending = gates.read_pending_gate(run_id)
    assert pending is not None
    assert pending["gate"] == "prd"
    assert pending["gate_id"] == "G1"
    assert pending["ui_index"] == 1
    assert pending["kind"] == "spec"


def test_record_gate_decision(tmp_path: Path, monkeypatch) -> None:
    import fz_gates as gates

    monkeypatch.setattr(gates, "FZ_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gates, "AI_FACTORY_FZ_ROOT", Path("e:/aifactory/ai_factory_fz"))
    run_id = "run2"
    root = tmp_path / run_id
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "prd.md").write_text("# PRD\n", encoding="utf-8", newline="\n")
    gdir = root / "gates"
    gdir.mkdir()
    import hashlib

    h = hashlib.sha256((root / "docs" / "prd.md").read_bytes()).hexdigest()
    (gdir / "G1.pending.json").write_text(
        json.dumps(
            {
                "gate": "prd",
                "gate_id": "G1",
                "summary": "ok",
                "documents": ["docs/prd.md"],
                "artifact_hashes": {"docs/prd.md": h},
            }
        ),
        encoding="utf-8",
    )
    result = gates.record_gate_decision(
        run_id,
        "prd",
        approved=True,
        approver="Pat Owner",
        role="product owner",
    )
    assert result["approved"] is True
    assert result["gate_id"] == "G1"
    assert (gdir / "G1.gate").is_file()
    assert not (gdir / "G1.pending.json").exists()


def test_agent_like_approver_rejected(tmp_path: Path, monkeypatch) -> None:
    import fz_gates as gates

    monkeypatch.setattr(gates, "FZ_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gates, "AI_FACTORY_FZ_ROOT", Path("e:/aifactory/ai_factory_fz"))
    run_id = "run3"
    root = tmp_path / run_id
    (root / "gates").mkdir(parents=True)
    (root / "gates" / "G1.pending.json").write_text(
        json.dumps(
            {
                "gate": "prd",
                "gate_id": "G1",
                "summary": "ok",
                "documents": [],
                "artifact_hashes": {},
            }
        ),
        encoding="utf-8",
    )
    try:
        gates.record_gate_decision(run_id, "prd", approved=True, approver="Claude")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "named people" in str(exc).lower() or "agent" in str(exc).lower()
