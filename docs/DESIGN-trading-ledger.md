# 本地成交台账设计（Phase 1）

## 边界

- SQLite `data/trading.sqlite3` 是初始账户快照之后的成交事实源。
- `data/portfolio.json` 继续作为 daily、weekly、dashboard 的兼容投影。
- 本模块不联网、不下单，也不改变 allocation、rebalance、weekly 或 backtest 规则。
- 初始化保存当前账户快照，不把既有持仓伪造成历史成交。

## 数据模型

- `schema_version`：记录已数据库版本及应用时间。
- `opening_account`：单行初始现金和既有 `cash_flows` 快照。
- `opening_positions`：初始化时的持仓、平均成本和兼容展示字段。
- `executions`：按自增 `sequence` 保存 BUY/SELL 成交，`execution_id` 唯一；非空
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
.venv\Scripts\python.exe -m quant_assistant trade-history --limit 20
.venv\Scripts\python.exe -m quant_assistant portfolio-reconcile
.venv\Scripts\python.exe -m quant_assistant portfolio-reconcile --repair
```

`--initialize` 可重复安全调用，但只会首次导入；不会产生 executions。`--repair` 默认关闭。
