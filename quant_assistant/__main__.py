"""
统一命令行入口。七条固定命令，任何模型/任何人照 CLAUDE.md 执行即可：

  python -m quant_assistant daily                 # 每日管道：行情→持仓→指标→风控→仪表盘
  python -m quant_assistant backtest 600519       # 回测（--strategy --days 可选）
  python -m quant_assistant backtest-portfolio    # ETF 组合级回测（--market 市价对照）
  python -m quant_assistant screen                # 多因子选股筛选
  python -m quant_assistant dashboard             # 仅生成仪表盘（离线可用）
  python -m quant_assistant plan / weekly         # ETF 周报与本周交易清单
  python -m quant_assistant record-trade          # 本地录入 ETF 成交
  python -m quant_assistant trade-history         # 查看本地成交台账
  python -m quant_assistant portfolio-reconcile   # 核对 SQLite 与持仓投影
  python -m quant_assistant notify-daily          # 生成30秒日报并单向推送到Telegram
"""
import sys
import os
import argparse
import datetime

from .models import Market


STRATEGY_CHOICES = ["dual_ma", "macd", "rsi", "kdj", "composite"]


def _build_strategy(name: str):
    from .backtest.strategies import (DualMAStrategy, MACDCrossStrategy,
                                      RSIReversalStrategy, KDJCrossStrategy,
                                      CompositeStrategy)
    if name == "dual_ma":
        return DualMAStrategy()
    if name == "macd":
        return MACDCrossStrategy()
    if name == "rsi":
        return RSIReversalStrategy()
    if name == "kdj":
        return KDJCrossStrategy()
    return CompositeStrategy([DualMAStrategy(), MACDCrossStrategy(), RSIReversalStrategy()])


def _guess_market(code: str) -> Market:
    """按代码推断市场：5位=港股，51/15/52/56/58开头=ETF，6开头=沪，其余=深"""
    if len(code) == 5:
        return Market.HK
    if code.startswith(("51", "15", "52", "56", "58")):
        return Market.ETF
    if code.startswith("6"):
        return Market.A_SH
    return Market.A_SZ


def cmd_daily(args):
    from .data.news import NewsDataService
    from .news.analysis import build_theme_observations
    from .portfolio.account_report import generate_account_reports
    from .analysis.account_advice import OpenAIAdviceProvider
    from .portfolio.holdings import PortfolioManager
    from .portfolio.suggestions import generate_suggestions
    from .pipeline import run_daily_pipeline
    from .dashboard import generate_dashboard
    from .trading.service import TradingService
    from .weekly import (HEARTBEAT_NO_RECORD_NOTICE, generate_emergency_rebalance,
                         print_emergency_summary, update_daily_heartbeat_check)

    trading = TradingService()
    if trading.is_initialized():
        reconciliation = trading.reconcile(repair=True)
        if reconciliation["repaired"]:
            print("已按成交台账恢复持仓数量、成本和现金投影")
    pm = PortfolioManager()
    if not pm.positions:
        print("持仓为空（data/portfolio.json），可参考 data/portfolio.example.json 录入")
        return None

    heartbeat_warning = update_daily_heartbeat_check()
    if heartbeat_warning:
        label = "提示" if heartbeat_warning == HEARTBEAT_NO_RECORD_NOTICE else "黄字警告"
        print(f"\n  [{label}] {heartbeat_warning}")

    result = run_daily_pipeline(pm, days=args.days)
    emergency = generate_emergency_rebalance(pm)
    circuit_warning = emergency["allocation"]["drawdown"] if emergency.get("triggered") else None
    if emergency.get("triggered"):
        print("\n  !!! 断路器日频监控告警 !!!")
        print(f"  当前回撤 {circuit_warning['drawdown_pct']:.2%}，动作 {circuit_warning['action']}")
        print_emergency_summary(emergency)
    elif emergency.get("note"):
        drawdown = emergency["allocation"]["drawdown"]
        print(f"\n  [提示] {emergency['note']}（当前回撤 {drawdown['drawdown_pct']:.2%}，动作 {drawdown['action']}）")

    result["analyzer"].print_dashboard()

    alerts = result["alerts"]
    if alerts:
        print("\n  风控告警:")
        for a in alerts:
            print(f"  [{a.level}] {a.rule_name}: {a.message}")
        print("\n  操作建议:")
        for s in generate_suggestions(pm, alerts):
            print(f"  - {s}")
    else:
        print("\n  风控检查通过，无告警")

    if result["fetch_errors"]:
        print(f"\n  [警告] 以下标的行情获取失败；仅在已有有效旧价格时沿用，"
              f"否则不计算盈亏: {', '.join(result['fetch_errors'])}")
    degraded_sources = getattr(result["fetcher"], "degraded_sources", [])
    if degraded_sources:
        print("  [警告/降级] 主行情源失败，备用源已成功提供行情: "
              + ", ".join(degraded_sources))

    dashboard_alerts = []
    if emergency.get("triggered"):
        dashboard_alerts.append(
            f"断路器日频应急已触发（当前回撤 {circuit_warning['drawdown_pct']:.2%}，动作 {circuit_warning['action']}）"
        )
    path = generate_dashboard(pm, stock_data=result["stock_data"],
                              top_alerts=dashboard_alerts)
    print(f"\n  HTML 仪表盘: {path}")
    try:
        news_result = NewsDataService().fetch_for_instruments(
            [position.code for position in pm.positions]
        )
    except Exception as error:
        news_result = {
            "items": [], "fetched_at": None, "news_as_of": None,
            "is_stale": True, "degraded": True,
            "source_status": {"bing_news_rss": "failed_no_cache"},
            "errors": [str(error)],
        }
    news_as_of = datetime.datetime.now(datetime.timezone.utc)
    theme_observations = build_theme_observations(
        [position.code for position in pm.positions], news_result["items"], news_as_of
    )
    reports = generate_account_reports(
        pm,
        stock_data=result["stock_data"],
        alerts=result["alerts"],
        fetch_errors=result["fetch_errors"],
        news_result=news_result,
        theme_observations=theme_observations,
        advice_provider=OpenAIAdviceProvider(result["fetcher"]),
    )
    print(f"  账户日报: {reports['daily']}")
    print(f"  详细报告: {reports['detail']}")
    reports["market_data_count"] = len(result["stock_data"])
    return reports


