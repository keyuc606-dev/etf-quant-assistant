"""基于标题关键词的确定性主题观察，不使用 LLM。"""

import datetime
from typing import Dict, Iterable

from .themes import ACCOUNT_THEME_MAP


MAJOR_TERMS = ("政策", "监管", "央行", "利率", "关税", "制裁", "并购", "事故", "停产", "创新高", "暴跌")
POSITIVE_TERMS = ("增长", "回升", "突破", "支持", "获批", "扩张", "增持", "创新高")
NEGATIVE_TERMS = ("下滑", "下降", "风险", "调查", "制裁", "停产", "暴跌", "亏损")
UNCERTAINTY_TERMS = ("不确定", "波动", "争议", "监管", "关税", "利率")


def build_theme_observations(etf_codes: Iterable[str], news_items: list,
                             as_of: datetime.datetime) -> Dict[str, dict]:
    result = {}
    for code in etf_codes:
        meta = ACCOUNT_THEME_MAP.get(code, {})
        items = [item for item in news_items if code in item.get(
            "matched_instruments", item.get("matched_etfs", [])
        )]
        items.sort(key=lambda item: item.get("published_at", ""), reverse=True)
        major = [item for item in items if any(term in item.get("title", "") for term in MAJOR_TERMS)]
        positive = [item["title"] for item in items if any(term in item.get("title", "") for term in POSITIVE_TERMS)]
        negative = [item["title"] for item in items if any(term in item.get("title", "") for term in NEGATIVE_TERMS)]
        uncertainties = [item["title"] for item in items if any(term in item.get("title", "") for term in UNCERTAINTY_TERMS)]
        if not items:
            status = "近期公开信息不足，暂不形成行业判断。"
        elif positive and negative:
            status = "近期公开信息同时包含支持性与风险线索，方向并不一致。"
        elif positive:
            status = "近期存在支持性信息，但仅凭标题证据不足以形成方向判断。"
        elif negative:
            status = "近期存在风险提示信息，需要结合更多资料复核。"
        else:
            status = "近期信息以事件更新为主，规则未形成方向性判断。"
        result[code] = {
            "code": code,
            "asset_class": meta.get("asset_class", "未分类"),
            "market": meta.get("market", "未知"),
            "theme": meta.get("theme", "未分类"),
            "benchmark": meta.get("benchmark"),
            "recent_news_count": len(items),
            "major_events": major[:3],
            "positive_factors": positive[:3],
            "negative_factors": negative[:3],
            "uncertainties": uncertainties[:3],
            "evidence": items[:5],
            "status": status,
            "event_strength": min(8, len(major) * 2 + min(len(items), 3)),
            "as_of": as_of.astimezone(datetime.timezone.utc).isoformat(),
        }
    return result
