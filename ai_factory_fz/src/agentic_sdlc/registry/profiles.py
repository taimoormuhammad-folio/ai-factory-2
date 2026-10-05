"""App-type profiles: stack, domain context, agent overrides, components and sandbox rules."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agentic_sdlc.settings import PROFILES_DIR, load_yaml


class Runtime(BaseModel):
    image: str                  # Docker image (docker mode)
    local_binary: str           # must be on PATH in local mode
    local_prefix: str = ""      # prepended to commands in local mode (e.g. an npx wrapper)
    # Docker mode only: run the container as root (for images whose SDK must write to root-owned
    # paths, e.g. Flutter), then give files it created back to the host user.
    run_as_root: bool = False
    env: dict[str, str] = Field(default_factory=dict)   # Docker mode only; container paths


class SandboxConfig(BaseModel):
    runtimes: dict[str, Runtime] = Field(default_factory=dict)
    allowed_commands: dict[str, list[str]] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)  # non-secret env for every command


class ScaffoldStep(BaseModel):
    run: str | None = None               # command (trusted, from this profile)
    runtime: str | None = None           # defaults to the component's runtime
    workdir: str = "."
    copy_from: str | None = None         # or: copy a workspace file
    template: str | None = None          # or: copy a file/folder from the profile's templates/ folder
    copy_to: str | None = None
    creates: str | None = None           # skip the step if this path already exists


class AcceptanceTests(BaseModel):
    """Where the Test Writer puts a component's locked acceptance tests and how they run (in the workdir)."""
    dir: str                             # e.g. server/test/acceptance (relative to the run folder)
    command: str                         # e.g. npm run test:acceptance
    note: str = ""                       # extra instruction for the Test Writer (e.g. add the npm script)


class Component(BaseModel):
    agent: str
    workdir: str
    runtime: str | None = None           # None: no toolchain needed (e.g. config files)
    checks: list[str] = Field(default_factory=list)   # must pass before an item counts as done
    scaffold: list[ScaffoldStep] = Field(default_factory=list)
    acceptance: AcceptanceTests | None = None


class ReleaseConfig(BaseModel):
    api_component: str = "backend"            # component that serves the API
    api_prefix: str = ""                       # e.g. /api/v1
    health_path: str = "/health"               # must return 200 when the API is up
    openapi_json_path: str = ""                # where the running API serves its OpenAPI JSON
    compose_file: str = "infra/docker-compose.staging.yml"
    compose_api_service: str = "api"          # service name of the API in the compose file
    env_file: str = "infra/staging.env"        # test-only values; written by the Deployment engineer
    local_prepare: list[str] = Field(default_factory=list)   # before starting the API locally
    local_start: str = ""                      # command that runs the API (in the component workdir)
    smoke_command: str = ""                    # runs the smoke suite; gets SMOKE_BASE_URL
    package_commands: list[str] = Field(default_factory=list)  # build release artifacts (API component)
    staging_notes: str = ""                    # extra instructions for the Deployment engineer


class DeviceConfig(BaseModel):
    """Running the mobile app on an Android emulator (release phase and `showcase`)."""
    app_component: str = "frontend"
    app_id: str = ""                       # Android application id, e.g. com.example.app
    system_image: str = "system-images;android-35;google_apis;x86_64"
    avd_name: str = "sdlc_phone"
    device_profile: str = "pixel_7"
    console_port: int = 5554               # device serial is emulator-<port>
    host_alias: str = "10.0.2.2"           # how the emulator reaches this machine
    # Command templates, run in the app component's workdir. Placeholders: {api_base}, {serial}.
    build_command: str = ""
    apk_path: str = ""                     # relative to the app workdir
    test_command: str = ""
    build_timeout_s: int = 2700            # the first Android build downloads Gradle + SDK parts
    test_dir: str = "integration_test"
    # Extra API environment while staging serves a device (placeholders {host}, {port}).
    asset_env: dict[str, str] = Field(default_factory=dict)


class Profile(BaseModel):
    name: str
    description: str = ""
    stack: dict[str, str] = Field(default_factory=dict)
    domain_entities: list[str] = Field(default_factory=list)
    # False: the API owns no database (e.g. it fronts another system); no Prisma schema, no
    # data-model guardrails, staging without PostgreSQL.
    database: bool = True
    agent_context: dict[str, list[str]] = Field(default_factory=dict)
    agent_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    components: dict[str, Component] = Field(default_factory=dict)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    release: ReleaseConfig = Field(default_factory=ReleaseConfig)
    device: DeviceConfig | None = None
    guardrails: dict[str, Any] = Field(default_factory=dict)   # facts the output guardrails check against
    root: Path

    @classmethod
    def load(cls, name: str, profiles_dir: Path | None = None) -> "Profile":
        root = (profiles_dir or PROFILES_DIR) / name
        path = root / "profile.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Profile '{name}' not found at {path}")
        return cls(**load_yaml(path), root=root)

    def stack_summary(self) -> str:
        return "; ".join(f"{k}: {v}" for k, v in self.stack.items())

    def layout_summary(self) -> str:
        """The repository folders the build tooling creates and the coding agents work in."""
        lines = [f"- {c.workdir}/: the {name} ({c.agent})" for name, c in self.components.items() if c.workdir != "."]
        copies = (self.guardrails.get("contract_copies") or {}).get("docs/api-contract.yaml", [])
        lines.append("- docs/api-contract.yaml: the API contract" + (f" (copied to {', '.join(copies)})" if copies else ""))
        for name, c in self.components.items():
            for step in c.scaffold:
                if step.template and step.copy_to:
                    lines.append(f"- {step.copy_to}/: provided ready-made from the profile; extend it, do not rewrite it")
                elif step.run and step.creates and Path(step.creates).parent.as_posix() not in (c.workdir, "."):
                    lines.append(f"- {Path(step.creates).parent.as_posix()}/: generated by the {name} scaffold")
        lines.append(f"- {self.release.compose_file}, {self.release.env_file}: staging")
        return "\n".join(lines)

    def layout_roots(self) -> set[str]:
        """Top-level folders of the layout (anything else is outside it)."""
        paths = [c.workdir for c in self.components.values()] + ["docs", self.release.compose_file]
        paths += [s.copy_to or s.creates or "" for c in self.components.values() for s in c.scaffold]
        return {p.split("/")[0] for p in paths if p and p != "."} | {"docs", "infra", ".github"}

    def context_for(self, agent_key: str) -> str:
        """Concatenated knowledge files configured for this agent."""
        parts = []
        for rel in self.agent_context.get(agent_key, []):
            parts.append((self.root / rel).read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)