def cmd_notify_daily(args):
    from .notifications.telegram import TelegramNotifier
    from .v3 import cloud_available, save_morning_advice

    try:
        reports = cmd_daily(args)
    except Exception as error:
        raise RuntimeError(f"日报生成失败: {error}") from error
    if not reports:
        raise RuntimeError("日报未生成，Telegram 未发送")
    if reports.get("market_data_count", 1) == 0:
        raise RuntimeError("全部持仓行情不可用，日报缺少有效行情，Telegram 未发送")
    if os.getenv("GITHUB_ACTIONS") == "true" and not cloud_available():
        raise RuntimeError("云端建议状态凭据缺失，Telegram 未发送")

    print("  建议审计：开始写入并回读私有状态")
    try:
        save_morning_advice(reports)
    except Exception as error:
        raise RuntimeError(f"建议/状态持久化失败，Telegram 未发送: {error}") from error

    print("  Telegram：开始发送日报")
    result = TelegramNotifier().send_daily_report(reports["daily"])
    if not result.success:
        raise RuntimeError(
            f"日报已正常生成，但 Telegram 推送失败: {result.error}"
        )
    print(f"  Telegram 推送成功: {result.sent_parts} 条消息")


def cmd_notify_intraday(args):
    from .v3 import notify_intraday
    path = notify_intraday()
    print(f"盘中风险快照已推送：{path}")


def cmd_advice_recalculate(args):
    from .cloud_state import CloudStateStore
    from .v3 import recalculate
    from .advice_performance import render_summary
    from .trading.service import TradingService
    from .data.fetcher import CN_TZ
    state, _ = CloudStateStore().load()
    _outcomes, summary = recalculate(
        state.get("advice_records", []), datetime.datetime.now(CN_TZ),
        executions=TradingService().repository.list_executions(),
    )
    print(render_summary(summary))


def cmd_backtest(args):
    from .backtest.runner import run_backtest

    market = Market[args.market] if args.market else _guess_market(args.code)
    strategy = _build_strategy(args.strategy)
    run_backtest(args.code, market, strategy, days=args.days,
                 initial_capital=args.capital, open_report=not args.no_open)


