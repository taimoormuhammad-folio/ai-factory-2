"""DeepEval judge backend using Cursor CLI + CURSOR_API_KEY (same as the factory pipeline)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from deepeval.models.base_model import DeepEvalBaseLLM


def _parse_version_dir(name: str) -> int:
    date_part = name.split("-")[0]
    year, month, day = date_part.split(".")
    return int(f"{year}{month.zfill(2)}{day.zfill(2)}")


def _resolve_agent_cli() -> tuple[str, str]:
    base = Path.home() / "AppData" / "Local" / "cursor-agent"
    versions_dir = base / "versions"
    if versions_dir.is_dir():
        version_dirs = [d for d in versions_dir.iterdir() if d.is_dir() and d.name[0].isdigit()]
        if version_dirs:
            latest = max(version_dirs, key=lambda d: _parse_version_dir(d.name))
            node = latest / "node.exe"
            index = latest / "index.js"
            if node.exists() and index.exists():
                return str(node), str(index)
    agent_bin = shutil.which("agent") or shutil.which("agent.cmd")
    if agent_bin:
        return agent_bin, ""
    local_agent = base / "agent.cmd"
    if local_agent.exists():
        return str(local_agent), ""
    raise RuntimeError(
        "Cursor CLI (agent) not found. Install: irm 'https://cursor.com/install?win32=true' | iex"
    )


class CursorJudgeLLM(DeepEvalBaseLLM):
    """GEval judge via `agent -p --trust` and CURSOR_API_KEY from ai_factory_fz/.env."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int | None = None,
        **kwargs: Any,
    ) -> None:
        self._cursor_model = (model or os.getenv("DEEPEVAL_JUDGE_MODEL") or os.getenv("CURSOR_PROXY_MODEL", "auto")).strip()
        self._api_key = (api_key or os.getenv("CURSOR_API_KEY", "")).strip()
        self._timeout = int(timeout_seconds or os.getenv("CURSOR_AGENT_TIMEOUT", "600"))
        super().__init__(model=self._cursor_model or "auto", **kwargs)

    def load_model(self, *args: Any, **kwargs: Any) -> CursorJudgeLLM:
        return self

    def get_model_name(self, *args: Any, **kwargs: Any) -> str:
        return f"cursor/{self._cursor_model or 'auto'}"

    def generate(self, prompt: str, **kwargs: Any) -> str:
        api_key = self._api_key
        if not api_key:
            raise RuntimeError("CURSOR_API_KEY is not set (configure ai_factory_fz/.env).")
        env = os.environ.copy()
        env["CURSOR_API_KEY"] = api_key
        node_exe, index_js = _resolve_agent_cli()
        cwd = Path(__file__).resolve().parent.parent
        if index_js:
            cmd = [node_exe, index_js, "-p", "--trust"]
        else:
            cmd = [node_exe, "-p", "--trust"]
        if self._cursor_model.lower() not in ("", "auto", "default"):
            cmd.extend(["--model", self._cursor_model])
        result = subprocess.run(
            cmd,
            input=prompt,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Cursor judge failed (exit {result.returncode}): {stderr}")
        return (result.stdout or "").strip()

    async def a_generate(self, prompt: str, **kwargs: Any) -> str:
        return await asyncio.to_thread(self.generate, prompt, **kwargs)
