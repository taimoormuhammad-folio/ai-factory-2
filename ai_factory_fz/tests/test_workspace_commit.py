"""Scoped commits (used while the frontend and backend lanes build in parallel)."""

from git import Repo

from agentic_sdlc.workspace import Workspace


def test_a_scoped_commit_takes_only_the_given_paths_and_skips_ignored_ones(tmp_path):
    ws = Workspace.create("r", runs_dir=tmp_path)
    ws.write_text("state.json", "{}")                       # git-ignored in run folders
    ws.write_text("server/a.ts", "a")
    ws.write_text("app/b.dart", "b")                        # the other lane's work in progress
    sha = ws.commit("server only", [*Workspace.BOOKKEEPING, "server"])
    assert sha
    assert Repo(ws.root).git.show("--name-only", "--format=", sha).split() == ["server/a.ts"]
    assert "app/b.dart" in Repo(ws.root).untracked_files
    ws.commit("everything")
    assert "app/b.dart" in Repo(ws.root).git.ls_files().split()


def test_a_scoped_commit_records_deleted_tracked_files(tmp_path):
    ws = Workspace.create("r", runs_dir=tmp_path)
    ws.write_text("server/a.ts", "a")
    ws.commit("add")
    (ws.root / "server/a.ts").unlink()
    ws.commit("remove", ["server/a.ts"])
    assert "server/a.ts" not in Repo(ws.root).git.ls_files().split()


def test_rewriting_a_document_that_only_differs_in_its_timestamp_keeps_the_file(tmp_path):
    from agentic_sdlc.workspace import Workspace

    ws = Workspace.create("w", runs_dir=tmp_path)
    old = "---\nagent: x\nwritten_at: 2026-01-01T00:00:00+00:00\n---\nbody\n"
    ws.write_text("docs/a.md", old)
    ws.write_text("docs/a.md", old.replace("2026-01-01", "2026-02-02"))
    assert ws.read_text("docs/a.md") == old                              # approved hashes keep matching
    ws.write_text("docs/a.md", old.replace("body", "new body").replace("2026-01-01", "2026-02-02"))
    assert "new body" in ws.read_text("docs/a.md")                       # a real change is written
