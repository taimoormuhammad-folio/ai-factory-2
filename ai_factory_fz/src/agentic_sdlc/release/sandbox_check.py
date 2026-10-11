"""Real-site sandbox check: the built API against the real third-party sandbox (e.g. the NetSuite SB2 site)
instead of the mock, with read-only requests only. It catches what a mock cannot: field names, defaults and
errors of the real system (the first NetSuite run found its checkout defaults this way, by hand, after release).

Safe by construction:
  - the env file (site URL, test account) lives in a git-ignored file; a missing or tracked file means the check
    is NOT RUN (reported as such, never a failure and never a block);
  - order submission is forced off by a compose override, whatever the env file says;
  - only GET requests, from the profile's list, and the containers are always removed afterwards.
"""

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agentic_sdlc.registry.profiles import SandboxCheckConfig, SandboxRequest
from agentic_sdlc.release.staging import http_get

OVERRIDE = "infra/.sandbox.override.yml"


@dataclass
class SandboxResult:
    ran: bool
    passed: bool | None = None
    note: str = ""
    problems: list[str] = field(default_factory=list)
    output: str = ""


def _run(cmd: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-4000:]


class SandboxCheck:
    def __init__(self, root: Path, cfg: SandboxCheckConfig, api_service: str = "api",
                 run: Callable = _run, get: Callable = http_get, sleep: Callable = time.sleep):
        self.root, self.cfg, self.api_service, self.run, self.get, self.sleep = root, cfg, api_service, run, get, sleep

    def why_not(self) -> str | None:
        env = self.root / self.cfg.env_file
        if not (self.root / self.cfg.compose_file).is_file():
            return f"{self.cfg.compose_file} does not exist (the Deployment engineer writes it)"
        if not env.is_file():
            return (f"{self.cfg.env_file} does not exist: create it with the sandbox site URL and test account "
                    f"(keep it out of git) to enable this check")
        try:
            tracked = self.run(["git", "ls-files", "--error-unmatch", self.cfg.env_file], self.root, 20)[0] == 0
        except (OSError, subprocess.SubprocessError):
            tracked = False
        if tracked:
            return f"{self.cfg.env_file} is tracked by git; it holds credentials, so the check will not use it (git rm --cached it)"
        return None

    def check(self) -> SandboxResult:
        reason = self.why_not()
        if reason:
            return SandboxResult(ran=False, note=f"sandbox check not run: {reason}")
        (self.root / OVERRIDE).write_text(
            f"services:\n  {self.api_service}:\n    environment:\n      CHECKOUT_SUBMIT_ENABLED: \"false\"\n", encoding="utf-8")
        base = ["docker", "compose", "-p", self.cfg.project, "-f", self.cfg.compose_file, "-f", OVERRIDE]
        url = f"http://localhost:{self.cfg.port}"
        problems, lines = [], []
        try:
            code, out = self.run([*base, "up", "--build", "-d"], self.root, 1200)
            if code != 0:
                return SandboxResult(ran=True, passed=False, output=out,
                                     problems=[f"The sandbox API did not start:\n{out[-1500:]}"])
            if not self._wait(url + self.cfg.health_path):
                _, logs = self.run([*base, "logs", "--tail", "60", self.api_service], self.root, 60)
                return SandboxResult(ran=True, passed=False, output=logs,
                                     problems=[f"The sandbox API never became healthy ({self.cfg.health_path}):\n{logs[-1500:]}"])
            for c in self._requests():
                status, body = self.get(url + c.path, timeout=30)
                ok = (0 < status < 500) if c.expect == 0 else status == c.expect
                lines.append(f"GET {c.path} -> {status or 'no response'} ({'ok' if ok else 'expected ' + (str(c.expect) if c.expect else 'below 500')})")
                if not ok:
                    problems.append(f"Sandbox check GET {c.path} returned {status or 'no response'}, expected "
                                    f"{c.expect or 'an answer below 500'}: {body[:600]}")
        finally:
            self.run([*base, "down", "-v", "--remove-orphans"], self.root, 300)
            try:
                (self.root / OVERRIDE).unlink()
            except OSError:
                pass
        return SandboxResult(ran=True, passed=not problems, problems=problems, output="\n".join(lines),
                             note="sandbox check passed" if not problems else "sandbox check failed")

    def _requests(self) -> list[SandboxRequest]:
        """The configured checks plus every parameter-free GET of the project's own API contract."""
        requests = list(self.cfg.checks)
        contract = self.root / "docs" / "api-contract.yaml"
        if self.cfg.contract_gets and contract.is_file():
            import yaml

            try:
                doc = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                doc = {}
            prefix = ""
            for server in doc.get("servers") or []:
                prefix = "/" + str(server.get("url", "")).split("://")[-1].partition("/")[2].strip("/")
                break
            have = {r.path for r in requests}
            for path, ops in sorted((doc.get("paths") or {}).items()):
                get = (ops or {}).get("get")
                if get is None or "{" in path:
                    continue
                if any(p.get("required") for p in (get.get("parameters") or []) if isinstance(p, dict) and p.get("in") != "header"):
                    continue
                full = (prefix.rstrip("/") if prefix != "/" else "") + path
                if full not in have:
                    requests.append(SandboxRequest(path=full, expect=0))
        return requests

    def _wait(self, url: str) -> bool:
        deadline = time.monotonic() + self.cfg.startup_timeout_s
        while time.monotonic() < deadline:
            if self.get(url, timeout=5)[0] == 200:
                return True
            self.sleep(3)
        return False
