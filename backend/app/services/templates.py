from __future__ import annotations

from typing import Any


def render_template(template: str, context: dict[str, Any]) -> str:
    output = template
    for key, value in context.items():
        output = output.replace("{{" + key + "}}", "" if value is None else str(value))
    return output
