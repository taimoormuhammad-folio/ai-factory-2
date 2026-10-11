"""Run flow-only DeepEval for one pipeline run and update deepeval/status.json.

Invoked by the console API (fz_deepeval) in a background subprocess.
Uses judge credentials from ai_factory_fz/.env (CURSOR_API_KEY, or OPENAI/ANTHROPIC).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEEPEVAL_DIR = Path(__file__).resolve().parent
FLOW_TEST = "flow_test_writeup/test_end_to_end_flow.py"


def _uv_executable() -> Path:
    """uv from repo ai_factory venv (console API often has no uv on PATH)."""
    repo_root = DEEPEVAL_DIR.parent.parent
    bundled = repo_root / "ai_factory" / ".venv" / "Scripts" / "uv.exe"
    if bundled.is_file():
        return bundled
    if os.name != "nt":
        alt = repo_root / "ai_factory" / ".venv" / "bin" / "uv"
        if alt.is_file():
            return alt
    found = shutil.which("uv")
    if found:
        return Path(found)
    raise FileNotFoundError(
        "uv not found. Run: cd ai_factory_fz/deepeval && ..\\..\\ai_factory\\.venv\\Scripts\\uv.exe sync"
    )


def _report_name(run_dir: Path) -> str | None:
    for name in ("eval_report_flow.html", "eval_report.html"):
        if (run_dir / name).is_file():
            return name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepEval flow suite for one run folder")
    parser.add_argument("--run-dir", required=True, help="Absolute path to runs/<run_id>")
    parser.add_argument("--status-file", required=True, help="Path to deepeval/status.json")
    parser.add_argument("--log-file", required=True, help="Path to deepeval/run.log")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    status_path = Path(args.status_file)
    log_path = Path(args.log_file)

    try:
        from dotenv import load_dotenv

        load_dotenv(DEEPEVAL_DIR.parent / ".env", override=False)
    except ImportError:
        pass
    env = {**os.environ, "DEEPEVAL_RUN_DIR": str(run_dir), "PYTHONUTF8": "1"}
    cmd = [str(_uv_executable()), "run", "pytest", FLOW_TEST, "-q"]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    rc = 1
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"DeepEval flow started {datetime.now(timezone.utc).isoformat()}\n")
            log.write(f"run_dir={run_dir}\n")
            log.flush()
            proc = subprocess.run(cmd, cwd=DEEPEVAL_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
            rc = proc.returncode
    except Exception as exc:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\nDeepEval fatal error: {exc}\n")
        rc = 1

    report = _report_name(run_dir)
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {}
    status["status"] = "completed" if rc == 0 else "failed"
    status["finished_at"] = datetime.now(timezone.utc).isoformat()
    status["exit_code"] = rc
    status["has_report"] = report is not None
    status["report_name"] = report
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
