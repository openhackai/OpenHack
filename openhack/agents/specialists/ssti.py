from openhack.prompts.specialists import SSTI_PLAYBOOK
from .base_specialist import SpecialistAgent


class SSTISpecialist(SpecialistAgent):
    name = "openhack-ssti"
    description = "SSTI → RCE exploitation specialist"
    vuln_class = "ssti"
    PLAYBOOK = SSTI_PLAYBOOK
