"""Architecture artifact, including the OpenAPI contract and Prisma schema."""

import re

import yaml
from openapi_spec_validator import validate
from pydantic import BaseModel, Field


# The approved API contract (copied into the components by the scaffold, checked against the running API).
CONTRACT_PATH = "docs/api-contract.yaml"


class Component(BaseModel):
    name: str
    responsibility: str
    technology: str


class BackendModule(BaseModel):
    name: str
    responsibility: str
    entities: list[str]
    endpoints: list[str] = Field(description="e.g. 'GET /api/v1/products'")


class AppFeature(BaseModel):
    name: str
    screens: list[str]
    state_management: str


class ADR(BaseModel):
    id: str = Field(description="ADR-001, ...")
    title: str
    context: str
    decision: str
    consequences: str


class DesignOption(BaseModel):
    name: str
    summary: str
    pros: list[str]
    cons: list[str]
    simplest: bool = Field(default=False, description="True for the simplest option that meets the spec")


class ArchitectureDoc(BaseModel):
    options: list[DesignOption] = Field(default_factory=list,
                                        description="At least two design options, including the simplest")
    recommended_option: str = Field(default="", description="Name of the recommended option, and the design below")
    overview: str
    components: list[Component]
    backend_modules: list[BackendModule]
    app_features: list[AppFeature]
    data_model_notes: str
    security: list[str]
    adrs: list[ADR]
    openapi_yaml: str = Field(description="Complete OpenAPI 3.1 document as YAML")
    prisma_schema: str = Field(description="Complete Prisma schema for PostgreSQL; empty when the stack has no database")

    def openapi_errors(self) -> list[str]:
        """Parse and validate the OpenAPI document; also require operationIds."""
        try:
            spec = yaml.safe_load(self.openapi_yaml)
        except yaml.YAMLError as e:
            return [f"openapi_yaml is not valid YAML: {e}"]
        if not isinstance(spec, dict):
            return ["openapi_yaml must be a YAML mapping"]
        try:
            validate(spec)
        except Exception as e:  # validator raises several exception types
            return [f"OpenAPI validation failed: {str(e).splitlines()[0]}"]
        errors = []
        for path, ops in (spec.get("paths") or {}).items():
            for method, op in ops.items():
                if method in {"get", "post", "put", "patch", "delete"} and not op.get("operationId"):
                    errors.append(f"{method.upper()} {path} has no operationId")
        if not spec.get("paths"):
            errors.append("OpenAPI document has no paths")
        return errors

    def prisma_errors(self) -> list[str]:
        s = self.prisma_schema
        errors = []
        if "datasource" not in s or "postgresql" not in s:
            errors.append("Prisma schema needs a postgresql datasource")
        if "model " not in s:
            errors.append("Prisma schema defines no models")
        return errors

    def no_database_errors(self) -> list[str]:
        """For stacks without a database: the API keeps no data of its own."""
        if self.prisma_schema.strip():
            return ["This stack has no database: leave prisma_schema empty (the API keeps no data of its own)"]
        return []

    def operations(self) -> dict[str, str]:
        """operationId -> 'METHOD /path' from the OpenAPI document (empty if it does not parse)."""
        try:
            spec = yaml.safe_load(self.openapi_yaml) or {}
        except yaml.YAMLError:
            return {}
        ops = {}
        for path, item in (spec.get("paths") or {}).items():
            for method, op in (item or {}).items():
                if method in {"get", "post", "put", "patch", "delete"} and isinstance(op, dict) and op.get("operationId"):
                    ops[op["operationId"]] = f"{method.upper()} {path}"
        return ops

    def data_models(self) -> list[str]:
        """Model names from the Prisma schema."""
        return re.findall(r"^model\s+(\w+)\s*\{", self.prisma_schema, flags=re.MULTILINE)

    def solution_summary(self) -> str:
        """Compact view of what must be built, for planning."""
        lines = ["Backend modules:"]
        lines += [f"- {m.name}: {m.responsibility} (entities: {', '.join(m.entities) or '-'})" for m in self.backend_modules]
        lines += ["", "API operations (operationId: method path):"]
        lines += [f"- {op}: {route}" for op, route in self.operations().items()]
        lines += ["", "Data models: " + (", ".join(self.data_models()) or "(none: no database)")]
        lines += ["", "App features:", self.app_features_summary()]
        return "\n".join(lines)

    def app_features_summary(self) -> str:
        return "\n".join(
            f"- {f.name}: screens {', '.join(f.screens)} (state: {f.state_management})"
            for f in self.app_features
        )

    def to_markdown(self) -> str:
        lines = ["# Design", ""]
        if self.options:
            lines += ["## Options considered", ""]
            for o in self.options:
                tag = " (simplest)" if o.simplest else ""
                mark = " **recommended**" if o.name == self.recommended_option else ""
                lines += [f"### {o.name}{tag}{mark}", o.summary, "", "Pros:"] + [f"- {p}" for p in o.pros] \
                    + ["", "Cons:"] + [f"- {c}" for c in o.cons] + [""]
            lines += [f"Recommendation: **{self.recommended_option}**", ""]
        lines += ["## Architecture", "", self.overview, "", "## Components"]
        lines += [f"- **{c.name}** ({c.technology}): {c.responsibility}" for c in self.components]
        lines += ["", "## Backend modules"]
        for m in self.backend_modules:
            lines += [f"### {m.name}", m.responsibility, f"Entities: {', '.join(m.entities)}", ""]
            lines += [f"- `{e}`" for e in m.endpoints]
            lines.append("")
        lines += ["## Mobile app features", self.app_features_summary(), ""]
        lines += ["## Data model", self.data_model_notes, "", "## Security"]
        lines += [f"- {s}" for s in self.security]
        lines += ["", "## Architecture decisions"]
        for a in self.adrs:
            lines += [
                f"### {a.id}: {a.title}",
                f"**Context:** {a.context}",
                "",
                f"**Decision:** {a.decision}",
                "",
                f"**Consequences:** {a.consequences}",
                "",
            ]
        if self.prisma_schema.strip():
            lines.append("The API contract is in `openapi.yaml`; the data model is in `schema.prisma`.")
        else:
            lines.append("The API contract is in `openapi.yaml`; the API has no database of its own.")
        return "\n".join(lines)
