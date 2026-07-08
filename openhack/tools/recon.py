"""
Reconnaissance tools: structured wrappers around the standard recon CLIs.

The agent could drive these through the raw shell, but dedicated tools give it
normalized output, sane non-interactive defaults, and a clear "tool not
installed" signal instead of a cryptic shell error. Everything here is the
active-but-standard recon a pentester runs at the start of an engagement:
subdomain discovery, HTTP fingerprinting, port scanning, DNS/WHOIS, and
templated scanning with nuclei.
"""

import json
import shlex
import subprocess
from shutil import which
from typing import Optional


def _run(cmd: list[str], timeout: int, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)


def _missing(tool: str, install_hint: str) -> dict:
    return {
        "error": "tool_not_installed",
        "tool": tool,
        "reason": f"`{tool}` is not installed. Install it ({install_hint}) to use this.",
    }


_MAX = 40_000


def _cap(text: str) -> str:
    return text if len(text) <= _MAX else text[:_MAX] + f"\n... [{len(text) - _MAX:,} chars truncated]"


class ReconTools:
    """Subdomain, HTTP, port, DNS and templated-scan reconnaissance."""

    def subdomains(self, domain: str, timeout: int = 180) -> dict:
        """Enumerate subdomains of a domain with subfinder (passive)."""
        if not domain:
            return {"error": "missing_domain"}
        if which("subfinder") is None:
            return _missing("subfinder", "brew install subfinder")
        try:
            proc = _run(["subfinder", "-silent", "-d", domain], timeout=min(timeout, 600))
        except subprocess.TimeoutExpired:
            return {"tool": "subfinder", "error": "timeout"}
        hosts = [h for h in proc.stdout.splitlines() if h.strip()]
        return {"tool": "subfinder", "domain": domain, "count": len(hosts), "subdomains": hosts[:1000]}

    def http_probe(self, target: str, timeout: int = 60) -> dict:
        """Fingerprint an HTTP target (status, title, tech, server) with httpx.

        `target` may be a host, URL, or comma-separated list. Falls back to curl
        headers if httpx isn't installed.
        """
        if not target:
            return {"error": "missing_target"}
        targets = [t.strip() for t in target.split(",") if t.strip()]
        if which("httpx") is not None:
            try:
                proc = _run(
                    ["httpx", "-silent", "-json", "-status-code", "-title",
                     "-tech-detect", "-web-server", "-no-color"],
                    timeout=min(timeout, 300),
                    stdin="\n".join(targets),
                )
            except subprocess.TimeoutExpired:
                return {"tool": "httpx", "error": "timeout"}
            rows = []
            for line in proc.stdout.splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return {"tool": "httpx", "count": len(rows), "results": rows[:200]}
        # Fallback: curl -I
        if which("curl") is None:
            return _missing("httpx", "brew install httpx (or install curl)")
        out = []
        for t in targets[:20]:
            url = t if t.startswith("http") else f"http://{t}"
            try:
                proc = _run(["curl", "-sS", "-I", "-m", "15", url], timeout=30)
                out.append({"target": url, "headers": _cap(proc.stdout)})
            except subprocess.TimeoutExpired:
                out.append({"target": url, "error": "timeout"})
        return {"tool": "curl", "count": len(out), "results": out}

    def port_scan(self, target: str, ports: Optional[str] = None, timeout: int = 300) -> dict:
        """Scan a host's TCP ports with nmap (service/version detection)."""
        if not target:
            return {"error": "missing_target"}
        if which("nmap") is None:
            return _missing("nmap", "brew install nmap")
        cmd = ["nmap", "-sV", "-Pn", "-T4"]
        if ports:
            cmd += ["-p", str(ports)]
        else:
            cmd += ["--top-ports", "1000"]
        cmd.append(target)
        try:
            proc = _run(cmd, timeout=min(timeout, 900))
        except subprocess.TimeoutExpired:
            return {"tool": "nmap", "error": "timeout", "note": "Narrow the port range or raise timeout."}
        return {"tool": "nmap", "target": target, "output": _cap(proc.stdout), "stderr": proc.stderr[:500]}

    def dns_lookup(self, name: str, record_type: str = "A") -> dict:
        """Resolve DNS records for a name (A/AAAA/MX/TXT/NS/CNAME...) via dig."""
        if not name:
            return {"error": "missing_name"}
        if which("dig") is None:
            return _missing("dig", "usually preinstalled; install bind tools")
        rtype = (record_type or "A").upper()
        try:
            proc = _run(["dig", "+short", name, rtype], timeout=30)
        except subprocess.TimeoutExpired:
            return {"tool": "dig", "error": "timeout"}
        answers = [a for a in proc.stdout.splitlines() if a.strip()]
        return {"tool": "dig", "name": name, "type": rtype, "answers": answers}

    def nuclei_scan(self, target: str, severity: Optional[str] = None,
                    tags: Optional[str] = None, timeout: int = 600) -> dict:
        """Run templated vulnerability scanning against a URL with nuclei.

        Optionally filter by `severity` (e.g. 'critical,high') or `tags`
        (e.g. 'cve,exposure'). Returns structured findings.
        """
        if not target:
            return {"error": "missing_target"}
        if which("nuclei") is None:
            return _missing("nuclei", "brew install nuclei")
        cmd = ["nuclei", "-silent", "-jsonl", "-target", target, "-no-color"]
        if severity:
            cmd += ["-severity", severity]
        if tags:
            cmd += ["-tags", tags]
        try:
            proc = _run(cmd, timeout=min(timeout, 1800))
        except subprocess.TimeoutExpired:
            return {"tool": "nuclei", "error": "timeout"}
        findings = []
        for line in proc.stdout.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = obj.get("info", {})
            findings.append({
                "template": obj.get("template-id", ""),
                "name": info.get("name", ""),
                "severity": info.get("severity", ""),
                "matched": obj.get("matched-at", obj.get("host", "")),
                "type": obj.get("type", ""),
            })
        return {"tool": "nuclei", "target": target, "count": len(findings), "findings": findings[:300]}

    def sqlmap_test(
        self,
        url: str,
        data: Optional[str] = None,
        cookie: Optional[str] = None,
        extra: Optional[str] = None,
        timeout: int = 900,
    ) -> dict:
        """Test a URL for SQL injection with sqlmap (fully automated, --batch).

        Prefer this over hand-crafting injection payloads — sqlmap detects the
        injection point, DBMS and technique in one call. Pass `data` for a POST
        body, `cookie` for an authenticated session, and `extra` for sqlmap flags
        (e.g. "--dump -T users", "--current-db", "-p id", "--level 3 --risk 2").
        """
        if not url:
            return {"error": "missing_url"}
        if which("sqlmap") is None:
            return _missing("sqlmap", "brew install sqlmap")
        cmd = ["sqlmap", "-u", url, "--batch", "--disable-coloring"]
        if data:
            cmd += ["--data", data]
        if cookie:
            cmd += ["--cookie", cookie]
        if extra:
            cmd += shlex.split(extra)
        try:
            proc = _run(cmd, timeout=min(timeout, 1800))
        except subprocess.TimeoutExpired:
            return {"tool": "sqlmap", "error": "timeout",
                    "note": "sqlmap exceeded the timeout — narrow with -p <param> or lower --level."}
        out = _cap(proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))
        low = proc.stdout.lower()
        injectable = ("is vulnerable" in low or "injectable" in low
                      or "sqlmap identified the following injection point" in low)
        return {
            "tool": "sqlmap",
            "target": url,
            "injectable": injectable,
            "output": out,
        }

    # -------------------------------------------------------------- tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "subdomains",
                "description": "Passively enumerate subdomains of a domain (subfinder). Good first recon step for a web target.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Apex domain, e.g. example.com"},
                        "timeout": {"type": "integer", "description": "Max seconds (default 180)."},
                    },
                    "required": ["domain"],
                },
            },
            {
                "name": "http_probe",
                "description": "Fingerprint HTTP target(s): status code, page title, detected tech and web server (httpx). Accepts a host, URL, or comma-separated list.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Host/URL or comma-separated list."},
                        "timeout": {"type": "integer", "description": "Max seconds (default 60)."},
                    },
                    "required": ["target"],
                },
            },
            {
                "name": "port_scan",
                "description": "Scan a host's TCP ports with service/version detection (nmap). Defaults to the top 1000 ports.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Host or IP to scan."},
                        "ports": {"type": "string", "description": "Port spec, e.g. '80,443' or '1-1000'. Omit for top 1000."},
                        "timeout": {"type": "integer", "description": "Max seconds (default 300)."},
                    },
                    "required": ["target"],
                },
            },
            {
                "name": "dns_lookup",
                "description": "Resolve DNS records for a name (A/AAAA/MX/TXT/NS/CNAME/...) via dig.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The DNS name to resolve."},
                        "record_type": {"type": "string", "description": "Record type (default A)."},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "nuclei_scan",
                "description": "Run templated vulnerability scanning against a URL with nuclei (CVEs, misconfigurations, exposures). Filter by severity or tags.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "URL to scan, e.g. https://example.com"},
                        "severity": {"type": "string", "description": "e.g. 'critical,high'"},
                        "tags": {"type": "string", "description": "e.g. 'cve,exposure'"},
                        "timeout": {"type": "integer", "description": "Max seconds (default 600)."},
                    },
                    "required": ["target"],
                },
            },
            {
                "name": "sqlmap_test",
                "description": (
                    "Test a URL for SQL injection with sqlmap (automated). Prefer this "
                    "over hand-crafting SQLi payloads — it finds the injection point, "
                    "DBMS and technique, and can dump data, in one call. Pass `data` for "
                    "POST, `cookie` for auth, and `extra` for sqlmap flags "
                    "(e.g. '--dump -T users', '--current-db', '-p id', '--level 3')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target URL, incl. a parameter to test, e.g. http://host/item?id=1"},
                        "data": {"type": "string", "description": "POST body to test instead of the query string."},
                        "cookie": {"type": "string", "description": "Cookie header for an authenticated session."},
                        "extra": {"type": "string", "description": "Extra sqlmap flags, e.g. '--dump -T users' or '-p id --level 3'."},
                        "timeout": {"type": "integer", "description": "Max seconds (default 900)."},
                    },
                    "required": ["url"],
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {
            "subdomains": self.subdomains,
            "http_probe": self.http_probe,
            "port_scan": self.port_scan,
            "dns_lookup": self.dns_lookup,
            "nuclei_scan": self.nuclei_scan,
            "sqlmap_test": self.sqlmap_test,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return func(**filtered)
