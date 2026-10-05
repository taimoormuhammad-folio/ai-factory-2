"""Profiles without a database (`database: false`), and the NetSuite profile that uses it."""

import re
from types import SimpleNamespace

from test_guardrails import good

from agentic_sdlc.artifacts.architecture import Component
from agentic_sdlc.build.scaffold import scaffold
from agentic_sdlc.crews import planning
from agentic_sdlc.crews.base import fill_template
from agentic_sdlc.guardrails import architecture as g
from agentic_sdlc.registry.profiles import Profile
from agentic_sdlc.release.releaser import Releaser
from agentic_sdlc.release.staging import Staging
from agentic_sdlc.settings import load_config
from agentic_sdlc.tools.sandbox_exec import SandboxMode, SandboxRunner
from agentic_sdlc.workspace import Workspace

NETSUITE = "flutter_nestjs_netsuite"


def netsuite_design():
    """A design the NetSuite profile should accept: no database, SuiteCommerce behind the API."""
    a = good()
    a.components = [Component(name="App", responsibility="r", technology="Flutter 3, Riverpod"),
                    Component(name="API", responsibility="r", technology="NestJS backend-for-frontend"),
                    Component(name="Store", responsibility="r", technology="NetSuite SuiteCommerce Advanced")]
    a.security = ["Encrypted session tokens (AES-256-GCM) carry the SuiteCommerce session",
                  "Input validation with class-validator", "Secrets only from environment variables"]
    a.prisma_schema = ""
    return a


def test_netsuite_profile_loads_with_its_knowledge_and_templates():
    p = Profile.load(NETSUITE)
    assert p.database is False
    for agent in p.agent_context:
        assert p.context_for(agent)
    templates = [s.template for c in p.components.values() for s in c.scaffold if s.template]
    assert templates and all((p.root / "templates" / t).exists() for t in templates)
    assert "docs/schema.prisma" not in p.guardrails["contract_copies"]


def test_existing_profile_still_has_a_database():
    assert Profile.load("flutter_nestjs_ecommerce").database is True


def test_without_a_database_the_prisma_schema_must_stay_empty():
    a = netsuite_design()
    assert a.no_database_errors() == [] and a.data_models() == []
    assert "no database of its own" in a.to_markdown()
    a.prisma_schema = good().prisma_schema
    assert a.no_database_errors()


def test_netsuite_design_passes_every_rule_that_applies():
    check = g.checker(Profile.load(NETSUITE), {"guardrails": {"architect": "all"}})
    assert check(netsuite_design()) == []


def test_netsuite_profile_rejects_a_database_and_requires_suitecommerce():
    check = g.checker(Profile.load(NETSUITE), {})
    a = netsuite_design()
    a.components[1].technology = "NestJS, Prisma, PostgreSQL"
    a.components[2].technology = "Static files"
    problems = " ".join(check(a))
    assert "prisma" in problems and "postgresql" in problems and "suitecommerce" in problems


def test_data_model_rules_only_run_with_a_database():
    a = good()
    a.backend_modules[1].entities = ["Item"]          # not a Prisma model
    pipeline = {"guardrails": {"architect": ["C1", "C2", "C3"]}}
    assert g.checker(Profile.load("flutter_nestjs_ecommerce"), pipeline)(a)
    assert g.checker(Profile.load(NETSUITE), pipeline)(a) == []


def test_layout_names_the_profile_folders_and_the_provided_mock():
    layout = Profile.load(NETSUITE).layout_summary()
    for path in ("server/", "app/", "docs/openapi.yaml", "app/packages/api_client/", "infra/suitecommerce-mock/"):
        assert path in layout
    assert "do not rewrite it" in layout
    assert Profile.load(NETSUITE).layout_roots() == {"server", "app", "docs", "infra", ".github"}


def test_architect_is_told_there_is_no_database():
    seen = {}

    class Runner:
        def run(self, phase, key, inputs, model, guardrail=None):
            seen.update(inputs)

    planning.design_architecture(Runner(), SimpleNamespace(to_markdown=lambda: "prd"), "stack", [], "", database=False,
                                 layout="- server/: the backend")
    assert seen["data_layer"] == planning.NO_DATA_LAYER and seen["layout"] == "- server/: the backend"
    planning.design_architecture(Runner(), SimpleNamespace(to_markdown=lambda: "prd"), "stack", [], "")
    assert seen["data_layer"] == planning.DATA_LAYER


def test_deployment_prompt_has_no_database_without_one():
    template = load_config("tasks")["deploy_staging"]["description"]
    values = {k: "x" for k in re.findall(r"\{(\w+)\}", template)}
    no_db = fill_template(template, {**values, **Releaser._data_store_rules(SimpleNamespace(profile=SimpleNamespace(database=False)))})
    with_db = fill_template(template, {**values, **Releaser._data_store_rules(SimpleNamespace(profile=SimpleNamespace(database=True)))})
    assert "PostgreSQL" not in no_db and "migrations" not in no_db and "DATABASE_URL" not in no_db
    assert "PostgreSQL 16" in with_db and "DATABASE_URL" in with_db


def test_scaffold_copies_a_profile_template_folder(tmp_path):
    p = Profile.load(NETSUITE)
    ws = Workspace.create("r", runs_dir=tmp_path)
    backend = p.components["backend"].model_copy(
        update={"scaffold": [s for s in p.components["backend"].scaffold if s.template]})
    log = scaffold("backend", backend, ws, SandboxRunner(ws, p.sandbox, SandboxMode.LOCAL), p.root / "templates")
    assert (ws.root / "infra/suitecommerce-mock/server.js").exists()
    assert (ws.root / "infra/suitecommerce-mock/fixtures/items.json").exists()
    assert log == ["copied template suitecommerce-mock -> infra/suitecommerce-mock"]
    assert scaffold("backend", backend, ws, None, p.root / "templates") == ["skip (exists): infra/suitecommerce-mock/server.js"]


def test_local_staging_needs_no_database_url_without_a_database(tmp_path, monkeypatch):
    monkeypatch.delenv("SDLC_STAGING_DATABASE_URL", raising=False)
    ws = Workspace.create("r", runs_dir=tmp_path)
    for name, needs_db in ((NETSUITE, False), ("flutter_nestjs_ecommerce", True)):
        p = Profile.load(name)
        st = Staging(ws, p, SandboxRunner(ws, p.sandbox, SandboxMode.LOCAL))
        reason = st.unavailable_reason() or ""
        assert ("SDLC_STAGING_DATABASE_URL" in reason) is needs_db


def test_integration_review_skips_migrations_without_a_database():
    template = load_config("tasks")["integration_review"]["description"]
    values = {k: "x" for k in re.findall(r"\{(\w+)\}", template)}
    for database in (False, True):
        filled = fill_template(template, {**values, **Releaser._data_store_rules(SimpleNamespace(profile=SimpleNamespace(database=database)))})
        assert ("migrations" in filled) is database
