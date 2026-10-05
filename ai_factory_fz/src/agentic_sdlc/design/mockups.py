"""Mockups: the UI/UX designer draws each screen state as body markup using a small CSS kit built from
the design tokens; this module turns that into a phone-sized HTML page per state, a gallery page, and PNG
files rendered with headless Chrome (when it is installed; the HTML pages work without it).

Files (in the run folder):
  docs/mockups/<SCR-xx>_<state>.html   one page per screen state, 390x844 by default
  docs/mockups/<SCR-xx>_<state>.png    the same page as an image
  docs/mockups/index.html              all screens and states side by side (shown at Gate 2)
"""

import html
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from agentic_sdlc.artifacts.design import DesignSystem, ScreenMockups, ScreenSpec, token_name

DEFAULT_VIEWPORT = (390, 844)
DEFAULT_STATES = ["success", "loading", "error", "empty"]
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

# Classes the designer may use; documented in the task prompt (kit_reference) and styled in kit_css.
KIT_CLASSES = {
    "appbar": "top app bar; put the title in <span class=\"title\">, icons as <span class=\"icon\">name</span>",
    "content": "scrolling page body with padding",
    "stack": "vertical list of children with spacing", "row": "horizontal row, items centered, spaced apart",
    "card": "surface card with radius and border", "divider": "thin horizontal rule",
    "field": "text input; label in <span class=\"label\">, value or placeholder as text",
    "btn": "filled primary button (full width)", "btn-outline": "outlined button", "btn-text": "text button",
    "disabled": "add to a button or field to show it disabled",
    "badge": "small pill; add badge-success / badge-warning / badge-error",
    "banner": "message strip; add banner-error / banner-info / banner-success",
    "img": "image placeholder box (set height inline, e.g. style=\"height:160px\")",
    "skeleton": "loading placeholder bar (set width/height inline)",
    "radio": "radio row; add selected when chosen", "stepper": "quantity stepper: − value +",
    "price": "price text", "muted": "secondary text", "center": "center content",
    "icon": "a named icon placeholder, e.g. <span class=\"icon\">cart</span>",
    "bottombar": "area pinned to the bottom (e.g. the main button)", "spinner": "small progress indicator",
}


def color_tokens(design: DesignSystem) -> dict[str, str]:
    return {token_name(c.name): c.light for c in design.colors}


