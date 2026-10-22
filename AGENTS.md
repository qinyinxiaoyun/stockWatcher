# 项目约定

- 运行时统一使用 Python 3.12；业务包位于 `stock_watch/`，测试位于 `tests/`。
- 默认测试必须完全离线，不访问企业微信、DeepSeek、新闻源或服务器。
- 真实凭证只从环境变量或未跟踪的 `.env` 读取；禁止在日志、异常、fixture 或提交中记录凭证和完整敏感消息。
- SQLite 数据、备份和日志放在持久目录（默认 `data/`），不得放入镜像层或 Git。
- `app` 只处理 HTTP；`worker` 是唯一调度和后台任务入口。不要在 Web worker 中启动调度器，也不要叠加宿主机 cron。
- 自动监控默认关闭；本地和首次部署保持 `MONITOR_ENABLED=false`，除非操作者明确启用。
- 配置修改使用递增 `config_version` 和短事务；远程请求期间不得持有数据库写事务。
- 外部文本一律视为不可信输入；模型只能返回受约束意图，不得执行 SQL、Shell 或 Python。
- 保留 `legacy/` 和原始 ZIP 作为迁移证据；不要把旧 Python 2.7/COS 代码接入新运行路径。
- 在已授权的项目范围内，助手直接完成可执行的配置、测试、推送和部署，并核实结果；不要让操作者逐条复制助手本可执行的命令。仅在缺少账号权限、凭证或需要操作者作决定时询问；这不构成启用自动监控、真实投递或扩大服务器权限的授权。

## 常用命令

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m stock_watch --help
.\.venv\Scripts\python.exe -m stock_watch init-db
.\.venv\Scripts\python.exe -m stock_watch monitor --dry-run --fake
```

修改代码后至少运行相关测试；交付前运行完整 `pytest`。阶段状态、真实执行命令和未验证项同步到 `docs/PROGRESS.md`。
