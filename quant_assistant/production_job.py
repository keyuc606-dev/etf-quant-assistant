"""Short-lived production entry point; preflight never prints account contents."""
import argparse
import datetime as dt
import os

from .cloud_state import CloudStateStore, restore_latest_cloud_state
from .data.fetcher import DataFetcher, CN_TZ
from .execution_window import execution_allowed
from .scheduled_execution import run
from .trading_calendar import TradingDayStatus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session", choices=("morning", "closing"))
    parser.add_argument("--check", action="store_true", help="Read-only credential/state preflight; never sends")
    args = parser.parse_args()
    for key in ("ACCOUNT_STATE_TOKEN", "ACCOUNT_STATE_REPO", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.getenv(key):
            raise RuntimeError(f"Missing required configuration: {key}")
    now = dt.datetime.now(CN_TZ)
    fetcher = DataFetcher()
    status = fetcher.a_share_trading_day_status(now.date())
    print(f"Production preflight: {now.isoformat()} trading_day={status.value}")
    if args.check:
        CloudStateStore(fetcher=fetcher).load()
        print("PASS private state parsed (including large Git blob); no writes, no Telegram, no trades")
        return
    if status is not TradingDayStatus.OPEN or not execution_allowed(now, args.session):
        print("Safe skip before restoring account or reserving delivery")
        return
    restore_latest_cloud_state()
    run(args.session, fetcher=fetcher)


if __name__ == "__main__":
    main()
