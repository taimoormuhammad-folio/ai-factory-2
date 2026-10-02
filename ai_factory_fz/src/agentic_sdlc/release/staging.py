"""Staging environment: start the built API, wait until healthy, stop it afterwards.

Two ways, chosen automatically:
  compose  Docker usable -> `docker compose up` with the compose file the Deployment engineer wrote.
  local    no Docker -> run the API as a local process against SDLC_STAGING_DATABASE_URL.
"""

import json
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from agentic_sdlc.registry.profiles import Profile
from agentic_sdlc.tools import docker_access
from agentic_sdlc.tools.sandbox_exec import SandboxMode, SandboxRunner
from agentic_sdlc.workspace import Workspace


class StagingError(RuntimeError):
    """Staging did not start. `environment` is True when the machine is the cause (port taken,
    Docker unusable, no database configured), not the code: a developer can't fix that."""

    def __init__(self, message: str, environment: bool = False):
        super().__init__(message)
        self.environment = environment


# Docker/compose messages that mean the machine, not the application, is the problem.
_ENVIRONMENT_ERRORS = ("port is already allocated", "address already in use", "Cannot connect to the Docker daemon",
                       "permission denied while trying to connect to the docker", "no space left on device",
                       "toomanyrequests", "TLS handshake timeout", "i/o timeout")
STAGING_LABEL = "agentic-sdlc.staging"   # on every staging container the pipeline starts (value: run id)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


_PROGRESS = ("Downloading", "Extracting", "Pulling fs layer", "Waiting", "Verifying Checksum",
             "Download complete", "Pull complete", "Already exists")


