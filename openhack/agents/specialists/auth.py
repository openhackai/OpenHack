from openhack.prompts.specialists import AUTH_PLAYBOOK
from .base_specialist import SpecialistAgent


class AuthSpecialist(SpecialistAgent):
    name = "openhack-auth"
    description = "Auth / access-control exploitation specialist"
    vuln_class = "auth"
    PLAYBOOK = AUTH_PLAYBOOK
