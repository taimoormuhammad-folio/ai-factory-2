"""Facts handed to the test-writing agents: collected from the code, never from memory."""

from agentic_sdlc.build import facts
from agentic_sdlc.registry.profiles import Profile


def project(tmp_path):
    (tmp_path / "app/lib/features/catalog").mkdir(parents=True)
    (tmp_path / "server/prisma").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "app/lib/features/catalog/tile.dart").write_text(
        "Card(key: Key('product-tile-$id'), child: Text('x'));\nElevatedButton(key: const ValueKey('add-to-cart-button'))")
    (tmp_path / "server/prisma/seed.ts").write_text(
        "export const P = [{\n  id: '6f1d-0001',\n  name: 'Ceramic Mug',\n}, {\n  id: '6f1d-0002',\n  name: 'Tote Bag',\n}];")
    (tmp_path / "docs/api-contract.yaml").write_text(
        "paths:\n  /products:\n    get: {}\n  /carts/{id}/items:\n    post: {}\n")
    return tmp_path


def test_keys_seed_ids_and_operations_are_read_from_the_code(tmp_path):
    root = project(tmp_path)
    assert facts.widget_keys(root, ["app/lib/**/*.dart"]) == [
        ("add-to-cart-button", "app/lib/features/catalog/tile.dart"),
        ("product-tile-$id", "app/lib/features/catalog/tile.dart")]
    assert [(i, n) for i, n, _ in facts.seed_records(root, ["server/prisma/seed*.ts"])] == [
        ("6f1d-0001", "Ceramic Mug"), ("6f1d-0002", "Tote Bag")]
    assert facts.operations(root) == ["POST /carts/{id}/items", "GET /products"]


def test_the_facts_text_tells_agents_to_use_them_and_marks_templates(tmp_path):
    text = facts.build(project(tmp_path), Profile.load("flutter_nestjs_ecommerce"))
    assert "never invent ids, keys or paths" in text and "Key('product-tile-$id')" in text
    assert "id 6f1d-0001  name Ceramic Mug" in text and "GET /products" in text and "key PREFIX" in text


def test_node_modules_and_pub_cache_are_not_scanned(tmp_path):
    root = project(tmp_path)
    (root / "app/lib/.pub-cache").mkdir()
    (root / "app/lib/.pub-cache/x.dart").write_text("Key('from-a-package')")
    assert "from-a-package" not in dict(facts.widget_keys(root, ["app/lib/**/*.dart"]))


def test_all_three_test_writing_prompts_carry_the_facts():
    from agentic_sdlc.settings import load_config

    tasks = load_config("tasks")
    for key in ("write_acceptance_tests", "write_smoke_tests", "write_device_tests"):
        assert "{facts}" in tasks[key]["description"], key


def test_the_test_writing_jobs_receive_the_facts(tmp_path, prd, backlog):
    from test_release import ScriptedWorker, releaser

    worker = ScriptedWorker()
    r, s, ws = releaser(tmp_path, prd, backlog, worker)
    ws.write_text("app/lib/a.dart", "Key('cart-total')")
    r._guarded("smoke_tester", "write_smoke_tests", {}, __import__("agentic_sdlc.artifacts.reports", fromlist=["x"]).WorkItemResult,
               r.api.workdir, "api", lambda _r: [], "smoke")
    job = [j for j in worker.jobs if j.task_key == "write_smoke_tests"][0]
    assert "Key('cart-total')" in job.inputs["facts"]


def test_a_mocks_fixture_records_are_listed_and_only_demo_passwords_are_shown(tmp_path):
    (tmp_path / "fx").mkdir()
    (tmp_path / "fx/items.json").write_text('{"items": [{"internalid": 93, "itemid": "CIELO", "displayname": "Step light", "price": {"a": 1}}]}')
    (tmp_path / "fx/customer.json").write_text(
        '{"_comment": "Made-up demo customer", "users": [{"email": "buyer@example.com", "password": "demo-password-1"}]}')
    (tmp_path / "fx/real.json").write_text('{"users": [{"email": "owner@corp.example", "password": "hunter2"}]}')
    lines = "\n".join(facts.fixture_records(tmp_path, ["fx/*.json"]))
    assert "internalid 93, itemid CIELO, displayname Step light" in lines and "price" not in lines
    assert "password demo-password-1 (made-up demo account)" in lines
    assert "owner@corp.example" in lines and "hunter2" not in lines          # not labelled as demo data: no password


def test_the_netsuite_profile_points_the_facts_at_the_mock_fixtures():
    p = Profile.load("flutter_nestjs_netsuite")
    assert p.guardrails["facts"]["fixtures"] == ["infra/suitecommerce-mock/fixtures/*.json"]
