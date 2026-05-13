from __future__ import annotations
from typing import Any, Dict
from sqlalchemy.orm import Session
from .registry import registry
from .base import SkillResult

class SkillExecutor:
    def __init__(self, session: Session):
        self.session = session

    async def run(self, skill_name: str, **kwargs) -> SkillResult:
        skill_class = registry.get_skill(skill_name)
        if not skill_class:
            return SkillResult(success=False, message=f"Skill {skill_name} not found", error="NotFound")
        
        # Instantiate and execute
        skill_instance = skill_class(session=self.session)
        
        # Validate input if schema exists
        if skill_class.input_schema:
            try:
                # This ensures parameters match the expected schema
                validated_params = skill_class.input_schema(**kwargs)
                return await skill_instance.execute(**validated_params.model_dump())
            except Exception as e:
                return SkillResult(success=False, message=f"Invalid parameters for {skill_name}", error=str(e))
        
        return await skill_instance.execute(**kwargs)
