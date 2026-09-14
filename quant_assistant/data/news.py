"""新闻元数据匹配、去重与缓存；网络请求委托给 DataFetcher。"""

import datetime
import difflib
import email.utils
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional

from ..config import CACHE_DIR
from ..news.themes import ACCOUNT_THEME_MAP, STOCK_THEME_MAP
from .fetcher import DataFetcher


NEWS_CACHE_PATH = CACHE_DIR / "news.json"
NEWS_STALE_HOURS = 24
NEWS_LOOKBACK_DAYS = 7
# 固定宽泛查询，不根据账户代码或主题动态构造，避免向新闻源披露持仓画像。
PUBLIC_MARKET_NEWS_QUERY = '"ETF" OR "股票市场" OR "黄金市场" OR "新能源" OR "科技行业"'


class NewsDataService:
    def __init__(self, cache_path: Optional[Path] = None,
                 fetcher: Optional[DataFetcher] = None):
        self.cache_path = Path(cache_path) if cache_path is not None else NEWS_CACHE_PATH
        self.fetcher = fetcher or DataFetcher()

    def fetch_for_etfs(self, codes: Iterable[str], now: Optional[datetime.datetime] = None,
                       force: bool = False) -> dict:
        return self.fetch_for_instruments(codes, now=now, force=force)

    def fetch_for_instruments(self, codes: Iterable[str], now: Optional[datetime.datetime] = None,
                              force: bool = False) -> dict:
        now = _as_utc(now or datetime.datetime.now(datetime.timezone.utc))
        codes = [code for code in dict.fromkeys(codes) if code in ACCOUNT_THEME_MAP]
        cached = self._load_cache()
        covered = set(cached.get("coverage_codes", [])) if cached else set()
        if (cached and not force and set(codes).issubset(covered)
                and not _is_stale(cached.get("fetched_at"), now)):
            result = dict(cached)
            cached_degraded = bool(cached.get("degraded"))
            result["source_status"] = {
                "news_cache": "fresh_partial" if cached_degraded else "fresh"
            }
            result["is_stale"] = False
            result["degraded"] = cached_degraded
            return result

        fetched_at = now.isoformat()
        raw_items = []
        failures = []
        payload = self.fetcher.fetch_public_news_rss(PUBLIC_MARKET_NEWS_QUERY)
        if payload is None:
            failures.append("宽泛财经新闻流")
        else:
            try:
                raw_items.extend(_parse_rss(payload, fetched_at))
            except (ET.ParseError, ValueError) as error:
                failures.append(f"宽泛财经新闻流({error})")

        for code in codes:
            if code not in STOCK_THEME_MAP:
                continue
            company_items = self.fetcher.fetch_stock_news_metadata(code)
            if company_items is None:
                failures.append(f"{code}公司新闻")
                continue
            for item in company_items:
                enriched = dict(item)
                enriched["fetched_at"] = fetched_at
                enriched["matched_instruments"] = [code]
                enriched["matched_etfs"] = [code]  # schema v1 兼容字段
                enriched["matched_keywords"] = [STOCK_THEME_MAP[code]["name"]]
                enriched["category"] = STOCK_THEME_MAP[code]["theme"]
                raw_items.append(enriched)

        if raw_items:
            items = _match_and_filter(raw_items, codes, now)
            items = deduplicate_news(items)
            result = {
                "schema_version": 1,
                "fetched_at": fetched_at,
                "news_as_of": max((item["published_at"] for item in items), default=None),
                "items": items,
                "coverage_codes": codes,
                "is_stale": False,
                "degraded": bool(failures),
                "source_status": {
                    "bing_news_rss": "partial" if failures else "ok",
                },
                "errors": failures,
            }
            self._save_cache(result)
            return result

        if cached:
            result = dict(cached)
            result["is_stale"] = _is_stale(cached.get("fetched_at"), now)
            result["degraded"] = True
            result["source_status"] = {"bing_news_rss": "failed_using_cache"}
            result["errors"] = failures or ["新闻源未返回数据"]
            return result
        return {
            "schema_version": 1,
            "fetched_at": fetched_at,
            "news_as_of": None,
            "items": [],
            "is_stale": True,
            "degraded": True,
            "source_status": {"bing_news_rss": "failed_no_cache"},
            "errors": failures or ["新闻源未返回数据"],
        }

    def _load_cache(self) -> Optional[dict]:
        if not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _save_cache(self, data: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.cache_path)


