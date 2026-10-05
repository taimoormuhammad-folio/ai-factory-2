"""Human approval gates. The flow pauses here until a person approves or rejects.

Modes (pipeline `gate_mode`, overridden by SDLC_GATE_MODE):
  console  ask on the terminal of the running pipeline
  async    stop the run and wait; a person runs `uv run approve <run_id> <G>`, then `uv run resume`
  auto     approve without asking: only for demo pipelines (`demo: true`) at risk tier L
"""

import getpass
import os
from pathlib import Path
from typing import Callable, Literal

from agentic_sdlc.gates.files import approver_problem
from agentic_sdlc.state import GateDecision

GateMode = Literal["console", "async", "auto"]
InputFn = Callable[[str], str]
MODES = ("console", "async", "auto")


def gate_mode(configured: str, demo: bool = False, risk_tier: str = "M") -> GateMode:
    mode = os.environ.get("SDLC_GATE_MODE", configured)
    if mode not in MODES:
        raise ValueError(f"Unknown gate mode '{mode}' (use console, async or auto)")
    if mode == "auto" and not (demo and risk_tier == "L"):
        raise ValueError("gate_mode auto is only allowed in demo pipelines (demo: true) at risk tier L; "
                         "use console or async so a named person approves")
    return mode  # type: ignore[return-value]


def console_approver() -> str:
    """Who is answering at this terminal: SDLC_APPROVER, else the OS user."""
    return os.environ.get("SDLC_APPROVER") or getpass.getuser()


def print_request(gate: str, summary: str, documents: list[Path]) -> None:
    print("\n" + "=" * 72)
    print(f"APPROVAL NEEDED: {gate}")
    print(summary)
    print("Review these documents:")
    for d in documents:
        print(f"  - {d}")
    print("=" * 72)


def ask(gate: str, input_fn: InputFn, need_risk_note: bool = False) -> GateDecision:
    """Ask approve/reject (and feedback, or the approver's risk note) on the given input."""
    while True:
        answer = input_fn("Approve? [y]es / [n]o: ").strip().lower()
        if answer in ("y", "yes"):
            note = ""
            while need_risk_note and len(note) < 20:
                note = input_fn("Risk note, in your own words (at least 20 characters): ").strip()
            notes = input_fn("Optional notes (Enter to skip): ").strip()
            return GateDecision(gate=gate, approved=True, feedback=notes, risk_note=note)
        if answer in ("n", "no"):
            feedback = ""
            while not feedback:
                feedback = input_fn("What should change? ").strip()
            return GateDecision(gate=gate, approved=False, feedback=feedback)


def request_approval(
    gate: str,
    summary: str,
    documents: list[Path],
    mode: GateMode,
    input_fn: InputFn = input,
    need_risk_note: bool = False,
) -> GateDecision:
    """Console and auto modes (async is handled by the flow with gate files)."""
    if mode == "auto":
        return GateDecision(gate=gate, approved=True, decided_by="auto")
    approver = console_approver()
    problem = approver_problem(approver)
    if problem:
        raise ValueError(f"{problem} Set SDLC_APPROVER to your name.")
    print_request(gate, summary, documents)
    decision = ask(gate, input_fn, need_risk_note)
    decision.approver = approver
    return decision