def kit_css(design: DesignSystem, viewport: tuple[int, int] = DEFAULT_VIEWPORT) -> str:
    colors = color_tokens(design)
    c = lambda name, default: f"var(--{name})" if name in colors else default  # noqa: E731
    lines = [":root {"]
    lines += [f"  --{n}: {v};" for n, v in colors.items()]
    lines += [f"  --sp-{i}: {v}px;" for i, v in enumerate(design.spacing)]
    lines += [f"  --r-{i}: {v}px;" for i, v in enumerate(design.corner_radii)]
    lines += ["  --font: Roboto, 'Segoe UI', system-ui, sans-serif;", "}"]
    for t in design.typography:
        lines.append(f".t-{token_name(t.name)} {{ font-size: {t.size}px; font-weight: {t.weight}; "
                     f"line-height: {t.line_height}px; }}")
    sp = lambda i: f"var(--sp-{min(i, len(design.spacing) - 1)})" if design.spacing else f"{4 * i}px"  # noqa: E731
    r = lambda i: f"var(--r-{min(i, len(design.corner_radii) - 1)})" if design.corner_radii else f"{4 * i}px"  # noqa: E731
    w, h = viewport
    lines += [
        "* { box-sizing: border-box; margin: 0; }",
        f"html, body {{ width: {w}px; height: {h}px; overflow: hidden; font-family: var(--font); font-size: 14px; "
        f"color: {c('onSurface', '#1B1F24')}; background: {c('background', '#F7F8FA')}; }}",
        f".screen {{ position: relative; width: {w}px; height: {h}px; display: flex; flex-direction: column; }}",
        f".appbar {{ display: flex; align-items: center; gap: {sp(3)}; min-height: 56px; padding: 0 {sp(4)}; "
        f"background: {c('surface', '#fff')}; border-bottom: 1px solid {c('divider', '#ddd')}; }}",
        ".appbar .title { flex: 1; font-size: 20px; font-weight: 600; }",
        f".content {{ flex: 1; overflow: hidden; padding: {sp(4)}; display: flex; flex-direction: column; gap: {sp(3)}; }}",
        f".stack {{ display: flex; flex-direction: column; gap: {sp(2)}; }}",
        f".row {{ display: flex; align-items: center; justify-content: space-between; gap: {sp(2)}; }}",
        f".card {{ background: {c('surface', '#fff')}; border: 1px solid {c('divider', '#ddd')}; border-radius: {r(2)}; "
        f"padding: {sp(3)}; display: flex; flex-direction: column; gap: {sp(1)}; }}",
        f".divider {{ height: 1px; background: {c('divider', '#ddd')}; }}",
        f".field {{ display: flex; flex-direction: column; gap: 2px; min-height: 56px; padding: {sp(2)} {sp(3)}; "
        f"border: 1px solid {c('outline', '#888')}; border-radius: {r(1)}; background: {c('surface', '#fff')}; "
        f"color: {c('onSurfaceVariant', '#555')}; }}",
        f".field .label {{ font-size: 12px; color: {c('primary', '#0B5CAD')}; }}",
        f".btn, .btn-outline, .btn-text {{ display: flex; align-items: center; justify-content: center; min-height: 48px; "
        f"padding: 0 {sp(4)}; border-radius: {r(3)}; font-weight: 600; }}",
        f".btn {{ background: {c('primary', '#0B5CAD')}; color: {c('onPrimary', '#fff')}; }}",
        f".btn-outline {{ border: 1px solid {c('primary', '#0B5CAD')}; color: {c('primary', '#0B5CAD')}; }}",
        f".btn-text {{ color: {c('primary', '#0B5CAD')}; min-height: 40px; }}",
        f".disabled {{ background: {c('surfaceVariant', '#E8ECF1')} !important; color: {c('outline', '#888')} !important; "
        "border-color: transparent !important; }",
        f".badge {{ display: inline-flex; align-self: flex-start; width: fit-content; align-items: center; "
        f"padding: 2px {sp(2)}; border-radius: 999px; font-size: 12px; font-weight: 500; "
        f"background: {c('surfaceVariant', '#E8ECF1')}; }}",
        f".badge-success {{ color: {c('stockIn', c('success', '#1B6E3C'))}; }}",
        f".badge-warning {{ color: {c('stockLow', c('warning', '#8A5100'))}; }}",
        f".badge-error {{ color: {c('stockOut', c('error', '#B3261E'))}; }}",
        f".banner {{ padding: {sp(3)} {sp(4)}; border-radius: {r(1)}; background: {c('primaryContainer', '#D6E6F8')}; "
        f"color: {c('onPrimaryContainer', '#06294D')}; }}",
        f".banner-error {{ background: {c('errorContainer', '#FCE4E2')}; color: {c('error', '#B3261E')}; }}",
        f".banner-success {{ color: {c('success', '#1B6E3C')}; }}",
        f".img {{ background: {c('surfaceVariant', '#E8ECF1')}; border-radius: {r(1)}; min-height: 64px; }}",
        f".skeleton {{ background: {c('skeletonBase', '#E3E7EC')}; border-radius: {r(0)}; min-height: 12px; }}",
        f".radio {{ display: flex; gap: {sp(3)}; align-items: flex-start; padding: {sp(3)}; border-radius: {r(2)}; "
        f"border: 1px solid {c('divider', '#ddd')}; background: {c('surface', '#fff')}; }}",
        ".radio::before { content: ''; flex: none; width: 18px; height: 18px; margin-top: 2px; border-radius: 50%; "
        f"border: 2px solid {c('outline', '#888')}; }}",
        f".radio.selected {{ border: 2px solid {c('primary', '#0B5CAD')}; }}",
        f".radio.selected::before {{ border-color: {c('primary', '#0B5CAD')}; "
        f"background: radial-gradient({c('primary', '#0B5CAD')} 45%, transparent 50%); }}",
        f".stepper {{ display: inline-flex; align-items: center; gap: {sp(3)}; border: 1px solid {c('outline', '#888')}; "
        f"border-radius: {r(3)}; padding: {sp(1)} {sp(3)}; font-weight: 600; }}",
        ".price { font-size: 16px; font-weight: 700; }",
        f".muted {{ color: {c('onSurfaceVariant', '#555')}; }}",
        ".center { display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }",
        f".icon {{ display: inline-flex; align-items: center; justify-content: center; min-width: 24px; height: 24px; "
        f"padding: 0 4px; border-radius: 6px; font-size: 10px; color: {c('onSurfaceVariant', '#555')}; "
        f"background: {c('surfaceVariant', '#E8ECF1')}; }}",
        f".bottombar {{ padding: {sp(4)}; background: {c('surface', '#fff')}; border-top: 1px solid {c('divider', '#ddd')}; }}",
        ".bottombar { display: flex; flex-direction: column; gap: 8px; } .bottombar .btn, .bottombar .btn-outline { width: 100%; }",
        f".spinner {{ width: 24px; height: 24px; border-radius: 50%; border: 3px solid {c('surfaceVariant', '#E8ECF1')}; "
        f"border-top-color: {c('primary', '#0B5CAD')}; }}",
    ]
    return "\n".join(lines)


