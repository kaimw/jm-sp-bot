from __future__ import annotations

import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import SystemConfig
from backend.app.services.crypto import encrypt_value
from backend.app.services.oms.jackyun_client import JackyunConfig, JackyunClient, jackyun_config_from_session, normalize_response, sign_params


def test_jackyun_sign_params_uses_sorted_common_params_and_secret_wrap():
    params = {
        "method": "erp-goods.goods.sku.search",
        "appkey": "app-key",
        "version": "1.0",
        "contenttype": "json",
        "timestamp": "2026-06-12 10:00:00",
        "bizcontent": '{"pageNo":1,"pageSize":1}',
    }
    expected_raw = (
        "secret"
        + "appkeyapp-key"
        + 'bizcontent{"pageNo":1,"pageSize":1}'
        + "contenttypejson"
        + "methoderp-goods.goods.sku.search"
        + "timestamp2026-06-12 10:00:00"
        + "version1.0"
        + "secret"
    ).lower()

    assert sign_params(params, "secret") == hashlib.md5(expected_raw.encode("utf-8")).hexdigest()


def test_jackyun_client_builds_signed_request_params():
    client = JackyunClient(JackyunConfig(gateway_url="https://example.test", app_key="app-key", app_secret="secret"))

    params = client.build_common_params("erp-stock.stock.skulist", {"warehouseCode": "A1"}, timestamp="2026-06-12 10:00:00")

    assert params["method"] == "erp-stock.stock.skulist"
    assert params["appkey"] == "app-key"
    assert params["bizcontent"] == '{"warehouseCode":"A1"}'
    assert len(params["sign"]) == 32


def test_jackyun_normalize_response_accepts_success_code_200():
    result = normalize_response({"code": 200, "msg": "操作成功", "data": {"goods": []}})

    assert result["ok"] is True
    assert result["data"] == {"goods": []}


def test_jackyun_config_decrypts_secret_app_secret():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(SystemConfig(key="oms_jackyun_app_key", value="app-key", is_secret=False))
    session.add(SystemConfig(key="oms_jackyun_app_secret", value=encrypt_value("plain-secret"), is_secret=True))
    session.commit()

    config = jackyun_config_from_session(session)

    assert config.app_key == "app-key"
    assert config.app_secret == "plain-secret"
