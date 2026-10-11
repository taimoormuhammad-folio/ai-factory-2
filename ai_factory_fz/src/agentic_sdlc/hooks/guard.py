"""PreToolUse guard for coding agents (Claude Code hook). Blocks, with a reason the agent sees:

  - writes outside the run folder, to protected paths (gates/, tests.lock, docs/, run state) or to
    locked acceptance tests;
  - writes outside the task's owned paths (when the job has them), except dependency manifests;
  - any write at all, for read-only steps (QA, review);
  - shell commands that deploy, publish or touch production (agents never deploy).

Run as:  python -m agentic_sdlc.hooks.guard <policy.json>   (Claude Code passes the tool call on stdin)
Exit 0 allows the call; exit 2 blocks it and returns stderr to the agent.
The same rules are checked again after the agent finishes (guardrails DV1/DV3), so a CLI without hooks
is still covered.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PROTECTED = ("gates/", "tests.lock", "docs/", "state.json", "status.md", "blocked.md", ".git/", ".sdlc/")
MANIFESTS = {"package.json", "package-lock.json", "pubspec.yaml", "pubspec.lock"}
BLOCKED_COMMANDS = [
    r"\bdeploy\b", r"\bproduction\b", r"\bgit\s+push\b", r"\bnpm\s+publish\b", r"\bdocker\s+(login|push)\b",
    r"\bkubectl\b", r"\bterraform\s+apply\b", r"\bfirebase\s+deploy\b", r"\bflutter\s+pub\s+publish\b",
]


def owned(path: str, globs: list[str]) -> bool:
    import fnmatch

    for g in globs:
        g = g.strip().strip("/")
        if any(ch in g for ch in "*?["):
            base = g.split("*")[0].rstrip("/")
            if fnmatch.fnmatch(path, g) or (g.endswith("/**") and (path == base or path.startswith(base + "/"))):
                return True
        elif g and (path == g or path.startswith(g + "/")):
            return True
    return False


def decide(policy: dict[str, Any], call: dict[str, Any]) -> str | None:
    """The reason to block this tool call, or None to allow it."""
    tool = call.get("tool_name", "")
    data = call.get("tool_input") or {}
    if tool == "Bash":
        command = str(data.get("command", ""))
        for pattern in policy.get("blocked_commands", BLOCKED_COMMANDS):
            if re.search(pattern, command, re.I):
                return f"`{command}` is not allowed: agents never deploy, publish or touch production."
        return None
    if tool not in WRITE_TOOLS:
        return None
    if policy.get("read_only"):
        return "You are read-only in this step: report what you find, never edit files (the builders fix it)."
    root = Path(policy["root"]).resolve()
    target = Path(str(data.get("file_path") or data.get("notebook_path") or "")).expanduser()
    target = (target if target.is_absolute() else root / target).resolve()
    if root not in target.parents:
        return f"{target} is outside the run folder; write only inside the project."
    rel = target.relative_to(root).as_posix()
    if rel in policy.get("locked", []):
        return f"{rel} is a locked acceptance test (tests.lock); change the code, not the test."
    for p in policy.get("protected", PROTECTED):
        if rel == p.rstrip("/") or rel.startswith(p if p.endswith("/") else p + "/") or rel == p:
            return f"{rel} is managed by the pipeline (or by people at the gates); do not edit it."
    owns = policy.get("owns") or []
    if owns:
        folder = (policy.get("workdir") or ".").rstrip("/")
        manifest = rel.rsplit("/", 1)[-1] in MANIFESTS and (folder in (".", "") or rel.startswith(folder + "/"))
        if not (owned(rel, owns) or owned(rel, policy.get("also_allowed", [])) or manifest):
            return (f"{rel} is outside the paths this task owns ({', '.join(owns)}). Change only owned files; "
                    "if the task needs another file, say so in your report (the Architect assigns ownership).")
    return None


def main() -> None:
    policy = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    call = json.loads(sys.stdin.read() or "{}")
    reason = decide(policy, call)
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
