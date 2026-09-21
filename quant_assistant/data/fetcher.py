import os
import time
import datetime
import base64
import binascii
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

import pandas as pd
import akshare as ak

from ..config import CACHE_DIR
from ..models import Market


# A股收盘以北京时间为准；部署在 UTC 容器/CI 上时本地时区会错位 8 小时
CN_TZ = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")
CALENDAR_PATH = CACHE_DIR / "trade_calendar.csv"

_CALENDAR_CACHE = None        # set[str "YYYY-MM-DD"]，模块级只读一次
_CALENDAR_LOADED = False


def cached_trade_dates() -> Optional[set]:
    """读取本地交易日历缓存（只读文件，不联网）。无缓存/损坏返回 None。"""
    global _CALENDAR_CACHE, _CALENDAR_LOADED
    if not _CALENDAR_LOADED:
        _CALENDAR_LOADED = True
        try:
            df = pd.read_csv(CALENDAR_PATH)
            col = "trade_date" if "trade_date" in df.columns else df.columns[0]
            _CALENDAR_CACHE = set(pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d"))
        except Exception:
            _CALENDAR_CACHE = None
    return _CALENDAR_CACHE


def _atomic_write_csv(path: Path, df: pd.DataFrame) -> None:
    """先写临时文件再原子替换，避免写一半被 kill 留下截断的 CSV。"""
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def _safe_float(value, default: float = 0.0) -> float:
    """东财快照表对停牌/无行情标的常填 '-'，直接 float() 会抛 ValueError。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """把日线行情聚合为周线，周线日期标为周五。"""
    if df is None or df.empty:
        return pd.DataFrame()
    if "日期" not in df.columns:
        raise ValueError("日线数据缺少 日期 列")

    daily = df.copy()
    daily["日期"] = pd.to_datetime(daily["日期"])
    daily = daily.sort_values("日期").set_index("日期")

    agg_map = {}
    if "开盘" in daily.columns:
        agg_map["开盘"] = "first"
    if "收盘" in daily.columns:
        agg_map["收盘"] = "last"
    if "最高" in daily.columns:
        agg_map["最高"] = "max"
    if "最低" in daily.columns:
        agg_map["最低"] = "min"
    if "成交量" in daily.columns:
        agg_map["成交量"] = "sum"
    if "成交额" in daily.columns:
        agg_map["成交额"] = "sum"
    if not agg_map:
        raise ValueError("日线数据缺少可聚合的 OHLCV 列")

    weekly = daily.resample("W-FRI").agg(agg_map).dropna(subset=["开盘", "收盘"])
    weekly = weekly.reset_index()
    return weekly


@contextmanager
def _no_proxy():
    """临时让 requests 绕过代理直连国内金融数据源，退出时自动恢复。
    不影响 Clash/Codex/Claude 等其他任何网络请求。"""
    import os
    # 保存原有代理配置
    saved = {}
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        saved[key] = os.environ.pop(key, None)
    # 设 NO_PROXY 排除 eastmoney 域名
    os.environ["no_proxy"] = "eastmoney.com,*.eastmoney.com,localhost,127.*,10.*,172.16.*,192.168.*"
    try:
        yield
    finally:
        # 恢复原有配置
        for key, val in saved.items():
            if val is not None:
                os.environ[key] = val
            elif key in os.environ:
                del os.environ[key]


class DataFetcher:
    """行情获取器。

    缓存策略：每个标的一份长期缓存 {code}_{period}.csv，历史下限只增不减。
    注意 qfq 前复权价格在每次除权后会整体漂移，因此刷新时不做新旧行拼接，
    而是把拉取起点定为「缓存最早日期」与「请求起点」的较小者，成功后整体替换，
    保证整个序列复权基准一致。akshare 拉取失败时降级使用旧缓存（离线模式）。
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = 2
        self._spot_cache: dict = {}  # 全市场实时快照表，按市场缓存，进程内复用
        self.degraded_sources: list[str] = []  # 主源失败后由备用历史行情成功接管的代码

    def _fetch_with_retry(self, fetch_fn, code: str, retries: Optional[int] = None):
        """带重试的数据获取，每次自动绕过代理直连数据源"""
        retry_count = self.max_retries if retries is None else max(0, retries)
        last_error = None
        for attempt in range(retry_count + 1):
            try:
                with _no_proxy():
                    return fetch_fn()
            except Exception as e:
                last_error = e
                if attempt < retry_count:
                    wait = (attempt + 1) * 3
                    time.sleep(wait)
        print(f"  获取 {code} 数据失败 (已重试{retry_count}次): {last_error}")
        return None

    # ---------- 历史行情缓存 ----------

    def _cache_path(self, code: str, period: str) -> Path:
        return self.cache_dir / f"{code}_{period}.csv"

    def _migrate_legacy_cache(self, code: str, period: str):
        """把旧的带日期后缀缓存（{code}_{period}_120d_2026-05-05.csv 等）合并进新文件并清理"""
        legacy = sorted(self.cache_dir.glob(f"{code}_{period}_*.csv"))
        if not legacy:
            return
        frames = []
        new_path = self._cache_path(code, period)
        if new_path.exists():
            try:
                frames.append(pd.read_csv(new_path, parse_dates=["日期"]))
            except Exception:
                pass
        for p in legacy:
            try:
                frames.append(pd.read_csv(p, parse_dates=["日期"]))
            except Exception:
                pass
        if frames:
            merged = (pd.concat(frames)
                      .drop_duplicates(subset="日期", keep="last")
                      .sort_values("日期"))
            _atomic_write_csv(new_path, merged)
        for p in legacy:
            p.unlink()

    def _load_cache(self, code: str, period: str) -> Optional[pd.DataFrame]:
        self._migrate_legacy_cache(code, period)
        path = self._cache_path(code, period)
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, parse_dates=["日期"])
            return df if not df.empty else None
        except Exception as e:
            print(f"  缓存文件 {path.name} 读取失败({e})，忽略缓存")
            return None

    @staticmethod
    def _last_completed_trading_day() -> datetime.date:
        """最近一个已收盘的交易日：北京时间 16:00 为界，优先用交易日历识别节假日。"""
        now = datetime.datetime.now(CN_TZ)
        d = now.date()
        if now.hour < 16:
            d -= datetime.timedelta(days=1)
        trade_dates = cached_trade_dates()
        if trade_dates is not None:
            floor = d - datetime.timedelta(days=40)
            while d.isoformat() not in trade_dates and d > floor:
                d -= datetime.timedelta(days=1)
            return d
        while d.weekday() >= 5:
            d -= datetime.timedelta(days=1)
        return d

    def _maybe_refresh_trade_calendar(self) -> None:
        """交易日历缺失或覆盖不足半年时联网刷新（只在本方法内发生）。"""
        global _CALENDAR_CACHE, _CALENDAR_LOADED
        trade_dates = cached_trade_dates()
        if trade_dates:
            try:
                latest = datetime.datetime.strptime(max(trade_dates), "%Y-%m-%d").date()
            except ValueError:
                latest = None
            if latest is not None and latest >= datetime.date.today() + datetime.timedelta(days=90):
                return

        def _do_fetch():
            return ak.tool_trade_date_hist_sina()

        df = self._fetch_with_retry(_do_fetch, "交易日历")
        if df is None or df.empty or "trade_date" not in df.columns:
            return
        try:
            dates = pd.to_datetime(df["trade_date"])
            frame = pd.DataFrame({"trade_date": dates.dt.strftime("%Y-%m-%d")})
            _atomic_write_csv(CALENDAR_PATH, frame)
            _CALENDAR_CACHE = set(frame["trade_date"])
            _CALENDAR_LOADED = True
        except Exception as e:
            print(f"  交易日历缓存写入失败({e})，继续用工作日近似")

    def fetch_hist(self, code: str, market: Market, period: str = "daily",
                   days: int = 120) -> Optional[pd.DataFrame]:
        self._maybe_refresh_trade_calendar()
        today = datetime.date.today()
        want_start = today - datetime.timedelta(days=days)
        cached = self._load_cache(code, period)

        if cached is not None:
            cache_last = cached["日期"].max().date()
            cache_first = cached["日期"].min().date()
            # 缓存已包含最近一个已收盘交易日、且覆盖请求起点（放宽一周容差）时直接用缓存
            if (cache_last >= self._last_completed_trading_day()
                    and cache_first <= want_start + datetime.timedelta(days=7)):
                return self._slice(cached, want_start)

        # 拉取起点取缓存最早日期与请求起点的较小者，保证复权基准整段一致
        fetch_start = want_start
        if cached is not None:
            fetch_start = min(fetch_start, cached["日期"].min().date())
        start_date = fetch_start.strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")

        def _do_fetch():
            if market == Market.HK:
                return ak.stock_hk_hist(
                    symbol=code, period=period,
                    start_date=start_date, end_date=end_date,
                    adjust="qfq"
                )
            elif market == Market.ETF:
                return ak.fund_etf_hist_em(
                    symbol=code, period=period,
                    start_date=start_date, end_date=end_date,
                    adjust="qfq"
                )
            else:
                return ak.stock_zh_a_hist(
                    symbol=code, period=period,
                    start_date=start_date, end_date=end_date,
                    adjust="qfq"
                )

        # A股和ETF都有免费新浪备用源；主源失败时立即切换，避免24标的账户被逐项重试拖慢。
        primary_retries = 0 if market in (Market.A_SH, Market.A_SZ, Market.ETF) else None
        df = self._fetch_with_retry(_do_fetch, code, retries=primary_retries)
        used_sina = False

        # 东财接口偶发主动断开连接。ETF 使用 AkShare 自带的新浪公开历史行情
        # 作为免费降级源；联网仍严格封装在本模块内，不改变缓存与陈旧数据门禁。
        if (df is None or df.empty) and market == Market.ETF:
            exchange = "sh" if code.startswith(("5", "6")) else "sz"

            def _do_fetch_sina():
                return ak.fund_etf_hist_sina(symbol=f"{exchange}{code}")

            sina = self._fetch_with_retry(_do_fetch_sina, f"{code}(新浪备用源)")
            if sina is not None and not sina.empty:
                rename = {
                    "date": "日期", "open": "开盘", "close": "收盘",
                    "high": "最高", "low": "最低", "volume": "成交量",
                    "amount": "成交额",
                }
                df = sina.rename(columns=rename)
                if "日期" in df.columns and "收盘" in df.columns:
                    df["日期"] = pd.to_datetime(df["日期"])
                    df = df[(df["日期"] >= pd.Timestamp(fetch_start))
                            & (df["日期"] <= pd.Timestamp(today))].copy()
                    if "涨跌幅" not in df.columns:
                        df["涨跌幅"] = pd.to_numeric(
                            df["收盘"], errors="coerce"
                        ).pct_change() * 100
                    used_sina = True

        # A股同样提供新浪免费历史行情降级，避免东财单点故障导致股票账户全量不可用。
        if (df is None or df.empty) and market in (Market.A_SH, Market.A_SZ):
            exchange = "sh" if market == Market.A_SH else "sz"

            def _do_fetch_stock_sina():
                return ak.stock_zh_a_daily(
                    symbol=f"{exchange}{code}", start_date=start_date,
                    end_date=end_date, adjust="qfq",
                )

            sina = self._fetch_with_retry(_do_fetch_stock_sina, f"{code}(新浪备用源)")
            if sina is not None and not sina.empty:
                rename = {
                    "date": "日期", "open": "开盘", "close": "收盘",
                    "high": "最高", "low": "最低", "volume": "成交量",
                    "amount": "成交额", "turnover": "换手率",
                }
                df = sina.rename(columns=rename)
                if "日期" in df.columns and "收盘" in df.columns:
                    df["日期"] = pd.to_datetime(df["日期"])
                    df = df[(df["日期"] >= pd.Timestamp(fetch_start))
                            & (df["日期"] <= pd.Timestamp(today))].copy()
                    if "涨跌幅" not in df.columns:
                        df["涨跌幅"] = pd.to_numeric(
                            df["收盘"], errors="coerce"
                        ).pct_change() * 100
                    used_sina = True

        if df is None or df.empty:
            if cached is not None:
                print(f"  {code}: 联网失败，使用本地缓存，数据截止 "
                      f"{cached['日期'].max().date()}（离线模式）")
                return self._slice(cached, want_start)
            return None

        # 接口改版/返回异常列时解析会抛错，此时与联网失败同等降级到缓存，
        # 而不是把异常抛给上层导致整条管道中断
        try:
            if "日期" not in df.columns:
                raise ValueError("接口返回缺少 日期 列")
            df["日期"] = pd.to_datetime(df["日期"])
            df = df.sort_values("日期").reset_index(drop=True)
        except Exception as e:
            if cached is not None:
                print(f"  {code}: 行情解析失败({e})，使用本地缓存，数据截止 "
                      f"{cached['日期'].max().date()}（离线模式）")
                return self._slice(cached, want_start)
            print(f"  {code}: 行情解析失败({e})，且无本地缓存可用")
            return None

        _atomic_write_csv(self._cache_path(code, period), df)
        sliced = self._slice(df, want_start)
        if used_sina and sliced is not None and not sliced.empty:
            self.degraded_sources.append(code)
            print(f"  [警告/降级] {code}: 东财主源不可用，已切换到新浪公开历史行情备用源")
        time.sleep(1)
        return sliced

    def fetch_nav(self, code: str, start_date, offline: bool = False) -> Optional[pd.DataFrame]:
        """拉取 ETF 累计净值，缓存为 {code}_nav.csv。

        数据源为天天基金历史净值明细，返回列统一为 日期/累计净值/单位净值
        （旧缓存可能缺单位净值）。联网只在本方法内发生；失败时降级为旧缓存。
        """
        if isinstance(start_date, str):
            want_start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
        else:
            want_start = start_date
        cached = self._load_cache(code, "nav")
        today = datetime.date.today()

        if cached is not None:
            cache_last = cached["日期"].max().date()
            cache_first = cached["日期"].min().date()
            has_unit_nav = "单位净值" in cached.columns
            if offline or (
                has_unit_nav and cache_last >= self._last_completed_trading_day()
                and cache_first <= want_start
            ):
                return self._slice(cached, want_start)
        if offline:
            return None

        fetch_start = want_start
        if cached is not None:
            fetch_start = min(fetch_start, cached["日期"].min().date())
        start_str = fetch_start.strftime("%Y%m%d")
        end_str = today.strftime("%Y%m%d")

        def _do_fetch():
            return ak.fund_etf_fund_info_em(
                fund=code,
                start_date=start_str,
                end_date=end_str,
            )

        df = self._fetch_with_retry(_do_fetch, f"{code}累计净值")
        if df is None or df.empty:
            if cached is not None:
                print(f"  {code}: 净值联网失败，使用本地缓存，数据截止 "
                      f"{cached['日期'].max().date()}（离线模式）")
                return self._slice(cached, want_start)
            return None

        df = self._normalize_nav(df)
        if df is None or df.empty:
            if cached is not None:
                print(f"  {code}: 净值列解析失败，使用本地缓存，数据截止 "
                      f"{cached['日期'].max().date()}（离线模式）")
                return self._slice(cached, want_start)
            return None

        df.to_csv(self._cache_path(code, "nav"), index=False)
        time.sleep(1)
        return self._slice(df, want_start)

    @staticmethod
    def _slice(df: pd.DataFrame, want_start: datetime.date) -> pd.DataFrame:
        return df[df["日期"] >= pd.Timestamp(want_start)].reset_index(drop=True)

    @staticmethod
    def _normalize_nav(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        date_col = None
        nav_col = None
        unit_nav_col = None
        for col in df.columns:
            if str(col) in ("日期", "净值日期"):
                date_col = col
            if str(col) == "累计净值":
                nav_col = col
            if str(col) == "单位净值":
                unit_nav_col = col
        if date_col is None or nav_col is None:
            return None
        cols = [date_col, nav_col]
        names = ["日期", "累计净值"]
        if unit_nav_col is not None:
            cols.append(unit_nav_col)
            names.append("单位净值")
        nav = df[cols].copy()
        nav.columns = names
        nav["日期"] = pd.to_datetime(nav["日期"])
        nav["累计净值"] = pd.to_numeric(nav["累计净值"], errors="coerce")
        if "单位净值" in nav.columns:
            nav["单位净值"] = pd.to_numeric(nav["单位净值"], errors="coerce")
        nav = (nav.dropna(subset=["日期", "累计净值"])
               .drop_duplicates(subset="日期", keep="last")
               .sort_values("日期")
               .reset_index(drop=True))
        return nav

    # ---------- 实时行情 ----------

    def _spot_table(self, market_key: str) -> Optional[pd.DataFrame]:
        """全市场实时快照表（约 5000 行），进程内只拉一次，按 code 多次查询"""
        if market_key not in self._spot_cache:
            fetch_fn = {
                "A": ak.stock_zh_a_spot_em,
                "ETF": ak.fund_etf_spot_em,
                "HK": ak.stock_hk_spot_em,
            }[market_key]
            self._spot_cache[market_key] = self._fetch_with_retry(fetch_fn, f"{market_key}股实时快照")
        return self._spot_cache[market_key]

    def _lookup_spot(self, market_key: str, code: str) -> Optional[dict]:
        df = self._spot_table(market_key)
        if df is None:
            return None
        row = df[df["代码"] == code]
        if row.empty:
            return None
        row = row.iloc[0]
        return {
            "code": code,
            "name": str(row.get("名称", "")),
            "price": _safe_float(row.get("最新价")),
            "change_pct": _safe_float(row.get("涨跌幅")),
            "volume": _safe_float(row.get("成交量")),
            "amount": _safe_float(row.get("成交额")),
            "open": _safe_float(row.get("开盘价")),
            "previous_close": _safe_float(row.get("昨收")),
            "quote_updated_at": row.get("更新时间"),
        }

    def fetch_realtime_a(self, code: str) -> Optional[dict]:
        return self._lookup_spot("A", code)

    def fetch_realtime_hk(self, code: str) -> Optional[dict]:
        return self._lookup_spot("HK", code)

    def fetch_intraday_quote(self, code: str, market: Market,
                             now: Optional[datetime.datetime] = None) -> Optional[dict]:
        """Ephemeral quote only; never writes a daily cache or backtest data."""
        now = (now or datetime.datetime.now(CN_TZ)).astimezone(CN_TZ)
        if not (datetime.time(9, 30) <= now.time() <= datetime.time(15, 5)):
            return None
        if market == Market.HK:
            return None
        if market != Market.ETF:
            # EastMoney's A-share table has no per-quote timestamp. Sina's dated
            # single-symbol response prevents a previous close posing as live.
            return (self._sina_intraday_quote(code, now)
                    or self._tencent_intraday_quote(code, now))
        item = self._lookup_spot("ETF", code)
        if item and item["price"] > 0 and item["volume"] > 0:
            stamp = pd.to_datetime(item.get("quote_updated_at"), errors="coerce")
            if not pd.isna(stamp):
                if stamp.tzinfo is None:
                    stamp = stamp.tz_localize(CN_TZ)
                stamp = stamp.tz_convert(CN_TZ)
                if stamp.date() == now.date() and abs((now - stamp.to_pydatetime()).total_seconds()) <= 900:
                    return {**item, "source": "eastmoney", "as_of": stamp.isoformat(),
                            "provisional": True}
        return (self._sina_intraday_quote(code, now)
                or self._tencent_intraday_quote(code, now))

    def _sina_intraday_quote(self, code: str, now: datetime.datetime) -> Optional[dict]:
        """Sina single-symbol quote is a fallback for both A shares and exchange ETFs."""
        exchange = "sh" if code.startswith(("5", "6")) else "sz"
        request = urllib.request.Request(
            f"https://hq.sinajs.cn/list={exchange}{code}",
            headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("gbk")
            fields = raw.split('"')[1].split(",")
            stamp = datetime.datetime.fromisoformat(f"{fields[30]}T{fields[31]}").replace(tzinfo=CN_TZ)
            price, previous, volume, amount = map(float, (fields[3], fields[2], fields[8], fields[9]))
            if (stamp.date() != now.date() or abs((now - stamp).total_seconds()) > 900
                    or price <= 0 or previous <= 0 or volume <= 0):
                return None
            return {"code": code, "name": fields[0], "price": price,
                    "change_pct": (price / previous - 1) * 100, "volume": volume / 100,
                    "amount": amount, "open": float(fields[1]), "previous_close": previous,
                    "source": "sina", "as_of": stamp.isoformat(),
                    "provisional": True}
        except (IndexError, ValueError, OSError, urllib.error.URLError):
            return None

    def _tencent_intraday_quote(self, code: str, now: datetime.datetime) -> Optional[dict]:
        """Independent dated quote fallback; Tencent volume is already in lots."""
        exchange = "sh" if code.startswith(("5", "6")) else "sz"
        request = urllib.request.Request(
            f"https://qt.gtimg.cn/q={exchange}{code}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("gbk")
            fields = raw.split('"')[1].split("~")
            stamp = datetime.datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
            price, previous, opened = map(float, (fields[3], fields[4], fields[5]))
            volume, amount = float(fields[36]), float(fields[37]) * 10000
            if (stamp.date() != now.date() or abs((now - stamp).total_seconds()) > 900
                    or price <= 0 or previous <= 0 or volume <= 0):
                return None
            return {"code": code, "name": fields[1], "price": price,
                    "change_pct": (price / previous - 1) * 100, "volume": volume,
                    "amount": amount, "open": opened, "previous_close": previous,
                    "source": "tencent", "as_of": stamp.isoformat(), "provisional": True}
        except (IndexError, ValueError, OSError, urllib.error.URLError):
            return None

    def identify_security(self, code: str) -> Optional[dict]:
        """从现有免费行情源识别六位沪深股票或场内 ETF。"""
        code = str(code).strip()
        if not re.fullmatch(r"\d{6}", code):
            return None
        for market_key, asset_type in (("ETF", "ETF"), ("A", "STOCK")):
            item = self._lookup_spot(market_key, code)
            if item is None:
                continue
            name = str(item.get("name") or "").strip()
            if (not name or item.get("price", 0.0) <= 0 or "退" in name
                    or "ST" in name.upper()):
                return None
            exchange = "上海" if code.startswith(("5", "6", "68")) else "深圳"
            return {
                "code": code, "name": name, "asset_type": asset_type,
                "market": "ETF" if asset_type == "ETF" else exchange,
                "exchange": exchange, "reference_price": float(item["price"]),
            }
        return None

    def request_openai_json(self, api_key: str, model: str, payload: dict) -> dict:
        """调用 OpenAI Responses API；调用方只传去标识化的规则候选与技术摘要。"""
        body = json.dumps({
            "model": model,
            "instructions": "你是投资报告文字编辑。只能解释输入中的既有结论，不得新增、修改或猜测任何数字；notes 中不得出现数字字符。",
            "input": json.dumps(payload, ensure_ascii=False),
            "store": False,
            "max_output_tokens": 1200,
            "text": {"format": {
                "type": "json_schema", "name": "account_advice_editorial",
                "strict": True,
                "schema": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "order": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {"code": {"type": "string"}, "note": {"type": "string"}},
                            "required": ["code", "note"],
                        }},
                    },
                    "required": ["order", "notes"],
                },
            }},
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses", data=body, method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        output_text = result.get("output_text")
        if not isinstance(output_text, str):
            for item in result.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        output_text = content.get("text")
                        break
        if not isinstance(output_text, str):
            raise ValueError("OpenAI 返回缺少 JSON 文本")
        return json.loads(output_text)

    # ---------- 公开新闻元数据 ----------

    def fetch_public_news_rss(self, query: str) -> Optional[bytes]:
        """读取 Bing News RSS；正文不下载，解析与缓存由 data.news 负责。"""
        encoded = urllib.parse.urlencode({
            "q": query,
            "format": "rss",
            "setlang": "zh-hans",
            "mkt": "zh-CN",
            "qft": 'sortbydate="1"',
        })
        url = f"https://www.bing.com/news/search?{encoded}"

        def _do_fetch():
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 ETFQuantAssistant/1.0"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()

        return self._fetch_with_retry(_do_fetch, f"新闻:{query}")

    def fetch_stock_news_metadata(self, code: str) -> Optional[list]:
        """读取东财个股新闻，只返回标题、时间、来源和链接，不返回或缓存正文。"""
        frame = self._fetch_with_retry(
            lambda: ak.stock_news_em(symbol=code), f"{code}公司新闻"
        )
        if frame is None:
            return None
        if frame.empty:
            return []
        items = []
        for _, row in frame.head(50).iterrows():
            title = str(row.get("新闻标题", "") or "").strip()
            url = str(row.get("新闻链接", "") or "").strip()
            source = str(row.get("文章来源", "东方财富") or "东方财富").strip()
            published = pd.to_datetime(row.get("发布时间"), errors="coerce")
            if not title or not url or pd.isna(published):
                continue
            moment = published.to_pydatetime()
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=CN_TZ)
            items.append({
                "title": title,
                "published_at": moment.astimezone(datetime.timezone.utc).isoformat(),
                "source": source,
                "url": url,
            })
        return items

    # ---------- Telegram 通知 ----------

    @staticmethod
    def _telegram_request(bot_token: str, method: str, payload: dict) -> dict:
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "ETFQuantAssistant/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            description = ""
            try:
                response_body = json.loads(error.read().decode("utf-8"))
                description = str(response_body.get("description", ""))
            except Exception:
                pass
            detail = f": {description}" if description else ""
            raise RuntimeError(f"Telegram API 返回 HTTP {error.code}{detail}") from None
        except urllib.error.URLError as error:
            raise RuntimeError(f"Telegram 网络连接失败: {error.reason}") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("Telegram API 返回了无法解析的响应") from None
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API 拒绝请求: {result.get('description', '未知错误')}")
        return result

    def send_telegram_message(self, bot_token: str, chat_id: str, text: str) -> dict:
        """调用 Telegram Bot API；敏感凭据不会进入日志或异常文本。"""
        result = self._telegram_request(bot_token, "sendMessage", {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": True,
        })
        message = result.get("result", {})
        return {"message_id": message.get("message_id"), "ok": True}

    def fetch_telegram_updates(self, bot_token: str, offset: int = 0,
                               limit: int = 100) -> list:
        """短轮询 Telegram 文本消息；游标由调用方持久化。"""
        result = self._telegram_request(bot_token, "getUpdates", {
            "offset": offset,
            "limit": limit,
            "timeout": 0,
            "allowed_updates": ["message"],
        })
        updates = result.get("result", [])
        if not isinstance(updates, list):
            raise RuntimeError("Telegram API 返回的 updates 格式无效")
        return updates

    # ---------- GitHub 私有状态仓库 ----------

    @staticmethod
    def _github_contents_request(token: str, repository: str, path: str,
                                 payload: Optional[dict] = None) -> Optional[dict]:
        safe_path = urllib.parse.quote(path.strip("/"), safe="/")
        url = f"https://api.github.com/repos/{repository}/contents/{safe_path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ETFQuantAssistant/1.0",
            },
            method="GET" if payload is None else "PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if payload is None and error.code == 404:
                return None
            detail = ""
            try:
                response_body = json.loads(error.read().decode("utf-8"))
                detail = str(response_body.get("message", ""))
            except Exception:
                pass
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"GitHub 状态仓库 API 返回 HTTP {error.code}{suffix}") from None
        except urllib.error.URLError as error:
            raise RuntimeError(f"GitHub 状态仓库连接失败: {error.reason}") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("GitHub 状态仓库返回了无法解析的响应") from None

    def get_github_repository_file(self, token: str, repository: str,
                                   path: str) -> Optional[dict]:
        result = self._github_contents_request(token, repository, path)
        if result is None:
            return None
        try:
            content = base64.b64decode(result["content"], validate=False)
            return {"content": content, "sha": str(result["sha"])}
        except (KeyError, TypeError, ValueError, binascii.Error) as error:
            raise RuntimeError("GitHub 状态文件格式无效") from error

    def put_github_repository_file(self, token: str, repository: str, path: str,
                                   content: bytes, message: str,
                                   sha: Optional[str] = None) -> str:
        payload = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        result = self._github_contents_request(token, repository, path, payload)
        try:
            return str(result["content"]["sha"])
        except (KeyError, TypeError) as error:
            raise RuntimeError("GitHub 状态仓库写入响应格式无效") from error
