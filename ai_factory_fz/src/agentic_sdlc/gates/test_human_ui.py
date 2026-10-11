"""UI gate handshake (gate_pending.json ↔ gate_decision.json)."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_sdlc.gates.human import (
    decision_path,
    pending_path,
    read_pending_gate,
    request_approval,
    write_gate_decision,
)


def test_ui_gate_approve(tmp_path: Path) -> None:
    write_gate_decision(tmp_path, gate="prd", approved=True, feedback="looks good")
    # Seed a pending so request_approval would overwrite; call the wait path via writing first.
    # Direct path: write decision after pending using a short poll by pre-writing decision.
    pending_path(tmp_path).write_text(
        json.dumps({"gate": "prd", "summary": "test", "document_paths": ["docs/prd.md"]}),
        encoding="utf-8",
    )
    # request_approval clears existing decision then waits — so write decision in another thread
    # is heavy. Instead unit-test the helpers used by API + worker.
    write_gate_decision(tmp_path, gate="prd", approved=True, feedback="lgtm", decided_by="ui")
    data = json.loads(decision_path(tmp_path).read_text(encoding="utf-8"))
    assert data["approved"] is True
    assert data["gate"] == "prd"
    assert data["decided_by"] == "ui"


def test_read_pending_gate(tmp_path: Path) -> None:
    assert read_pending_gate(tmp_path) is None
    pending_path(tmp_path).write_text(
        json.dumps({"gate": "architecture", "summary": "ready", "document_paths": ["docs/architecture.md"]}),
        encoding="utf-8",
    )
    pending = read_pending_gate(tmp_path)
    assert pending is not None
    assert pending["gate"] == "architecture"


def test_request_approval_ui_reads_decision(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_GATE_POLL_S", "0.01")
    docs = [tmp_path / "docs" / "prd.md"]
    docs[0].parent.mkdir(parents=True, exist_ok=True)
    docs[0].write_text("# PRD\n", encoding="utf-8")

    # Write decision shortly after pending is created — simulate API while polling.
    import threading
    import time

    def approve_soon() -> None:
        time.sleep(0.05)
        # Wait until pending exists
        for _ in range(50):
            if pending_path(tmp_path).is_file():
                break
            time.sleep(0.01)
        write_gate_decision(tmp_path, gate="prd", approved=True, feedback="", decided_by="ui")

    threading.Thread(target=approve_soon, daemon=True).start()
    decision = request_approval(
        "prd",
        "12 stories",
        docs,
        "ui",
        run_dir=tmp_path,
    )
    assert decision.approved is True
    assert decision.decided_by == "ui"
    assert not pending_path(tmp_path).is_file()
    assert not decision_path(tmp_path).is_file()


def test_request_approval_ui_reject_requires_feedback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_GATE_POLL_S", "0.01")
    docs = [tmp_path / "docs" / "prd.md"]
    docs[0].parent.mkdir(parents=True, exist_ok=True)
    docs[0].write_text("# PRD\n", encoding="utf-8")

    import threading
    import time

    def reject_then_approve() -> None:
        time.sleep(0.05)
        for _ in range(50):
            if pending_path(tmp_path).is_file():
                break
            time.sleep(0.01)
        # Invalid reject (no feedback) is ignored
        write_gate_decision(tmp_path, gate="prd", approved=False, feedback="", decided_by="ui")
        time.sleep(0.05)
        write_gate_decision(
            tmp_path,
            gate="prd",
            approved=False,
            feedback="Add checkout story",
            decided_by="ui",
        )

    threading.Thread(target=reject_then_approve, daemon=True).start()
    decision = request_approval("prd", "summary", docs, "ui", run_dir=tmp_path)
    assert decision.approved is False
    assert "checkout" in decision.feedback
