from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_compose_declares_postgres_and_app_healthchecks():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "postgres:" in compose
    assert "image: postgres:16-alpine" in compose
    assert "DATABASE_URL: postgresql+psycopg://" in compose
    assert "condition: service_healthy" in compose
    assert "http://127.0.0.1:8000/health" in compose
    assert "postgres_data:" in compose
    assert "app_data:" in compose


def test_production_env_example_keeps_required_secrets_as_placeholders():
    env = (ROOT / ".env.production.example").read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD=replace-with-strong-database-password" in env
    assert "ADMIN_PASSWORD=replace-with-strong-admin-password" in env
    assert "AUTH_SECRET=replace-with-long-random-auth-secret" in env
    assert "BOT_EMAIL_PASSWORD=" in env
    assert "MODEL_API_KEY=" in env
