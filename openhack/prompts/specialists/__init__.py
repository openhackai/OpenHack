"""Exploitation playbooks for the per-vuln-class specialist agents."""

from .xss import XSS_PLAYBOOK
from .injection import INJECTION_PLAYBOOK
from .ssrf import SSRF_PLAYBOOK
from .ssti import SSTI_PLAYBOOK
from .auth import AUTH_PLAYBOOK
from .blind import BLIND_PLAYBOOK

__all__ = [
    "XSS_PLAYBOOK",
    "INJECTION_PLAYBOOK",
    "SSRF_PLAYBOOK",
    "SSTI_PLAYBOOK",
    "AUTH_PLAYBOOK",
    "BLIND_PLAYBOOK",
]
