# GitHub 构建与镜像部署

仓库：<https://github.com/qinyinxiaoyun/stockWatcher>。本方案替代阶段 4 原先计划的服务器 bare Git 构建入口；服务器只拉取 GitHub 构建的镜像，调度仍仅由应用 worker 执行。

## 工作流

`.github/workflows/build-deploy.yml` 在 PR 上执行 Python 3.12 离线测试；推送 `main` 或在 `main` 手动触发后，先测试，再构建 amd64/arm64 镜像到 GHCR。镜像带完整提交 SHA 和 OCI revision 标签。发布后用禁止联网的容器验证初始化，并打印容器 SQLite 版本；通过后才允许进入部署。

生产部署使用实际镜像 digest，不依赖可变的 latest 标签。官方 Actions 固定到已核验的提交。PR 不获得发布或生产凭证；生产部署串行执行，不中断已经运行的部署。开始更新前再核对 main 最新 SHA，跳过已被更新提交替代的旧任务，避免构建完成顺序造成版本倒退。`DEPLOY_ENABLED` 未设置时只构建，跳过服务器更新。

## 首次设置（由执行部署的助手完成可访问的部分）

先完成 `docs/DEPLOY_FIRST_TIME.md` 的服务器验收：现有 Nginx/证书可用，共享 `.env` 已建立，app 只通过 `127.0.0.1:8000` 接入 Nginx。不要同时启用 `compose.yaml` 的 Caddy。

`DEPLOY_ENABLED` 必须设为**仓库级** Variable（job 条件求值时环境级变量尚不可用）。其余 Variables 可设在仓库或 `production` Environment：

| 名称 | 值 |
| --- | --- |
| `DEPLOY_HOST` | 服务器主机名或 IPv4 |
| `DEPLOY_PORT` | SSH 端口，默认 22 |
| `DEPLOY_USER` | root，专用密钥必须配置固定 forced command，不能获得通用 root shell |
| `DEPLOY_ENABLED` | 先保持未设置；验收和凭证准备完毕后设为 `true` |

设置 Secrets：

| 名称 | 内容 |
| --- | --- |
| `DEPLOY_SSH_KEY` | 仅供该仓库部署的专用 SSH 私钥；服务器以 `restrict,command="/usr/bin/python3 -I /srv/stock-watch/deploy-tools/github-gate.py"` 限制用途，不复用个人 SSH 私钥 |
| `DEPLOY_KNOWN_HOSTS` | 已通过现有可信 SSH 连接核对的服务器公钥记录；非 22 端口使用 `[host]:port` 格式 |

GHCR 登录使用工作流短期 `GITHUB_TOKEN`，不要求另存长期 PAT。工作流只通过 SSH 的固定 `deploy` 命令发送有大小限制的结构化请求；服务器入口只接受本仓库的镜像digest、SHA、合法账号和临时拉取凭证，拒绝其他命令、任意文件上传及额外字段。远端临时 Docker 登录目录由入口结束时清理。企业微信、DeepSeek、推送凭证只保存在服务器共享 `.env`，不上传到 GitHub。

服务器受控入口 `deploy/github-gate.py` 与固定的 Compose/部署脚本由助手通过已有可信管理连接安装到 `/srv/stock-watch/deploy-tools/`，必须为root所有且组/其他用户不可写。GitHub密钥无法更改这些部署工具，也不能运行SSH shell、SCP、SFTP或端口转发；工具更新须经管理连接重新审核安装。镜像中的业务代码仍以UID10001运行。

首次部署保持 `MONITOR_ENABLED=false`。该开关不会禁止 worker 消费已有管理任务和 outbox。脚本在停止服务之前检查队列；默认发现待处理任务便拒绝本次发布，旧服务继续运行，待队列正常处理后可重试失败的部署任务。GitHub受限入口不能提供队列豁免；确需让新版本消费已有队列时，仍须经可信管理连接单独审核该次手动执行的 `WORKER_QUEUE_APPROVED=true`。