def kit_reference(design: DesignSystem) -> str:
    """The kit as the designer sees it in the prompt."""
    colors = ", ".join(f"--{n}" for n in color_tokens(design))
    typo = ", ".join(f"t-{token_name(t.name)}" for t in design.typography)
    classes = "\n".join(f"  .{k}: {v}" for k, v in KIT_CLASSES.items())
    return (f"Layout: wrap each state in <div class=\"screen\">…</div> with an appbar (if the screen has one), a "
            f"content area and, if needed, a bottombar.\nClasses:\n{classes}\n"
            f"Typography classes: {typo}\n"
            f"Colors (use only as var(--name) in inline styles): {colors}\n"
            f"Spacing: var(--sp-0) … var(--sp-{max(len(design.spacing) - 1, 0)}); radii: var(--r-0) … "
            f"var(--r-{max(len(design.corner_radii) - 1, 0)}).")


def states_to_draw(screen: ScreenSpec, wanted: list[str], limit: int = 4) -> list[str]:
    """The spec's own state names that match the wanted kinds (success, loading, ...), keeping spec order.
    A screen without a 'success'-like state gets its first state (e.g. 'idle') drawn instead."""
    def kind(state: str) -> str:
        return re.split(r"[\s:(/]", state.strip().lower(), maxsplit=1)[0]

    picked = [s for s in screen.states if kind(s) in {w.lower() for w in wanted}]
    if not any(kind(s) == "success" for s in picked) and screen.states and screen.states[0] not in picked:
        picked.insert(0, screen.states[0])
    return picked[:limit]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "state"


def page_html(design: DesignSystem, screen: ScreenSpec, state: str, body: str,
              viewport: tuple[int, int] = DEFAULT_VIEWPORT) -> str:
    return (f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(screen.id)} {html.escape(screen.name)} – {html.escape(state)}</title>"
            f"<style>\n{kit_css(design, viewport)}\n</style></head>\n<body>\n{body}\n</body></html>\n")


