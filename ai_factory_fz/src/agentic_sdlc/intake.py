"""Intake (step 0, the Product Owner): a request becomes intent.md with a risk tier.

A brief file may start with YAML front matter:

    ---
    title: B2B mobile store
    product_owner: Pat Owner
    risk_tier: M          # L | M | H
    ---
    <the request, in the customer's words>

`uv run intake` asks the Product Owner for these and writes the brief file; `uv run kickoff --brief` reads it.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

TIERS = ("L", "M", "H")
TIER_MEANING = {"L": "low risk: internal or reversible; the design gate may be waived",
                "M": "medium risk: every gate is held by a person",
                "H": "high risk: money, personal data or hard-to-undo changes; every gate, closest review"}
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Front matter (dict) and the request body. No front matter: ({}, text)."""
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        return {}, text
    tier = str(meta.get("risk_tier", "")).strip().upper()
    if tier and tier not in TIERS:
        raise ValueError(f"risk_tier must be one of {', '.join(TIERS)}, not '{tier}'")
    return meta, text[m.end():]


def intent_markdown(meta: dict[str, Any], body: str, risk_tier: str) -> str:
    title = meta.get("title") or "Intent"
    lines = ["---", "agent: product_owner (human)", f"product_owner: {meta.get('product_owner') or '(not given)'}",
             f"risk_tier: {risk_tier}", f"written_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             "---", "", f"# {title}", "", f"Risk tier **{risk_tier}**: {TIER_MEANING.get(risk_tier, '')}", "",
             "## Request", "", body.strip(), ""]
    return "\n".join(lines)


def brief_file(title: str, product_owner: str, risk_tier: str, request: str) -> str:
    meta = yaml.safe_dump({"title": title, "product_owner": product_owner, "risk_tier": risk_tier},
                          sort_keys=False).strip()
    return f"---\n{meta}\n---\n{request.strip()}\n"


def interview(input_fn: Callable[[str], str] = input) -> str:
    """Ask the Product Owner for the intake fields; returns the brief file text."""
    title = ""
    while not title:
        title = input_fn("Title of the request: ").strip()
    owner = ""
    while len(owner) < 2:
        owner = input_fn("Product Owner (your name): ").strip()
    tier = ""
    while tier not in TIERS:
        tier = input_fn("Risk tier [L/M/H] (L low, M medium, H high): ").strip().upper()
    print("Describe the request in the customer's words. End with a line containing only a dot (.)")
    lines = []
    while (line := input_fn("")) != ".":
        lines.append(line)
    return brief_file(title, owner, tier, "\n".join(lines))


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "request"


def write_brief(briefs_dir: Path, text: str) -> Path:
    meta, _ = parse(text)
    path = briefs_dir / f"{slug(str(meta.get('title', 'request')))}.md"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
