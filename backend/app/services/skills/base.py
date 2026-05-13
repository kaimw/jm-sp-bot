from __future__ import annotations
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

class SkillResult(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class BaseSkill:
    """
    Base class for all Agent Skills.
    A Skill is a discrete unit of logic that an Agent can invoke.
    """
    name: str = ""
    description: str = ""
    
    # Optional Pydantic model for parameter validation
    input_schema: Optional[type[BaseModel]] = None

    def __init__(self, session=None):
        self.session = session

    async def execute(self, **kwargs) -> SkillResult:
        """
        Execute the skill logic.
        """
        raise NotImplementedError("Skills must implement execute()")

    def get_metadata(self) -> Dict[str, Any]:
        """
        Returns metadata about the skill for LLM tool selection.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema.model_json_schema() if self.input_schema else {}
        }
