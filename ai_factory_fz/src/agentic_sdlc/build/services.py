"""Throwaway services for the acceptance suites: a real database that lives only while a suite runs.

Acceptance tests exercise the whole app, so an app with a database needs one. The sandbox has no network,
so the pipeline starts a disposable PostgreSQL container on the host, creates the schema with the
profile's prepare command (e.g. `prisma db push`), runs the suite against it on the host network, and
removes the container afterwards.
"""

import logging
import re
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from agentic_sdlc.registry.profiles import AcceptanceDatabase
from agentic_sdlc.tools import docker_access
from agentic_sdlc.tools.sandbox_exec import SandboxMode

log = logging.getLogger(__name__)

# Failures that say "the environment is broken", not "the feature is missing". A suite that fails only
# for these reasons proves nothing, before or after the build.
ENVIRONMENT_FAILURES = re.compile(
    r"Can't reach database server|PrismaClientInitializationError|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|"
    r"getaddrinfo|connect ETIMEDOUT|database .* does not exist|password authentication failed|"
    r"SocketException|Connection refused|No devices are connected|No supported devices", re.I)


def environment_failure(output: str) -> str | None:
    """The first environment-error line in a test run's output, if any."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output or "")
    lines = [ln.strip() for ln in clean.splitlines() if ENVIRONMENT_FAILURES.search(ln)]
    return max(lines, key=len)[:200] if lines else None


class ServiceError(RuntimeError):
    pass


@contextmanager
def acceptance_database(sandbox, db: AcceptanceDatabase | None) -> Iterator[dict[str, str]]:
    """Yield the environment (e.g. {"DATABASE_URL": ...}) for the suite; {} when no database is needed or
    Docker is not the sandbox. Raises ServiceError if the database cannot be started."""
    if db is None or getattr(sandbox, "mode", None) is not SandboxMode.DOCKER:
        yield {}
        return
    name = f"sdlc-accdb-{uuid.uuid4().hex[:10]}"
    started = docker_access.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={db.password}",
         "-e", f"POSTGRES_USER={db.user}", "-e", f"POSTGRES_DB={db.name}", "-p", "127.0.0.1::5432", db.image],
        capture_output=True, text=True, timeout=300)
    if started.returncode != 0:
        raise ServiceError(f"could not start the acceptance database ({db.image}): {(started.stderr or started.stdout)[-300:]}")
    try:
        port = _port(name)
        _wait_ready(name, db)
        yield {db.env_var: db.url.format(port=port, user=db.user, password=db.password, name=db.name)}
    finally:
        docker_access.run(["docker", "kill", name], capture_output=True, text=True)


def _port(name: str) -> str:
    out = docker_access.run(["docker", "port", name, "5432/tcp"], capture_output=True, text=True, timeout=30)
    m = re.search(r":(\d+)\s*$", (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else "")
    if not m:
        raise ServiceError(f"no host port for the acceptance database: {out.stdout or out.stderr}")
    return m.group(1)


def _wait_ready(name: str, db: AcceptanceDatabase, timeout_s: int = 90) -> None:
    """Ready = answering over TCP (while it initialises, PostgreSQL only listens on its socket)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        probe = docker_access.run(
            ["docker", "exec", name, "psql", "-h", "127.0.0.1", "-U", db.user, "-d", db.name, "-c", "select 1"],
            capture_output=True, text=True, timeout=30)
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise ServiceError(f"the acceptance database did not become ready within {timeout_s}s")