def cmd_backtest_portfolio(args):
    import datetime
    from tabulate import tabulate

    from .backtest.portfolio_engine import PortfolioBacktestConfig, PortfolioBacktestEngine
    from .backtest.report import generate_portfolio_backtest_report
    from .config import ETF_POOL
    from .data.fetcher import DataFetcher

    start_date = datetime.datetime.strptime(args.start, "%Y-%m-%d").date()
    data_start = datetime.date(2015, 1, 1)
    fetcher = DataFetcher()
    nav_data = {}
    coverage_rows = []
    for code, meta in ETF_POOL.items():
        df = fetcher.fetch_nav(code, data_start, offline=True)
        if df is not None and not df.empty:
            nav_data[code] = df
            coverage_rows.append([
                code,
                meta["name"],
                df["日期"].min().date(),
                df["日期"].max().date(),
                len(df),
            ])
        else:
            coverage_rows.append([code, meta["name"], "-", "-", 0])

    print("\n累计净值缓存覆盖区间：")
    print(tabulate(coverage_rows, headers=["代码", "名称", "起始", "截止", "行数"], tablefmt="grid"))

    engine = PortfolioBacktestEngine(PortfolioBacktestConfig(initial_capital=args.capital))
    variants = [
        ("full", "完整规则"),
        ("no_circuit", "关闭断路器"),
        ("no_trend", "关闭趋势过滤"),
        ("higher_equity", "权益中枢上调"),
    ]
    results = []
    for variant, _label in variants:
        result = engine.run(nav_data=nav_data, start_date=start_date, variant=variant)
        results.append(result)

    if args.market:
        from .weekly import load_etf_market_data
        market_data, _prices, md_warnings = load_etf_market_data()
        if not market_data:
            print("\n[警告] --market 市价模式需要 data/cache/{code}_daily.csv（先运行 bootstrap_etf_cache.py），本次跳过。")
        else:
            for warning in md_warnings:
                print(f"  [警告] {warning}")
            market_result = engine.run(
                nav_data=nav_data, start_date=start_date,
                variant="full", market_data=market_data,
            )
            market_result.name = "完整规则(市价撮合)"
            results.append(market_result)
            print(f"\n市价模式使用场内价格的标的: {', '.join(sorted(market_data))}")

    rows = []
    for result in results:
        m = result.metrics
        calmar = m.get("calmar_ratio")
        dd_2018 = _annual_drawdown_pair(result, 2018)
        dd_2022 = _annual_drawdown_pair(result, 2022)
        rows.append([
            result.name,
            f"{m['annual_return']:.2%}",
            f"{m['max_drawdown']:.2%}",
            "N/A" if calmar is None else f"{calmar:.2f}",
            dd_2018[0],
            dd_2018[1],
            dd_2022[0],
            dd_2022[1],
        ])
    print("\n四组回测对照：")
    print(tabulate(
        rows,
        headers=["方案", "年化", "最大回撤", "卡玛", "2018策略回撤", "2018沪深300回撤", "2022策略回撤", "2022沪深300回撤"],
        tablefmt="grid",
    ))

    annual_rows = []
    for row in results[0].annual_returns:
        bm = row["benchmark_return"]
        excess = row["excess_return"]
        annual_rows.append([
            row["year"],
            f"{row['strategy_return']:.2%}",
            f"{row['strategy_max_drawdown']:.2%}",
            "N/A" if bm is None else f"{bm:.2%}",
            "N/A" if row["benchmark_max_drawdown"] is None else f"{row['benchmark_max_drawdown']:.2%}",
            "N/A" if excess is None else f"{excess:.2%}",
        ])
    print("\n完整规则年度收益 vs 沪深300ETF：")
    print(tabulate(
        annual_rows,
        headers=["年份", "完整规则", "策略回撤", "沪深300ETF", "沪深300回撤", "超额"],
        tablefmt="grid",
    ))

    report_path = generate_portfolio_backtest_report(results)
    print(f"\n组合回测报告: {report_path}")
    if args.market and any(r.name == "完整规则(市价撮合)" for r in results):
        print("口径说明: 净值组按累计净值成交（溢价闸门不生效，理想上界）；"
              "市价组按场内价格成交，QDII 溢价闸门生效，与生产周度管线一致。两组差异即溢折价敏感性。")
    else:
        print("\n口径说明: 回测按天天基金累计净值成交，QDII 溢价闸门不生效，结果为理想成交假设下的上界；"
              "加 --market 可输出市价撮合对照。")


def _annual_drawdown_pair(result, year):
    for row in result.annual_returns:
        if row["year"] == year:
            strategy_dd = f"{row['strategy_max_drawdown']:.2%}"
            benchmark_dd = row["benchmark_max_drawdown"]
            return strategy_dd, "N/A" if benchmark_dd is None else f"{benchmark_dd:.2%}"
    return "N/A", "N/A"


def cmd_screen(args):
    from .screening.screener import StockScreener, print_screening_report
    from .screening.filters import MATrendFilter, VolumeActiveFilter, PEMinFilter

    screener = StockScreener()
    screener.add_filter(PEMinFilter(0))
    screener.add_filter(MATrendFilter())
    screener.add_filter(VolumeActiveFilter())
    result = screener.run(top_n=args.top)
    print_screening_report(result)


