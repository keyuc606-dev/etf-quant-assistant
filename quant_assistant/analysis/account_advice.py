"""真实持仓的可追溯规则建议，以及可选的 AI 文字压缩层。"""

import os
import re
from typing import Dict, Iterable, List, Optional

import pandas as pd

from ..asset_routing import (BOND_ETF, COMMODITY_ETF, GOLD_ETF, QDII_ETF,
                             subtype_for)


ACTION_LABELS = {"HOLD": "持有", "WATCH": "观望", "REDUCE": "减仓", "ADD_SMALL": "小幅加仓"}
ACTION_TOKENS = {"HOLD": "持有", "WATCH": "观望", "REDUCE": "减仓", "ADD_SMALL": "加仓"}


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _build_equity_advice(view: dict, frame: Optional[pd.DataFrame]) -> dict:
    """所有价格和仓位数字均在这里由确定性规则产生。"""
    pos = view["position"]
    if not view["available"] or frame is None or len(frame) < 20:
        return {
            "code": pos.code, "action": "WATCH", "action_label": "观望",
            "buy_range": None, "reduce_range": None, "stop": None, "target": None,
            "position_change": "0 个百分点", "confidence": "低",
            "reasons": ["行情、指标或样本不足，按规则降级为观望"], "ai_note": None,
        }
    last = frame.iloc[-1]
    close = _finite(last.get("收盘"))
    atr = _finite(last.get("ATR"))
    ma20 = _finite(last.get("MA20"))
    ma60 = _finite(last.get("MA60"))
    boll_dn = _finite(last.get("BOLL_DN"))
    boll_up = _finite(last.get("BOLL_UP"))
    rsi = _finite(last.get("RSI14"))
    macd = _finite(last.get("MACD"))
    k_value = _finite(last.get("K"))
    d_value = _finite(last.get("D"))
    volume_ratio = _finite(last.get("量比"))
    lows = pd.to_numeric(frame.tail(20).get("最低"), errors="coerce")
    highs = pd.to_numeric(frame.tail(20).get("最高"), errors="coerce")
    low20 = _finite(lows.min()) if lows is not None else None
    high20 = _finite(highs.max()) if highs is not None else None
    if close is None or close <= 0 or atr is None or atr <= 0:
        return _build_equity_advice({**view, "available": False}, None)

    supports = [v for v in (ma20, boll_dn, low20, float(pos.cost_price)) if v and v > 0 and v <= close * 1.15]
    resistances = [v for v in (ma60, boll_up, high20) if v and v > 0 and v >= close * 0.85]
    support = max([v for v in supports if v <= close] or [close - atr])
    resistance = min([v for v in resistances if v >= close] or [close + 2 * atr])
    buy = (max(0.01, support - 0.35 * atr), support + 0.20 * atr)
    reduce = (max(close, resistance - 0.20 * atr), resistance + 0.35 * atr)
    stop = max(0.01, min(low20 or close, support - 1.5 * atr))
    target = max(reduce[1], close + 2 * atr)

    weak = ((ma20 is not None and close < ma20)
            or (macd is not None and macd < 0)
            or (k_value is not None and d_value is not None and k_value < d_value))
    overheated = rsi is not None and rsi >= 70
    oversold = rsi is not None and rsi <= 35
    if view["weight"] > 0.25 or (weak and overheated):
        action, change = "REDUCE", "减少 3–5 个百分点"
    elif not weak and oversold and view["weight"] < 0.15:
        action, change = "ADD_SMALL", "增加 1–2 个百分点"
    elif weak:
        action, change = "WATCH", "0 个百分点"
    else:
        action, change = "HOLD", "0 个百分点"
    reasons = [
        f"收盘价相对 MA20 {'偏强' if ma20 is not None and close >= ma20 else '偏弱'}",
        f"20日支撑/压力与 ATR 波动共同限定价格区间",
        (f"量比显示成交量{'活跃' if volume_ratio is not None and volume_ratio >= 1.2 else '未明显放大'}；"
         f"KDJ {'偏强' if k_value is not None and d_value is not None and k_value >= d_value else '偏弱或不可用'}"),
    ]
    observation = view.get("theme_observation", {})
    if observation.get("status"):
        reasons.append(str(observation["status"]))
    confidence = "高" if len(frame) >= 60 and ma60 is not None else "中"
    return {
        "code": pos.code, "action": action, "action_label": ACTION_LABELS[action],
        "buy_range": buy, "reduce_range": reduce, "stop": stop, "target": target,
        "position_change": change, "confidence": confidence,
        "reasons": reasons, "ai_note": None,
    }


def _bond_advice(view: dict, frame: Optional[pd.DataFrame], subtype: str) -> dict:
    pos = view["position"]
    unavailable = not view.get("available") or frame is None or len(frame) < 20
    weight = float(view.get("weight") or 0)
    if unavailable:
        status = "防守资产数据不足"
        action = "WATCH"
        confidence = "低"
        reasons = ["债券ETF行情或样本不足，仅保留账户仓位复核",
                   "宏观利率、久期和折溢价信息未纳入，不作方向推断"]
    else:
        last = frame.iloc[-1]
        close, ma20, atr = (_finite(last.get(key)) for key in ("收盘", "MA20", "ATR"))
        if weight > .20:
            status, action = "防守仓位偏高需复核", "WATCH"
        elif weight < .08:
            status, action = "防守仓位偏低可关注", "WATCH"
        else:
            status, action = "防守仓位正常", "HOLD"
        trend = "不可用"
        if close is not None and ma20 is not None:
            trend = "偏强" if close >= ma20 else "偏弱"
        atr_note = (f"ATR波动约占价格 {atr / close:.2%}，仅作异常监测"
                    if close and atr else "ATR波动数据不足")
        liquidity = "成交额/深度数据不足，流动性需人工核验"
        amount = _finite(last.get("成交额"))
        if amount and amount > 0:
            liquidity = f"最近完整交易日成交额约 ￥{amount:,.0f}，仍需核验实时深度"
        reasons = [f"{status}；价格趋势{trend}仅作辅助", atr_note, liquidity,
                   "宏观利率、久期和折溢价信息未纳入，本条仅做账户防守仓位复核"]
        confidence = "中" if close and atr else "低"
    return {
        "code": pos.code, "asset_subtype": subtype, "action": action,
        "action_label": "持有" if action == "HOLD" else status,
        "buy_range": None, "reduce_range": None, "stop": None, "target": None,
        "position_change": "0 个百分点", "confidence": confidence,
        "reasons": reasons, "ai_note": None, "asset_status": status,
        "routing_note": "不按股票 RSI、BOLL、压力位、浅套/深套或做T纪律机械处理",
    }


