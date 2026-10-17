# 盘中推送调度迁移（2026-10-16）

## 事故证据

所有时间为北京时间。GitHub 的 run 创建与启动时间相同，说明主要延迟发生在 schedule 事件到达之前，不是 Python 计算排队五小时。

|运行|创建时间|实际行为|
|---|---|---|
|38867339300|15:27:33|早盘入口15:27:53开始；15:28:58发降级正文144／附件145，随后仍运行ETF V2推送|
|38867451450|15:28:35|备用早盘触发，读取当日morning已发送记录，抑制重复|
|38892604980|19:15:43|尾盘入口19:16:06开始；发降级正文148／附件149|
|38893301826|19:22:06|备用尾盘触发，读取closing已发送记录，抑制重复|

早盘目标10:30，主事件晚约4小时58分；尾盘原目标14:10，晚约5小时6分。备用事件同样迟到。原策略没有被当成强动作发送，但“成功完成”的降级通知和附带V2卡依然在收盘后打扰用户。

## 方案选择与成本

用户确认没有现成云环境。本机没有云平台CLI／云授权配置；已有GitHub仓库写权限和现有账户状态／Telegram secrets。

|方案|可靠性与网络|成本与复杂度|状态及密钥|结论|
|---|---|---|---|---|
|GitHub schedule|本次连续数小时延迟；备用触发同样受影响|无需新账户|现有secrets和私有状态|退出盘中生产路径|
|VPS + systemd timer|可控；日本节点可直接出站GitHub／Telegram，但需维护操作系统与进程|常驻资源、补丁、监控和备份|需另迁密钥，磁盘可持久化|无现成VPS，运维负担较大|
|AWS EventBridge + Lambda／容器任务|关闭flexible window后分钟粒度；须验证行情出站|可低用量，但Lambda依赖打包／超时或容器网络权限更多|IAM／Secrets，复用GitHub私有状态|可行备选|
|Railway等平台cron|Python容器简单；仍需验证启动延迟与区域|计划额度及定价需按账户确认|平台变量；外部私有状态|没有已有账户优势|
|Cloud Scheduler + Cloud Run Jobs（选用）|指定Asia/Shanghai；东京运行Python，直接访问GitHub、公开行情和Telegram|无常驻服务器；两个scheduler job，按短任务计费|Secret Manager，GitHub OIDC短期授权；原私有状态继续为事实源|最符合此项目|

Google每个结算账户有三个免费Scheduler job额度，本方案使用两个；Cloud Run、镜像存储、Secret Manager及外网流量各自计费／免费额度独立，不能承诺总账单永久为零。首次仍需本人开通结算账户。建议设小额预算告警（告警并非硬性消费上限）。