def cmd_dashboard(args):
    from .portfolio.holdings import PortfolioManager
    from .dashboard import generate_dashboard

    pm = PortfolioManager()
    path = generate_dashboard(pm)
    print(f"HTML 仪表盘已生成（未联网，使用现有持仓价格）: {path}")


def cmd_plan(args):
    from .portfolio.holdings import PortfolioManager
    from .weekly import generate_weekly_report, print_weekly_summary

    pm = PortfolioManager()
    result = generate_weekly_report(pm)
    print_weekly_summary(result)


def cmd_weekly(args):
    cmd_plan(args)


def cmd_record_trade(args):
    from .data.fetcher import DataFetcher
    from .trading.service import TradingService

    service = TradingService()
    instrument = None
    if args.side == "BUY" and not service.has_position(args.code):
        instrument = DataFetcher().identify_security(args.code)
        if instrument is None:
            raise ValueError(f"{args.code} 无法从行情源识别或当前状态异常")
    result = service.record_trade(
        side=args.side,
        code=args.code,
        quantity=args.quantity,
        price=args.price,
        fee=args.fee,
        external_id=args.external_id,
        source=args.source,
        related_plan_id=args.related_plan_id,
        note=args.note,
        executed_at=args.executed_at,
        asset_type=args.asset_type or (instrument or {}).get("asset_type"),
        instrument=instrument,
    )
    execution = result.execution
    status = "重复 external_id，未重复记账" if result.duplicate else "成交已记账"
    print(f"{status}: {execution['execution_id']}")
    unit = "股" if execution.get("asset_type") == "STOCK" else "份"
    print(
        f"{execution['side']} {execution['code']} {execution['quantity']} {unit} @ "
        f"￥{execution['price']:.4f}，费用 ￥{execution['fee']:.2f}"
    )
    print(f"成交后现金: ￥{result.cash:,.2f}")
    print(f"成交后持仓: {result.quantity} 份，平均成本 ￥{result.average_cost:.6f}")
    if execution["side"] == "SELL":
        print(f"本笔已实现盈亏: ￥{execution['realized_pnl']:+,.2f}")


def cmd_trade_history(args):
    from tabulate import tabulate

    from .trading.service import TradingService

    executions = TradingService().recent_executions(args.limit)
    if not executions:
        print("暂无成交记录")
        return
    rows = []
    for item in executions:
        pnl = item["realized_pnl"]
        rows.append([
            item["executed_at"], item["side"], item["code"], item.get("asset_type", "ETF"), item["quantity"],
            f"{item['price']:.4f}", f"{item['fee']:.2f}",
            "-" if pnl is None else f"{pnl:+.2f}", item["source"],
            item["external_id"] or "-",
        ])
    print(tabulate(
        rows,
        headers=["成交时间", "方向", "代码", "类型", "数量", "价格", "费用", "已实现盈亏", "来源", "external_id"],
        tablefmt="grid",
    ))


def cmd_portfolio_reconcile(args):
    from .trading.service import TradingService

    service = TradingService()
    if args.initialize:
        if service.is_initialized():
            print("成交台账已经初始化，未重复导入")
        else:
            summary = service.initialize_from_portfolio()
            print(
                f"初始快照已导入: 现金 ￥{summary['cash']:,.2f}，"
                f"持仓 {summary['positions']} 项，历史成交 0 笔"
            )
    result = service.reconcile(repair=args.repair)
    if result["ok"]:
        suffix = "（已修复 portfolio.json）" if result["repaired"] else ""
        print(f"portfolio-reconcile: OK{suffix}")
        return
    print("portfolio-reconcile: MISMATCH")
    for difference in result["differences"]:
        print(f"- {difference}")


