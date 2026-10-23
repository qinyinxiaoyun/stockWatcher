# 自建股票新闻监控

Python 3.12 + SQLite + 企业微信的单机新闻监控服务，支持组织内成员独立订阅及原有全局模式。HTTP app 接收管理指令和推送请求；独立 worker 处理持久任务、回执、新闻分析及唯一的调度入口。原始 ZIP 与 `legacy/` 是迁移参考，新运行路径不依赖腾讯云函数、COS 或 ServerChan。

## 本地启动（Windows PowerShell）

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.\.venv\Scripts\python.exe -m stock_watch init-db
.\.venv\Scripts\python.exe -m stock_watch demo
.\.venv\Scripts\python.exe -m stock_watch monitor --dry-run --fake
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m stock_watch serve
```

开发 HTTP 服务监听 `http://127.0.0.1:8000`。`GET /health` 查看存活和运行 SHA；`GET /ready` 查看 SQLite/schema 就绪。没有真实凭证也能启动、查看配置并运行上述离线演示。`MONITOR_ENABLED` 默认为 `false`。

需要本地修改配置时：

```powershell
.\.venv\Scripts\python.exe -m stock_watch config
.\.venv\Scripts\python.exe -m stock_watch command "添加方向 半导体设备"
.\.venv\Scripts\python.exe -m stock_watch command "设置关注 603986.SH 存储芯片涨价、库存、业绩预告"
.\.venv\Scripts\python.exe -m stock_watch status
```

`demo` 使用独立临时数据库和模拟接口，展示配置实际进入检索、分析及投递记录；不会修改正式 `data/`。`--dry-run` 将正式数据库快照复制到临时库，不修改正式 seen 或任务，也不发消息。默认模型与新闻均模拟；显式 `--real` 才访问真实来源。成员模式下 `--dry-run --real` 可以抓取新闻，但 DeepSeek 仅预览已有分析缓存，不新增模型调用；无缓存的任务保持待处理。

独立订阅的实现、迁移及生产状态见[独立订阅计划与使用](docs/INDEPENDENT_SUBSCRIPTIONS.md)。成员分别维护股票、方向、关注点和推送等级；相同公开分析需求复用AI结果，通知各自投递和重试。数据库升级保留旧全局模式，只有可信运维显式指定旧配置拥有者后才切换；新成员默认空订阅。

当前成员已通过组织微信插件接通个人微信，文字通知接收与“查看监控”双向回复均已真实验收；复用原企业微信后端，无需重复订阅或重复发送。接入和排查见[个人微信接收](docs/PERSONAL_WECHAT.md)，其他成员须分别准入和验收。

## 成员新闻质量策略（schema4）

本节描述独立订阅流程；旧全局模式继续兼容，升级数据库不会自动切换模式或启用监控。报道按来源身份及规范URL归一，保留原始观察；检索摘要、空格及追踪参数变化不单独成为新事实。明确金额更正、撤销或实施等实质修订可重新评估。跨URL事件只合并主体、类型、日期和规范事实标题等证据一致的确切事件，不凭标题相似度合并。

- 纯资金流、融资融券日报、涨跌成交回顾、技术指标及普通目标价模板先过滤，不调用新闻模型；混有业绩、订单、供需、重大减持或经营风险事实的材料继续判断。
- 普通实时事实有效期为可信披露时间起2小时。隔夜经营事实在09:00开始收集分析，09:10至09:15前最多发一次晨间摘要、最多3个事件；疑似重大风险须有披露证据才可使用24小时例外。未知、冲突或未来超过5分钟的来源时间先待核对。
- 普通重要事件按成员在半小时边界合并，每30分钟最多一条摘要、每条最多3个独立事件；超额候选继续排队但不延长有效期。重大事件及时入队，发送每段前仍复查资格及固定到期时间。
- 每轮最多10个新逻辑分析任务，同一目标及关注需求复用结果；不同目标分别判断。DeepSeek每批最多5项，仅合并相同组织和兼容隐私域，重大预留任务与普通任务分批；逐项校验、缓存和恢复失败项。
- 默认新闻预算为每小时60,000、北京时间自然日300,000 token，其中20%保留给疑似重大任务。可通过 `NEWS_HOURLY_TOKEN_BUDGET`、`NEWS_DAILY_TOKEN_BUDGET`、`NEWS_MAJOR_RESERVE_FRACTION` 调整。供应商实际输入、输出、缓存命中/未命中和总token单独记录；无usage的尝试保持未知，保守估算只用于预算收费，不冒充实耗或费用账单。
- 失败状态独立持久保存，首次、第二次失败后分别冷却5、15分钟，第三次及以后冷却60分钟再恢复；同公共任务同轮失败不重复请求，预算不足不写成已完成。真实新闻检索通常只用主词，每15分钟补查全部别名/主题关键词；主词结果为空或请求失败时立即回退完整查询，主词非空也不取消定期补查。

