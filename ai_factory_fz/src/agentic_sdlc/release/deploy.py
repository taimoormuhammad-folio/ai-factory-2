"""`uv run deploy <run_id>`: the only way a release reaches production, run by the named approver.

The pipeline ends "ready to deploy" and never deploys itself. This command refuses unless G6 (user acceptance)
and G7 (go-live) are approved, every document the approvers signed is unchanged, the evidence manifest still
matches the files, and the person running it is the one who approved G7. It then runs the configured production
command with that person's own environment and records the log.
"""

import os
import shlex
import subprocess
from pathlib import Path

from agentic_sdlc.gates import files as gate_files
from agentic_sdlc.release.acceptance import verify_manifest
from agentic_sdlc.state import GateDecision, ProjectState


def _latest(state: ProjectState, gate: str) -> GateDecision | None:
    decisions = [d for d in state.gate_history if d.gate == gate]
    return decisions[-1] if decisions and decisions[-1].approved else None


def problems(state: ProjectState, root: Path, approver: str, command: str) -> list[str]:
    """Everything that stops this deployment (empty: go ahead)."""
    out: list[str] = []
    if state.release.production == "deployed":
        return ["This release is already deployed."]
    if state.release.production != "ready":
        out.append(f"The run is not ready to deploy (release state: {state.release.production}); "
                   f"it finishes ready only after G6 and G7 are approved.")
    signed = {}
    for gate, label in (("release", "G6 (user acceptance)"), ("production", "G7 (go-live)")):
        d = _latest(state, gate)
        if d is None:
            out.append(f"{label} is not approved.")
            continue
        signed[gate] = d
        changed = gate_files.changed_since(root, d.artifact_hashes)
        if changed:
            out.append(f"{label} was approved, but these documents changed afterwards: {', '.join(changed)}.")
    bad = verify_manifest(root)
    if bad:
        out.append(f"The evidence no longer matches evidence-manifest.sha256: {', '.join(bad[:8])}"
                   f"{' …' if len(bad) > 8 else ''}.")
    g7 = signed.get("production")
    if g7 and g7.approver and approver.strip().lower() != g7.approver.strip().lower():
        out.append(f"G7 was approved by {g7.approver}; only that person deploys this release (you gave: {approver}).")
    if not command.strip():
        out.append("No production command is configured (release.production_command in the pipeline config).")
    return out


def run(root: Path, command: str, timeout_s: int) -> tuple[bool, str]:
    """Run the production command in the run folder with the caller's environment; returns (ok, log tail)."""
    proc = subprocess.run(shlex.split(command), cwd=root, capture_output=True, text=True, timeout=timeout_s, env=dict(os.environ))
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-6000:]
