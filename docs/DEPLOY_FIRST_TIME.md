# 阶段 3：首次部署与现有 Nginx 接入

## 当前服务器与入口

2026-10-21 操作者确认 Ubuntu20.04.6、x86_64、1核1GB、Docker28.1.1、Compose2.35.1，已存在 Nginx、MySQL 和 trojan-go。首次构建、app/worker健康检查、公网HTTPS、容器重建持久化、在线备份及临时目录恢复已通过，实际测试结果见 PROGRESS.md；操作系统维护升级、整机重启和资源峰值验收另行安排。

本机通过已配置的 `stock-server` SSH 别名连接，严格检查已有主机密钥，不读取或打印私钥。域名为 `stock.janettepa.uk`。复用宿主机 Nginx，使用独立的 `/etc/nginx/conf.d/stock-watch.conf`，原有 `www.janettepa.life` 站点不变。本部署模式使用 `compose.host-nginx.yaml`，不要同时启动原 Compose 中占用80/443的 Caddy。

证书由现有 acme.sh 使用 webroot `/srv/platform/acme` 签发，正式副本位于 `/srv/platform/certs/stock.janettepa.uk/`，续期 hook 为 `nginx -t && systemctl reload nginx`。HTTP 校验路径保留，其余 HTTP 请求跳转 HTTPS。证书签发及 hook 已执行，不等于自动续期已演练。Cloudflare 代理在源站验收前保持 DNS only，重新开启时使用 Full (strict)，并验证回调无需浏览器挑战。

## 发行包与部署入口

本地 Python3.12：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/package-release.py
```

打包器要求业务源码相对 HEAD 无修改，只收录明确列出的 Python 源码、Dockerfile、项目元数据和部署文件，不收录 `.env`、私钥、数据库、日志、虚拟环境、ZIP 或 legacy。输出位于已忽略的 `dist/`，包含归档 SHA256；归档内部 `release-manifest.json` 同时记录代码 SHA 和逐文件 SHA256。未提交部署文件的精确内容由 manifest 识别，不能只凭代码 SHA 判断整个部署包相同。

将归档与 `.sha256` 上传到 `/srv/stock-watch/incoming/`，在该目录执行 `sha256sum -c <归档>.sha256`。解包到 `/srv/stock-watch/releases/<完整代码SHA>/` 后：

```bash
bash -n scripts/deploy-host-nginx.sh
bash scripts/deploy-host-nginx.sh <完整代码SHA>
```

入口会检查证书、Nginx、磁盘余量与部署锁；构建同一 app/worker 镜像，在初始化迁移前备份已有 SQLite，发现历史待处理队列时停止自动启动 worker，等待操作者核对。容器和 HTTPS 就绪通过后才更新 `current` 软链接。Nginx 更新仅限专用站点文件，检查或 reload 失败时恢复原文件。此入口支持串行手动部署，不提供数据库自动回滚或零停机升级保证。

共享环境文件为 `/srv/stock-watch/shared/.env`（root、0600）；首次不存在时创建空外部凭证模板并在服务器生成独立 PUSH_TOKEN，禁止输出该文件。Compose 插值与 env_file 使用同一个共享环境文件。脚本要求 `MONITOR_ENABLED=false`，不会自动启用监控。`.env` 中现有环境配置优先级与修改后的容器重建需在联调时核对。

数据固定 `/srv/stock-watch/shared/data/`，UID/GID10001；备份在其 `backups/`，企业微信验证文件放 `verification/`。app 仅发布 `127.0.0.1:8000`；worker 无发布端口。容器为只读根文件系统、临时 tmpfs、非 root、丢弃 capabilities、日志轮转，每个容器内存上限192MiB。上限不是实测峰值；默认不增设数据库服务或监控面板。

项目 Nginx 及 Gunicorn 不记录完整访问 URL，避免回调查询串进入访问日志；容器保留应用结构化运行日志。排查优先使用健康状态、版本、队列数量和错误类型，不粘贴完整请求或环境配置。

## 运维与未完成事项

SSH 登录后，在 `/srv/stock-watch/current/` 设置以下非敏感运维变量（每次新终端都需设置）：

```bash
export BUILD_SHA=$(basename "$(readlink -f /srv/stock-watch/current)")
export STOCK_WATCH_ENV_FILE=/srv/stock-watch/shared/.env
export STOCK_WATCH_DATA_DIR=/srv/stock-watch/shared/data
docker compose --env-file "$STOCK_WATCH_ENV_FILE" -f compose.host-nginx.yaml ps
docker compose --env-file "$STOCK_WATCH_ENV_FILE" -f compose.host-nginx.yaml exec -T worker python -m stock_watch status
```

真实企业微信、DeepSeek、新闻接口仍需专用凭证与指定成员联调；自动监控开关只控制新闻调度，worker 仍会消费管理任务和投递队列。首次容器重建与临时目录备份恢复已验证，异地备份、整机重启、实际资源峰值、旧系统停用与最终 seen 导入仍需分别验收。阶段4改用 [GitHub构建与受限镜像部署](GITHUB_DEPLOY.md)，实际部署和回退演练状态分别记录在进度文档。
