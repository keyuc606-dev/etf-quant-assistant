"""Deterministic asset subtype classification and presentation policy.

This module is deliberately offline.  It only consumes persisted instrument
facts and the project's existing metadata; it never guesses unavailable macro
or premium data from price indicators.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .config import ETF_POOL
from .news.themes import ACCOUNT_THEME_MAP


ROUTING_VERSION = "asset-routing-v1"

STOCK = "STOCK"
EQUITY_ETF = "EQUITY_ETF"
BOND_ETF = "BOND_ETF"
GOLD_ETF = "GOLD_ETF"
COMMODITY_ETF = "COMMODITY_ETF"
QDII_ETF = "QDII_ETF"

ASSET_SUBTYPES = {
    STOCK, EQUITY_ETF, BOND_ETF, GOLD_ETF, COMMODITY_ETF, QDII_ETF,
}

_CODE_OVERRIDES = {
    "159649": BOND_ETF,
    "511010": BOND_ETF,
    "511360": BOND_ETF,
    "159934": GOLD_ETF,
    "518600": GOLD_ETF,
    "518880": GOLD_ETF,
    "159866": QDII_ETF,
    "513010": QDII_ETF,
    "513100": QDII_ETF,
    "513500": QDII_ETF,
}

_LABELS = {
    STOCK: "个股",
    EQUITY_ETF: "权益ETF",
    BOND_ETF: "防守债券ETF",
    GOLD_ETF: "黄金ETF",
    COMMODITY_ETF: "商品ETF",
    QDII_ETF: "QDII ETF",
}


def _metadata(code: str, supplied: Optional[Mapping] = None) -> dict:
    result = {}
    result.update(ETF_POOL.get(code, {}))
    result.update(ACCOUNT_THEME_MAP.get(code, {}))
    if supplied:
        result.update(supplied)
    return result


def classify_asset_subtype(code: str, name: str = "", market=None,
                           asset_type: str = "", metadata: Optional[Mapping] = None,
                           explicit: str = "") -> str:
    """Classify a persisted holding without any network access.

    Explicit valid values win, followed by stable code metadata, then narrow
    name/metadata rules.  Unknown ETFs intentionally fall back to equity ETFs,
    while non-ETFs fall back to stocks.
    """
    normalized = str(explicit or "").strip().upper() if isinstance(explicit, str) else ""
    if normalized in ASSET_SUBTYPES:
        return normalized

    code = str(code).strip()
    type_name = str(asset_type or "").strip().upper() if isinstance(asset_type, str) else ""
    market_value = getattr(market, "value", market)
    if code in _CODE_OVERRIDES:
        return _CODE_OVERRIDES[code]
    if type_name == STOCK or (type_name and type_name != "ETF"):
        return STOCK
    etf_code = len(code) == 6 and code[:2] in {"15", "16", "18", "50", "51", "52", "56", "58"}
    if type_name != "ETF" and str(market_value) != "ETF" and not etf_code:
        return STOCK

    meta = _metadata(code, metadata)
    text = " ".join(str(value) for value in (
        name, meta.get("name"), meta.get("asset_class"), meta.get("market"),
        meta.get("theme"), meta.get("benchmark"), meta.get("layer"), meta.get("role"),
    ) if value).lower()

    if any(token in text for token in ("黄金", "上海金", "金etf", "gold")):
        return GOLD_ETF
    if any(token in text for token in (
        "国债", "国开债", "政策性金融债", "政金债", "短融", "信用债", "债券", "bond",
    )):
        return BOND_ETF
    if any(token in text for token in (
        "日经", "恒生", "纳指", "纳斯达克", "标普", "海外权益", "境外", "qdii",
        "德国", "法国", "沙特", "越南", "印度", "美国", "日本股票", "中国香港",
    )) or code.startswith("513"):
        return QDII_ETF
    if any(token in text for token in (
        "商品期货", "原油", "豆粕", "能源化工", "有色期货", "commodity",
    )):
        return COMMODITY_ETF
    return EQUITY_ETF


def subtype_for(position) -> str:
    return classify_asset_subtype(
        position.code, position.name, position.market,
        getattr(position, "asset_type", ""),
        explicit=getattr(position, "asset_subtype", ""),
    )


def subtype_label(subtype: str) -> str:
    return _LABELS.get(subtype, subtype or "未分类资产")


def role_label(subtype: str) -> str:
    return {
        STOCK: "个股资产",
        EQUITY_ETF: "权益资产",
        BOND_ETF: "防守资产",
        GOLD_ETF: "黄金分散资产",
        COMMODITY_ETF: "商品分散资产",
        QDII_ETF: "境外权益资产",
    }.get(subtype, "未分类资产")
