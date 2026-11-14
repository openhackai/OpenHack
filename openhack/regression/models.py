"""
Data model for security regression test generation.

A generated regression test is only *accepted* when it demonstrably fails
because the vulnerability exists and passes once the vulnerability has been
remediated. Every finding the workflow looks at ends in exactly one
RegressionTestStatus, so an operator can tell "validated" apart from the many
legitimate reasons a test is not available.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Optional


class RegressionTestStatus(str, Enum):
    VALIDATED = "validated"
    UNSUPPORTED_VULNERABILITY = "unsupported_vulnerability"
    UNSUPPORTED_FRAMEWORK = "unsupported_framework"
    MISSING_EVIDENCE = "missing_evidence"
    GENERATION_FAILED = "generation_failed"
    VALIDATION_FAILED = "validation_failed"
    REMEDIATION_UNAVAILABLE = "remediation_unavailable"
    SANDBOX_ERROR = "sandbox_error"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


STATUS_LABELS: dict[RegressionTestStatus, str] = {
    RegressionTestStatus.VALIDATED: "generated and validated",
    RegressionTestStatus.UNSUPPORTED_VULNERABILITY: "unsupported vulnerability class",
    RegressionTestStatus.UNSUPPORTED_FRAMEWORK: "unsupported test framework",
    RegressionTestStatus.MISSING_EVIDENCE: "missing exploit evidence",
    RegressionTestStatus.GENERATION_FAILED: "test generation failed",
    RegressionTestStatus.VALIDATION_FAILED: "test validation failed",
    RegressionTestStatus.REMEDIATION_UNAVAILABLE: "remediation unavailable",
    RegressionTestStatus.SANDBOX_ERROR: "sandbox error",
}


@dataclass
class SecurityInvariant:
    """The security property a confirmed exploit violated.

    The generated test asserts this property for every principal and
    operation it covers, rather than replaying the one payload that happened
    to work in the sandbox.
    """

    vulnerability_class: str
    cwe: str
    statement: str
    operation: str
    resource: str
    method: str = ""
    path_template: str = ""
    example_path: str = ""
    object_param: Optional[str] = None
    param_location: Optional[str] = None
    denial: str = ""
    control: str = ""
    evidence: str = ""
    source_location: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SecurityInvariant":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class RegressionTestResult:
    """Outcome of the regression-test workflow for one finding.

    Serialized onto ``Finding.regression_test`` (and therefore into the scan
    report), so it must stay JSON-safe.
    """

    finding_id: str
    status: RegressionTestStatus
    reason: str
    vulnerability_class: Optional[str] = None
    invariant: Optional[SecurityInvariant] = None
    framework: Optional[str] = None
    test_path: Optional[str] = None
    test_code: Optional[str] = None
    run_command: Optional[str] = None
    invariant_tests: list[str] = field(default_factory=list)
    control_tests: list[str] = field(default_factory=list)
    summary: str = ""
    validation: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    artifacts: dict[str, str] = field(default_factory=dict)
    generated_at: str = ""
    cost: float = 0.0

    @property
    def reproduces_vulnerability(self) -> bool:
        """True when the test was proven to fail on the vulnerable revision."""
        vulnerable = self.validation.get("vulnerable") or {}
        return bool(vulnerable.get("passed"))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["status_label"] = self.status.label
        data["invariant"] = self.invariant.to_dict() if self.invariant else None
        data.pop("finding_id", None)
        return data

    @classmethod
    def from_dict(cls, finding_id: str, data: dict[str, Any]) -> "RegressionTestResult":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["finding_id"] = finding_id
        try:
            kwargs["status"] = RegressionTestStatus(data.get("status"))
        except ValueError:
            kwargs["status"] = RegressionTestStatus.GENERATION_FAILED
        kwargs.setdefault("reason", "")
        invariant = data.get("invariant")
        kwargs["invariant"] = (
            SecurityInvariant.from_dict(invariant) if isinstance(invariant, dict) else None
        )
        return cls(**kwargs)
