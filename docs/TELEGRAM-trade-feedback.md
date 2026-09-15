# Telegram 交易反馈 MVP

## 交互

Bot 接受六位沪深 A 股代码，方向可用中文或英文：

```text
买入 600104 300股 18.72
买入 600104 300股 18.72 手续费5
卖出 002074 500股 39.10
BUY 600104 300 18.72 FEE 5
SELL 002074 500 39.10
```

第一条消息只生成摘要并预检现金或持仓，绝不记账。随后必须发送完全匹配的
`确认` 才会更新账户；发送 `取消` 会清除待确认交易。存在待确认交易时，新交易
不会覆盖旧交易。Bot 只处理 `TELEGRAM_CHAT_ID` 对应的单一会话。

GitHub Actions 每五分钟轮询一次，因此回复通常不是即时的，GitHub 繁忙时可能更慢。
这是无需常驻服务器的取舍。

## 持久化与安全边界

`ACCOUNT_SNAPSHOT_B64` 不能由普通 workflow 使用默认 `GITHUB_TOKEN` 安全地自更新，
因此它只保留为首次迁移和 09:20 日报的兼容后备。最新账户快照、Telegram 更新游标、
待确认交易和待发送回复统一写入一个专用私有 GitHub 仓库的
`state/account-state.json`。两个 workflow 使用同一个 concurrency group 串行读写，
GitHub Contents API 的文件 SHA 提供乐观并发保护。

Base64 是编码，不是加密。状态仓库必须是私有仓库，令牌只授予该仓库 Contents
读写权限。代码仓库不会保存 Token、chat_id、SQLite、portfolio.json、报告或账户状态。

## 一次性配置

1. 在 GitHub 右上角 `+` → `New repository`，创建例如 `etf-quant-state` 的
   **Private** 仓库，并勾选 `Add a README file` 以建立默认分支。
2. GitHub 头像 → `Settings` → `Developer settings` →
   `Personal access tokens` → `Fine-grained tokens` → `Generate new token`。
   `Repository access` 选择 `Only select repositories` 并只选状态仓库；
   `Repository permissions` 中把 `Contents` 设为 `Read and write`，其余保持默认。
3. 回到本项目仓库 → `Settings` → `Secrets and variables` → `Actions` →
   `New repository secret`，新增：
   - `ACCOUNT_STATE_REPO`：`你的用户名/etf-quant-state`
   - `ACCOUNT_STATE_TOKEN`：上一步生成的 fine-grained token
4. 保留已有 `ACCOUNT_SNAPSHOT_B64`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。
   不要把任何 secret 发到聊天、日志或提交中。
5. `Actions` → `Account Trade Telegram` → `Run workflow`。首次运行会把现有
   `ACCOUNT_SNAPSHOT_B64` 迁移到私有状态仓库。成功后，09:20 日报自动优先读取它。

如果 Bot 之前配置过 webhook，`getUpdates` 会被 Telegram 拒绝；部署此轮询方案前需
先移除 webhook。

## 部署验证与回滚

本地测试使用隔离的临时账户跑完整的“下单文本 → 摘要 → 确认 → 新 runner 恢复”流程，
不会接触真实账户。线上首次验证建议发送一笔能通过预检的交易，看到摘要后发送
`取消`，确认回复为“账户未变更”。

状态仓库的每次更新都是 Git commit，可在
`state/account-state.json` → `History` 审计。回滚成交时，从成交前的版本复制
**仅 `account_snapshot_b64` 字段**到当前文件并提交；保留当前 `telegram` 对象，避免
旧的“确认”更新被再次消费。随后手动运行 `Account Daily Telegram` 验证日报。

明天 09:20 workflow 会执行 `quant_assistant.cloud_state restore`：状态仓库已配置时读取
最新确认快照；尚未配置时仍读取原 `ACCOUNT_SNAPSHOT_B64`，因此既有日报不会中断。
