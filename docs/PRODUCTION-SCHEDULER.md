# 免费生产调度：Cloudflare → GitHub Actions

默认方案为 Cloudflare Workers **Free** 的两个 Cron，只调用 GitHub workflow_dispatch。Python、行情、Telegram、private state、CI 均留在 GitHub。无需 Google Cloud、结算账户、固定月费或域名。旧 Google 部署入口已移除；deploy/*gcp.sh 与 Dockerfile 仅保留历史参考，不要执行。原方案尚未启用，因此无需迁移云端状态。

## 最短部署（本人操作一次）

1. 创建 Cloudflare 免费账号，进入 Workers & Pages，确认 Workers Free；不要升级 Paid 或添加付费资源。无需购买域名。
2. 在 GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens 创建 token：Resource owner 为 keyuc606-dev；Only select repositories 仅选 etf-quant-assistant；Repository permissions 仅加 **Actions: Read and write**（Metadata read 自动附带）。设置有效期并记住到期轮换。token 只填 Cloudflare Secret，**不要发给 ChatGPT，也不要写进仓库**。无需提交 Telegram token 或重新配置已有 repo secrets。
3. Cloudflare → 创建 Worker，命名 etf-free-scheduler → Edit code，将 deploy/cloudflare-scheduler/worker.mjs 全文替换进去 → Deploy。Worker Settings → Variables and Secrets → Add secret，名称 `GITHUB_TOKEN`，填刚创建的 token。再添加一个本人随机生成的长随机 Secret `VERIFY_TOKEN`，仅用于验证 endpoint。
4. 先验证（见下节），通过后 Worker Settings → Triggers → Cron Triggers 添加两条：`30 2 * * 1-5` 和 `0 6 * * 1-5`。都是 **UTC**，分别对应北京时间周一至周五 **10:30 / 14:00**；节假日由 Python 的真实 A 股日历静默阻断。生效传播最多可能需要15分钟，不要临近开盘才部署。
5. 保持 Workers Free。查看 Worker 的 Logs/Observability 和 GitHub Actions 的对应运行。设置 token 到期提醒；到期后只替换 Cloudflare Secret。

如熟悉终端，也可在本目录运行：`npx wrangler login` → `npx wrangler secret put GITHUB_TOKEN` → `npx wrangler secret put VERIFY_TOKEN` → `npx wrangler deploy`。Wrangler 可能提示首次创建；按提示先 deploy 创建 Worker 后立刻设置两个 Secret。配置内两个 Cron 会一起生效；务必至少提前一天完成。不要把 secret 放命令参数或 wrangler.toml。首次使用需本机 Node.js 和 Wrangler，本人浏览器授权即可。

## 先验证，绝不试发真实交易建议

GitHub Actions → Account Morning Execution Telegram → Run workflow：选择 main、session=morning、**dry_run=true**；尾盘同理 session=closing。validate 应成功，notify 必须显示 skipped。dry-run 仅运行离线安全回归，不读 private state、不访问 Telegram、不修改持仓。故意在早盘 workflow 选择 closing 应验证失败。

再在本人的终端向 Worker 发 **POST**：`https://你的Worker.workers.dev/verify?session=morning`，请求头 `Authorization: Bearer 本人的VERIFY_TOKEN`；closing 同理。不要将含 token 的命令或截图发给他人。HTTP 200 和 accepted 只说明 GitHub 接受请求；仍需去 Actions 确认 validate 成功、notify skipped。GET/其他路径404、无凭证401、无效session400、GitHub失败502。该 endpoint 永远强制 dry_run=true，没有生产HTTP入口。

PowerShell 示例（仅本人本机执行，粘贴不会回显）：

```powershell
$verifySecret = Read-Host 'VERIFY_TOKEN' -AsSecureString
$verifyCredential = [pscredential]::new('verify', $verifySecret)
$verifyHeaders = @{ Authorization = 'Bearer ' + $verifyCredential.GetNetworkCredential().Password }
Invoke-RestMethod -Method Post -Uri 'https://你的Worker.workers.dev/verify?session=morning' -Headers $verifyHeaders
$verifyHeaders.Clear()
Remove-Variable verifySecret,verifyCredential,verifyHeaders
```

## 安全边界与可观测性

- Worker 固定仓库、main、workflow与session映射，不执行策略；未知cron不触发。Secret仅用于GitHub API。非2xx、网络失败会抛错并记录dispatch_failed；不会记录token或API响应体。故障无自动重试，避免已接受请求被盲目重试；GitHub侧仍做最终幂等。
- 两个workflow显式检查session与入口一致，共用 account-cloud-state 并发锁，生产仅main且dry_run=false。手动灾备默认dry_run=true；实际灾备需本人主动取消该选项，并受同样门禁。
- Python继续校验真实交易日、早盘10:30–11:30、尾盘14:00–14:15:00；14:15:00之后禁止当天执行建议。生成、写入、发送环节已有截止保护；不会收盘后补发。quote超过300秒不给强动作。
- 私有state的date+session预订、SHA CAS及回读保留。重复抑制；sending/含糊结果失败关闭，核对原日志与Telegram回执前绝不删除key重发。发送无动作健康推送；非交易日/未知日历静默。旧state、09:20历史和monthly review不改。
- Worker成功不代表策略已送达。请检查GitHub步骤日志、execution_deliveries原运行记录及Telegram message IDs；Worker日志不存账户数据。关闭Cron可暂停生产；不要删除private state。GitHub Actions可能排队；外部Cron并不提供分钟级SLA，迟到会安全跳过。
- 旧生产GitHub schedule不恢复。account-trade-telegram.yml原有每5分钟轮询是交易反馈，保持不变。cron-job.org仅为可选人工备用，不默认启用；替换主触发源前先关闭Cloudflare Cron，避免双生产源。

## 免费额度与避免费用

核验日期2026-10-17：[Cloudflare限制](https://developers.cloudflare.com/workers/platform/limits/)为Free每账号5个Cron、每天100,000请求、10ms CPU。本方案用2个Cron，每工作日2个GitHub请求，不用KV、D1、Queues、付费日志导出或Cloudflare Workflows。I/O等待不等同CPU执行。账号已有Cron计入同一额度。普通内置日志用于排错，不依赖付费监控。

[GitHub计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)：公共仓库标准GitHub hosted runner免费；私有仓库受账号免费分钟与存储总额度约束，所有workflow（尤其每5分钟反馈轮询）合并计算。请在Billing确认仓库可见性、用量与预算，关闭超额付费/设支持的停止使用预算，勿启用付费runner。系统不创建付费资源；不能保证用户其他项目用量或未来平台政策永远不变。

官方：[Cron与UTC](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)、[Free定价](https://developers.cloudflare.com/workers/platform/pricing/)、[GitHub dispatch与最小Actions write权限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。

## 下一交易日验收

2026-10-19（周一，最终以真实A股日历门禁为准），分别在北京时间10:30和14:00检查Worker Cron日志、GitHub run创建/开始时间与对应session。确认正常交易日即使无动作也有健康推送，状态各只有一个date+session记录；报价陈旧只降级，无强动作。14:15之后不得补发当天执行建议。休市日应无Telegram。勿用改系统时间、假行情、假成交或清空幂等记录做线上测试。

## 历史事故

2026-10-15的GitHub scheduled事件曾延迟约5小时，run 38867339300/38867451450与38892604980/38893301826在盘后运行。此事推动移除盘中production schedule；后续Google Cloud方案因用户零成本约束取消。安全截止与幂等继续保留。