def _parse_rss(payload: bytes, fetched_at: str) -> list:
    root = ET.fromstring(payload)
    items = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        url = (node.findtext("link") or "").strip()
        published = _parse_published_at(node.findtext("pubDate"))
        source = "Bing News RSS"
        for child in node:
            if child.tag.lower().endswith("source") and (child.text or "").strip():
                source = child.text.strip()
                break
        if source == "Bing News RSS" and " - " in title:
            source = title.rsplit(" - ", 1)[-1].strip()
        if title and url and published:
            items.append({
                "title": title,
                "published_at": published,
                "source": source,
                "url": url,
                "fetched_at": fetched_at,
            })
    return items


def _match_and_filter(items: list, codes: list, now: datetime.datetime) -> list:
    cutoff = now - datetime.timedelta(days=NEWS_LOOKBACK_DAYS)
    result = []
    for item in items:
        published = _parse_iso(item["published_at"])
        if published is None or published < cutoff or published > now + datetime.timedelta(hours=2):
            continue
        if item.get("matched_instruments"):
            result.append(dict(item))
            continue
        matched_etfs = []
        matched_keywords = []
        categories = []
        title_lower = item["title"].lower()
        for code in codes:
            meta = ACCOUNT_THEME_MAP[code]
            hits = [word for word in meta["keywords"] if word.lower() in title_lower]
            if meta["theme"].lower() in title_lower and meta["theme"] not in hits:
                hits.append(meta["theme"])
            if hits:
                matched_etfs.append(code)
                matched_keywords.extend(hits)
                categories.append(meta["theme"])
        if not matched_etfs:
            continue
        enriched = dict(item)
        enriched["matched_etfs"] = sorted(set(matched_etfs))
        enriched["matched_instruments"] = sorted(set(matched_etfs))
        enriched["matched_keywords"] = sorted(set(matched_keywords))
        enriched["category"] = "、".join(sorted(set(categories)))
        result.append(enriched)
    return result


def deduplicate_news(items: list) -> list:
    deduplicated = []
    for item in sorted(items, key=lambda row: row.get("published_at", ""), reverse=True):
        fingerprint = _title_fingerprint(item.get("title", ""))
        duplicate = None
        for existing in deduplicated:
            same_url = _clean_url(item.get("url", "")) == _clean_url(existing.get("url", ""))
            similar = difflib.SequenceMatcher(
                None, fingerprint, existing["_fingerprint"]
            ).ratio() >= 0.82
            close_time = _within_hours(item.get("published_at"), existing.get("published_at"), 48)
            if same_url or (similar and close_time):
                duplicate = existing
                break
        if duplicate:
            duplicate["matched_etfs"] = sorted(set(duplicate["matched_etfs"] + item.get("matched_etfs", [])))
            duplicate["matched_instruments"] = sorted(set(
                duplicate.get("matched_instruments", duplicate.get("matched_etfs", []))
                + item.get("matched_instruments", item.get("matched_etfs", []))
            ))
            duplicate["matched_keywords"] = sorted(set(duplicate["matched_keywords"] + item.get("matched_keywords", [])))
            continue
        saved = dict(item)
        saved["_fingerprint"] = fingerprint
        deduplicated.append(saved)
    for item in deduplicated:
        item.pop("_fingerprint", None)
    return deduplicated


def _title_fingerprint(title: str) -> str:
    title = title.rsplit(" - ", 1)[0]
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", title).lower()


def _clean_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    # Bing RSS 用统一 apiclick 路径跳转，真实媒体 URL 位于 url 参数中。
    target = query.get("url", [None])[0]
    if target:
        return urllib.parse.unquote(target).rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), parsed.query, ""))


def _parse_published_at(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return _as_utc(parsed).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_iso(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return _as_utc(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _is_stale(fetched_at: Optional[str], now: datetime.datetime) -> bool:
    parsed = _parse_iso(fetched_at)
    return parsed is None or now - parsed > datetime.timedelta(hours=NEWS_STALE_HOURS)


def _within_hours(left: Optional[str], right: Optional[str], hours: int) -> bool:
    left_dt = _parse_iso(left)
    right_dt = _parse_iso(right)
    return bool(left_dt and right_dt and abs(left_dt - right_dt) <= datetime.timedelta(hours=hours))
