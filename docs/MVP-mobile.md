# MVP Mobile：Windows → Telegram 单向账户日报

## 数据流

```text
本地 SQLite 成交台账
        ↓
portfolio.json 兼容投影
        ↓
股票/ETF公开行情与新闻
        ↓
my-portfolio-daily.md
        ↓
Telegram 单向短消息
```

运行命令：

```powershell
.venv\Scripts\python.exe -m quant_assistant notify-daily
```

程序只从当前 Windows 进程环境读取 `TELEGRAM_BOT_TOKEN` 和
`TELEGRAM_CHAT_ID`。两项都不会写入配置文件；`TELEGRAM_CHAT_ID` 只接受一个
数字会话 ID，它就是本次单向推送的白名单目标。

命令会先按 SQLite 中最后确认的 opening snapshot 与 executions 恢复持仓数量、
平均成本和现金，再刷新行情与新闻。没有新成交不影响刷新；只有明确录入的成交才会
改变数量、成本和现金。Telegram 失败时日报文件仍保留，命令返回错误以便用户发现。

## GitHub Actions 云端日报 MVP

仓库为公开仓库，真实持仓不能提交。云端任务使用三个 Repository Secret：

- `TELEGRAM_BOT_TOKEN`：Telegram Bot 凭据；
- `TELEGRAM_CHAT_ID`：唯一允许接收日报的数字会话 ID；
- `ACCOUNT_SNAPSHOT_B64`：最后确认账户状态的 Base64 JSON，不含任何凭据。

本地每次确认新成交或出入金后，运行：

```powershell
.venv\Scripts\python.exe -m quant_assistant.cloud_snapshot export
Get-Content -Raw data\cloud-account-snapshot.b64 | Set-Clipboard
```

然后只把剪贴板内容更新到 GitHub 的 `ACCOUNT_SNAPSHOT_B64` Secret。导出文件已被
`.gitignore` 明确排除；它仍含真实持仓和现金，禁止提交、分享或粘贴到日志。

`.github/workflows/account-daily-telegram.yml` 使用 Python 3.12，支持手动触发，并在
工作日 `01:20 UTC`（北京时间 `09:20`）运行。每次任务从 Secret 恢复一份临时
`trading.sqlite3` opening snapshot，再复用现有 `notify-daily` 生成并推送日报；不上传
数据库、账户投影、报告或 Artifact，结束时清理敏感运行文件。

这是只读推送 MVP，不提供云端成交回写、Telegram 成交反馈或自动交易。它保存的是导出时的
最后确认账户状态，而不是持续写入的云数据库；账户发生变化后必须重新导出并更新 Secret。
