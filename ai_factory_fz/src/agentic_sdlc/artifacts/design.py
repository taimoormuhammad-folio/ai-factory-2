"""UI/UX artifact: design tokens, screen specs and navigation, and per-screen mockups."""

import re

from pydantic import BaseModel, Field


class ColorToken(BaseModel):
    name: str
    light: str = Field(description="Hex, e.g. #1A73E8")
    dark: str = Field(description="Hex, e.g. #8AB4F8")


class TypeStyle(BaseModel):
    name: str
    size: float
    weight: int
    line_height: float


class SharedWidget(BaseModel):
    name: str
    description: str


class ScreenSpec(BaseModel):
    id: str = Field(description="SCR-01, ...")
    name: str
    route: str = Field(description="go_router path, e.g. /products/:id")
    purpose: str
    components: list[str]
    states: list[str] = Field(description="e.g. loading, empty, error, success")
    story_ids: list[str]


class NavigationLink(BaseModel):
    from_screen: str
    to_screen: str
    trigger: str


class DesignSystem(BaseModel):
    colors: list[ColorToken]
    typography: list[TypeStyle]
    spacing: list[int] = Field(description="Spacing scale in dp")
    corner_radii: list[int]
    shared_widgets: list[SharedWidget]
    screens: list[ScreenSpec]
    navigation: list[NavigationLink]
    accessibility: list[str]

    def uncovered_stories(self, must_have_story_ids: list[str]) -> list[str]:
        covered = {sid for s in self.screens for sid in s.story_ids}
        return [sid for sid in must_have_story_ids if sid not in covered]

    def to_markdown(self) -> str:
        lines = ["# Design system", "", "## Colors", "| Token | Light | Dark |", "|---|---|---|"]
        lines += [f"| {c.name} | {c.light} | {c.dark} |" for c in self.colors]
        lines += ["", "## Typography", "| Style | Size | Weight | Line height |", "|---|---|---|---|"]
        lines += [f"| {t.name} | {t.size} | {t.weight} | {t.line_height} |" for t in self.typography]
        lines += [
            "",
            f"Spacing scale: {', '.join(map(str, self.spacing))} dp. "
            f"Corner radii: {', '.join(map(str, self.corner_radii))} dp.",
            "",
            "## Shared widgets",
        ]
        lines += [f"- **{w.name}**: {w.description}" for w in self.shared_widgets]
        lines += ["", "## Screens"]
        for s in self.screens:
            lines += [
                f"### {s.id} {s.name} (`{s.route}`)",
                s.purpose,
                f"- Components: {', '.join(s.components)}",
                f"- States: {', '.join(s.states)}",
                f"- Stories: {', '.join(s.story_ids)}",
                "",
            ]
        lines += ["## Navigation"]
        lines += [f"- {n.from_screen} → {n.to_screen}: {n.trigger}" for n in self.navigation]
        lines += ["", "## Accessibility"] + [f"- {a}" for a in self.accessibility]
        return "\n".join(lines)


def token_name(name: str) -> str:
    """CSS-safe token name: 'splashBackground (placeholder brand)' -> 'splashBackground'."""
    return re.sub(r"[^A-Za-z0-9_-]", "", name.split(" (")[0])


_FORBIDDEN_TAG = re.compile(r"<\s*(script|style|link|html|head|body|iframe|img)\b", re.I)
_FORBIDDEN_MARKUP = [
    (re.compile(r"https?://|url\(", re.I), "an external URL or url()"),
    (re.compile(r"\b(rgba?|hsla?)\(", re.I), "a raw {0}() color"),
]
# Colors only count inside style attributes: "Order #1001" in the text is not a color.
_STYLE_ATTR = re.compile(r"""style\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
# Non-color variables the mockup kit defines: spacing (--sp-0..), corner radii (--r-0..) and the font.
_KIT_VAR = re.compile(r"^(sp-\d+|r-\d+|font)$")


def _state_key(name: str) -> str:
    """'success (confirmation number, totals)' -> 'success': the name without the spec's description."""
    return name.split(" (")[0].strip().lower()


class StateMockup(BaseModel):
    state: str = Field(description="The screen state this mockup shows, named as in the screen spec, e.g. success")
    html: str = Field(description="Body markup only (no html/head/body/style/script/img tags), styled with the "
                                  "mockup kit classes and var(--token) colors")


class ScreenMockups(BaseModel):
    """Mockups of one screen, one per state drawn."""
    screen_id: str = Field(description="SCR-01, ...")
    mockups: list[StateMockup]

    def problems(self, color_tokens: set[str], states: list[str]) -> list[str]:
        """What the mockup kit cannot render faithfully, or what is missing."""
        errors: list[str] = []
        drawn = {_state_key(m.state) for m in self.mockups}
        errors += [f"{self.screen_id}: no mockup for state '{st}'" for st in states if _state_key(st) not in drawn]
        for m in self.mockups:
            if not m.html.strip():
                errors.append(f"{self.screen_id} {m.state}: empty markup")
            for tag in dict.fromkeys(t.lower() for t in _FORBIDDEN_TAG.findall(m.html)):
                why = "use an .img box instead" if tag == "img" else "body markup only; the page and styles are provided"
                errors.append(f"{self.screen_id} {m.state}: uses a <{tag}> tag ({why})")
            for style in (attr.group(2) for attr in _STYLE_ATTR.finditer(m.html)):
                for hex_color in _HEX_COLOR.findall(style):
                    errors.append(f"{self.screen_id} {m.state}: uses a raw hex color ({hex_color}); use var(--token)")
            for pattern, what in _FORBIDDEN_MARKUP:
                found = pattern.search(m.html)
                if found:
                    errors.append(f"{self.screen_id} {m.state}: uses {what.format(found.group(1) if found.groups() else found.group(0))}")
            for var in re.findall(r"var\(--([A-Za-z0-9_-]+)\)", m.html):
                if var not in color_tokens and not _KIT_VAR.match(var):
                    errors.append(f"{self.screen_id} {m.state}: var(--{var}) is not a design-system token")
        return errors
