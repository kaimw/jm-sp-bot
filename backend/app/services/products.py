import json
import logging
import re
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.app.models import ProductSPU, ProductSKU, ChannelPricing, PromotionRule, SystemConfig, new_id
from backend.app.services.jsonutil import loads


logger = logging.getLogger(__name__)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def get_spu(session: Session, spu_id: str) -> ProductSPU | None:
    return session.query(ProductSPU).filter(ProductSPU.id == spu_id).first()

def get_spus(session: Session, skip: int = 0, limit: int = 100, query: str = None) -> tuple[list[ProductSPU], int]:
    q = session.query(ProductSPU)
    if query:
        q = q.filter(ProductSPU.name.ilike(f"%{query}%") | ProductSPU.spu_id.ilike(f"%{query}%"))
    total = q.count()
    items = q.order_by(ProductSPU.created_at.desc()).offset(skip).limit(limit).all()
    return items, total

def get_skus(session: Session, skip: int = 0, limit: int = 100, spu_id: str = None, spu_uuid: str = None) -> tuple[list[ProductSKU], int]:
    q = session.query(ProductSKU)
    if spu_uuid:
        q = q.where(ProductSKU.spu_uuid == spu_uuid)
    if spu_id:
        q = q.join(ProductSPU).where(ProductSPU.spu_id == spu_id)
    total = q.count()
    items = q.order_by(ProductSKU.created_at.desc()).offset(skip).limit(limit).all()
    return items, total

def get_channel_pricing(session: Session, skip: int = 0, limit: int = 100, sku_id: str = None, sku_uuid: str = None) -> tuple[list[ChannelPricing], int]:
    q = session.query(ChannelPricing)
    if sku_uuid:
        q = q.where(ChannelPricing.sku_uuid == sku_uuid)
    if sku_id:
        q = q.join(ProductSKU).where(ProductSKU.sku_id == sku_id)
    total = q.count()
    items = q.order_by(ChannelPricing.updated_at.desc()).offset(skip).limit(limit).all()
    return items, total

def get_promotions(session: Session, skip: int = 0, limit: int = 100) -> tuple[list[PromotionRule], int]:
    q = session.query(PromotionRule)
    total = q.count()
    items = q.order_by(PromotionRule.priority.desc(), PromotionRule.created_at.desc()).offset(skip).limit(limit).all()
    return items, total

def create_promotion_rule(
    session: Session,
    name: str,
    discount_type: str,
    discount_value: int,
    channel: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    priority: int = 0
) -> PromotionRule:
    rule = PromotionRule(
        name=name,
        channel=channel,
        start_time=start_time,
        end_time=end_time,
        priority=priority,
        discount_type=discount_type,
        discount_value=discount_value
    )
    session.add(rule)
    session.flush() # Ensure ID is generated
    return rule


def update_promotion_rule(
    session: Session,
    rule_id: str,
    **kwargs
) -> PromotionRule | None:
    from sqlalchemy import select
    rule = session.execute(select(PromotionRule).filter(PromotionRule.id == rule_id)).scalar_one_or_none()
    if not rule:
        return None
    
    for k, v in kwargs.items():
        if hasattr(rule, k):
            setattr(rule, k, v)
    
    rule.updated_at = now_utc()
    session.flush()
    return rule


def delete_promotion_rule(
    session: Session,
    rule_id: str
) -> bool:
    from sqlalchemy import select
    rule = session.execute(select(PromotionRule).filter(PromotionRule.id == rule_id)).scalar_one_or_none()
    if not rule:
        return False
    session.delete(rule)
    return True


def toggle_promotion_rule(
    session: Session,
    rule_id: str,
    is_active: bool
) -> PromotionRule | None:
    from sqlalchemy import select
    rule = session.execute(select(PromotionRule).filter(PromotionRule.id == rule_id)).scalar_one_or_none()
    if not rule:
        return None
    rule.is_active = is_active
    rule.updated_at = now_utc()
    session.flush()
    return rule

def create_spu(session: Session, spu_id: str, name: str, brand: str = None, category: str = None) -> ProductSPU:
    spu = ProductSPU(spu_id=spu_id, name=name, brand=brand, category=category)
    session.add(spu)
    return spu

def create_sku(session: Session, spu_uuid: str, sku_id: str, attributes: dict = None) -> ProductSKU:
    sku = ProductSKU(spu_uuid=spu_uuid, sku_id=sku_id, attributes_json=json.dumps(attributes or {}))
    session.add(sku)
    return sku

