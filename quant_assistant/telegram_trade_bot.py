"""GitHub Actions 单次轮询入口：处理至多一条已授权 Telegram 消息。"""

import os
import re
from pathlib import Path
from typing import Optional

from .cloud_snapshot import SNAPSHOT_SECRET_NAME, build_snapshot, encode_snapshot, restore_snapshot
from .cloud_state import CloudStateStore
from .config import DATA_DIR
from .data.fetcher import DataFetcher
from .trading.service import TradingService
from .trading.telegram_feedback import TelegramTradeFeedback


class TelegramTradeBot:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None,
                 store: Optional[CloudStateStore] = None,
                 fetcher: Optional[DataFetcher] = None,
                 database_path: Path = DATA_DIR / "trading.sqlite3",
                 portfolio_path: Path = DATA_DIR / "portfolio.json"):
        self.bot_token = bot_token if bot_token is not None else os.getenv(
            "TELEGRAM_BOT_TOKEN", ""
        )
        self.chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        if not self.bot_token:
            raise ValueError("缺少 TELEGRAM_BOT_TOKEN")
        if not re.fullmatch(r"-?\d{1,20}", self.chat_id):
            raise ValueError("TELEGRAM_CHAT_ID 必须是单个数字会话ID")
        self.fetcher = fetcher or DataFetcher()
        self.store = store or CloudStateStore(fetcher=self.fetcher)
        self.database_path = Path(database_path)
        self.portfolio_path = Path(portfolio_path)

    def run_once(self, initial_snapshot_b64: str = "") -> dict:
        state, sha = self.store.load(initial_snapshot_b64)
        telegram = state["telegram"]

        if telegram.get("outbox") is not None:
            outbox = telegram["outbox"]
            if str(outbox.get("chat_id")) != self.chat_id or not isinstance(
                outbox.get("text"), str
            ):
                raise ValueError("云端 Telegram outbox 状态无效")
            self.fetcher.send_telegram_message(
                self.bot_token, self.chat_id, outbox["text"]
            )
            telegram["outbox"] = None
            self.store.save(state, sha, "Deliver Telegram trade reply")
            return {"status": "outbox-delivered"}

        updates = self.fetcher.fetch_telegram_updates(
            self.bot_token, telegram["next_update_id"]
        )
        selected = None
        next_update_id = telegram["next_update_id"]
        for update in sorted(updates, key=lambda item: item.get("update_id", -1)):
            update_id = update.get("update_id")
            if isinstance(update_id, bool) or not isinstance(update_id, int):
                continue
            next_update_id = max(next_update_id, update_id + 1)
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
                continue
            if not isinstance(message.get("text"), str):
                continue
            selected = (update_id, message["text"])
            break

        if selected is None:
            if sha is None or next_update_id != telegram["next_update_id"]:
                telegram["next_update_id"] = next_update_id
                self.store.save(state, sha, "Initialize or advance Telegram cursor")
            return {"status": "idle"}

        restore_snapshot(
            state["account_snapshot_b64"], self.database_path, self.portfolio_path
        )
        service = TradingService(self.database_path, self.portfolio_path)
        feedback = TelegramTradeFeedback(
            service, getattr(self.fetcher, "identify_security", None)
        ).handle(
            selected[1], selected[0], self.chat_id, telegram.get("pending")
        )
        if feedback.account_changed:
            state["account_snapshot_b64"] = encode_snapshot(build_snapshot(service))
        telegram["pending"] = feedback.pending
        telegram["next_update_id"] = selected[0] + 1
        telegram["outbox"] = {"chat_id": self.chat_id, "text": feedback.response}
        sha = self.store.save(state, sha, "Process confirmed Telegram trade feedback")

        self.fetcher.send_telegram_message(self.bot_token, self.chat_id, feedback.response)
        telegram["outbox"] = None
        self.store.save(state, sha, "Mark Telegram trade reply delivered")
        return {
            "status": "account-updated" if feedback.account_changed else "message-processed"
        }


def main() -> None:
    bot = TelegramTradeBot()
    result = bot.run_once(os.getenv(SNAPSHOT_SECRET_NAME, ""))
    print(f"Telegram 交易反馈轮询完成: {result['status']}")


if __name__ == "__main__":
    main()