def gallery_html(design: DesignSystem, entries: list[dict[str, Any]], viewport: tuple[int, int] = DEFAULT_VIEWPORT) -> str:
    """entries: [{screen, states: [{state, html_file, png_file or None}]}]."""
    w, h = viewport
    scale = 0.5
    rows = []
    for e in entries:
        s: ScreenSpec = e["screen"]
        cells = []
        for st in e["states"]:
            if st["png_file"]:
                view = f"<img src=\"{st['png_file']}\" width=\"{int(w * scale)}\" height=\"{int(h * scale)}\" alt=\"\">"
            else:
                view = (f"<div class=\"frame\"><iframe src=\"{st['html_file']}\" width=\"{w}\" height=\"{h}\" "
                        f"style=\"transform:scale({scale});transform-origin:0 0\"></iframe></div>")
            cells.append(f"<figure><a href=\"{st['html_file']}\">{view}</a>"
                         f"<figcaption>{html.escape(st['state'])}</figcaption></figure>")
        rows.append(f"<section><h2>{html.escape(s.id)} {html.escape(s.name)} <code>{html.escape(s.route)}</code></h2>"
                    f"<p>{html.escape(s.purpose)}</p><div class=\"states\">{''.join(cells)}</div></section>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Screen mockups</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; background: #f3f4f6; color: #111; }}
section {{ margin-bottom: 32px; }} h2 {{ font-size: 18px; margin-bottom: 4px; }} code {{ font-size: 13px; color: #555; }}
p {{ color: #444; max-width: 900px; }}
.states {{ display: flex; gap: 16px; flex-wrap: wrap; }}
figure {{ margin: 0; }} figcaption {{ font-size: 13px; color: #333; margin-top: 4px; max-width: {int(w * scale)}px; }}
img, .frame {{ display: block; border-radius: 16px; border: 1px solid #ccc; background: #fff; overflow: hidden; }}
.frame {{ width: {int(w * scale)}px; height: {int(h * scale)}px; }} .frame iframe {{ border: 0; }}
</style></head><body>
<h1>Screen mockups</h1>
<p>Drawn by the UI/UX designer from docs/design_system.md, with the design tokens. Click a screen to open it full size.</p>
{''.join(rows)}
</body></html>
"""


def chrome_binary() -> str | None:
    return next((p for name in CHROME_NAMES if (p := shutil.which(name))), None)


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def render_png(html_file: Path, png_file: Path, viewport: tuple[int, int] = DEFAULT_VIEWPORT,
               chrome: str | None = None, run: Runner | None = None) -> bool:
    """Screenshot an HTML page with headless Chrome. False if Chrome is missing or the render failed."""
    chrome = chrome or chrome_binary()
    if not chrome:
        return False
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
           f"--window-size={viewport[0]},{viewport[1]}", f"--screenshot={png_file}", html_file.resolve().as_uri()]
    run = run or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=60))
    try:
        result = run(cmd)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and png_file.exists()


def write_mockups(workspace, design: DesignSystem, all_mockups: list[ScreenMockups],
                  viewport: tuple[int, int] = DEFAULT_VIEWPORT, render: Callable[..., bool] | None = None) -> dict[str, int]:
    """Write every state page, its PNG (if Chrome is available) and the gallery. Returns counts."""
    render = render or render_png
    screens = {s.id: s for s in design.screens}
    entries, pages, pngs = [], 0, 0
    for sm in all_mockups:
        screen = screens.get(sm.screen_id)
        if screen is None:
            continue
        states = []
        for m in sm.mockups:
            base = f"{screen.id}_{slug(m.state.split(' (')[0])}"   # drop the spec's description in brackets
            html_path = workspace.write_text(f"docs/mockups/{base}.html", page_html(design, screen, m.state, m.html, viewport))
            pages += 1
            ok = render(html_path, html_path.with_suffix(".png"), viewport)
            pngs += ok
            states.append({"state": m.state, "html_file": f"{base}.html", "png_file": f"{base}.png" if ok else None})
        entries.append({"screen": screen, "states": states})
    workspace.write_text("docs/mockups/index.html", gallery_html(design, entries, viewport))
    return {"pages": pages, "pngs": pngs}
