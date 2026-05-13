from __future__ import annotations
import os
import re
import logging
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session
from backend.app.models import ModelProviderConfig
from backend.app.services.model_provider import call_model, extract_chat_content
from .registry import registry

logger = logging.getLogger(__name__)

SKILL_TEMPLATE = """
from __future__ import annotations
from pydantic import BaseModel, Field
from ..base import BaseSkill, SkillResult
from ..registry import SkillRegistry

# 你可以根据需要导入其他 service
# from backend.app.services.products import ...

class {class_name}Input(BaseModel):
    {input_fields}

@SkillRegistry.register
class {class_name}Skill(BaseSkill):
    name = "{skill_name}"
    description = "{description}"
    input_schema = {class_name}Input

    async def execute(self, **kwargs) -> SkillResult:
        try:
            # 在这里实现逻辑
            # 使用 self.session 进行数据库操作
            {logic_code}
            return SkillResult(success=True, message="执行成功", data={{}})
        except Exception as e:
            return SkillResult(success=False, message=str(e), error=str(e))
"""

class SkillFactory:
    @staticmethod
    async def generate_skill(session: Session, user_requirement: str) -> Dict[str, Any]:
        """
        Calls LLM to generate a new skill based on user requirement.
        """
        config = session.query(ModelProviderConfig).filter_by(status="Active").first()
        if not config:
            raise RuntimeError("没有激活的模型配置，请先在后台设置。")

        system_prompt = (
            "你是一个高级 Python 开发者，专注于构建自动化 Skill。"
            "你需要根据用户需求生成一个符合 BaseSkill 规范的 Python 类。"
            "规则：\n"
            "1. 只输出代码，不要有任何解释。\n"
            "2. 类名必须以 Skill 结尾。\n"
            "3. 必须定义 input_schema (Pydantic Model)。\n"
            "4. 必须使用 @SkillRegistry.register 装饰器。\n"
            "5. 逻辑必须在 async execute 方法中。\n"
            "6. 名字(name) 只能使用小写字母和下划线。\n"
            "7. 所有的数据库操作应使用 self.session。"
        )

        user_prompt = f"用户需求：{user_requirement}\n\n请生成代码："

        response = call_model(
            session,
            config,
            task_type="SkillGeneration",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )

        code = extract_chat_content(response)
        # 清理可能存在的 markdown 标记
        code = re.sub(r"```python\n|```", "", code).strip()

        # 尝试从代码中提取技能名称
        name_match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', code)
        skill_name = name_match.group(1) if name_match else "dynamic_skill_" + str(os.urandom(2).hex())

        return {
            "skill_name": skill_name,
            "code": code
        }

    @staticmethod
    def validate_code(code: str) -> bool:
        """
        Performs static analysis on the generated code to ensure safety and correctness.
        """
        import ast
        try:
            tree = ast.parse(code)

            # 简单的安全性检查：禁止执行危险模块
            forbidden_imports = {"os", "subprocess", "sys", "shutil"}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_imports:
                            raise ValueError(f"Forbidden import detected: {alias.name}")
                if isinstance(node, ast.ImportFrom):
                    if node.module in forbidden_imports:
                        raise ValueError(f"Forbidden import detected: {node.module}")

                # 检查是否有 execute 方法
                if isinstance(node, ast.ClassDef):
                    methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
                    if "execute" not in methods:
                        raise ValueError("Missing required 'execute' method in Skill class")

            return True
        except Exception as e:
            logger.error(f"Skill validation failed: {e}")
            return False

    @staticmethod
    def save_and_load(skill_name: str, code: str) -> bool:
        """
        Saves the generated code to the dynamic directory and reloads the registry.
        """
        # 增加安全性验证
        if not SkillFactory.validate_code(code):
            logger.error(f"Generated code for {skill_name} failed validation.")
            return False

        try:
            dynamic_path = os.path.join(os.path.dirname(__file__), "dynamic")
            file_path = os.path.join(dynamic_path, f"{skill_name}.py")

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)

            # 触发热加载
            registry.load_dynamic_skills()
            return True
        except Exception as e:
            logger.exception(f"Failed to save and load skill {skill_name}")
            return False
    @staticmethod
    def delete_skill(skill_name: str) -> bool:
        """
        Deletes the skill file (both .py and .py.disabled).
        """
        try:
            dynamic_path = os.path.join(os.path.dirname(__file__), "dynamic")
            paths = [
                os.path.join(dynamic_path, f"{skill_name}.py"),
                os.path.join(dynamic_path, f"{skill_name}.py.disabled")
            ]
            for p in paths:
                if os.path.exists(p):
                    os.remove(p)

            registry.load_dynamic_skills()
            return True
        except Exception as e:
            logger.exception(f"Failed to delete skill {skill_name}")
            return False

    @staticmethod
    def toggle_skill(skill_name: str, active: bool) -> bool:
        """
        Enables or disables a skill by renaming the file.
        """
        try:
            dynamic_path = os.path.join(os.path.dirname(__file__), "dynamic")
            py_path = os.path.join(dynamic_path, f"{skill_name}.py")
            disabled_path = os.path.join(dynamic_path, f"{skill_name}.py.disabled")

            if active:
                # Enable: rename .py.disabled -> .py
                if os.path.exists(disabled_path):
                    if os.path.exists(py_path):
                        os.remove(py_path) # Overwrite existing if any
                    os.rename(disabled_path, py_path)
            else:
                # Disable: rename .py -> .py.disabled
                if os.path.exists(py_path):
                    if os.path.exists(disabled_path):
                        os.remove(disabled_path)
                    os.rename(py_path, disabled_path)

            registry.load_dynamic_skills()
            return True
        except Exception as e:
            logger.exception(f"Failed to toggle skill {skill_name}")
            return False
