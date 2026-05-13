from typing import Dict, Type, List, Any, Optional
import os
import re
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
    def list_skills(cls) -> List[Dict[str, Any]]:
        skills = []
        # Active skills from registry
        for name, skill in cls._skills.items():
            module = getattr(skill, "__module__", "")
            source = "dynamic" if module and "skills.dynamic" in module else "builtin"
            skills.append({
                "name": name,
                "description": getattr(skill, "description", "无描述"),
                "active": True,
                "enabled": True,
                "status": "Enabled",
                "status_label": "已启用",
                "source": source,
                "toggleable": source == "dynamic",
                "deletable": source == "dynamic",
            })
        
        # Scan for disabled ones
        dynamic_path = os.path.join(os.path.dirname(__file__), "dynamic")
        if os.path.exists(dynamic_path):
            try:
                for filename in os.listdir(dynamic_path):
                    if filename.endswith(".py.disabled"):
                        name, desc = cls._extract_metadata_from_file(os.path.join(dynamic_path, filename))
                        if name not in cls._skills: # Avoid duplicates
                            skills.append({
                                "name": name,
                                "description": desc,
                                "active": False,
                                "enabled": False,
                                "status": "Disabled",
                                "status_label": "已停用",
                                "source": "dynamic",
                                "toggleable": True,
                                "deletable": True,
                            })
            except Exception:
                pass
        return sorted(skills, key=lambda x: x.get("name", ""))

    @classmethod
    def _extract_metadata_from_file(cls, file_path: str) -> tuple[str, str]:
        """Simple regex extraction of name and description from file."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                name_match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', content)
                desc_match = re.search(r'description\s*=\s*["\']([^"\']+)["\']', content)
                name = name_match.group(1) if name_match else os.path.basename(file_path).split(".")[0]
                desc = desc_match.group(1) if desc_match else "无描述 (已停用)"
                return name, desc
        except Exception:
            return os.path.basename(file_path).split(".")[0], "解析失败"

    @classmethod
    def clear_dynamic_skills(cls):
        """Removes skills that were loaded from the dynamic directory."""
        to_remove = []
        for name, skill in cls._skills.items():
            module = getattr(skill, "__module__", "")
            if module and "skills.dynamic" in module:
                to_remove.append(name)
        
        for name in to_remove:
            if name in cls._skills:
                del cls._skills[name]

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
        cls.clear_dynamic_skills()
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
