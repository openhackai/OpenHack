from openhack.prompts.specialists import SSRF_PLAYBOOK
from .base_specialist import SpecialistAgent


class SSRFSpecialist(SpecialistAgent):
    name = "openhack-ssrf"
    description = "SSRF exploitation specialist"
    vuln_class = "ssrf"
    PLAYBOOK = SSRF_PLAYBOOK