def set_channel_pricing(
    session: Session, 
    sku_uuid: str, 
    channel: str, 
    tier_a_price: int = None, 
    tier_b_price: int = None, 
    tier_c_price: int = None, 
    map_price: int = None, 
    promo_start_time: datetime = None,
    promo_end_time: datetime = None,
    currency: str = "USD"
) -> ChannelPricing:
    from sqlalchemy import select
    pricing = session.execute(select(ChannelPricing).where(ChannelPricing.sku_uuid == sku_uuid, ChannelPricing.channel == channel)).scalars().first()
    if not pricing:
        pricing = ChannelPricing(sku_uuid=sku_uuid, channel=channel)
        session.add(pricing)
    
    pricing.tier_a_price = tier_a_price
    pricing.tier_b_price = tier_b_price
    pricing.tier_c_price = tier_c_price
    pricing.map_price = map_price
    pricing.promo_start_time = promo_start_time
    pricing.promo_end_time = promo_end_time
    pricing.currency = currency
    pricing.updated_at = now_utc()
    return pricing


def config_bool(session: Session, key: str, default: bool) -> bool:
    row = session.get(SystemConfig, key)
    if row is None:
        return default
    value = str(row.value or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def parse_price_to_cents(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value * 100))
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(?:[¥￥$]\s*)?(\d+(?:,\d{3})*(?:\.\d+)?|\d+)(?:\s*(元|块|rmb|cny|人民币|分))?", text, flags=re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").lower()
    if unit == "分":
        return int(round(number))
    return int(round(number * 100))


PRICE_PATTERN = re.compile(
    r"(?:单价|价格|售价|报价|含税价|成交价|销售价)\s*[:：]?\s*[¥￥]?\s*(?P<price>\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:元|块|rmb|cny|人民币)?",
    flags=re.IGNORECASE,
)


def extract_order_products_from_text(session: Session, text: str, *, channel: str = "default") -> list[dict]:
    source = str(text or "")
    if not source.strip():
        return []
    active_skus = (
        session.execute(select(ProductSKU).where(ProductSKU.status == "Active").order_by(ProductSKU.sku_id.desc()))
        .scalars()
        .all()
    )
    if not active_skus:
        return []
    active_promotions = (
        session.query(PromotionRule)
        .filter(
            PromotionRule.is_active == True,
            (PromotionRule.channel == channel) | (PromotionRule.channel == None),
        )
        .all()
    )
    promotion_names = [promo.name for promo in active_promotions if promo.name and promo.name in source]
    items: list[dict] = []
    lower_source = source.lower()
    for sku in sorted(active_skus, key=lambda row: len(row.sku_id or ""), reverse=True):
        sku_id = str(sku.sku_id or "").strip()
        if not sku_id or sku_id.lower() not in lower_source:
            continue
        unit_price = _extract_price_near_sku(source, sku_id)
        items.append({"sku_id": sku_id, "sku_code": sku_id, "unit_price": unit_price, "promotion_applied": promotion_names})
    return items


def _extract_price_near_sku(text: str, sku_id: str) -> int | None:
    sku_index = text.lower().find(sku_id.lower())
    windows: list[str] = []
    if sku_index >= 0:
        windows.append(text[max(0, sku_index - 80): sku_index + len(sku_id) + 120])
    windows.append(text)
    for window in windows:
        match = PRICE_PATTERN.search(window)
        if match:
            return parse_price_to_cents(match.group("price"))
    return None


def normalize_extracted_product_item(item: dict) -> dict:
    normalized = dict(item)
    sku_id = (
        normalized.get("sku_id")
        or normalized.get("sku_code")
        or normalized.get("product_code")
        or normalized.get("code")
        or normalized.get("product_id")
    )
    normalized["sku_id"] = str(sku_id).strip() if sku_id is not None else ""
    normalized["sku_code"] = normalized.get("sku_code") or normalized["sku_id"]
    normalized["unit_price"] = parse_price_to_cents(normalized.get("unit_price"))
    promotions = normalized.get("promotion_applied") or normalized.get("promotions") or []
    if isinstance(promotions, str):
        promotions = [promotions]
    normalized["promotion_applied"] = [str(item).strip() for item in promotions if str(item).strip()]
    return normalized


def extract_order_products_for_review(session: Session, product_summary: str, source_text: str = "", *, channel: str = "default") -> list[dict]:
    local_items = extract_order_products_from_text(session, f"{product_summary or ''}\n{source_text or ''}", channel=channel)
    if local_items:
        return local_items
    if not config_bool(session, "product_price_review_llm_enabled", False):
        return []
    return extract_products_with_llm(session, product_summary or source_text)