官方依据：[定时执行Cloud Run Jobs](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule)、[Scheduler定价](https://cloud.google.com/scheduler/pricing)、[Cloud Run定价](https://cloud.google.com/run/pricing)、[Secret Manager](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets)、[Scheduler至少一次投递](https://docs.cloud.google.com/scheduler/docs/overview)、[AWS调度精度](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)。

东京任务的出站路径不经过用户电脑，也不依赖电脑开机；中国境内访问Google控制台／Telegram仍受当地网络条件影响。日本区域到具体行情源的可用性必须在部署后实测，尚未实测前不声称已验证。托管调度也不保证秒级到达，截止／新鲜度／幂等保护始终必需。

## 新的行为

- 10:30早盘；10:35、10:40备用事件。原早盘确认／条件执行规则保留，超过11:30不再补发早盘。
- 14:00尾盘；14:05、14:10备用事件。14:15:00之后拒绝当天执行。包含微秒，因此14:15:00.000001也拒绝。时钟以UTC+8（Asia/Shanghai现行北京时间，无夏令时）比较，不依赖本机TZ。
- 外层任务在恢复账户之前检查交易日／执行窗口；v3在生成前、持久化前、每次正文／附件发送前复查。计算跨过截止会失败关闭并记录`execution cutoff exceeded`。
- 每份正常执行卡显示实际有效quote timestamp范围（北京时间）。核心报价最多300秒；未来偏差最多30秒，缺少时区或不同日期都降级。生成期间报价老化也拒绝发送。
- 休市／交易日unknown静默。正常交易日无动作仍发简短健康卡；无可信报价发“本次不执行”降级卡或明确安全跳过日志。
- 15:00后默认不推送；离线builder被用于收盘后时只返回`REVIEW／下一交易日观察`，不生成强动作、不写正式执行session。没有新增自动收盘报告。
- 旧GitHub早盘／尾盘schedule全部移除，仅保留workflow_dispatch灾备；CI、交易反馈轮询及状态读取检查保留。交易反馈是用户主动成交记录回执，不是策略推送。
- 原随早盘追加的ETF V2推送从生产执行路径拆下，避免收盘后仍扫候选和推卡；V2 CLI、回测、模拟账户及历史兼容保留，手动推送也受早盘／截止保护。
- 所有主／备用／手动灾备使用同一私有状态`date:session` reservation。GitHub Contents SHA乐观锁是跨平台仲裁，不依赖GitHub workflow concurrency。独立UUID区分同一时刻的竞争者。竞争写失败者不能发送；sending状态遇到模糊回执不盲重试。
- 硬截止前已成功保存的有效基准可保留；若之后统计重算或发送阶段跨过截止则不发送。截止后开始或取完行情时才超时的任务不写正式基准／尾盘observations。没有改真实持仓、现金、交易事实或胜率定义。

## 首次开通：不需要任何secret值

1. 在[Google Cloud控制台](https://console.cloud.google.com/)本人登录，创建／选择一个项目并关联结算账户。这一步涉及账户和支付，必须由本人完成。
2. 打开该项目的Cloud Shell，执行以下代码（将`YOUR_PROJECT_ID`换成项目ID）：

```bash
git clone https://github.com/keyuc606-dev/etf-quant-assistant.git
cd etf-quant-assistant
bash deploy/bootstrap-gcp.sh YOUR_PROJECT_ID
```

脚本仅创建API、东京镜像仓库、三种服务身份及限定本仓库main的OIDC信任，secret容器不含值。记录最后打印的项目ID／项目数字编号即可；这两项不是secret，可交给代理继续部署，也可自行执行下一步。

3. GitHub Actions → `Deploy production scheduler` → Run workflow（main），填写项目ID和数字编号。它先跑完整pytest，使用GitHub已有secrets自动创建Secret Manager版本，部署两个job，执行只读state预检，通过后才开schedule。无需重填Telegram／private state密钥，不需要服务账号JSON密钥。

更新代码后重新运行部署工作流即可；生产镜像固定到Git commit SHA，不在每个交易时段临时拉main／安装依赖。镜像构建上下文白名单只含requirements和程序代码，账户／缓存／日志／venv／GitHub临时凭据均不会入镜像。

## 验收、可观察性与回滚

首次只读预检验证真实私有state读取／JSON解析（沿用大Git blob逻辑），不写state、不发Telegram、不执行交易。它不能代替行情源及消息回执的实际运行验收。

部署后下个真实交易日（若2026-10-19已部署完成，则当天）：

1. 北京时间10:30／14:00检查Cloud Scheduler执行日志和Cloud Run job开始时间；必须在各自窗口内。
2. Telegram收到一份主卡（无动作也有健康卡），显示报价时间；明细附件属于同次报告。没有额外V2卡。
3. 10:35／10:40、14:05／14:10应仅出现`Duplicate suppressed`。私有state中每个`date:session`最多一个sent；核对Telegram回执和commit SHA。
4. 14:15以后手动运行closing应只出现`execution cutoff exceeded`，没有Telegram、正式session或胜率新增。休市日没有消息。
5. 预订后崩溃／网络超时的sending记录需要核对原任务回执，禁止删key后盲重发。SHA冲突会失败关闭，不强行覆盖交易反馈更新。应在Cloud Run失败执行和日志中排查；跨系统无法保证Telegram exactly-once，当前取舍为防重复优先。

控制台查看Cloud Run Jobs的Execution／Logs；可按需创建失败／缺失通知的监控告警，当前没有自动为用户添加其他通知渠道。

回滚先暂停`etf-morning`和`etf-closing`两个Cloud Scheduler job，再用指定旧镜像重部署；保留截止保护，勿恢复旧生产cron。暂停后Scheduler仍计job额度；彻底停用可删除两个schedule与job。不要删除私有state或secret来回滚代码。
