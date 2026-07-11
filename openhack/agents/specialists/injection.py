from openhack.prompts.specialists import INJECTION_PLAYBOOK
from .base_specialist import SpecialistAgent


class InjectionSpecialist(SpecialistAgent):
    name = "openhack-injection"
    description = "Injection (SQLi/NoSQLi/cmd) exploitation specialist"
    vuln_class = "injection"
    PLAYBOOK = INJECTION_PLAYBOOK
