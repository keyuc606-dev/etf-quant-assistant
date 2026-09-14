# 真实账户新闻与主题观察设计（Phase 2.7）

## 边界

新闻只进入账户报告，不进入 allocation、rebalance、backtest、风控阈值或规则交易清单。
系统不读取新闻正文，不使用 LLM，不生成自动买卖方向。

## 数据流

```text
固定宽泛财经 RSS 请求（不含账户代码/持仓）
  ↓ quant_assistant/data/fetcher.py（唯一联网入口）
标题元数据解析、UTC 时间统一、去重、缓存
  ↓ quant_assistant/data/news.py
ETF 主题关键词本地匹配
  ↓ quant_assistant/news/analysis.py
规则化主题观察
  ↓
my-portfolio-daily.md / my-portfolio-detail.md
```

## 隐私与数据源

系统不会向新闻源发送 ETF 代码、持仓数量或完整主题清单。外部请求始终使用固定的宽泛财经查询，
拿到 RSS 后再在本地匹配。当前来源为公开 Bing News RSS，只保存标题、发布时间、来源、链接、
抓取时间和匹配元数据，不保存受版权保护的正文。

## 主题映射

`quant_assistant/news/themes.py` 中的 `ETF_THEME_MAP` 与策略 `ETF_POOL` 相互独立。新增账户分析映射
不会让 ETF 进入策略池。

## 缓存与降级

新闻缓存为 `data/cache/news.json`，由现有 ignore 规则保护。缓存包含 schema version、覆盖代码、
抓取时间、新闻截止时间、来源状态和新闻条目。24 小时后标记为 stale。联网失败时读取旧缓存；
没有缓存时返回空新闻集合，日报和详报仍可正常生成并明确显示降级。

## 去重与时间

- 相同目标 URL 去重；Bing 跳转链接先提取真实媒体 URL。
- 48 小时内标题高度相似的转载视为同一事件，并合并匹配 ETF 和关键词。
- 发布时间和抓取时间统一保存为 UTC ISO-8601，报告显示北京时间。
- 新闻按自然时间保留，因此可以晚于行情截止日期，也可以发生在周末。
- 新闻不会回填成交易日价格信号。

## 规则化观察

结构化对象包含 `recent_news_count`、`major_events`、`positive_factors`、
`negative_factors`、`uncertainties`、`evidence` 和 `as_of`。分类只依据标题关键词；
没有证据时固定输出“近期公开信息不足，暂不形成行业判断。”

新闻事件对重点 ETF 排序最多贡献 8 分，避免转载数量压倒仓位、亏损、波动和技术信号。
