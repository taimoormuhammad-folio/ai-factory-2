"""UI/UX mockups: what the designer may draw, which states are drawn, and the pages, PNGs and gallery."""

import subprocess

from agentic_sdlc.artifacts.design import (ColorToken, DesignSystem, ScreenMockups, ScreenSpec, StateMockup,
                                           TypeStyle, token_name)
from agentic_sdlc.crews import design as design_crew
from agentic_sdlc.design import mockups as kit
from agentic_sdlc.workspace import Workspace


def screen(states=("loading", "empty", "error: offline", "success", "line pending")):
    return ScreenSpec(id="SCR-05", name="Cart", route="/cart", purpose="Review the cart",
                      components=["AppBar", "Cart line tile", "AppPrimaryButton (Checkout)"], states=list(states),
                      story_ids=["US-014"])


def spec():
    return DesignSystem(
        colors=[ColorToken(name="primary", light="#0B5CAD", dark="#8AB4F8"),
                ColorToken(name="splashBackground (placeholder brand)", light="#0B5CAD", dark="#0B5CAD")],
        typography=[TypeStyle(name="titleLarge", size=20, weight=600, line_height=28)],
        spacing=[0, 4, 8, 16], corner_radii=[4, 8], shared_widgets=[], screens=[screen()], navigation=[],
        accessibility=["48dp touch targets"])


def mock(state, html='<div class="screen"><div class="appbar"><span class="title">Cart</span></div></div>'):
    return StateMockup(state=state, html=html)


def test_token_names_are_css_safe():
    assert token_name("splashBackground (placeholder brand)") == "splashBackground"
    assert "--splashBackground: #0B5CAD;" in kit.kit_css(spec())


def test_only_kit_markup_and_design_tokens_are_accepted():
    tokens = set(kit.color_tokens(spec()))
    ok = ScreenMockups(screen_id="SCR-05", mockups=[
        mock("success", '<div class="card" style="color:var(--primary);padding:var(--sp-3);border-radius:var(--r-1)">x</div>')])
    assert ok.problems(tokens, ["success"]) == []
    bad = ScreenMockups(screen_id="SCR-05", mockups=[
        mock("success", '<style>a{}</style><div style="color:#ff0000;background:var(--brandPink)">'
                        '<img src="https://x/y.png"></div>')])
    problems = " ".join(bad.problems(tokens, ["success", "error"]))
    for expected in ("no mockup for state 'error'", "<style>", "<img>", "external URL", "raw hex color (#ff0000)",
                     "var(--brandPink) is not a design-system token"):
        assert expected in problems


def test_order_numbers_are_not_colors_and_short_state_names_count():
    tokens = set(kit.color_tokens(spec()))
    m = ScreenMockups(screen_id="SCR-10", mockups=[
        mock("success", '<div class="card"><span class="t-titleLarge">Order #1001</span> confirmation #ABC123</div>')])
    assert m.problems(tokens, ["success (confirmation number, order number, totals)"]) == []
    styled = ScreenMockups(screen_id="SCR-10", mockups=[mock("success", '<div style="border:1px solid #abc">#1001</div>')])
    assert styled.problems(tokens, ["success"]) == ["SCR-10 success: uses a raw hex color (#abc); use var(--token)"]


def test_states_to_draw_follow_the_spec_names():
    assert kit.states_to_draw(screen(), ["success", "loading", "error", "empty"]) == \
        ["loading", "empty", "error: offline", "success"]
    assert kit.states_to_draw(screen(("idle", "validation error", "submitting", "error: credentials")),
                              ["success", "error"]) == ["idle", "error: credentials"]
    assert len(kit.states_to_draw(screen(), ["success", "loading", "error", "empty"], limit=2)) == 2


def test_designer_task_gets_the_kit_product_and_stories():
    from agentic_sdlc.artifacts.prd import PRD, AcceptanceCriterion, UserStory
    seen = {}

    class Runner:
        def run(self, phase, key, inputs, model, guardrail=None):
            seen.update(key=key, inputs=inputs, guardrail=guardrail)

    prd = PRD(title="Lighting store", summary="B2B lighting products", personas=[], non_functional_requirements=[],
              out_of_scope=[], user_stories=[UserStory(
                  id="US-014", title="Cart", as_a="buyer", i_want="see my cart", so_that="I can check out",
                  priority="must", acceptance_criteria=[AcceptanceCriterion(
                      given="an item in stock", when="I view it", then="only In stock is shown, never the quantity")])])
    design_crew.design_mockups(Runner(), spec(), screen(), ["success", "loading"], prd=prd)
    assert seen["key"] == "design_mockups"
    assert "--primary" in seen["inputs"]["kit"] and ".btn" in seen["inputs"]["kit"]
    assert seen["inputs"]["states"] == "- success\n- loading"
    assert seen["inputs"]["product"] == "Lighting store: B2B lighting products"
    assert "never the quantity" in seen["inputs"]["stories"] and seen["inputs"]["stories"].startswith("US-014 Cart")
    assert seen["guardrail"] is not None


