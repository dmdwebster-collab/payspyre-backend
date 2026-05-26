"""Guardrail: PlatformCreditApplication.status is written ONLY by flow_orchestrator.py.

Hard rule (kickoff §3, spec §4.3): application status transitions are owned by the
orchestrator. This scans every file under app/ that references
``PlatformCreditApplication`` and fails if any of them (other than
``flow_orchestrator.py``) contains a ``.status =`` assignment.

Scoping by ``PlatformCreditApplication`` reference is deliberate: the V1
loan/funding/underwriting endpoints write ``application.status`` on *V1* models
that never reference ``PlatformCreditApplication``, so they are correctly out of
scope for this V2 rule. ORM model files define ``status = Column(...)`` (no
leading dot) and so never match the assignment pattern.
"""
import re
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
_OWNER = "flow_orchestrator.py"
# A `.status =` assignment — excluding `==` comparisons and `.status_updated_at` / `.status_code`.
_STATUS_WRITE = re.compile(r"\.status\s*=(?!=)")


def test_application_status_written_only_by_orchestrator():
    offenders: list[str] = []
    for path in _APP_DIR.rglob("*.py"):
        if path.name == _OWNER:
            continue
        text = path.read_text(encoding="utf-8")
        if "PlatformCreditApplication" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "status_updated_at" in line or "status_code" in line:
                continue
            if _STATUS_WRITE.search(line):
                offenders.append(f"{path.relative_to(_APP_DIR.parent)}:{lineno}: {stripped}")

    assert not offenders, (
        "PlatformCreditApplication.status must only be written in flow_orchestrator.py.\n"
        "Offending lines:\n" + "\n".join(offenders)
    )