已有受信镜像发布记录时，停前还须确认旧 app/worker 都在运行、健康、镜像及版本一致，且数据库存在；原本停止/不健康的服务不会被发布脚本擅自启动。没有发布记录却存在该项目容器时拒绝自动接管，交由可信管理连接处理。首次没有容器、没有数据库的空部署仍可初始化。

## 部署入口及持久目录

镜像配置为 `compose.registry.yaml`，无服务器 build。`scripts/deploy-image.sh` 接收镜像 digest 和完整代码 SHA，发布文件放在 `/srv/stock-watch/image-releases/<sha>/`。共用项目名 `stock-watch`、锁 `/srv/stock-watch/shared/deploy.lock`、环境文件 `/srv/stock-watch/shared/.env`、数据目录 `/srv/stock-watch/shared/data/` 以及 UID/GID 10001。

脚本校验输入和镜像 revision，核对旧服务身份及队列后才停止旧 app/worker，先备份 SQLite 再执行初始化/迁移，然后验证 app 版本与 worker 的本轮心跳。停前会保存数据库 `user_version` 和 SQLite 表/索引/视图/触发器定义的稳定结构指纹；备份检查完整性，迁移前再次比对结构。只有验收通过才记录镜像部署成功。镜像发布记录独立于源码首部署的 `current` 指针，不覆盖首部署配置或 Nginx。

失败时不自动恢复数据库。只有数据库版本及结构指纹都未改变、旧镜像记录可用、且停前旧服务已验证在运行时，才允许恢复旧 app/worker。停止期间新进入的队列交回该旧版本处理，不因它非空而把原先正常运行的 worker 留在停止状态；这不等于批准候选版本消费队列。备份、就绪检查、心跳或发布记录写入失败同样走该恢复路径，发布元数据及 `current-image` 恢复到尝试前状态。

schema 变化（包括已提交 DDL 但版本号尚未更新）或无法证实兼容时，不盲目回退旧应用；正式数据不从备份自动覆盖。数据库恢复会影响已投递状态和配置，必须另行审核，不能把应用版本回退等同于数据恢复。具体真实/隔离演练结果见进度文档，不把离线测试写成生产故障演练。

### 独立恢复演练工具

`scripts/verify-deploy-recovery.py` 在 Linux 管理连接下以 root 运行，需要本机已有两份不同版本的不可变 GHCR 镜像及对应完整 SHA。入口为 `python3 scripts/verify-deploy-recovery.py --old-image <digest引用> --old-sha <完整SHA> --new-image <digest引用> --new-sha <完整SHA>`；若部署脚本不在同目录，用 `--deploy-script` 指定已审核文件。

工具只替换待测脚本的固定根目录和 Compose 项目名，其余分支保持原样；使用随机 `stock-watch-recovery-*` 项目、临时数据库和无生产凭证的环境。容器禁网、不开端口，app/worker 命令替换为本地就绪/心跳 fixture，初始化仍使用真实镜像代码。覆盖停前队列拒绝、候选就绪失败回退、停止时新入队、原 worker 已停止、成功发布保留数据共五项。结束清理自身测试容器和临时业务文件，仅保留非敏感 `summary.json`；不构建镜像、不覆盖正式库、不向生产服务注入故障。部署脚本会拉取给定镜像，因此 Docker/GHCR 控制面并非完全离线；业务 fixture 没有网络访问。

## 当前验收边界

2026-10-22已实际跑通GitHub测试、双架构镜像构建和受限SSH自动部署；公网版本、app/worker健康、监控关闭、数据库与升级前备份完整性均已核对。Windows上传的SSH凭证统一使用LF，工作流也会清除CR，避免密钥解析失败。具体提交、工作流和未完成项见进度文档。

本地离线测试、Compose 解析、GitHub 实际构建、首次镜像部署和回退演练分别记录在 `docs/PROGRESS.md`。文件已编写不代表服务器已部署。首次服务器部署与真实企业微信/DeepSeek/新闻源联调仍分别验收，自动监控须由操作者明确启用。

官方依据：[GitHub 镜像构建与发布](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)、[GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。