def review_order_products(session: Session, extracted_items: list[dict], channel: str = "default") -> list[dict]:
    """
    Review a list of extracted material items against the database.
    Each item in extracted_items is expected to have:
      - 'sku_id'
      - 'unit_price' (integer representing cents/base unit)
      - 'promotion_applied' (optional list of promotion names)

    Returns the same list with added 'review_result' and 'risk_flags' keys.
    """
    results = []
    now = now_utc()
    
    # Get active promotions for the channel
    active_promotions = session.query(PromotionRule).filter(
        PromotionRule.is_active == True,
        (PromotionRule.channel == channel) | (PromotionRule.channel == None),
        (PromotionRule.start_time == None) | (PromotionRule.start_time <= now),
        (PromotionRule.end_time == None) | (PromotionRule.end_time >= now)
    ).all()
    active_promo_names = {p.name: p for p in active_promotions}

    require_unit_price = config_bool(session, "product_price_review_require_unit_price", False)
    for raw_item in extracted_items:
        item = normalize_extracted_product_item(raw_item)
        review = {"status": "Pass", "risk_flags": []}
        
        sku_id = item.get("sku_id")
        unit_price = item.get("unit_price")
        
        if not sku_id:
            review["status"] = "Warning"
            review["risk_flags"].append("未识别到 SKU，无法完成价格审查")
            item["review"] = review
            results.append(item)
            continue
            
        sku = session.scalar(select(ProductSKU).where(ProductSKU.sku_id == sku_id, ProductSKU.status == "Active"))
        if not sku:
            review["status"] = "Exception"
            review["risk_flags"].append(f"SKU 不存在或未启用：{sku_id}")
            item["review"] = review
            results.append(item)
            continue

        # Check Pricing
        if unit_price is not None:
            pricing = session.execute(select(ChannelPricing).where(ChannelPricing.sku_uuid == sku.id, ChannelPricing.channel == channel)).scalars().first()
            if pricing:
                # Determine applicable minimum price based on time
                is_promo_period = False
                if pricing.promo_start_time and pricing.promo_end_time:
                    if pricing.promo_start_time <= now <= pricing.promo_end_time:
                        is_promo_period = True
                elif pricing.promo_start_time and now >= pricing.promo_start_time:
                    is_promo_period = True
                elif pricing.promo_end_time and now <= pricing.promo_end_time:
                    is_promo_period = True
                
                # Base minimum price
                base_min_price = pricing.map_price if pricing.map_price is not None else pricing.tier_a_price
                
                if is_promo_period:
                    candidates = [x for x in [pricing.tier_b_price, pricing.tier_c_price, base_min_price] if x is not None]
                    promo_min = min(candidates) if candidates else None
                    if promo_min is not None and unit_price < promo_min:
                        review["status"] = "Exception"
                        review["risk_flags"].append(f"销售单价 {format_cents(unit_price)} 低于促销期最低价 {format_cents(promo_min)}")
                else:
                    if base_min_price is not None and unit_price < base_min_price:
                        review["status"] = "Exception"
                        review["risk_flags"].append(f"销售单价 {format_cents(unit_price)} 低于最低限价 {format_cents(base_min_price)}")
                if pricing.max_price is not None and unit_price > pricing.max_price:
                    review["status"] = "Exception"
                    review["risk_flags"].append(f"销售单价 {format_cents(unit_price)} 高于最高限价 {format_cents(pricing.max_price)}")
                
                # Check promotions if applied
                promos_applied = item.get("promotion_applied", [])
                for promo_name in promos_applied:
                    if promo_name not in active_promo_names:
                        review["status"] = "Exception"
                        review["risk_flags"].append(f"促销不存在或未生效：{promo_name}")
            else:
                review["status"] = "Warning"
                review["risk_flags"].append(f"渠道 {channel} 未配置价格规则，无法完成价格审查")
        elif require_unit_price:
            review["status"] = "Warning"
            review["risk_flags"].append(f"SKU {sku_id} 未识别到销售单价，无法完成价格审查")
                
        if review["risk_flags"]:
            review["status"] = "Exception"

        item["review"] = review
        results.append(item)
        
    return results


def format_cents(value: int | None) -> str:
    if value is None:
        return "未配置"
    return f"{value / 100:.2f}元"

def extract_products_with_llm(session: Session, product_summary: str) -> list[dict]:
    """
    Use LLM to extract a list of materials from the material summary text.
    Expected output format is a JSON array of objects with keys:
    - sku_id (string)
    - unit_price (integer, representing cents)
    - promotion_applied (list of strings)
    """
    from backend.app.services.model_provider import call_model, extract_chat_content
    from backend.app.services.workflow import get_active_model
    
    if not product_summary or not product_summary.strip():
        return []

    system_prompt = (
        "You are a helpful assistant that extracts material information from order text. "
        "Extract the materials into a JSON array. Each object MUST have: "
        "'sku_id' (string, SKU code if found; otherwise empty string), "
        "'sku_code' (string, same as sku_id if found), "
        "'unit_price' (integer, the price in cents, if not found use null), "
        "'promotion_applied' (list of strings, any discount or promo codes mentioned). "
        "Return ONLY valid JSON. No markdown formatting or extra text."
    )
    user_prompt = f"Extract materials from the following text:\n\n{product_summary}"
    
    try:
        config = get_active_model(session)
        if config is None:
            return []
        response = call_model(
            session,
            config,
            task_type="ProductPriceReviewExtract",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = extract_chat_content(response)
        # Clean up markdown if model still returns it
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
            
        items = loads(content, [])
        if isinstance(items, list):
            return [normalize_extracted_product_item(item) for item in items if isinstance(item, dict)]
    except Exception as e:
        logger.warning("Failed to extract products with LLM: %s", e)
    
    return []
