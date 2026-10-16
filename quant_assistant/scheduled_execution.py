"""Serialize redundant schedule events using the existing cloud-state SHA CAS.

GitHub disaster workflows also retain account-cloud-state concurrency. A reservation
is never automatically retried: a crash after Telegram accepts a message has
an ambiguous outcome and blindly retrying could send duplicate advice.
"""
import argparse
import datetime as dt
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

from .cloud_state import CloudStateStore
from .data.fetcher import CN_TZ, DataFetcher
from .trading_calendar import TradingDayStatus
from .execution_window import TARGETS, execution_allowed


def run(session, now=None, store=None, fetcher=None, action=None):
    now = (now or dt.datetime.now(CN_TZ)).astimezone(CN_TZ)
    target = TARGETS[session]
    status = (fetcher or DataFetcher()).a_share_trading_day_status(now.date())
    print(f"Schedule gate: date={now.date()} session={session} trading_day={status.value}")
    if status is not TradingDayStatus.OPEN or not execution_allowed(now, session):
        print("No notification: market closed/unknown or session not due")
        return False
    store = store or CloudStateStore()
    key = f"{now.date()}:{session}"
    state, sha = store.load(os.getenv("ACCOUNT_SNAPSHOT_B64", ""))
    previous = state.get("execution_deliveries", {}).get(key)
    if previous:
        if previous["status"] not in ("sent", "skipped"):
            raise RuntimeError(f"Ambiguous delivery {key}; inspect original run {previous.get('run_id')} receipts before retry")
        print(f"Duplicate suppressed: {key} status={previous['status']}; inspect original run for receipts")
        return False
    record = {"status": "sending", "started_at": now.isoformat(),
              "reservation_id": uuid.uuid4().hex,
              "run_id": os.getenv("GITHUB_RUN_ID") or os.getenv("CLOUD_RUN_EXECUTION", "local"),
              "code_commit": os.getenv("GITHUB_SHA", "local"),
              "scheduled_for": dt.datetime.combine(now.date(), target, CN_TZ).isoformat()}
    state.setdefault("execution_deliveries", {})[key] = record
    store.save(state, sha, f"Reserve execution delivery {key}")
    verified, _ = store.load()
    if verified.get("execution_deliveries", {}).get(key) != record:
        raise RuntimeError("Execution reservation readback failed; nothing sent")
    if action is None:
        from .__main__ import cmd_notify_intraday, cmd_notify_morning_execution
        action = lambda: bool(cmd_notify_morning_execution(SimpleNamespace(days=120))
                               if session == "morning" else cmd_notify_intraday(SimpleNamespace()))
    delivered = action() is not False
    # Reload: the action may have appended advice/observations. Never overwrite it.
    state, sha = store.load()
    if state.get("execution_deliveries", {}).get(key) != record:
        raise RuntimeError("Execution reservation changed; inspect receipts before retry")
    state["execution_deliveries"][key] = {**record, "status": "sent" if delivered else "skipped",
                                          "completed_at": dt.datetime.now(CN_TZ).isoformat()}
    store.save(state, sha, f"Confirm execution delivery {key}")
    verified, _ = store.load()
    if verified.get("execution_deliveries", {}).get(key) != state["execution_deliveries"][key]:
        raise RuntimeError("Execution receipt readback failed; do not resend blindly")
    if not delivered:
        print(f"Execution delivery {key}: safely skipped; no Telegram delivery confirmed")
        return False
    summary = (f"Execution delivery {key}: sent; scheduled={record['scheduled_for']}; "
               f"actual={record['started_at']}; Telegram message IDs are in the sending step log.\n")
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    if os.getenv("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
            handle.write("sent=true\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("session", choices=TARGETS)
    run(parser.parse_args().session)
