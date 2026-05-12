import json
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from backend.app.models import ProductSPU, ProductSKU, ChannelPricing, PromotionRule, new_id

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

def review_order_products(session: Session, extracted_items: list[dict], channel: str = "default") -> list[dict]:
    """
    Review a list of extracted product items against the database.
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

    for item in extracted_items:
        review = {"status": "Pass", "risk_flags": []}
        
        sku_id = item.get("sku_id")
        unit_price = item.get("unit_price")
        
        if not sku_id:
            review["status"] = "Warning"
            review["risk_flags"].append("Missing SKU ID")
            item["review"] = review
            results.append(item)
            continue
            
        sku = session.scalar(select(ProductSKU).where(ProductSKU.sku_id == sku_id, ProductSKU.status == "Active"))
        if not sku:
            review["status"] = "Exception"
            review["risk_flags"].append(f"Unknown SKU: {sku_id}")
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
                    promo_min = min(x for x in [pricing.tier_b_price, pricing.tier_c_price, base_min_price] if x is not None)
                    if unit_price < promo_min:
                        review["status"] = "Exception"
                        review["risk_flags"].append(f"Price {unit_price} is below Promotional Min Price ({promo_min}) during active promo period")
                else:
                    if base_min_price is not None and unit_price < base_min_price:
                        review["status"] = "Exception"
                        review["risk_flags"].append(f"Price {unit_price} is below Base/MAP Min Price ({base_min_price}) outside promo period")
                
                # Check promotions if applied
                promos_applied = item.get("promotion_applied", [])
                for promo_name in promos_applied:
                    if promo_name not in active_promo_names:
                        review["status"] = "Exception"
                        review["risk_flags"].append(f"Invalid or expired promotion: {promo_name}")
            else:
                review["status"] = "Warning"
                review["risk_flags"].append(f"No pricing rule found for channel: {channel}")
                
        if review["risk_flags"]:
            review["status"] = "Exception"

        item["review"] = review
        results.append(item)
        
    return results

def extract_products_with_llm(session: Session, product_summary: str) -> list[dict]:
    """
    Use LLM to extract a list of products from the product_summary text.
    Expected output format is a JSON array of objects with keys:
    - sku_id (string)
    - unit_price (integer, representing cents)
    - promotion_applied (list of strings)
    """
    from backend.app.services.model_provider import call_model, extract_chat_content
    from backend.app.services.jsonutil import loads
    
    if not product_summary or not product_summary.strip():
        return []

    system_prompt = (
        "You are a helpful assistant that extracts product information from order text. "
        "Extract the products into a JSON array. Each object MUST have: "
        "'sku_code' (string, the product code or name if code is missing), "
        "'unit_price' (integer, the price in cents, if not found use null), "
        "'promotion_applied' (list of strings, any discount or promo codes mentioned). "
        "Return ONLY valid JSON. No markdown formatting or extra text."
    )
    user_prompt = f"Extract products from the following text:\n\n{product_summary}"
    
    try:
        response_text = call_model(session, system_prompt, user_prompt, temperature=0.1)
        content = extract_chat_content(response_text)
        # Clean up markdown if model still returns it
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
            
        items = loads(content, [])
        if isinstance(items, list):
            return items
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to extract products with LLM: {e}")
    
    return []

