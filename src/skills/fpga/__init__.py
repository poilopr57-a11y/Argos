"""FPGA Skill package — re-export для SkillLoader ARGOS."""
from .skill import (
    SKILL_NAME, SKILL_DESCRIPTION, TRIGGERS,
    setup, teardown, handle, execute,
    snapshot, heal,
)

__all__ = [
    "SKILL_NAME", "SKILL_DESCRIPTION", "TRIGGERS",
    "setup", "teardown", "handle", "execute", "snapshot", "heal",
]