def _macro_asset_advice(view: dict, frame: Optional[pd.DataFrame], subtype: str) -> dict:
    advice = _build_equity_advice(view, frame)
    advice["asset_subtype"] = subtype
    if not view.get("available") or frame is None or len(frame) < 20:
        advice["routing_note"] = "资产类别数据不足，已降级为观望"
        return advice
    last = frame.iloc[-1]
    close, ma20, atr = (_finite(last.get(key)) for key in ("收盘", "MA20", "ATR"))
    weight = float(view.get("weight") or 0)
    trend = close is not None and ma20 is not None and close >= ma20
    if weight > .20:
        advice.update(action="WATCH", action_label="组合暴露偏高需复核",
                      position_change="0 个百分点")
    elif trend:
        advice.update(action="HOLD", action_label="持有", position_change="0 个百分点")
    else:
        advice.update(action="WATCH", action_label="趋势偏弱，观察", position_change="0 个百分点")
    if subtype in (GOLD_ETF, COMMODITY_ETF):
        label = "黄金" if subtype == GOLD_ETF else "商品"
        advice["reasons"] = [
            f"{label}属性资产以趋势、ATR和组合暴露为主，RSI/BOLL不直接触发减仓",
            f"价格趋势{'偏强' if trend else '偏弱或不可用'}；"
            + (f"ATR约占价格 {atr / close:.2%}" if close and atr else "ATR数据不足"),
            "宏观/商品驱动数据未完整接入；需复核同主题资产的组合重叠",
        ]
        advice["routing_note"] = "弱化企业/行业事件；做T仅在可信日内字段和足够波动空间下人工评估"
    else:
        advice["reasons"] = [
            "QDII以趋势和ATR为辅助，A股盘中量能不直接解释境外资产价格",
            f"价格趋势{'偏强' if trend else '偏弱或不可用'}；"
            + (f"ATR约占价格 {atr / close:.2%}" if close and atr else "ATR数据不足"),
            "境外市场开闭市、汇率和实时折溢价数据未纳入，相关判断已降级",
        ]
        advice["confidence"] = "低"
        advice["routing_note"] = "需额外核验境外时差、汇率与折溢价；缺失时不作价差归因"
    return advice


def build_rule_advice(view: dict, frame: Optional[pd.DataFrame]) -> dict:
    """Route deterministic advice by the instrument's persisted subtype."""
    subtype = subtype_for(view["position"])
    if subtype == BOND_ETF:
        return _bond_advice(view, frame, subtype)
    if subtype in (GOLD_ETF, COMMODITY_ETF, QDII_ETF):
        return _macro_asset_advice(view, frame, subtype)
    advice = _build_equity_advice(view, frame)
    advice["asset_subtype"] = subtype
    advice["routing_note"] = ("个股可结合事件与主题" if subtype == "STOCK"
                              else "权益ETF以指数趋势和组合暴露为主，降低企业级事件权重")
    return advice


def build_rule_advices(views: Iterable[dict], stock_data: Dict[str, pd.DataFrame]) -> List[dict]:
    return [build_rule_advice(view, stock_data.get(view["position"].code)) for view in views]


class OpenAIAdviceProvider:
    """只允许模型压缩文字和排序；规则产生的字段永不交由模型覆盖。"""

    def __init__(self, fetcher, api_key: Optional[str] = None, model: Optional[str] = None):
        self.fetcher = fetcher
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-5-mini"

    def enhance(self, advices: List[dict]) -> tuple[List[dict], str]:
        if not self.api_key:
            return advices, "纯规则（未配置 OPENAI_API_KEY）"
        payload = {"items": [{
            "code": item["code"], "action": item["action_label"],
            "confidence": item["confidence"], "reasons": item["reasons"],
        } for item in advices]}
        try:
            response = self.fetcher.request_openai_json(self.api_key, self.model, payload)
            known = {item["code"]: item for item in advices}
            notes = response.get("notes", [])
            if not isinstance(notes, list):
                raise ValueError("notes 格式无效")
            for note_item in notes:
                if not isinstance(note_item, dict):
                    continue
                code, note = note_item.get("code"), note_item.get("note")
                if (code in known and isinstance(note, str) and 0 < len(note) <= 240
                        and re.search(r"[0-9０-９]", note) is None
                        and not any(token in note for action, token in ACTION_TOKENS.items()
                                    if action != known[code]["action"])):
                    known[code]["ai_note"] = note
            order = response.get("order", [])
            if not isinstance(order, list) or any(code not in known for code in order):
                order = []
            ordered = [known[code] for code in order]
            ordered.extend(item for item in advices if item["code"] not in set(order))
            return ordered, f"AI 文字压缩（{self.model}；数字仍来自规则层）"
        except Exception:
            return advices, "纯规则（AI 调用失败或返回异常，已自动降级）"
