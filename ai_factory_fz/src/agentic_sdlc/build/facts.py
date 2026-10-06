"""Facts the pipeline already knows, handed to the agents that write tests, so they read them instead of guessing:
the widget keys the app really defines, the seeded records (their real ids), the API operations of the approved
contract and who owns which files. Collected from the code itself (never from an agent's memory), so it is correct
by construction and refreshed before every test-writing job."""

import re
from pathlib import Path

import yaml

KEY = re.compile(r"""\b(?:Value)?Key\(\s*(?:const\s+)?(['"])(.+?)\1\s*\)""")
SEED_ID = re.compile(r"""\bid:\s*(['"`])([^'"`$]+)\1""")
SEED_NAME = re.compile(r"""\bname:\s*(['"`])([^'"`$]+)\1""")
DEFAULT = {"keys": ["app/lib/**/*.dart"], "seeds": ["server/prisma/seed*.ts", "server/src/**/seed*.ts"]}


def _files(root: Path, globs: list[str]) -> list[Path]:
    seen: dict[str, Path] = {}
    for g in globs:
        for p in sorted(root.glob(g)):
            if p.is_file() and "node_modules" not in p.parts and ".pub-cache" not in p.parts:
                seen[str(p)] = p
    return list(seen.values())


def widget_keys(root: Path, globs: list[str]) -> list[tuple[str, str]]:
    """(key, file) for every Key('...') literal; `$id` style parts mark a template (the id is filled at run time)."""
    out: dict[str, str] = {}
    for p in _files(root, globs):
        for m in KEY.finditer(p.read_text(encoding="utf-8", errors="replace")):
            out.setdefault(m.group(2), p.relative_to(root).as_posix())
    return sorted(out.items())


def seed_records(root: Path, globs: list[str]) -> list[tuple[str, str, str]]:
    """(id, name, file): each seeded record with its real id and the name that follows it."""
    out = []
    for p in _files(root, globs):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in SEED_ID.finditer(text):
            name = SEED_NAME.search(text, m.end(), m.end() + 200)
            out.append((m.group(2), name.group(2) if name else "", p.relative_to(root).as_posix()))
    return out


def operations(root: Path) -> list[str]:
    contract = root / "docs" / "api-contract.yaml"
    if not contract.is_file():
        return []
    try:
        doc = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    return [f"{method.upper()} {path}" for path, ops in sorted((doc.get("paths") or {}).items())
            for method in (ops or {}) if method in ("get", "post", "put", "patch", "delete")]


def build(root: Path, profile, wbs=None) -> str:
    """The facts as markdown, for the `{facts}` placeholder of the test-writing tasks."""
    cfg = {**DEFAULT, **(profile.guardrails.get("facts") or {})}
    keys, seeds, ops = widget_keys(root, cfg["keys"]), seed_records(root, cfg["seeds"]), operations(root)
    lines = ["FACTS (collected from the code just now; use these exactly, never invent ids, keys or paths):", ""]
    lines += ["Widget keys defined in the app ('$x' parts are filled at run time, so find those by key PREFIX):"]
    lines += [f"- Key('{k}')  [{f}]" for k, f in keys] or ["- (the app defines no keys yet)"]
    lines += ["", "Seeded records (real ids; the staging database holds exactly these):"]
    lines += [f"- id {i}{'  name ' + n if n else ''}  [{f}]" for i, n, f in seeds] or ["- (no seed data found)"]
    lines += ["", "API operations of the approved contract:"] + [f"- {o}" for o in ops] or ["- (none)"]
    if wbs is not None:
        lines += ["", "Who may change which files:"]
        lines += [f"- {t.id} ({t.component}): {', '.join(t.owns)}" for t in wbs.tasks]
    return "\n".join(lines) + "\n"
