"""Integrate and Verify: what a milestone must prove before anyone reviews it. All of it is deterministic
(exit codes and hashes), so no agent can talk its way past it.

Integrator (step 7): the combined build still builds and passes its tests (regressions), every planned task
of the milestone is done, the contract copies match the approved contract, the locked acceptance tests are
unchanged, and no secret is in the code. It fixes nothing; findings go back to the builders as bugs.

Verify scans (step 8): the profile's per-component `scans` (lint, dependency audit, coverage...), each saved
raw under reports/ so that a "pass" always has a report file behind it.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from agentic_sdlc.artifacts.backlog import Milestone, WorkItem
from agentic_sdlc.artifacts.reports import Bug
from agentic_sdlc.guardrails import code as code_guardrails
from agentic_sdlc.guardrails import secrets

log = logging.getLogger(__name__)

SEVERITY_BY_SCAN = {"blocker": "blocker", "major": "major", "minor": "minor"}


@dataclass
class VerifyResult:
    bugs: list[Bug] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)      # human-readable result per check
    evidence: list[str] = field(default_factory=list)   # report files written

    def extend(self, other: "VerifyResult") -> None:
        self.bugs += other.bugs
        self.lines += other.lines
        self.evidence += other.evidence

    def text(self) -> str:
        return "\n".join(self.lines) or "(nothing was run)"


def _bug(counter: list[int], prefix: str, item: str, title: str, severity: str, steps: str, expected: str,
         actual: str) -> Bug:
    counter[0] += 1
    return Bug(id=f"{prefix}-{counter[0]:03d}", work_item_id=item, title=title, severity=severity,  # type: ignore[arg-type]
               steps=steps, expected=expected, actual=actual[-1500:])


def integrate(b: Any, m: Milestone, items: list[WorkItem], round_no: int) -> VerifyResult:
    """The Integrator's checks for milestone `m`. `b` is the Builder (state, workspace, profile, sandbox)."""
    res, n = VerifyResult(), [0]
    first = items[0].id if items else m.work_item_ids[0]
    done = [i for i in items if b.s.build.item(i.id).status == "done"]

    for i in items:                                  # 1. every planned task is done
        status = b.s.build.item(i.id).status
        if status != "done":
            res.lines.append(f"- {i.id}: {status.upper()} ({b.s.build.item(i.id).reason[:120] or 'not built'})")
            res.bugs.append(_bug(n, "INT", i.id, f"{i.id} is not done ({status})", "major",
                                 "see the build state", "every planned task is built", b.s.build.item(i.id).reason))
    res.lines.append(f"- Tasks: {len(done)} of {len(items)} done")

    for source, copies in (b.profile.guardrails.get("contract_copies") or {}).items():   # 2. one contract
        src = b.ws.root / source
        for copy in copies:
            dst = b.ws.root / copy
            if src.exists() and dst.exists() and dst.read_bytes() != src.read_bytes():
                res.lines.append(f"- Contract: {copy} DIFFERS from {source}")
                res.bugs.append(_bug(n, "INT", first, f"{copy} differs from the approved contract", "blocker",
                                     f"compare {copy} with {source}", "identical copies", "the copy drifted"))
    res.lines.append("- Contract copies: checked")

    changed = code_guardrails.dv1_locked(b.ws, [], b.s.build.locked_tests)        # 3. locked tests intact
    res.lines.append(f"- Locked tests: {len(b.s.build.locked_tests)} files, "
                     f"{'ALL UNCHANGED' if not changed else str(len(changed)) + ' CHANGED'}")
    res.bugs += [_bug(n, "INT", first, "A locked acceptance test changed", "blocker", c.split(" is a locked")[0][5:],
                      "locked tests never change", c) for c in changed]

    leaks = []                                                                      # 4. no secrets in the code
    tracked = [p for p in code_guardrails.tracked_files(b.ws) if p.startswith(
        tuple(c.workdir.rstrip("/") + "/" for c in b.profile.components.values() if c.workdir not in (".", "")))]
    leaks = code_guardrails.dv2_secrets(b.ws, b.profile, tracked)
    res.lines.append(f"- Secret scan: {len(tracked)} files, {len(leaks)} finding(s)")
    res.bugs += [_bug(n, "INT", first, "Secret in the code", "blocker", leak.split(" contains")[0][5:],
                      "no secrets in code", leak) for leak in leaks]

    for comp_name in sorted({i.component for i in done}):                           # 5. the whole build + all tests
        comp = b.profile.components[comp_name]
        if not comp.runtime:
            continue
        owner = next(i.id for i in done if i.component == comp_name)
        for cmd in comp.checks:
            run = b.sandbox.run_trusted(comp.runtime, b.effective_component_for(comp_name).workdir, cmd)
            report = f"reports/integration_{m.id}_r{round_no}_{comp_name}_{_slug(cmd)}.txt"
            b.ws.write_text(report, f"$ {cmd}\nexit {run.exit_code}\n\n{run.output[-30000:]}")
            res.evidence.append(report)
            res.lines.append(f"- {comp_name}: `{cmd}` {'PASSED' if run.ok else 'FAILED (exit ' + str(run.exit_code) + ')'}")
            if not run.ok:
                res.bugs.append(_bug(n, "INT", owner, f"Integrated {comp_name} fails `{cmd}`", "blocker",
                                     f"run `{cmd}` in {comp.workdir}", "the combined build is green", run.output))
    return res


def scans(b: Any, m: Milestone, items: list[WorkItem], round_no: int) -> VerifyResult:
    """The profile's `scans` for the components built in this milestone; raw output saved under reports/."""
    res, n = VerifyResult(), [0]
    for comp_name in sorted({i.component for i in items if b.s.build.item(i.id).status == "done"}):
        comp = b.profile.components[comp_name]
        owner = next(i.id for i in items if i.component == comp_name and b.s.build.item(i.id).status == "done")
        for scan in comp.scans:
            if not comp.runtime:
                continue
            run = b.sandbox.run_trusted(comp.runtime, b.effective_component_for(comp_name).workdir, scan.command,
                                        host_network=scan.network)
            report = f"reports/scan_{m.id}_r{round_no}_{comp_name}_{_slug(scan.name)}.txt"
            b.ws.write_text(report, f"$ {scan.command}\nexit {run.exit_code}\n\n{run.output[-30000:]}")
            res.evidence.append(report)
            res.lines.append(f"- {comp_name} {scan.name}: {'PASSED' if run.ok else 'FAILED'} (`{scan.command}`, {report})")
            if not run.ok and scan.severity != "info":
                res.bugs.append(_bug(n, "SCAN", owner, f"{scan.name} failed in {comp_name}", scan.severity,
                                     f"run `{scan.command}`", "the scan passes", run.output))
    if not res.lines:
        res.lines.append("- (this profile defines no scans for the built components)")
    return res


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:30]


def signature(bugs: list[Bug]) -> frozenset[str]:
    """What is still wrong, independent of wording: used to notice a fix round that changed nothing."""
    return frozenset(hashlib.sha1(f"{b.work_item_id}|{b.title}".encode()).hexdigest()[:10] for b in bugs)