def test_pages_pngs_and_gallery_are_written(tmp_path):
    ws = Workspace.create("r", runs_dir=tmp_path)
    rendered = []

    def fake_render(html_file, png_file, viewport):
        rendered.append(png_file.name)
        png_file.write_bytes(b"png")
        return True

    sm = ScreenMockups(screen_id="SCR-05", mockups=[mock("success"), mock("error: offline")])
    counts = kit.write_mockups(ws, spec(), [sm], render=fake_render)
    assert counts == {"pages": 2, "pngs": 2}
    page = (ws.root / "docs/ui/SCR-05_error-offline.html").read_text()
    assert "<style>" in page and "--primary: #0B5CAD;" in page and 'class="appbar"' in page
    gallery = (ws.root / "docs/ui/index.html").read_text()
    assert 'src="SCR-05_success.png"' in gallery and "SCR-05 Cart" in gallery
    assert rendered == ["SCR-05_success.png", "SCR-05_error-offline.png"]


def test_without_chrome_the_gallery_embeds_the_html_pages(tmp_path):
    ws = Workspace.create("r", runs_dir=tmp_path)
    counts = kit.write_mockups(ws, spec(), [ScreenMockups(screen_id="SCR-05", mockups=[mock("success")])],
                               render=lambda *a: False)
    assert counts == {"pages": 1, "pngs": 0}
    assert '<iframe src="SCR-05_success.html"' in (ws.root / "docs/ui/index.html").read_text()


def test_render_png_runs_headless_chrome(tmp_path):
    html_file = tmp_path / "a.html"
    html_file.write_text("<p>x</p>")
    png = tmp_path / "a.png"
    calls = []

    def run(cmd):
        calls.append(cmd)
        png.write_bytes(b"png")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert kit.render_png(html_file, png, (390, 844), chrome="/usr/bin/chrome", run=run)
    assert "--headless=new" in calls[0] and "--window-size=390,844" in calls[0] and f"--screenshot={png}" in calls[0]
    failed = lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "boom")  # noqa: E731
    assert not kit.render_png(html_file, tmp_path / "b.png", chrome="/usr/bin/chrome", run=failed)


def test_flow_draws_each_screen_shows_the_gallery_at_the_ui_gate_and_redraws_on_rejection(tmp_path, canned, monkeypatch,
                                                                                         capsys):
    from test_flow import PIPELINE, FakeRunner, make_flow

    monkeypatch.setattr(kit, "render_png", lambda html_file, png_file, viewport: png_file.write_bytes(b"png") or True)
    canned = {**canned, "design_mockups": ScreenMockups(screen_id="SCR-01", mockups=[mock("loading")])}
    runner = FakeRunner(canned)
    pipeline = {**PIPELINE, "design": {"mockups": True}, "gates": {**PIPELINE["gates"], "ui": True}}
    answers = iter(["y", "", "y", "", "n", "Denser product cards", "y", ""])   # G1, G2, G4 rejected, G4
    flow = make_flow(tmp_path, runner, [], pipeline=pipeline)
    flow_deps = flow._deps_factory

    def deps_factory(state):
        deps = flow_deps(state)
        deps.input_fn = lambda _prompt: next(answers)
        return deps

    flow._deps_factory = deps_factory
    flow.kickoff(inputs={"run_id": "m1", "brief": "shop"})

    assert flow.state.status == "completed", flow.state.stop_reason
    calls = [i for k, i in runner.calls if k == "design_mockups"]
    assert len(calls) == 2                                   # one screen, drawn again after the rejection
    assert calls[0]["states"] == "- loading" and calls[1]["revision_notes"] == "Denser product cards"
    keys = runner.keys()
    assert keys.index("plan_delivery") < keys.index("design_ui") < keys.index("design_mockups")
    root = tmp_path / "m1" / "docs" / "ui"
    assert (root / "SCR-01_loading.html").exists() and (root / "SCR-01_loading.png").exists()
    assert (root / "index.html").exists()
    assert [m.screen_id for m in flow.state.mockups] == ["SCR-01"]
    ui_gate = capsys.readouterr().out.split("APPROVAL NEEDED: ui")[1]
    assert "docs/ui/index.html" in ui_gate


def test_mockups_are_off_unless_the_pipeline_asks(tmp_path, canned):
    from test_flow import FakeRunner, make_flow

    runner = FakeRunner(canned)
    flow = make_flow(tmp_path, runner, ["y", "", "y", ""])
    flow.kickoff(inputs={"run_id": "m2", "brief": "shop"})
    assert "design_mockups" not in runner.keys()
    assert not (tmp_path / "m2" / "docs" / "ui").exists()
