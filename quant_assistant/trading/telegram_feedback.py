"""Telegram 成交文本解析与整批“先预览、再确认”的离线业务逻辑。"""
import datetime
import math
import re
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

from .service import ETF_CODE_PATTERN, TradingService

TRADE_PATTERN = re.compile(r"^\s*(买入|卖出|BUY|SELL)\s*([0-9]{6})\s+([1-9][0-9]*)\s*(?:股|份)?\s+@?\s*([0-9]+(?:\.[0-9]+)?)(?:\s*(?:手续费|费用|FEE)\s*[:：=]?\s*([0-9]+(?:\.[0-9]+)?))?\s*$", re.IGNORECASE)
TRADE_PREFIX = re.compile(r"^\s*(?:买入|卖出|BUY|SELL)", re.IGNORECASE)
HELP_TEXT = "每行一笔：买入 600104 300股 18.72 手续费5\n可在同一消息中输入多行；整批必须再发送“确认”才会原子记账，发送“取消”放弃。"


@dataclass(frozen=True)
class TradeCommand:
    side: str
    code: str
    quantity: int
    price: float
    fee: float = 0.0
    asset_type: Optional[str] = None
    instrument: Optional[dict] = None


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
    price, fee = float(price_text), float(fee_text or 0.0)
    if not math.isfinite(price) or not math.isfinite(fee):
        raise ValueError("价格和手续费必须是有限数字")
    side = "BUY" if side_text.upper() in ("BUY", "买入") else "SELL"
    return TradeCommand(side, code, int(quantity_text), price, fee)


def parse_trade_commands(text: str) -> Optional[List[TradeCommand]]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return None
    commands = []
    for index, line in enumerate(lines, 1):
        try:
            command = parse_trade_command(line)
        except ValueError as error:
            raise ValueError(f"第{index}笔：{error}") from error
        if command is None:
            return None
        commands.append(command)
    return commands


class TelegramTradeFeedback:
    def __init__(self, service: TradingService, instrument_resolver: Optional[Callable[[str], Optional[dict]]] = None):
        self.service = service
        self.instrument_resolver = instrument_resolver

    def handle(self, text: str, update_id: int, chat_id: str, pending: Optional[dict]) -> FeedbackResult:
        normalized = str(text or "").strip()
        if normalized == "确认":
            if pending is None:
                return FeedbackResult(None, "当前没有待确认交易。\n" + HELP_TEXT)
            commands = self._commands_from_pending(pending, chat_id)
            result = self.service.record_trades([asdict(item) for item in commands], f"telegram:{chat_id}:{pending['origin_update_id']}")
            return FeedbackResult(None, f"已确认整批记账：{len(commands)} 笔\n成交后现金 ￥{result['cash']:,.2f}", True)
        if normalized == "取消":
            if pending is None:
                return FeedbackResult(None, "当前没有待确认交易。")
            self._commands_from_pending(pending, chat_id)
            return FeedbackResult(None, "已取消整批待确认交易，账户未变更。")
        if pending is not None:
            self._commands_from_pending(pending, chat_id)
            return FeedbackResult(pending, "已有一笔或一批待确认交易。请先发送“确认”或“取消”，账户尚未变更。")
        try:
            parsed = parse_trade_commands(normalized)
            if parsed is None:
                return FeedbackResult(None, HELP_TEXT)
            commands = [self._resolve(item) for item in parsed]
            preview = self.service.preview_trades([asdict(item) for item in commands])
        except ValueError as error:
            return FeedbackResult(None, f"交易批次已拒绝：{error}\n整批账户未变更。")
        new_pending = {"commands": [asdict(item) for item in commands], "chat_id": str(chat_id), "origin_update_id": int(update_id), "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        lines = [f"请确认整批交易（{len(commands)} 笔）："]
        for index, item in enumerate(commands, 1):
            action = "买入" if item.side == "BUY" else "卖出"
            unit = "份" if item.asset_type == "ETF" else "股"
            name = (item.instrument or {}).get("name", "")
            lines.append(f"{index}. {action} {item.code} {name} {item.quantity}{unit} @ ￥{item.price:.4f}；费用 ￥{item.fee:.2f}")
        lines.extend([f"预计成交后现金 ￥{preview['cash']:,.2f}", "发送“确认”全部记账，或发送“取消”整批放弃。"])
        return FeedbackResult(new_pending, "\n".join(lines))

    def _resolve(self, item: TradeCommand) -> TradeCommand:
        if self.service.has_position(item.code):
            guessed = "ETF" if ETF_CODE_PATTERN.fullmatch(item.code) else "STOCK"
            return TradeCommand(item.side, item.code, item.quantity, item.price, item.fee, guessed)
        if item.side == "SELL":
            return TradeCommand(item.side, item.code, item.quantity, item.price, item.fee, "STOCK")
        if self.instrument_resolver is None:
            guessed = "ETF" if ETF_CODE_PATTERN.fullmatch(item.code) else "STOCK"
            return TradeCommand(item.side, item.code, item.quantity, item.price, item.fee, guessed)
        instrument = self.instrument_resolver(item.code)
        if not instrument:
            raise ValueError(f"{item.code} 无法从行情源识别或当前状态异常")
        return TradeCommand(item.side, item.code, item.quantity, item.price, item.fee, instrument["asset_type"], instrument)

    @staticmethod
    def _commands_from_pending(pending: dict, chat_id: str) -> List[TradeCommand]:
        try:
            if str(pending["chat_id"]) != str(chat_id):
                raise ValueError("待确认交易不属于当前会话")
            raw = pending.get("commands")
            if raw is None and "command" in pending:
                raw = [pending["command"]]
            commands = [TradeCommand(**item) for item in raw]
            if not commands:
                raise ValueError("待确认交易为空")
            return commands
        except (KeyError, TypeError) as error:
            raise ValueError("待确认交易状态无效") from error