def main():
    parser = argparse.ArgumentParser(prog="quant_assistant",
                                     description="股票量化分析系统（回测/组合/选股，无实盘下单）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_daily = sub.add_parser("daily", help="每日管道：拉行情→更新持仓→指标→风控→仪表盘")
    p_daily.add_argument("--days", type=int, default=120, help="行情回看天数（默认120）")
    p_daily.set_defaults(func=cmd_daily)

    p_notify = sub.add_parser("notify-daily", help="生成30秒账户日报并单向推送到Telegram")
    p_notify.add_argument("--days", type=int, default=120, help="行情回看天数（默认120）")
    p_notify.set_defaults(func=cmd_notify_daily)

    p_intraday = sub.add_parser("notify-intraday", help="14:30 盘中风险与执行检查")
    p_intraday.set_defaults(func=cmd_notify_intraday)

    p_perf = sub.add_parser("advice-recalculate", help="从私有原始建议与市场日线重算统计")
    p_perf.set_defaults(func=cmd_advice_recalculate)

    p_bt = sub.add_parser("backtest", help="单标的策略回测")
    p_bt.add_argument("code", help="股票代码，如 600519（A股6位）/ 00700（港股5位）")
    p_bt.add_argument("--strategy", choices=STRATEGY_CHOICES, default="dual_ma",
                      help="策略（默认 dual_ma）")
    p_bt.add_argument("--days", type=int, default=365, help="回测区间天数（默认365）")
    p_bt.add_argument("--capital", type=float, default=100_000, help="初始资金（默认10万）")
    p_bt.add_argument("--market", choices=[m.name for m in Market], default=None,
                      help="市场（默认按代码自动推断）")
    p_bt.add_argument("--no-open", action="store_true", help="不自动打开HTML报告")
    p_bt.set_defaults(func=cmd_backtest)

    p_pbt = sub.add_parser("backtest-portfolio", help="ETF 组合级周度配置回测")
    p_pbt.add_argument("--start", default="2016-01-01", help="回测开始日期 YYYY-MM-DD")
    p_pbt.add_argument("--capital", type=float, default=1_000_000, help="初始资金（默认100万）")
    p_pbt.add_argument("--market", action="store_true",
                       help="额外用场内价格（含QDII溢价闸门）跑一组市价撮合对照")
    p_pbt.set_defaults(func=cmd_backtest_portfolio)

    p_screen = sub.add_parser("screen", help="多因子选股筛选（观察池见 screening/universe.py）")
    p_screen.add_argument("--top", type=int, default=10, help="展示前 N 名（默认10）")
    p_screen.set_defaults(func=cmd_screen)

    p_dash = sub.add_parser("dashboard", help="仅生成持仓仪表盘（离线可用）")
    p_dash.set_defaults(func=cmd_dashboard)

    p_plan = sub.add_parser("plan", help="ETF 周度配置计划（Phase 2，缓存优先不补拉行情）")
    p_plan.set_defaults(func=cmd_plan)

    p_weekly = sub.add_parser("weekly", help="ETF 周报与本周交易清单")
    p_weekly.set_defaults(func=cmd_weekly)

    p_trade = sub.add_parser("record-trade", help="将人工确认的 ETF 成交写入本地台账")
    p_trade.add_argument("side", choices=["BUY", "SELL"], help="成交方向")
    p_trade.add_argument("code", help="六位A股股票或场内ETF代码")
    p_trade.add_argument("quantity", type=int, help="成交份额（正整数）")
    p_trade.add_argument("price", type=float, help="成交单价（大于0）")
    p_trade.add_argument("--fee", type=float, default=0.0, help="成交费用（默认0）")
    p_trade.add_argument("--asset-type", choices=["ETF", "STOCK"],
                         help="资产类型；省略时按证券代码格式判断")
    p_trade.add_argument("--external-id", help="外部成交编号；重复提交保持幂等")
    p_trade.add_argument("--source", default="manual", help="记录来源（默认manual）")
    p_trade.add_argument("--related-plan-id", help="关联的计划编号")
    p_trade.add_argument("--note", help="备注")
    p_trade.add_argument("--executed-at", help="成交时间 ISO-8601；默认当前时间")
    p_trade.set_defaults(func=cmd_record_trade)

    p_history = sub.add_parser("trade-history", help="查看最近的本地成交记录")
    p_history.add_argument("--limit", type=int, default=20, help="返回条数（默认20）")
    p_history.set_defaults(func=cmd_trade_history)

    p_reconcile = sub.add_parser("portfolio-reconcile", help="核对 SQLite 与 portfolio.json 投影")
    p_reconcile.add_argument(
        "--initialize", action="store_true",
        help="首次使用时将当前 portfolio.json 导入为初始快照，不生成历史成交",
    )
    p_reconcile.add_argument(
        "--repair", action="store_true",
        help="发现差异时用 SQLite 重建结果修复 portfolio.json",
    )
    p_reconcile.set_defaults(func=cmd_portfolio_reconcile)

    args = parser.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        # 数据文件损坏等已知错误：打印可读信息而非堆栈
        sys.stdout.flush()
        print(f"\n[致命错误] {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        # 参数格式错误（如 --start 不是 YYYY-MM-DD）：给可读提示而非堆栈
        sys.stdout.flush()
        print(f"\n参数错误: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
