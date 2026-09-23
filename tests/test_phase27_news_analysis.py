import copy
import datetime
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from quant_assistant.config import ETF_POOL, TARGET_WEIGHTS
from quant_assistant.data.news import (
    NewsDataService,
    _is_stale,
    _match_and_filter,
    deduplicate_news,
)
from quant_assistant.news.analysis import build_theme_observations
from quant_assistant.news.themes import ETF_THEME_MAP
from quant_assistant.portfolio.account_report import (
    _position_views,
    generate_account_reports,
    select_focus_positions,
)

from test_phase26_account_report import position, signal_frame


NOW = datetime.datetime(2026, 9, 13, 4, 0, tzinfo=datetime.timezone.utc)


def news_item(title="金融科技政策发布", code="159851", published="2026-09-12T02:00:00+00:00"):
    return {
        "title": title, "published_at": published, "source": "测试媒体",
        "url": "https://example.com/event", "fetched_at": NOW.isoformat(),
        "matched_etfs": [code], "matched_keywords": ["金融科技"],
        "category": "金融科技",
    }


def rss_payload():
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel><item>
<title>金融科技政策发布 - 测试媒体</title>
<link>https://example.com/event</link>
<pubDate>Sat, 12 Sep 2026 02:00:00 GMT</pubDate>
<source>测试媒体</source>
</item></channel></rss>""".encode("utf-8")


class FakeNewsFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.queries = []

    def fetch_public_news_rss(self, query):
        self.calls += 1
        self.queries.append(query)
        return self.payload


class NewsAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_etf_theme_mapping_covers_real_account_without_strategy_pool_changes(self):
        expected = {
            "561550", "159866", "159851", "560860", "159201", "159782",
            "159160", "159934", "159625", "560710", "518600", "513010", "515880",
            "159649",
        }
        self.assertEqual(set(ETF_THEME_MAP), expected)
        for meta in ETF_THEME_MAP.values():
            self.assertTrue({"asset_class", "market", "theme", "keywords"} <= set(meta))
        self.assertTrue(expected.isdisjoint(ETF_POOL))

    def test_news_deduplication_merges_reposts_of_same_event(self):
        first = news_item()
        second = dict(first, title="金融科技：政策发布", source="转载媒体",
                      url="https://other.example/event")

        result = deduplicate_news([first, second])

        self.assertEqual(len(result), 1)

    def test_distinct_bing_redirect_targets_are_not_deduplicated(self):
        first = news_item(title="黄金市场事件一")
        first["url"] = "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fa.example%2F1"
        second = news_item(title="新能源行业事件二")
        second["url"] = "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fb.example%2F2"

        result = deduplicate_news([first, second])

        self.assertEqual(len(result), 2)

    def test_keyword_matching_records_etf_keyword_and_category(self):
        raw = [{
            "title": "金融科技监管政策发布", "published_at": "2026-09-12T02:00:00+00:00",
            "source": "测试媒体", "url": "https://example.com/a", "fetched_at": NOW.isoformat(),
        }]

        matched = _match_and_filter(raw, ["159851", "561550"], NOW)

        self.assertEqual(matched[0]["matched_etfs"], ["159851"])
        self.assertIn("金融科技", matched[0]["matched_keywords"])
        self.assertEqual(matched[0]["category"], "金融科技")

    def test_news_cache_is_written_and_reused_while_fresh(self):
        fetcher = FakeNewsFetcher(rss_payload())
        cache = self.root / "news.json"
        service = NewsDataService(cache, fetcher)

        first = service.fetch_for_etfs(["159851"], now=NOW)
        calls_after_first = fetcher.calls
        second = service.fetch_for_etfs(["159851"], now=NOW + datetime.timedelta(hours=1))

        self.assertTrue(cache.exists())
        self.assertEqual(first["source_status"]["bing_news_rss"], "ok")
        self.assertEqual(second["source_status"]["news_cache"], "fresh")
        self.assertEqual(fetcher.calls, calls_after_first)

    def test_external_query_does_not_contain_account_codes_or_specific_themes(self):
        fetcher = FakeNewsFetcher(rss_payload())
        service = NewsDataService(self.root / "news.json", fetcher)

        service.fetch_for_etfs(["159851", "159160"], now=NOW)

        self.assertEqual(fetcher.calls, 1)
        self.assertNotIn("159851", fetcher.queries[0])
        self.assertNotIn("159160", fetcher.queries[0])
        self.assertNotIn("金融科技", fetcher.queries[0])
        self.assertNotIn("动力电池", fetcher.queries[0])

    def test_network_failure_uses_existing_cache(self):
        cache = self.root / "news.json"
        service = NewsDataService(cache, FakeNewsFetcher(rss_payload()))
        service.fetch_for_etfs(["159851"], now=NOW)
        service.fetcher = FakeNewsFetcher(None)

        result = service.fetch_for_etfs(
            ["159851"], now=NOW + datetime.timedelta(hours=2), force=True
        )

        self.assertEqual(result["source_status"]["bing_news_rss"], "failed_using_cache")
        self.assertTrue(result["degraded"])
        self.assertEqual(len(result["items"]), 1)

    def test_stale_news_cache_is_marked(self):
        self.assertTrue(_is_stale("2026-09-11T00:00:00+00:00", NOW))
        self.assertFalse(_is_stale("2026-09-13T03:00:00+00:00", NOW))

    def test_report_generates_normally_without_news(self):
        manager = _manager(self.root, [position(1)])
        paths = generate_account_reports(
            manager, {manager.positions[0].code: signal_frame()}, report_dir=self.root
        )
        daily = paths["daily"].read_text(encoding="utf-8")
        detail = paths["detail"].read_text(encoding="utf-8")

        self.assertIn("新闻截止：无可用新闻（陈旧缓存）", daily)
        self.assertIn("新闻状态：", daily)
        self.assertIn("已降级", daily)
        self.assertIn("无可用新闻；报告已按无新闻模式正常降级", detail)

    def test_news_analysis_does_not_touch_strategy_configuration_or_trade_modules(self):
        pool_before = copy.deepcopy(ETF_POOL)
        weights_before = copy.deepcopy(TARGET_WEIGHTS)

        build_theme_observations(["159851"], [news_item()], NOW)

        self.assertEqual(ETF_POOL, pool_before)
        self.assertEqual(TARGET_WEIGHTS, weights_before)
        source = inspect.getsource(NewsDataService) + inspect.getsource(build_theme_observations)
        self.assertNotIn("rebalance", source)
        self.assertNotIn("allocation", source)

    def test_news_affects_focus_order_but_score_is_capped(self):
        positions = [position(1), position(2)]
        manager = _manager(self.root, positions, cash=1_000_000.0)
        observations = {
            positions[1].code: {"event_strength": 100, "recent_news_count": 100},
        }

        views = _position_views(
            manager, {item.code: signal_frame() for item in positions}, observations
        )
        focus = select_focus_positions(views)

        news_view = next(item for item in views if item["position"].code == positions[1].code)
        self.assertEqual(news_view["news_score"], 4)
        self.assertEqual(focus[0]["position"].code, positions[1].code)

    def test_news_report_has_sources_times_and_no_explicit_trade_instruction(self):
        item = position(1)
        manager = _manager(self.root, [item])
        event = news_item(title="金融科技政策发布", code=item.code)
        observation = build_theme_observations([item.code], [event], NOW)
        bundle = {
            "items": [event], "fetched_at": NOW.isoformat(),
            "news_as_of": event["published_at"], "is_stale": False,
            "degraded": False, "source_status": {"bing_news_rss": "ok"},
        }

        paths = generate_account_reports(
            manager, {item.code: signal_frame()}, news_result=bundle,
            theme_observations=observation, report_dir=self.root,
        )
        text = paths["daily"].read_text(encoding="utf-8") + paths["detail"].read_text(encoding="utf-8")

        self.assertIn("测试媒体", text)
        self.assertIn("2026-09-12", text)
        self.assertIn("行情截止", text)
        self.assertIn("新闻截止", text)
        self.assertNotIn("建议买入", text)
        self.assertNotIn("建议卖出", text)


def _manager(root: Path, positions, cash=100000.0):
    from quant_assistant.portfolio.holdings import PortfolioManager
    manager = PortfolioManager(root / "missing.json")
    manager.positions = positions
    manager.cash = cash
    return manager


if __name__ == "__main__":
    unittest.main()
