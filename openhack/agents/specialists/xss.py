from openhack.prompts.specialists import XSS_PLAYBOOK
from .base_specialist import SpecialistAgent


class XSSSpecialist(SpecialistAgent):
    name = "openhack-xss"
    description = "XSS exploitation specialist"
    vuln_class = "xss"
    PLAYBOOK = XSS_PLAYBOOK
    NEEDS_STATEFUL_BROWSER = True
