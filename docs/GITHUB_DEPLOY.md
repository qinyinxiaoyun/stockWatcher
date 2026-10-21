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
| `DEPLOY_USER` | 当前入口要求 root；后续可另行收紧为受限部署用户 |
| `DEPLOY_ENABLED` | 先保持未设置；验收和凭证准备完毕后设为 `true` |

设置 Secrets：

| 名称 | 内容 |
| --- | --- |
| `DEPLOY_SSH_KEY` | 仅供该仓库部署的专用 SSH 私钥，不复用个人 SSH 私钥 |
| `DEPLOY_KNOWN_HOSTS` | 已通过现有可信 SSH 连接核对的服务器公钥记录；非 22 端口使用 `[host]:port` 格式 |

GHCR 登录使用工作流短期 `GITHUB_TOKEN`，不要求另存长期 PAT。远端部署临时 Docker 登录目录在任务结束时清理。企业微信、DeepSeek、推送凭证只保存在服务器共享 `.env`，不上传到 GitHub。

首次部署保持 `MONITOR_ENABLED=false`。该开关不会禁止 worker 消费已有管理任务和 outbox；脚本会检查待消费队列，非空时停止，只有操作者已核实并通过受控入口显式提供 `WORKER_QUEUE_APPROVED=true` 才能继续。GitHub 工作流默认不提供此豁免。

## 部署入口及持久目录

镜像配置为 `compose.registry.yaml`，无服务器 build。`scripts/deploy-image.sh` 接收镜像 digest 和完整代码 SHA，发布文件放在 `/srv/stock-watch/image-releases/<sha>/`。共用项目名 `stock-watch`、锁 `/srv/stock-watch/shared/deploy.lock`、环境文件 `/srv/stock-watch/shared/.env`、数据目录 `/srv/stock-watch/shared/data/` 以及 UID/GID 10001。

脚本校验输入和镜像 revision，拉取镜像后才停止旧 app/worker，先备份 SQLite 再执行初始化/迁移，然后验证 app 版本与 worker 的本轮心跳。只有验收通过才记录镜像部署成功。镜像发布记录独立于源码首部署的 `current` 指针，不覆盖首部署配置或 Nginx。

失败时不自动恢复数据库。只有数据库 schema 未改变且旧镜像记录可用时才允许尝试恢复旧应用；首次从源码部署切换、schema 变化或无法证实兼容时需要单独处理。数据库恢复会影响已投递状态和配置，必须另行审核，不能把应用版本回退等同于数据恢复。

## 当前验收边界

本地离线测试、Compose 解析、GitHub 实际构建、首次镜像部署和回退演练分别记录在 `docs/PROGRESS.md`。文件已编写不代表服务器已部署。首次服务器部署与真实企业微信/DeepSeek/新闻源联调仍分别验收，自动监控须由操作者明确启用。

官方依据：[GitHub 镜像构建与发布](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)、[GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。
