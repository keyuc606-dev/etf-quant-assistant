"""V2 Telegram 简报与详细审计报告。"""

from __future__ import annotations

from pathlib import Path


REGIME_ZH = {"RISK_ON": "偏强", "NEUTRAL": "中性", "RISK_OFF": "偏弱", "UNKNOWN": "未知"}


def _money(value) -> str:
    return f"￥{float(value or 0):,.0f}"


def render_compact(scan: dict, allocation: dict, holdings_count: int,
                   paper_snapshot: dict | None = None) -> str:
    regime = scan["regime"]
    lines = ["【个人 AI ETF 资产配置助手 V2】",
             f"市场状态：{REGIME_ZH.get(regime['state'], regime['state'])}｜{regime['reason']}",
             f"现有仓位：{holdings_count}项；具体持有/加减仓信号沿用今日10:30执行卡，V2不覆盖。",
             f"本次闲置资金：{_money(allocation['investable_cash'])}"]
    if allocation["allocations"]:
        lines.append(f"满足全部规则：{len(allocation['allocations'])}只")
        for index, row in enumerate(allocation["allocations"], 1):
            corr = "未知" if row.get("correlation") is None else f"{row['correlation']:.2f}"
            lines.append(f"{index}. {row['name']} {row['code']}｜{row['shares']:,}份≈{_money(row['amount'])}｜"
                         f"评分{row['score']:.1f}｜组合相关性{corr}")
        lines.append(f"现金选项：保留{_money(allocation['cash_amount'])}；{allocation['cash_reason']}")
    else:
        lines.append(f"现金选项：保留全部{_money(allocation['cash_amount'])}；{allocation['cash_reason']}")
    if paper_snapshot:
        excess = (paper_snapshot.get("nav") or 1) - (paper_snapshot.get("benchmark_nav") or 1)
        lines.append(f"模拟账户：净值{paper_snapshot.get('nav', 1):.4f}｜相对沪深300 {excess:+.2%}")
    lines.extend(["依据：趋势、动量、波动/回撤、成交额、市场环境及与现有组合相关性。",
                  "历史亏损不参与新资金评分；仅为候选配置，所有交易仍需你确认，系统不下单。"])
    return "\n".join(lines)


def render_detail(scan: dict, allocation: dict, paper_state: dict | None = None) -> str:
    lines = ["# 个人 AI ETF 资产配置助手 V2 详细报告", "",
             "> 规则输出，不是收益承诺；历史成本与浮亏不进入新资金目标函数。", "",
             "## 市场环境", "", f"- {scan['regime']['reason']}"]
    if scan["regime"].get("breadth") is not None:
        lines.append(f"- 60日均线上方候选占比：{scan['regime']['breadth']:.1%}")
    lines.extend(["", "## 新资金候选配置", ""])
    if not allocation["allocations"]:
        lines.append(f"- 当前不买：{allocation['cash_reason']}")
    else:
        lines.extend(["|代码|名称|份额|金额|总分|趋势|动量|风险|流动性|相关性|依据|",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"])
        for row in allocation["allocations"]:
            corr = "-" if row.get("correlation") is None else f"{row['correlation']:.2f}"
            lines.append(f"|{row['code']}|{row['name']}|{row['shares']:,}|{_money(row['amount'])}|"
                         f"{row['score']:.1f}|{row['trend_score']:.0f}|{row['momentum_score']:.0f}|"
                         f"{row['risk_score']:.0f}|{row['liquidity_score']:.0f}|{corr}|{row['basis']}|")
    lines.extend(["", f"- 现金保留：{_money(allocation['cash_amount'])}（{allocation['cash_reason']}）",
                  "- 风险上限：单ETF {single_etf_max:.0%}；单行业 {single_sector_max:.0%}；总权益 {total_equity_max:.0%}".format(**allocation["constraints"]),
                  "", "## 全部候选审计", "",
                  "|代码|名称|资产类别|总分|是否通过|20日均成交额|60日波动率|120日最大回撤|说明|",
                  "|---|---|---|---:|---:|---:|---:|---:|---|"])
    for row in scan["candidates"]:
        amount = row.get("avg_amount_20d")
        vol, dd = row.get("volatility"), row.get("max_drawdown_120d")
        lines.append(f"|{row['code']}|{row['name']}|{row['asset_class']}|{row['score']:.1f}|"
                     f"{'是' if row['eligible'] else '否'}|{'-' if amount is None else _money(amount)}|"
                     f"{'-' if vol is None else f'{vol:.1%}'}|{'-' if dd is None else f'{dd:.1%}'}|"
                     f"{'；'.join(row['reasons'])}|")
    if paper_state:
        lines.extend(["", "## 模拟账户", "", f"- 初始资金：{_money(paper_state.get('initial_cash'))}",
                      f"- 当前现金：{_money(paper_state.get('cash'))}",
                      f"- 已模拟成交：{len(paper_state.get('trades', []))}笔",
                      "- 建议日不成交；下一可用交易日开盘加滑点和佣金模拟成交。"])
    lines.extend(["", "## 风险提示", "", "- 允许全部持有现金，不从相对排名中强行挑选。",
                  "- 新资金只看当前条件，不以弥补任何历史亏损为目标。",
                  "- 数据陈旧、成交额不足或风险约束无余量时，不给买入配置。",
                  "- 无自动下单接口，所有交易由用户最终确认。", ""])
    return "\n".join(lines)


def write_reports(report_dir: Path, compact: str, detail: str) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    compact_path = report_dir / "etf-v2-daily.md"
    detail_path = report_dir / "etf-v2-detail.md"
    compact_path.write_text(compact + "\n", encoding="utf-8")
    detail_path.write_text(detail + "\n", encoding="utf-8")
    return compact_path, detail_path
