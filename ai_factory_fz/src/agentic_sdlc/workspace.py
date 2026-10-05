"""Per-run workspace: runs/<run_id>/ holds docs, generated code, reports and state.json."""

import re
from datetime import datetime
from pathlib import Path

from git import Actor, Repo
from pydantic import BaseModel

from agentic_sdlc.settings import RUNS_DIR

STATE_FILE = "state.json"
SUBDIRS = ("docs", "app", "server", "infra", "reports")
_AUTHOR = Actor("agentic-sdlc", "agentic-sdlc@localhost")
# Generated and installed files never belong in the run's history.
GITIGNORE = "\n".join([
    "state.json", ".env", "node_modules/", "dist/", "coverage/", "*.tsbuildinfo",
    ".dart_tool/", "build/", ".flutter-plugins", ".flutter-plugins-dependencies", ".pub-cache/", ".sdlc/", ".gradle/", ".android/", "",
])


class PathEscapeError(ValueError):
    """Raised when a path points outside the run workspace."""


def new_run_id(product_hint: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", product_hint.lower()).strip("-")[:30]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slug}" if slug else stamp


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()

    @classmethod
    def create(cls, run_id: str, runs_dir: Path | None = None) -> "Workspace":
        ws = cls((runs_dir or RUNS_DIR) / run_id)
        for d in SUBDIRS:
            (ws.root / d).mkdir(parents=True, exist_ok=True)
        if not (ws.root / ".git").exists():
            Repo.init(ws.root)
            (ws.root / ".gitignore").write_text(
                GITIGNORE, encoding="utf-8"
            )
        return ws

    @classmethod
    def open(cls, run_id: str, runs_dir: Path | None = None) -> "Workspace":
        root = (runs_dir or RUNS_DIR) / run_id
        if not (root / STATE_FILE).exists():
            raise FileNotFoundError(f"No run state at {root / STATE_FILE}")
        return cls(root)

    def resolve(self, rel_path: str) -> Path:
        """Resolve a path inside the workspace; refuse anything that escapes it."""
        p = (self.root / rel_path).resolve()
        if p != self.root and self.root not in p.parents:
            raise PathEscapeError(f"Path '{rel_path}' is outside the run workspace")
        if ".git" in p.relative_to(self.root).parts:
            raise PathEscapeError("The workspace .git directory is off limits")
        return p

    def write_text(self, rel_path: str, content: str) -> Path:
        p = self.resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def read_text(self, rel_path: str) -> str:
        return self.resolve(rel_path).read_text(encoding="utf-8")

    def doc_header(self, agent: str, inputs: list[str], status: str = "draft") -> str:
        """Standard header for a step's output: who wrote it, from which inputs (with sha256), its status."""
        import hashlib
        from datetime import datetime, timezone

        lines = ["---", f"agent: {agent}", f"status: {status}",
                 f"written_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}", "inputs:"]
        for rel in inputs:
            p = self.root / rel
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else "missing"
            lines.append(f"  - {rel} (sha256 {digest})")
        if not inputs:
            lines[-1] = "inputs: []"
        return "\n".join(lines + ["---", ""])

    def save_artifact(self, name: str, artifact: BaseModel, agent: str = "", inputs: list[str] | None = None) -> None:
        """Save an artifact as JSON (for machines) and Markdown (for people) under docs/.
        With `agent`, the Markdown starts with the standard header (agent, inputs + hashes, status)."""
        self.write_text(f"docs/{name}.json", artifact.model_dump_json(indent=2))
        to_md = getattr(artifact, "to_markdown", None)
        if to_md:
            header = self.doc_header(agent, inputs or []) if agent else ""
            self.write_text(f"docs/{name}.md", header + to_md())

    def save_state(self, state: BaseModel) -> None:
        tmp = self.root / f"{STATE_FILE}.tmp"
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.root / STATE_FILE)

    def load_state_json(self) -> str:
        return (self.root / STATE_FILE).read_text(encoding="utf-8")

    # Pipeline bookkeeping a checkpoint may commit while builders are still working on code (parallel build).
    BOOKKEEPING = ("state.json", "status.md", "blocked.md", "tests.lock", "reports", "docs", "gates")

    def commit(self, message: str, paths: list[str] | tuple[str, ...] | None = None) -> str | None:
        """Commit everything in the workspace, or only `paths` (files or folders). Returns the commit sha,
        or None if nothing changed."""
        import os

        # Avoid interactive prompts / credential helpers hanging headless worker runs.
        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        env.setdefault("GCM_INTERACTIVE", "never")
        env.setdefault("GIT_OPTIONAL_LOCKS", "0")
        repo = Repo(self.root)
        with repo.git.custom_environment(**env):
            lock = self.root / ".git" / "index.lock"
            if lock.exists():
                try:
                    lock.unlink()
                except OSError:
                    pass
            if paths is None:
                repo.git.add(A=True)
            else:
                existing = [p for p in dict.fromkeys(paths) if p and (self.root / p).exists()]
                tracked_gone = [p for p in dict.fromkeys(paths) if p and not (self.root / p).exists()
                                and repo.head.is_valid() and repo.git.ls_files("--", p)]
                if existing or tracked_gone:
                    repo.git.add("-A", "--", *existing, *tracked_gone)
            if repo.head.is_valid() and not repo.index.diff("HEAD"):
                return None
            return repo.index.commit(message, author=_AUTHOR, committer=_AUTHOR).hexsha

    def tag(self, name: str) -> None:
        Repo(self.root).create_tag(name, message=name)
