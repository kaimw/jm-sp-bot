from __future__ import annotations

import logging
import os
import re
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import SystemConfig, ModelProviderConfig
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.crypto import encrypt_value, decrypt_value, get_encryption_cipher
from backend.app.main import (
    get_secret_values,
    SensitiveDataFormatter,
    setup_log_scrubbing,
    update_mail_config,
    update_erp_config,
    update_model_provider,
)
from backend.app.schemas import MailRuntimeConfigUpdate, ErpRuntimeConfigUpdate, ModelProviderUpdate


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session, engine


def test_encryption_and_decryption_in_database():
    session, engine = make_session()
    
    key = "test_sensitive_api_key"
    val = "my-super-secret-api-key-12345"
    
    config = SystemConfig(key=key, value=val, is_secret=True)
    session.add(config)
    session.commit()
    
    # 验证底层 SQL 查询到的是密文
    with engine.connect() as conn:
        res = conn.execute(text("SELECT value FROM system_configs WHERE key = :key"), {"key": key}).fetchone()
        raw_db_val = res[0]
        assert raw_db_val.startswith("enc:")
        assert raw_db_val != val
        
    # 验证 ORM 读取后自动解密
    session.close()
    
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    new_session = Session()
    orm_config = new_session.get(SystemConfig, key)
    assert orm_config.value == val
    
    # 验证 instance state 干净，没有被标记为 dirty
    from sqlalchemy.orm import attributes
    state = attributes.instance_state(orm_config)
    assert "value" not in state.committed_state
    
    new_session.close()


def test_crypto_fallback_and_error_handling(caplog):
    original_key = os.environ.get("CONFIG_ENCRYPTION_KEY")
    if "CONFIG_ENCRYPTION_KEY" in os.environ:
        del os.environ["CONFIG_ENCRYPTION_KEY"]
        
    from backend.app.services import crypto
    crypto._cipher = None
    
    with caplog.at_level(logging.WARNING):
        cipher = get_encryption_cipher()
        assert cipher is not None
        assert any("CONFIG_ENCRYPTION_KEY environment variable is not set" in r.message for r in caplog.records)
        
    plain = "hello-world"
    enc = encrypt_value(plain)
    assert enc.startswith("enc:")
    dec = decrypt_value(enc)
    assert dec == plain
    
    # 验证解密失败时原样返回
    bad_enc = "enc:garbage-token-decryption-failure"
    dec_bad = decrypt_value(bad_enc)
    assert dec_bad == bad_enc
    
    if original_key is not None:
        os.environ["CONFIG_ENCRYPTION_KEY"] = original_key
    crypto._cipher = None


def test_logging_scrubbing():
    from unittest.mock import patch
    session, engine = make_session()
    
    MemSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    
    with patch("backend.app.database.SessionLocal", MemSessionLocal):
        key = "model_api_key_test_scrub"
        val = "dify-super-secret-key-to-scrub-12345"
        config = SystemConfig(key=key, value=val, is_secret=True)
        session.add(config)
        session.commit()
        
        # 强制将 cache 设为过期
        import backend.app.main as main_mod
        main_mod._last_cache_time = 0.0
        
        secrets = get_secret_values()
        assert val in secrets
        
        formatter = SensitiveDataFormatter(secrets_getter=get_secret_values)
        # 静态匹配测试
        log_record_1 = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="API Key settings: api_key='secret_api_key' and passwd: 'some_password'",
            args=(),
            exc_info=None
        )
        formatted_1 = formatter.format(log_record_1)
        assert "api_key='***'" in formatted_1
        assert "passwd: '***'" in formatted_1
        assert "secret_api_key" not in formatted_1
        assert "some_password" not in formatted_1
        
        # 动态匹配测试
        log_record_2 = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg=f"Sending request with API Key: {val}",
            args=(),
            exc_info=None
        )
        formatted_2 = formatter.format(log_record_2)
        assert val not in formatted_2
        assert "***" in formatted_2
        
        session.close()


def test_api_placeholder_prevention():
    session, engine = make_session()
    
    # 1. Mail config
    set_config(session, "bot_email_password", "original-bot-password", is_secret=True)
    session.commit()
    
    payload_mail = MailRuntimeConfigUpdate(bot_email_password="***")
    update_mail_config(payload_mail, session=session)
    session.commit()
    assert session.get(SystemConfig, "bot_email_password").value == "original-bot-password"
    
    # 2. ERP config
    set_config(session, "erp_app_sec", "original-erp-sec", is_secret=True)
    session.commit()
    
    payload_erp = ErpRuntimeConfigUpdate(erp_app_sec="***")
    update_erp_config(payload_erp, session=session)
    session.commit()
    assert session.get(SystemConfig, "erp_app_sec").value == "original-erp-sec"
    
    # 3. Model config
    model = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if model is None:
        model = ModelProviderConfig(status="Active")
        session.add(model)
    set_config(session, "model_api_key", "original-model-key", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()
    
    payload_model = ModelProviderUpdate(api_key="***")
    update_model_provider(payload_model, session=session)
    session.commit()
    assert session.get(SystemConfig, "model_api_key").value == "original-model-key"
    
    session.close()
