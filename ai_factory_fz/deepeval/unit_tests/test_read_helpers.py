import json

import common


def test_read_glob_spreads_budget_across_many_files(tmp_path, monkeypatch):
    root = tmp_path / "run"
    (root / "apps/api/src/auth").mkdir(parents=True)
    (root / "apps/api/src/catalog").mkdir(parents=True)
    (root / "apps/api/src/auth/auth.controller.ts").write_text("AUTH" + ("x" * 20_000), encoding="utf-8")
    (root / "apps/api/src/catalog/catalog.controller.ts").write_text(
        "CATALOG" + ("y" * 500),
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("DEEPEVAL_RUN_DIR", str(root))
    monkeypatch.setenv("DEEPEVAL_MAX_CHARS", "5000")
    out = common.read_glob("apps/api/src/**/*.ts", prefer=("controller",))
    assert "catalog.controller.ts" in out
    assert "CATALOG" in out


def test_read_dossier_supplement_includes_state_traceability(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    state = {
        "backlog": {
            "work_items": [
                {"id": "WI-1", "title": "Cart API", "story_ids": ["US-003", "US-005"]},
            ]
        },
        "prd": {"user_stories": [{"id": "US-003", "title": "Browse catalog"}]},
        "build": {"items": {"WI-1": {"status": "done", "commit": "abc123", "summary": "Implemented cart"}}},
    }
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (root / "reports").mkdir()
    (root / "reports/run_summary.md").write_text("# Run summary\nAll milestones complete.", encoding="utf-8")
    monkeypatch.setenv("DEEPEVAL_RUN_DIR", str(root))
    supp = common.read_dossier_supplement("project_manager", max_chars=4000)
    assert "US-003" in supp
    assert "WI-1" in supp
    assert "run_summary" in supp


def test_read_agent_output_merges_primary_and_dossier(tmp_path, monkeypatch):
    root = tmp_path / "run"
    (root / "docs").mkdir(parents=True)
    (root / "docs/backlog.md").write_text("# Backlog\n| WI | Title |\n", encoding="utf-8")
    (root / "state.json").write_text(
        json.dumps({"backlog": {"work_items": [{"id": "WI-1", "story_ids": ["US-001"]}]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPEVAL_RUN_DIR", str(root))
    out = common.read_agent_output("project_manager", extra=("docs/backlog.md",))
    assert "backlog.md" in out
    assert "SDLC dossier context" in out
    assert "US-001" in out
