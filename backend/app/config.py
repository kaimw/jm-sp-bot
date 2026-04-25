from __future__ import annotations

import os
from dataclasses import dataclass


MAIL_WORKER_MIN_INTERVAL_SECONDS = 60
MAIL_LOGIN_MIN_INTERVAL_SECONDS = 60


def int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "local")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/app.db")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin")
    auth_secret: str = os.getenv("AUTH_SECRET", "jm-sp-bot-local-auth-secret")
    auth_session_seconds: int = int_env("AUTH_SESSION_SECONDS", 28800)
    mail_auto_worker_enabled: bool = os.getenv("MAIL_AUTO_WORKER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    mail_login_min_interval_seconds: int = MAIL_LOGIN_MIN_INTERVAL_SECONDS
    mail_worker_min_interval_seconds: int = MAIL_WORKER_MIN_INTERVAL_SECONDS
    mail_auto_worker_interval_seconds: int = max(
        MAIL_WORKER_MIN_INTERVAL_SECONDS,
        int_env("MAIL_AUTO_WORKER_INTERVAL_SECONDS", MAIL_LOGIN_MIN_INTERVAL_SECONDS),
    )
    mail_rate_limit_interval_seconds: int = max(
        MAIL_LOGIN_MIN_INTERVAL_SECONDS,
        int_env("MAIL_RATE_LIMIT_INTERVAL_SECONDS", MAIL_LOGIN_MIN_INTERVAL_SECONDS),
    )
    mail_auto_worker_limit: int = int_env("MAIL_AUTO_WORKER_LIMIT", 20)

    bot_email: str = os.getenv("BOT_EMAIL", "bot.market@jimuyida.com")
    bot_email_password: str = os.getenv("BOT_EMAIL_PASSWORD", "")
    bot_display_name: str = os.getenv("BOT_DISPLAY_NAME", "商务部小J")
    bot_signature: str = os.getenv("BOT_SIGNATURE", "积木易搭AI机器人")
    imap_host: str = os.getenv("IMAP_HOST", "imap.exmail.qq.com")
    imap_port: int = int_env("IMAP_PORT", 993)
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.exmail.qq.com")
    smtp_port: int = int_env("SMTP_PORT", 465)
    ceo_email: str = os.getenv("CEO_EMAIL", "dingyong@jimuyida.com")
    ops_cc_email: str = os.getenv("OPS_CC_EMAIL", "jinlei@jimuyida.com")
    e2e_sales_email: str = os.getenv("E2E_SALES_EMAIL", "bot.sales@jimuyida.com")
    e2e_sales_password: str = os.getenv("E2E_SALES_PASSWORD", "")
    e2e_production_email: str = os.getenv("E2E_PRODUCTION_EMAIL", "bot.production@jimuyida.com")
    e2e_production_password: str = os.getenv("E2E_PRODUCTION_PASSWORD", "")

    model_title: str = os.getenv("MODEL_TITLE", "Dify deepseekV3")
    model_provider: str = os.getenv("MODEL_PROVIDER", "openai")
    model_name: str = os.getenv("MODEL_NAME", "DeepSeek-V3")
    model_api_base: str = os.getenv("MODEL_API_BASE", "http://192.168.10.55:5000/v1")
    baidu_map_ak: str = os.getenv("BAIDU_MAP_AK", "WlbmVQwUBkBnqJgXjmkK6mReKCyEdWSi")

    zip_max_bytes: int = int_env("ZIP_MAX_BYTES", 104857600)
    zip_max_depth: int = int_env("ZIP_MAX_DEPTH", 1)
    storage_budget_bytes: int = int_env("STORAGE_BUDGET_BYTES", 10737418240)
    non_target_retention_days: int = int_env("NON_TARGET_RETENTION_DAYS", 30)
    attachment_storage_dir: str = os.getenv("ATTACHMENT_STORAGE_DIR", "data/attachments")


settings = Settings()
