"""
Findings tools — let the interactive agent record and recall vulnerabilities.

The agent talks in prose, but confirmed vulnerabilities should also become
structured findings so the operator can see them with /findings, and so the
agent can recall and summarise them later (across turns) instead of re-deriving
them. report_finding writes into the session; list_findings reads them back.
"""

from typing import Optional


_SEVERITIES = {"critical", "high", "medium", "low", "info"}


class FindingsTools:
    """Record confirmed findings into the session and read them back."""

    def __init__(self, session):
        self.session = session

    def report_finding(
        self,
        title: str,
        severity: str = "medium",
        description: str = "",
        category: str = "",
        file_path: str = "",
        line_number: Optional[int] = None,
        poc: Optional[str] = None,
        fix: Optional[str] = None,
        cvss_score: Optional[float] = None,
    ) -> dict:
        """Record a confirmed vulnerability so it shows up in /findings.

        Only call this once you've actually confirmed the issue — this is the
        operator-facing findings list, not a scratchpad.
        """
        if not title:
            return {"error": "a title is required"}
        from openhack.agents.session import Finding

        sev = (severity or "medium").lower().strip()
        if sev not in _SEVERITIES:
            sev = "medium"
        finding = Finding(
            category=category or "misc",
            severity=sev,
            title=title,
            description=description or "",
            file_path=file_path or "",
            line_number=line_number,
            poc=poc,
            fix=fix,
            cvss_score=cvss_score,
            source="agent",
            validated=True,
        )
        self.session.add_finding(finding)
        # Surface it in the live transcript / sidebar too.
        try:
            self.session.add_trace(
                agent="openhack", event_type="finding_added",
                content={"severity": sev, "title": title, "file_path": file_path or ""},
            )
        except Exception:
            pass
        return {"recorded": True, "id": finding.id, "total_findings": len(self.session.findings)}

    def list_findings(self) -> dict:
        """List all findings recorded in this session (for summary / Q&A)."""
        findings = getattr(self.session, "findings", []) or []
        return {
            "count": len(findings),
            "findings": [
                {
                    "title": f.title,
                    "severity": f.severity,
                    "category": f.category,
                    "file": f.file_path,
                    "line": f.line_number,
                    "description": (f.description or "")[:500],
                    "fix": (f.fix or "")[:500] if f.fix else None,
                }
                for f in findings[:200]
            ],
        }

    # -------------------------------------------------------------- tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "report_finding",
                "description": (
                    "Record a CONFIRMED vulnerability into the session's findings so "
                    "the operator can see it with /findings and you can recall it later. "
                    "Only report issues you've actually verified."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short finding title."},
                        "severity": {"type": "string", "description": "critical | high | medium | low | info"},
                        "description": {"type": "string", "description": "What it is and the impact."},
                        "category": {"type": "string", "description": "e.g. sqli, xss, idor, secret, dependency."},
                        "file_path": {"type": "string", "description": "File (and use line_number) or URL/endpoint."},
                        "line_number": {"type": "integer", "description": "Line number, if applicable."},
                        "poc": {"type": "string", "description": "Proof-of-concept / reproduction steps."},
                        "fix": {"type": "string", "description": "Recommended remediation."},
                        "cvss_score": {"type": "number", "description": "CVSS score if known."},
                    },
                    "required": ["title", "severity"],
                },
            },
            {
                "name": "list_findings",
                "description": (
                    "List all findings recorded in this session so far. Use this to "
                    "summarise results or answer the operator's questions about findings."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {
            "report_finding": self.report_finding,
            "list_findings": self.list_findings,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return func(**filtered)
