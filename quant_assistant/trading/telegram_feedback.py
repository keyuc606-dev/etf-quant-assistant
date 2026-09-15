"""Telegram 成交文本解析与“先预览、再确认”的离线业务逻辑。"""

import datetime
import math
import re
from dataclasses import asdict, dataclass
from typing import Optional

from .service import TradingService


TRADE_PATTERN = re.compile(
    r"^\s*(买入|卖出|BUY|SELL)\s*"
    r"([0-9]{6})\s+([1-9][0-9]*)\s*(?:股|份)?\s+@?\s*"
    r"([0-9]+(?:\.[0-9]+)?)"
    r"(?:\s*(?:手续费|费用|FEE)\s*[:：=]?\s*([0-9]+(?:\.[0-9]+)?))?\s*$",
    re.IGNORECASE,
)
TRADE_PREFIX = re.compile(r"^\s*(?:买入|卖出|BUY|SELL)", re.IGNORECASE)
HELP_TEXT = (
    "格式：买入 600104 300股 18.72 手续费5\n"
    "也支持：SELL 002074 500 39.10\n"
    "提交后必须再发送“确认”才会记账；发送“取消”放弃。"
)


@dataclass(frozen=True)
class TradeCommand:
    side: str
    code: str
    quantity: int
    price: float
    fee: float = 0.0
    asset_type: str = "STOCK"


@dataclass(frozen=True)
class FeedbackResult:
    pending: Optional[dict]
    response: str
    account_changed: bool = False


def parse_trade_command(text: str) -> Optional[TradeCommand]:
    normalized = str(text or "").strip()
    match = TRADE_PATTERN.fullmatch(normalized)
    if match is None:
        if TRADE_PREFIX.match(normalized):
            raise ValueError(f"交易格式无法识别。\n{HELP_TEXT}")
        return None
    side_text, code, quantity_text, price_text, fee_text = match.groups()
    side = "BUY" if side_text.upper() in ("BUY", "买入") else "SELL"
    price = float(price_text)
    fee = float(fee_text or 0.0)
    if not math.isfinite(price) or not math.isfinite(fee):
        raise ValueError("价格和手续费必须是有限数字")
    return TradeCommand(side, code, int(quantity_text), price, fee)


def _summary(command: TradeCommand, preview: dict) -> str:
    action = "买入" if command.side == "BUY" else "卖出"
    amount = command.price * command.quantity
    lines = [
        "请确认这笔交易：",
        f"{action} {command.code} {command.quantity}股 @ ￥{command.price:.4f}",
        f"成交额 ￥{amount:,.2f}；手续费 ￥{command.fee:,.2f}",
        f"预计成交后现金 ￥{preview['cash']:,.2f}",
        f"预计成交后持仓 {preview['position_quantity']}股",
    ]
    if preview["realized_pnl"] is not None:
        lines.append(f"预计本笔已实现盈亏 ￥{preview['realized_pnl']:+,.2f}")
    lines.append("发送“确认”记账，或发送“取消”放弃。")
    return "\n".join(lines)


class TelegramTradeFeedback:
    def __init__(self, service: TradingService):
        self.service = service

    def handle(self, text: str, update_id: int, chat_id: str,
               pending: Optional[dict]) -> FeedbackResult:
        normalized = str(text or "").strip()
        if normalized == "确认":
            if pending is None:
                return FeedbackResult(None, "当前没有待确认交易。\n" + HELP_TEXT)
            command = self._command_from_pending(pending, chat_id)
            result = self.service.record_trade(
                command.side,
                command.code,
                command.quantity,
                command.price,
                fee=command.fee,
                asset_type="STOCK",
                external_id=f"telegram:{chat_id}:{pending['origin_update_id']}",
                source="telegram-confirmed",
                note="Telegram 二次确认成交",
            )
            action = "买入" if command.side == "BUY" else "卖出"
            duplicate = "（幂等重放，未重复记账）" if result.duplicate else ""
            response = (
                f"已确认记账{duplicate}：{action} {command.code} "
                f"{command.quantity}股 @ ￥{command.price:.4f}\n"
                f"成交后现金 ￥{result.cash:,.2f}；持仓 {result.quantity}股"
            )
            return FeedbackResult(None, response, account_changed=True)

        if normalized == "取消":
            if pending is None:
                return FeedbackResult(None, "当前没有待确认交易。")
            self._command_from_pending(pending, chat_id)
            return FeedbackResult(None, "已取消待确认交易，账户未变更。")

        if pending is not None:
            self._command_from_pending(pending, chat_id)
            return FeedbackResult(
                pending,
                "已有一笔待确认交易。请先发送“确认”或“取消”，账户尚未变更。",
            )

        try:
            command = parse_trade_command(normalized)
        except ValueError as error:
            return FeedbackResult(None, str(error))
        if command is None:
            return FeedbackResult(None, HELP_TEXT)
        try:
            preview = self.service.preview_trade(
                command.side, command.code, command.quantity, command.price,
                command.fee, asset_type="STOCK",
            )
        except ValueError as error:
            return FeedbackResult(None, f"交易已拒绝：{error}\n账户未变更。")
        new_pending = {
            "command": asdict(command),
            "chat_id": str(chat_id),
            "origin_update_id": int(update_id),
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        return FeedbackResult(new_pending, _summary(command, preview))

    @staticmethod
    def _command_from_pending(pending: dict, chat_id: str) -> TradeCommand:
        try:
            if str(pending["chat_id"]) != str(chat_id):
                raise ValueError("待确认交易不属于当前会话")
            command = TradeCommand(**pending["command"])
            if command.asset_type != "STOCK":
                raise ValueError("待确认交易资产类型无效")
            return command
        except (KeyError, TypeError) as error:
            raise ValueError("待确认交易状态无效") from error
