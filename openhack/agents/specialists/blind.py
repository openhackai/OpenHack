from openhack.prompts.specialists import BLIND_PLAYBOOK
from .base_specialist import SpecialistAgent


class BlindOOBSpecialist(SpecialistAgent):
    name = "openhack-blind"
    description = "Blind / out-of-band exploitation specialist"
    vuln_class = "blind"
    PLAYBOOK = BLIND_PLAYBOOK
