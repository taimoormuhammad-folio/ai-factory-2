"""Guardrails for the other agents' outputs (all code checks).

  QA1 consistent verdict: passed <=> no blocker/major bugs          (QA engineer, Integration pass)
  QA2 bugs point to real work items and have steps/expected/actual   (QA engineer, Integration pass)
  DE1 container hygiene: non-root image, toolchain's runtime major    (Deployment engineer)
  ST1 real smoke/device suites: enough journeys, real HTTP, no mocks  (Smoke tester)
"""

import json
import re
from pathlib import Path
from typing import Any

from agentic_sdlc.artifacts.reports import QAReport
from agentic_sdlc.registry.profiles import Profile
from agentic_sdlc.workspace import Workspace

MAX_DEVICE_JOURNEYS = 8
AGENT_RULES = ["DV1", "DV2", "DV3", "QA1", "QA2", "DE1", "ST1"]


def enabled(pipeline: dict[str, Any]) -> set[str]:
    """Agent guardrails switched on (`guardrails.agents`: a list, or 'all'). Default: all."""
    setting = (pipeline.get("guardrails") or {}).get("agents", "all")
    if setting == "all":
        return set(AGENT_RULES)
    unknown = [r for r in setting if r not in AGENT_RULES]
    if unknown:
        raise ValueError(f"Unknown agent guardrails in pipeline config: {unknown} (known: {AGENT_RULES})")
    return set(setting)


# ---------- QA engineer / Integration pass ----------

def report_errors(report: QAReport, item_ids: list[str], rules: set[str], extra_ids: tuple[str, ...] = ()) -> list[str]:
    errors = []
    if "QA1" in rules and report.passed != (not report.blocking_bugs()):
        errors.append(f"QA1: passed is {report.passed} but there are {len(report.blocking_bugs())} blocker/major bugs; "
                      "passed must be true exactly when there are none")
    if "QA2" in rules:
        valid = set(item_ids) | set(extra_ids)
        for b in report.bugs:
            if b.work_item_id not in valid:
                errors.append(f"QA2: {b.id} names work item '{b.work_item_id}', which is not one of {sorted(valid)}")
            missing = [f for f in ("steps", "expected", "actual") if not getattr(b, f).strip()]
            if missing:
                errors.append(f"QA2: {b.id} has no {', '.join(missing)}")
    return errors


# ---------- Deployment engineer ----------

def _major(image: str) -> str | None:
    m = re.search(r":(\d+)", image)
    return m.group(1) if m else None


def de1_container(ws: Workspace, profile: Profile) -> list[str]:
    api = profile.components.get(profile.release.api_component)
    dockerfile = ws.root / (api.workdir if api else ".") / "Dockerfile"
    if not dockerfile.exists():
        return [f"DE1: {dockerfile.relative_to(ws.root)} is missing"]
    text = dockerfile.read_text(encoding="utf-8", errors="replace")
    stages = re.split(r"(?im)^\s*FROM\s+", text)[1:]
    if not stages:
        return ["DE1: the Dockerfile has no FROM instruction"]
    errors = []
    final = stages[-1]
    users = re.findall(r"(?im)^\s*USER\s+(\S+)", final)
    if not users or users[-1] in ("root", "0", "0:0"):
        errors.append("DE1: the final image runs as root; add a non-root USER (e.g. USER node) in the last stage")
    want = _major(profile.sandbox.runtimes[api.runtime].image) if api and api.runtime in profile.sandbox.runtimes else None
    if want:
        for stage in dict.fromkeys(s.split()[0] for s in stages):   # each base image once
            image = stage
            got = _major(image)
            if image.startswith(("node:", "node@")) and got and got != want:
                errors.append(f"DE1: base image {image} uses Node {got}, but the build toolchain uses Node {want} "
                              "(the lockfile was made with it); use node:" + want + "-slim or similar")
    return errors


# ---------- Smoke tester ----------

def st1_smoke_suite(ws: Workspace, profile: Profile, min_tests: int) -> list[str]:
    api = profile.components.get(profile.release.api_component)
    if not api or not profile.release.smoke_command:
        return []
    root = ws.root / api.workdir
    pkg = root / "package.json"
    script = profile.release.smoke_command.removeprefix("npm run ").strip()
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, ValueError):
        scripts = {}
    if script not in scripts:
        return [f"ST1: package.json has no '{script}' script"]
    files = [p for p in root.rglob("*") if p.is_file() and "node_modules" not in p.parts and "smoke" in str(p.relative_to(root)).lower()
             and p.suffix in (".ts", ".js")]
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
    errors = []
    cases = len(re.findall(r"\b(it|test)\s*\(", text))
    if cases < min_tests:
        errors.append(f"ST1: the smoke suite has {cases} test(s); write at least {min_tests} journeys")
    if "SMOKE_BASE_URL" not in text:
        errors.append("ST1: the smoke suite does not use SMOKE_BASE_URL to reach the running API")
    if re.search(r"from\s+['\"](\.\./)+src/|require\(['\"](\.\./)+src/", text):
        errors.append("ST1: the smoke suite imports application code; test the running API over HTTP only")
    if re.search(r"\bjest\.mock\(|\bnock\(|\bmsw\b|setupServer\(", text):
        errors.append("ST1: the smoke suite mocks the network; call the real API")
    return errors


def st1_device_suite(ws: Workspace, profile: Profile) -> list[str]:
    dev = profile.device
    if not dev:
        return []
    app = profile.components.get(dev.app_component)
    root = ws.root / (app.workdir if app else ".") / dev.test_dir
    files = list(root.rglob("*.dart")) if root.exists() else []
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
    errors = []
    if not re.search(r"\btestWidgets\s*\(", text):
        errors.append(f"ST1: no on-device journey tests (testWidgets) in {dev.test_dir}/")
    if re.search(r"\bMock(Client|Dio|HttpClient)\b|package:mocktail|package:mockito|http_mock_adapter", text):
        errors.append("ST1: the device tests mock the network; they must use the real staging API")
    journeys = len(re.findall(r"\btestWidgets\s*\(", text))
    if journeys > MAX_DEVICE_JOURNEYS:
        errors.append(f"ST1: {journeys} on-device journeys; write at most {MAX_DEVICE_JOURNEYS} (a handful of reliable "
                      f"ones beats many fragile ones)")
    guessed = sorted(set(re.findall(r"find\.byType\(\s*(ElevatedButton|TextButton|OutlinedButton|FilledButton|IconButton|"
                                    r"FloatingActionButton|ListTile|Card|TextField|TextFormField)\b", text)))
    if guessed:
        errors.append(f"ST1: widgets found by guessed type ({', '.join(guessed)}); find them by the app's own "
                      f"Key (find.byKey, see lib/ and docs/ui-design.md), which is stable")
    return errors
