from __future__ import annotations
from typing import Dict, Type, List
from .base import BaseSkill

class SkillRegistry:
    _skills: Dict[str, Type[BaseSkill]] = {}

    @classmethod
    def register(cls, skill_class: Type[BaseSkill]):
        """Decorator to register a skill."""
        cls._skills[skill_class.name] = skill_class
        return skill_class

    @classmethod
    def get_skill(cls, name: str) -> Optional[Type[BaseSkill]]:
        return cls._skills.get(name)

    @classmethod
    def list_skills(cls) -> List[Dict[str, str]]:
        return [
            {"name": name, "description": skill.description}
            for name, skill in cls._skills.items()
        ]

    @classmethod
    def get_all_skills(cls) -> Dict[str, Type[BaseSkill]]:
        return cls._skills

    @classmethod
    def load_dynamic_skills(cls):
        """Scan and load skills from the dynamic directory."""
        import importlib
        import pkgutil
        from . import dynamic
        import logging
        
        logger = logging.getLogger(__name__)
        for loader, module_name, is_pkg in pkgutil.walk_packages(dynamic.__path__, dynamic.__name__ + "."):
            try:
                # Import or reload the module to trigger @SkillRegistry.register
                module = importlib.import_module(module_name)
                importlib.reload(module)
                logger.info(f"Successfully loaded dynamic skill module: {module_name}")
            except Exception as e:
                logger.error(f"Failed to load dynamic skill module {module_name}: {e}")

# Singleton-like access
registry = SkillRegistry()
