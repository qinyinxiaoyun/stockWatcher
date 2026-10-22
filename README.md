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

`demo` 使用独立临时数据库和模拟接口，展示配置实际进入检索、分析及投递记录；不会修改正式 `data/`。`--dry-run` 将正式数据库快照复制到临时库，不修改正式 seen 或任务，也不发消息。默认模型与新闻均模拟；只有显式 `--real` 才访问真实新闻和 DeepSeek。

独立订阅的实现、迁移及生产状态见[独立订阅计划与使用](docs/INDEPENDENT_SUBSCRIPTIONS.md)。成员分别维护股票、方向、关注点和推送等级；相同公开分析需求复用AI结果，通知各自投递和重试。数据库升级保留旧全局模式，只有可信运维显式指定旧配置拥有者后才切换；新成员默认空订阅。

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

新增/恢复目标默认只处理生效后的新闻。需要补查时使用 `backfill` 指定唯一启用目标和1～24小时窗口，最多分析10条；默认模拟预览。真实预览加 `--dry-run --real`；真实补查写入正式待投递队列还须显式 `--real --ack-max-notifications 10`，表示确认可能新增最多10条通知。已有相同新闻的投递记录不会重新入队。

## 容器结构

`compose.yaml` 固定项目名 `stock-watch`，包含 app、worker 和 caddy。app/worker 同镜像、同持久数据目录，app 一个 Web worker；生产只通过 Caddy 对外开放 80/443。环境变量替换由 Compose 的 `--env-file` 提供，容器变量由 `env_file` 单独注入，两者不可互相替代。

本地 Docker 可仅选择两个服务，跳过 Caddy：

```powershell
docker compose --env-file .env -f compose.yaml -f compose.dev.yaml up --build app worker
```

`.env` 中保持 `MONITOR_ENABLED=false`。容器内数据库固定 `/data/stock-watch.db`，Linux bind 目录需允许 UID/GID 10001 写入。不要通过移除只读根文件系统或其他安全限制解决目录权限。

阶段 0～2 已完成本地模拟验收，阶段 3 已完成首次服务器部署及备份/容器重建验证；真实微信与模型联调另行验收。阶段 4 使用 [GitHub 构建与镜像部署](docs/GITHUB_DEPLOY.md)，服务器只拉取已构建镜像。实际执行状态见 [进度](docs/PROGRESS.md)，微信联调见 [微信配置](docs/WECOM_SETUP.md)，数据语义见 [架构](docs/ARCHITECTURE.md)。
