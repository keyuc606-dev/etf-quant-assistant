"""私有 GitHub 仓库中的云端账户状态与 Telegram 确认游标。"""

import argparse
import datetime
import json
import os
import re
from pathlib import Path
from typing import Optional

from .cloud_snapshot import SNAPSHOT_SECRET_NAME, decode_snapshot, restore_snapshot
from .config import DATA_DIR
from .data.fetcher import DataFetcher


STATE_FORMAT = "etf-quant-cloud-state"
STATE_VERSION = 1
DEFAULT_STATE_PATH = "state/account-state.json"
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_cloud_state(account_snapshot_b64: str) -> dict:
    decode_snapshot(account_snapshot_b64)
    return {
        "format": STATE_FORMAT,
        "version": STATE_VERSION,
        "updated_at": _now_iso(),
        "account_snapshot_b64": account_snapshot_b64.strip(),
        "telegram": {
            "next_update_id": 0,
            "pending": None,
            "outbox": None,
        },
    }


def validate_cloud_state(state: dict) -> dict:
    if not isinstance(state, dict) or state.get("format") != STATE_FORMAT:
        raise ValueError("云端状态格式标识无效")
    if state.get("version") != STATE_VERSION:
        raise ValueError("云端状态版本不受支持")
    if not isinstance(state.get("updated_at"), str):
        raise ValueError("云端状态缺少更新时间")
    decode_snapshot(state.get("account_snapshot_b64", ""))
    telegram = state.get("telegram")
    if not isinstance(telegram, dict):
        raise ValueError("云端状态缺少 telegram 对象")
    next_update_id = telegram.get("next_update_id")
    if isinstance(next_update_id, bool) or not isinstance(next_update_id, int) or next_update_id < 0:
        raise ValueError("telegram.next_update_id 无效")
    for field in ("pending", "outbox"):
        if telegram.get(field) is not None and not isinstance(telegram[field], dict):
            raise ValueError(f"telegram.{field} 无效")
    if not isinstance(state.get("advice_records", []), list):
        raise ValueError("advice_records 必须是列表")
    return state


class CloudStateStore:
    """用 GitHub Contents API 读写专用私有仓库中的单个状态文件。"""

    def __init__(self, token: Optional[str] = None, repository: Optional[str] = None,
                 path: Optional[str] = None, fetcher: Optional[DataFetcher] = None):
        self.token = token if token is not None else os.getenv("ACCOUNT_STATE_TOKEN", "")
        self.repository = (
            repository if repository is not None else os.getenv("ACCOUNT_STATE_REPO", "")
        )
        self.path = path if path is not None else os.getenv(
            "ACCOUNT_STATE_PATH", DEFAULT_STATE_PATH
        )
        self.fetcher = fetcher or DataFetcher()
        if not self.token:
            raise ValueError("缺少 ACCOUNT_STATE_TOKEN")
        if not REPOSITORY_PATTERN.fullmatch(self.repository):
            raise ValueError("ACCOUNT_STATE_REPO 必须是 owner/repository")
        if not self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("ACCOUNT_STATE_PATH 必须是仓库内相对路径")

    def load(self, initial_snapshot_b64: str = "") -> tuple[dict, Optional[str]]:
        item = self.fetcher.get_github_repository_file(
            self.token, self.repository, self.path
        )
        if item is None:
            if not initial_snapshot_b64:
                raise ValueError(
                    "私有状态仓库尚未初始化，且缺少 ACCOUNT_SNAPSHOT_B64 迁移快照"
                )
            return new_cloud_state(initial_snapshot_b64), None
        try:
            state = json.loads(item["content"].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("私有状态仓库中的状态文件不是有效 JSON") from error
        return validate_cloud_state(state), item["sha"]

    def save(self, state: dict, sha: Optional[str], message: str) -> str:
        state = validate_cloud_state(state)
        state["updated_at"] = _now_iso()
        content = json.dumps(
            state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ).encode("utf-8")
        return self.fetcher.put_github_repository_file(
            self.token, self.repository, self.path, content, message, sha
        )


def restore_latest_cloud_state(
    database_path: Path = DATA_DIR / "trading.sqlite3",
    portfolio_path: Path = DATA_DIR / "portfolio.json",
    store: Optional[CloudStateStore] = None,
) -> dict:
    initial = os.getenv(SNAPSHOT_SECRET_NAME, "")
    if store is None and not (
        os.getenv("ACCOUNT_STATE_TOKEN", "") and os.getenv("ACCOUNT_STATE_REPO", "")
    ):
        # 兼容已经上线的 09:20 日报；状态仓库配置完成后自动切换到最新状态。
        return restore_snapshot(initial, database_path, portfolio_path)
    state, _sha = (store or CloudStateStore()).load(initial)
    return restore_snapshot(
        state["account_snapshot_b64"], database_path, portfolio_path
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="从私有状态仓库恢复最后确认的账户")
    parser.add_argument("command", choices=["restore"])
    args = parser.parse_args()
    if args.command == "restore":
        result = restore_latest_cloud_state()
        print(f"云端账户状态恢复成功（{result['positions']} 项持仓；未输出账户明细）")


if __name__ == "__main__":
    main()