def _without_progress(output: str) -> str:
    """Drop image download progress so the actual error is what remains at the end."""
    return "\n".join(line for line in output.splitlines() if not any(p in line for p in _PROGRESS))


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    """GET a URL; returns (status, body). Status 0 means no connection."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return 0, ""


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Staging:
    def __init__(self, workspace: Workspace, profile: Profile, sandbox: SandboxRunner, port: int = 3100,
                 startup_timeout_s: int = 90):
        self.ws = workspace
        self.rel = profile.release
        self.database = profile.database
        self.comp = profile.components[profile.release.api_component]
        self.sandbox = sandbox
        self.port = port
        self.startup_timeout_s = startup_timeout_s
        self.base_url = f"http://localhost:{port}"
        self._proc: subprocess.Popen | None = None
        self._log_file = None
        self.extra_env: dict[str, str] = {}   # e.g. device-reachable asset URLs, set before start()

    @property
    def mode(self) -> str:
        return "compose" if self.sandbox.mode is SandboxMode.DOCKER else "local"

    def unavailable_reason(self) -> str | None:
        if self.mode == "compose":
            return self.sandbox.unavailable_reason(self.comp.runtime)
        if self.database and not os.environ.get("SDLC_STAGING_DATABASE_URL"):
            return ("Local staging needs a PostgreSQL database: set SDLC_STAGING_DATABASE_URL in .env "
                    "(an empty database it may reset), or use Docker (build.sandbox: docker)")
        return self.sandbox.unavailable_reason(self.comp.runtime)

    def _env(self) -> dict[str, str]:
        env = read_env_file(self.ws.resolve(self.rel.env_file))
        env.update({"PORT": str(self.port), "NODE_ENV": env.get("NODE_ENV", "production")})
        if self.mode == "local" and self.database:
            env["DATABASE_URL"] = os.environ["SDLC_STAGING_DATABASE_URL"]
        env.update(self.extra_env)
        return env

    def start(self) -> None:
        reason = self.unavailable_reason()
        if reason:
            raise StagingError(reason, environment=True)
        self._free_port()
        (self._start_compose if self.mode == "compose" else self._start_local)()
        self._wait_healthy()

    def _compose(self, *args: str) -> subprocess.CompletedProcess:
        # Overrides for the API service, kept out of the repo: a label that marks it as pipeline
        # staging (so a later run can recognise and stop it) and extra env (beats env_file).
        override = self.ws.resolve(".sdlc/compose.override.yml")
        override.parent.mkdir(parents=True, exist_ok=True)
        lines = ["services:", f"  {self.rel.compose_api_service}:", "    labels:",
                 f"      {STAGING_LABEL}: {json.dumps(self.ws.root.name)}"]
        if self.extra_env:
            lines.append("    environment:")
            lines += [f"      {k}: {json.dumps(v)}" for k, v in self.extra_env.items()]
        override.write_text("\n".join(lines) + "\n", encoding="utf-8")
        files = ["-f", self.rel.compose_file, "-f", str(override)]
        cmd = ["docker", "compose", "--progress", "plain", *files, "--env-file", self.rel.env_file, *args]
        env = {**os.environ, "STAGING_PORT": str(self.port)}
        return docker_access.run(cmd, cwd=self.ws.root, capture_output=True, text=True, timeout=1800, env=env)

    def _start_compose(self) -> None:
        for f in (self.rel.compose_file, self.rel.env_file):
            if not self.ws.resolve(f).exists():
                raise StagingError(f"{f} is missing; the Deployment engineer must create it")
        r = self._compose("up", "-d", "--build", "--quiet-pull")
        if r.returncode != 0:
            output = _without_progress(r.stdout + r.stderr)
            env_problem = next((e for e in _ENVIRONMENT_ERRORS if e.lower() in output.lower()), None)
            raise StagingError(f"docker compose up failed{' (machine problem: ' + env_problem + ')' if env_problem else ''}:"
                               f"\n{output[-4000:]}", environment=bool(env_problem))

    # ---------- the port ----------

    def _free_port(self) -> None:
        """Make sure nothing else answers on the staging port, or the health check and contract check
        would test whatever is there. Staging stacks the pipeline started earlier (e.g. left running
        by `uv run showcase`) are stopped; anything else is a machine problem for the human."""
        if not port_in_use(self.port):
            return
        for project, run in self._sdlc_stacks_on_port():
            print(f"Stopping earlier staging '{project}' (run {run or 'unknown'}) that holds port {self.port}", flush=True)
            ids = docker_access.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                                    capture_output=True, text=True, timeout=60).stdout.split()
            if ids:
                docker_access.run(["docker", "stop", *ids], capture_output=True, text=True, timeout=120)
        deadline = time.monotonic() + 10
        while port_in_use(self.port) and time.monotonic() < deadline:
            time.sleep(1)
        if port_in_use(self.port):
            raise StagingError(f"Port {self.port} is already in use by something that is not this pipeline's staging "
                               f"({self._port_owner() or 'unknown process'}). Stop it, or set release.staging_port in "
                               "the pipeline config, then resume the run.", environment=True)

    def _containers_on_port(self) -> list[dict[str, str]]:
        if self.mode != "compose" or docker_access.problem():
            return []
        fmt = ('{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "' + STAGING_LABEL + '"}}'
               '\t{{.Label "com.docker.compose.project.config_files"}}')
        r = docker_access.run(["docker", "ps", "--filter", f"publish={self.port}", "--format", fmt],
                              capture_output=True, text=True, timeout=60)
        rows = []
        for line in r.stdout.splitlines():
            name, project, run, files = (line.split("\t") + ["", "", "", ""])[:4]
            rows.append({"name": name, "project": project, "run": run, "files": files})
        return rows

    def _sdlc_stacks_on_port(self) -> list[tuple[str, str]]:
        """(compose project, run id) of pipeline staging stacks publishing the port. Recognised by the
        label, or (stacks from before the label existed) by the pipeline's override file."""
        return list(dict.fromkeys((c["project"], c["run"]) for c in self._containers_on_port()
                                  if c["project"] and (c["run"] or ".sdlc/compose.override.yml" in c["files"])))

    def _port_owner(self) -> str:
        names = [c["name"] for c in self._containers_on_port()]
        return "container " + ", ".join(names) if names else ""

    def _api_running(self) -> bool:
        """In compose mode, the API that answers must be this stack's API service."""
        if self.mode != "compose":
            return True
        r = self._compose("ps", "--status", "running", "--services")
        return self.rel.compose_api_service in r.stdout.split()

    def _start_local(self) -> None:
        env = self._env()
        for cmd in self.rel.local_prepare:
            r = self.sandbox.run_trusted(self.comp.runtime, self.comp.workdir, cmd, env=env)
            if not r.ok:
                raise StagingError(f"`{cmd}` failed:\n{r.output[-4000:]}")
        log_path = self.ws.resolve("reports/staging.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(log_path, "w", encoding="utf-8")
        self._proc = subprocess.Popen(
            shlex.split(self.rel.local_start), cwd=self.ws.resolve(self.comp.workdir),
            env={**os.environ, **self.sandbox.config.env, **env},
            stdout=self._log_file, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_s
        url = self.base_url + self.rel.health_path
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise StagingError(f"The API exited during startup (code {self._proc.returncode}):\n{self.logs()}")
            status, _ = http_get(url)
            if status == 200:
                if not self._api_running():
                    raise StagingError(f"{url} answers, but this run's API service is not running: something "
                                       f"else is serving port {self.port}", environment=True)
                return
            time.sleep(1)
        raise StagingError(f"{url} did not return 200 within {self.startup_timeout_s}s:\n{self.logs()}")

    def logs(self, chars: int = 4000) -> str:
        if self.mode == "compose":
            r = self._compose("logs", "--no-color", "--tail", "200")
            return (r.stdout + r.stderr)[-chars:]
        p = self.ws.resolve("reports/staging.log")
        if self._log_file:
            self._log_file.flush()
        return p.read_text(encoding="utf-8", errors="replace")[-chars:] if p.exists() else ""

    def stop(self) -> None:
        if self.mode == "compose":
            self._compose("down", "-v")
            return
        if self._proc is not None and self._proc.poll() is None:
            os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)
        self._proc = None
        if self._log_file:
            self._log_file.close()
            self._log_file = None
