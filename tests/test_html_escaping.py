import json
import tempfile
import unittest
from pathlib import Path

from quant_assistant.backtest.report import _json_for_script as report_json_for_script
from quant_assistant.dashboard import _json_for_script, generate_dashboard
from quant_assistant.portfolio.holdings import PortfolioManager


POISON_NAME = '</script><script>alert("xss")</script>'


def write_portfolio(path):
    path.write_text(json.dumps({
        "cash": 1000.0,
        "cash_flows": [],
        "positions": [{
            "code": "510300",
            "name": POISON_NAME,
            "market": "ETF",
            "shares": 1000,
            "cost_price": 1.0,
            "current_price": 1.0,
            "sector": "",
            "last_updated": None,
        }],
    }, ensure_ascii=False), encoding="utf-8")


class HtmlEscapingTest(unittest.TestCase):

    def test_json_embed_helpers_escape_script_close(self):
        for func in (_json_for_script, report_json_for_script):
            payload = [{"name": POISON_NAME, "sep": "a\u2028b"}]
            rendered = func(payload)
            self.assertNotIn("<", rendered)
            self.assertNotIn("\u2028", rendered)

    def test_dashboard_output_never_contains_raw_script_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_path = Path(tmp) / "portfolio.json"
            write_portfolio(portfolio_path)
            pm = PortfolioManager(portfolio_path)
            output = Path(tmp) / "dashboard.html"
            generate_dashboard(pm, output_path=output)
            html = output.read_text(encoding="utf-8")
            self.assertNotIn(POISON_NAME, html)
            self.assertIn("\\u003c/script\\u003e", html)


if __name__ == "__main__":
    unittest.main()
