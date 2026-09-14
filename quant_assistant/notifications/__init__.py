"""外部通知适配器；当前仅支持 Telegram 单向推送。"""

from .telegram import NotificationResult, TelegramNotifier

__all__ = ["NotificationResult", "TelegramNotifier"]
