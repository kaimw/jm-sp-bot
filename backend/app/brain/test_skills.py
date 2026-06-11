import asyncio
import os

from backend.app.database import SessionLocal
from backend.app.services.skills import mail_skills  # noqa: F401
from backend.app.services.skills.registry import registry


async def main() -> None:
    print("Registered skills:")
    for skill in registry.list_skills():
        print(f"- {skill['name']}: {skill['description']}")

    if os.getenv("RUN_SKILL_SELFTEST") != "1":
        print("\nSet RUN_SKILL_SELFTEST=1 to execute the receive_mails skill.")
        return

    with SessionLocal() as session:
        from backend.app.services.skills.executor import SkillExecutor

        executor = SkillExecutor(session)
        result = await executor.run("receive_mails", limit=1)
        print(f"\nreceive_mails: success={result.success}, message={result.message}")


if __name__ == "__main__":
    asyncio.run(main())
