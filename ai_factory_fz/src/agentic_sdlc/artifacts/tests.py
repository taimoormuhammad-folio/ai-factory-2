"""Acceptance tests written by the Test Writer before the build: at least one test per acceptance
criterion (AC-..), in the component's acceptance folder. They must fail before the code exists, then
they are locked by content hash (tests.lock); builders change the code, never these tests."""

from pydantic import BaseModel, Field


class AcceptanceTest(BaseModel):
    ac_id: str = Field(description="The acceptance criterion this test proves, e.g. AC-07")
    file: str = Field(description="Test file path relative to the project root, e.g. server/test/acceptance/ac-07.e2e-spec.ts")
    test_name: str = Field(description="The test's name as written in the file (contains the AC id)")
    how: str = Field(default="", description="One line: what the test does and checks")


class AcceptanceSuite(BaseModel):
    tests: list[AcceptanceTest]
    notes: str = ""
    blocked: bool = Field(default=False, description="True if a criterion cannot be tested as written; explain")
    blocked_reason: str = ""

    def errors(self, ac_ids: list[str], folder: str) -> list[str]:
        covered = {t.ac_id for t in self.tests}
        errors = [f"No acceptance test for {a}" for a in ac_ids if a not in covered]
        errors += [f"{t.file} is outside the acceptance folder {folder}/" for t in self.tests
                   if not t.file.startswith(folder.rstrip("/") + "/")]
        errors += [f"Test for {t.ac_id} must name the criterion id in its test name" for t in self.tests
                   if t.ac_id not in t.test_name]
        return errors


def test_plan_markdown(suites: dict[str, AcceptanceSuite]) -> str:
    """docs/test-plan.md: acceptance criterion -> test, for every milestone and component."""
    lines = ["# Test plan", "", "Acceptance tests written before the build and locked (tests.lock).", "",
             "| Criterion | Milestone / component | Test file | Test | What it checks |", "|---|---|---|---|---|"]
    for key, suite in sorted(suites.items()):
        lines += [f"| {t.ac_id} | {key} | {t.file} | {t.test_name} | {t.how or '-'} |" for t in suite.tests]
    return "\n".join(lines) + "\n"