这些规则可由合成材料离线验证；真实模型质量和token节省仍须受控对照及自然轮次观察，不据此宣称已达到50%节省目标。详细语义见[架构](docs/ARCHITECTURE.md)。

## 管理与运维

```powershell
.\.venv\Scripts\python.exe -m stock_watch --help
.\.venv\Scripts\python.exe -m stock_watch --env-file .env worker --once --fake
.\.venv\Scripts\python.exe -m stock_watch test-push --to offline-user
.\.venv\Scripts\python.exe -m stock_watch backup backups\local-check.db
.\.venv\Scripts\python.exe -m stock_watch import-seen path\to\seen.json
.\.venv\Scripts\python.exe -m stock_watch retry-outbox 1
.\.venv\Scripts\python.exe -m stock_watch backfill --target 603986.SH --hours 24
```

`--env-file` 必须显式指定，已有环境变量优先；示例配置见 `.env.example`。真实 `test-push --real --to 指定成员` 仅在操作者准备好凭证并希望发送该消息时运行。`monitor --real` 会抓取新闻并调用模型，成功结果写入 outbox；运行 worker 后再投递。模拟 `monitor` 和 `worker --fake` 自动使用临时数据库快照，既不消耗模型额度，也不修改正式 seen、配置或投递状态。

新增/恢复目标默认只处理生效后的新闻。需要补查时使用 `backfill` 指定唯一启用目标和1～24小时窗口，最多分析10个逻辑任务；默认模拟预览。真实来源预览加 `--dry-run --real`，成员模式仍只复用模型缓存；真实补查写入正式待投递队列还须显式 `--real --ack-max-notifications 10`，表示确认可能新增最多10条通知。成员模式还必须指定 `--member USERID`。已有相同新闻的投递记录不会重新入队，补查不自动扩大成员或收件范围。

## 容器结构

`compose.yaml` 固定项目名 `stock-watch`，包含 app、worker 和 caddy。app/worker 同镜像、同持久数据目录，app 一个 Web worker；生产只通过 Caddy 对外开放 80/443。环境变量替换由 Compose 的 `--env-file` 提供，容器变量由 `env_file` 单独注入，两者不可互相替代。

本地 Docker 可仅选择两个服务，跳过 Caddy：

```powershell
docker compose --env-file .env -f compose.yaml -f compose.dev.yaml up --build app worker
```

`.env` 中保持 `MONITOR_ENABLED=false`。容器内数据库固定 `/data/stock-watch.db`，Linux bind 目录需允许 UID/GID 10001 写入。不要通过移除只读根文件系统或其他安全限制解决目录权限。

阶段 0～2 已完成本地模拟验收，阶段 3 已完成首次服务器部署及备份/容器重建验证；真实微信与模型联调另行验收。阶段 4 使用 [GitHub 构建与镜像部署](docs/GITHUB_DEPLOY.md)，服务器只拉取已构建镜像。实际执行状态见 [进度](docs/PROGRESS.md)，微信联调见 [微信配置](docs/WECOM_SETUP.md)，数据语义见 [架构](docs/ARCHITECTURE.md)。
