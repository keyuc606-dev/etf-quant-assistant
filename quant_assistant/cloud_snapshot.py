"""Export and restore a read-only cloud account snapshot.

The encoded snapshot is intended for a GitHub Actions secret.  It contains
account data, but never credentials.  A cloud runner restores the flattened
latest confirmed account state as a fresh SQLite opening snapshot.
"""

import argparse
import base64
import binascii
import datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
from typing import Optional

from .config import DATA_DIR
from .trading.service import TradingService


SNAPSHOT_FORMAT = "etf-quant-account-snapshot"
SNAPSHOT_VERSION = 1
SNAPSHOT_SECRET_NAME = "ACCOUNT_SNAPSHOT_B64"
DEFAULT_EXPORT_PATH = DATA_DIR / "cloud-account-snapshot.b64"
MAX_ENCODED_BYTES = 45_000
VALID_MARKETS = {"深圳", "上海", "港股", "ETF"}
VALID_ASSET_TYPES = {"ETF", "STOCK"}


def _canonical_json(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _checksum(snapshot_without_checksum: dict) -> str:
    return hashlib.sha256(_canonical_json(snapshot_without_checksum)).hexdigest()


def _finite_number(value, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{field} 必须是不小于 {minimum:g} 的有限数字")
    return number


def _validate_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("账户快照顶层必须是 JSON 对象")
    if snapshot.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("账户快照格式标识无效")
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise ValueError("账户快照版本不受支持")

    expected_checksum = snapshot.get("sha256")
    if not isinstance(expected_checksum, str) or len(expected_checksum) != 64:
        raise ValueError("账户快照缺少有效校验值")
    unsigned = dict(snapshot)
    unsigned.pop("sha256", None)
    if not hmac.compare_digest(expected_checksum, _checksum(unsigned)):
        raise ValueError("账户快照校验失败")

    generated_at = snapshot.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("账户快照缺少生成时间")
    try:
        parsed_time = datetime.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("账户快照生成时间格式无效") from error
    if parsed_time.tzinfo is None:
        raise ValueError("账户快照生成时间必须包含时区")

    account = snapshot.get("account")
    if not isinstance(account, dict):
        raise ValueError("账户快照缺少 account 对象")
    _finite_number(account.get("cash"), "account.cash")
    cash_flows = account.get("cash_flows")
    if not isinstance(cash_flows, list) or not all(isinstance(item, dict) for item in cash_flows):
        raise ValueError("account.cash_flows 必须是对象列表")

    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        raise ValueError("账户快照 positions 必须是列表")
    seen_codes = set()
    for index, position in enumerate(positions):
        prefix = f"positions[{index}]"
        if not isinstance(position, dict):
            raise ValueError(f"{prefix} 必须是对象")
        code = position.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"{prefix}.code 无效")
        if code in seen_codes:
            raise ValueError(f"账户快照包含重复持仓代码: {code}")
        seen_codes.add(code)
        if not isinstance(position.get("name"), str) or not position["name"].strip():
            raise ValueError(f"{prefix}.name 无效")
        if position.get("market") not in VALID_MARKETS:
            raise ValueError(f"{prefix}.market 无效")
        if position.get("asset_type") not in VALID_ASSET_TYPES:
            raise ValueError(f"{prefix}.asset_type 无效")
        shares = position.get("shares")
        if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
            raise ValueError(f"{prefix}.shares 必须是正整数")
        _finite_number(position.get("cost_price"), f"{prefix}.cost_price")
        _finite_number(position.get("current_price"), f"{prefix}.current_price")
    return snapshot


def build_snapshot(service: Optional[TradingService] = None) -> dict:
    service = service or TradingService()
    if not service.is_initialized():
        raise ValueError("成交台账尚未初始化，无法导出最后确认账户快照")
    state = service.rebuild_portfolio()
    unsigned = {
        "format": SNAPSHOT_FORMAT,
        "version": SNAPSHOT_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "schema_version": service.repository.get_schema_version(),
        "account": {
            "cash": state["cash"],
            "cash_flows": state.get("cash_flows", []),
        },
        "positions": state["positions"],
    }
    snapshot = {**unsigned, "sha256": _checksum(unsigned)}
    return _validate_snapshot(snapshot)


def encode_snapshot(snapshot: dict) -> str:
    encoded = base64.b64encode(_canonical_json(_validate_snapshot(snapshot))).decode("ascii")
    if len(encoded.encode("ascii")) > MAX_ENCODED_BYTES:
        raise ValueError("账户快照过大，不能安全放入 GitHub Actions Secret")
    return encoded


def decode_snapshot(encoded: str) -> dict:
    if not isinstance(encoded, str) or not encoded.strip():
        raise ValueError(f"缺少 {SNAPSHOT_SECRET_NAME}")
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
        snapshot = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("账户快照不是有效的 Base64 JSON") from error
    return _validate_snapshot(snapshot)


def export_snapshot(output_path: Path = DEFAULT_EXPORT_PATH) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = encode_snapshot(build_snapshot())
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        temp_path.write_text(encoded, encoding="ascii")
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return output_path


def restore_snapshot(
    encoded: str,
    database_path: Path = DATA_DIR / "trading.sqlite3",
    portfolio_path: Path = DATA_DIR / "portfolio.json",
) -> dict:
    database_path = Path(database_path)
    portfolio_path = Path(portfolio_path)
    if database_path.exists() or portfolio_path.exists():
        raise FileExistsError("拒绝覆盖已有本地账户数据；云端恢复必须在干净工作区运行")

    snapshot = decode_snapshot(encoded)
    projection = {
        "cash": snapshot["account"]["cash"],
        "cash_flows": snapshot["account"]["cash_flows"],
        "positions": snapshot["positions"],
    }
    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = portfolio_path.with_suffix(".json.tmp")
    try:
        temp_path.write_text(
            json.dumps(projection, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temp_path, portfolio_path)
        service = TradingService(database_path=database_path, portfolio_path=portfolio_path)
        service.initialize_from_portfolio()
        if not service.reconcile()["ok"]:
            raise RuntimeError("账户快照恢复后校验不一致")
    except Exception:
        for path in (
            temp_path,
            portfolio_path,
            database_path,
            Path(str(database_path) + "-shm"),
            Path(str(database_path) + "-wal"),
        ):
            if path.exists():
                path.unlink()
        raise
    return {"positions": len(projection["positions"]), "generated_at": snapshot["generated_at"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="导出或恢复 GitHub Actions 账户快照")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="从本地成交台账导出加密存储用 Base64 快照")
    export_parser.add_argument("--output", type=Path, default=DEFAULT_EXPORT_PATH)
    subparsers.add_parser("restore", help=f"从环境变量 {SNAPSHOT_SECRET_NAME} 恢复快照")
    args = parser.parse_args()

    if args.command == "export":
        output = export_snapshot(args.output)
        print(f"账户快照已写入本地忽略文件: {output}")
        print("该文件包含真实账户数据，只能复制到 GitHub Actions Secret，禁止提交或分享。")
        return

    encoded = os.getenv(SNAPSHOT_SECRET_NAME, "")
    result = restore_snapshot(encoded)
    print(f"账户快照恢复并校验成功（{result['positions']} 项持仓；未输出账户明细）")


if __name__ == "__main__":
    main()
