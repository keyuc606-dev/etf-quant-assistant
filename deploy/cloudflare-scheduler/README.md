# Worker 部署

请按 [生产调度最短步骤](../../docs/PRODUCTION-SCHEDULER.md) 操作。默认 Workers Free；只需两个 Cron 和 Worker Secrets，无 Google Cloud。

本目录 `node --test worker.test.mjs`，`node --check worker.mjs`；无需第三方测试依赖。wrangler.toml 定义完整 UTC Cron；控制台部署需手工设置同样两条。不要同时创建两套 Worker。
