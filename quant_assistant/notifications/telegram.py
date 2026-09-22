"""Telegram 单向通知适配器（网络实现委托给 data.fetcher）。"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..data.fetcher import DataFetcher


TELEGRAM_SAFE_LIMIT = 3800


@dataclass(frozen=True)
class NotificationResult:
    success: bool
    sent_parts: int = 0
    error: Optional[str] = None


def daily_markdown_to_telegram(markdown: str) -> str:
    """保留日报核心区，去掉其他持仓表和 Markdown 展示标记。"""
    output = []
    skip_other_positions = False
    for original in markdown.splitlines():
        line = original.strip()
        if line.startswith("## 五、其他持仓"):
            skip_other_positions = True
            continue
        if line.startswith("## 六、今日摘要"):
            skip_other_positions = False
        if skip_other_positions or not line:
            continue
        if line.startswith("详细数据见"):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", line)
        line = line.replace("`", "")
        if line.startswith("- "):
            line = "• " + line[2:]
        output.append(line)
    return "\n".join(output).strip()


def split_telegram_message(text: str, limit: int = TELEGRAM_SAFE_LIMIT) -> list[str]:
    """优先按换行切分；超长单行再按字符安全切分。"""
    if limit < 100:
        raise ValueError("Telegram 分段上限不能小于100字符")
    pieces: list[str] = []
    current = ""
    for line in text.splitlines() or [text]:
        line_parts = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for part in line_parts:
            candidate = part if not current else f"{current}\n{part}"
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                current = part
    if current:
        pieces.append(current)
    if len(pieces) <= 1:
        return pieces
    return [f"（{index}/{len(pieces)}）\n{piece}" for index, piece in enumerate(pieces, 1)]


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None,
                 fetcher: Optional[DataFetcher] = None):
        self.bot_token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.fetcher = fetcher or DataFetcher()

    def send_daily_report(self, report_path: Path,
                          detail_path: Optional[Path] = None) -> NotificationResult:
        if not self.bot_token or not self.chat_id:
            return NotificationResult(False, error="缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
        if not re.fullmatch(r"-?\d{1,20}", self.chat_id):
            return NotificationResult(False, error="TELEGRAM_CHAT_ID 必须是单个数字会话ID")
        try:
            markdown = Path(report_path).read_text(encoding="utf-8")
            message = daily_markdown_to_telegram(markdown)
            if not message:
                return NotificationResult(False, error="日报内容为空")
            parts = split_telegram_message(message)
            sent = 0
            for part in parts:
                self.fetcher.send_telegram_message(self.bot_token, self.chat_id, part)
                sent += 1
            if detail_path is not None:
                self.fetcher.send_telegram_document(self.bot_token, self.chat_id, detail_path)
                sent += 1
            return NotificationResult(True, sent_parts=sent)
        except Exception as error:
            safe_error = str(error).replace(self.bot_token, "***").replace(self.chat_id, "***")
            return NotificationResult(False, sent_parts=sent if "sent" in locals() else 0,
                                      error=safe_error)
