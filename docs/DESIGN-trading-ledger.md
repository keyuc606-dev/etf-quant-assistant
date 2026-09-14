# 成交台账与持久化接口设计（Phase 1-2）

## 边界

- SQLite `data/trading.sqlite3` 是初始账户快照之后的成交事实源。
- `data/portfolio.json` 继续作为 daily、weekly、dashboard 的兼容投影。
- 本模块不联网、不下单，也不改变 allocation、rebalance、weekly 或 backtest 规则。
- 初始化保存当前账户快照，不把既有持仓伪造成历史成交。

## 数据模型

- `schema_version`：记录已数据库版本及应用时间。
- `opening_account`：单行初始现金和既有 `cash_flows` 快照。
- `opening_positions`：初始化时的持仓、平均成本、`asset_type`（`STOCK`/`ETF`）和兼容展示字段。
- `executions`：按自增 `sequence` 保存股票或ETF的 BUY/SELL 成交及 `asset_type`，`execution_id` 唯一；非空
  `external_id` 由部分唯一索引保证幂等。`realized_pnl` 保存卖出时按原平均成本计算的结果。
- `cash_events`：只允许 `DEPOSIT`/`WITHDRAWAL`，普通买卖永不写入此表。

## 记账规则

- 买入现金减少 `quantity * price + fee`。平均成本包含买入费用：
  `(原数量 * 原平均成本 + 成交金额 + fee) / 新数量`。
- 卖出现金增加 `quantity * price - fee`，剩余平均成本不变。
- 卖出已实现盈亏为 `quantity * (price - 原平均成本) - fee`。
- 现金不足、持仓不足或参数非法时整笔拒绝。

## 一致性与恢复

每笔成交使用 SQLite `BEGIN IMMEDIATE`：先基于快照和既有成交重建账户并校验，再插入成交、
原子替换 `portfolio.json`，最后提交数据库。投影写入或提交抛出异常时，数据库回滚并恢复成交前的
JSON 与备份。意外断电等进程外中断可通过 `portfolio-reconcile` 检测，并且只有 `--repair` 会覆盖投影。

reconcile 比较现金、`cash_flows`、持仓集合、数量、平均成本和稳定元数据。`current_price`、
`last_updated` 及基本面字段允许由 daily 独立刷新，不作为会计不一致。

## 初始化与命令

```powershell
.venv\Scripts\python.exe -m quant_assistant portfolio-reconcile --initialize
.venv\Scripts\python.exe -m quant_assistant record-trade BUY 510300 1000 4.125 --fee 5
.venv\Scripts\python.exe -m quant_assistant record-trade SELL 510300 500 4.210 --fee 5
.venv\Scripts\python.exe -m quant_assistant record-trade BUY 600000 100 10.00 --fee 5 --asset-type STOCK
.venv\Scripts\python.exe -m quant_assistant trade-history --limit 20
.venv\Scripts\python.exe -m quant_assistant portfolio-reconcile
.venv\Scripts\python.exe -m quant_assistant portfolio-reconcile --repair
```

`--initialize` 可重复安全调用，但只会首次导入；不会产生 executions。`--repair` 默认关闭。

账户持仓与策略池是两个独立概念：`ETF_POOL` 只决定 allocation/rebalance/backtest 的策略标的；
opening snapshot 和成交台账可保存符合基本格式的A股股票与场内ETF。离线记账只做格式校验，
不会把账户标的写入 `ETF_POOL`，也不会把代码格式校验冒充交易所上市状态验证。

## Repository Pattern

应用层只依赖 `TradingRepository`，不接触连接对象、SQL、游标或 SQLite 异常。接口覆盖：

- schema 初始化与版本读取；
- opening account/positions 快照读写；
- execution 按 `execution_id` 追加、按 `external_id` 查询及历史列表；
- cash event 追加与列表；
- 账户事实重建；
- 可提交或回滚的原子事务上下文。

`TradingService` 保留参数校验、幂等参数核对、BUY/SELL 计算、事实回放、reconcile 和
`portfolio.json` 投影协调。`SQLiteTradingRepository` 独占 sqlite3、schema SQL、连接与
`BEGIN IMMEDIATE`。Fake Repository 可在内存中验证交易业务，不访问数据库或网络。

## 本地开发架构

```text
CLI
 ↓
TradingService
 ↓
TradingRepository
 └── SQLiteTradingRepository → data/trading.sqlite3
 ↓
portfolio.json 投影 → daily / weekly / dashboard
```

SQLite 当前使用 schema v2。v2 仅在 `opening_positions` 和 `executions` 增加 `asset_type`；
打开 schema v1 数据库时会原地补列，既有记录默认按 ETF 解释，无需重新初始化。

## 未来生产架构

```text
GitHub Actions / Telegram Gateway
 ↓
Application Service
 ↓
TradingRepository
 ├── SQLite（本地）
 └── Postgres / Supabase（云端）
```

未来 `PostgresTradingRepository` 或 `SupabaseTradingRepository` 至少保持同一组逻辑表：
`opening_account`、`opening_positions`、`executions`、`cash_events`、`schema_version`。
数据库必须保证 `execution_id` 唯一，以及 `external_id IS NOT NULL` 时唯一；时间字段统一写入
UTC。一次成交的幂等检查、追加和账户状态读取必须位于同一数据库事务中。并发写入应依赖数据库
唯一约束和行锁/可串行化事务，或在账户聚合上增加版本号做乐观锁；不能只依赖应用进程内锁。

GitHub Actions runner 是短生命周期临时环境：任务结束后其工作目录和本地 SQLite 不保证保留，
不同任务也可能落到不同机器，并发任务无法共享同一文件。把 SQLite 提交到 Git 会泄露私人账户
数据、产生二进制冲突且无法提供可靠事务，因此生产自动化必须使用外部持久化数据库；SQLite 只
用于本地开发和单机运行。
