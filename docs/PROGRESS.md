# 项目进度

更新时间：2026-10-22（Asia/Shanghai）

## 已确认基线

- 当前目录起初没有 Git 仓库，只有 `CODEX_PROJECT_PROMPT.md`、`stockNews.zip` 和 `selfhost-wechat-push.zip`。
- 已先创建安全 `.gitignore`，再初始化本地 `main` 分支；没有需要保留的历史、remote 或未提交工作树。
- 已保留原始 ZIP，并解包到 `legacy/`：旧云函数是 Python 2.7 + COS seen；新闻源已是东方财富 JSONP；分析提示词仍写死三家公司；推送仍是 ServerChan。
- 自建推送服务已具备 `/push`、Bearer 鉴权、企业微信 token 缓存/刷新、UTF-8 分段、失败检查和 `/health`；缺少回调、SQLite 配置、worker 和调度。
- 旧推送测试全部是模拟响应，不能作为真实微信投递证据。

## 阶段 0～2：代码完成、离线模拟验证通过

- Python3.12工程已建立，保留旧接口；本地Git已有输入基线提交，服务器Git自动部署留在阶段4。
- SQLite schema v2、幂等迁移、默认三公司及原搜索别名、稳定目标ID、生效时间、全局推送等级、乐观版本、审计与确认机制已实现。空名单初始化不会恢复默认股票，v1暂停状态迁移保留。
- 东方财富JSONP、动态股票/行业查询、动态关注点提示词、重大/重要/普通分析、24小时/10条/15天规则、首次基线、旧24位ID兼容及seen导入已实现。
- 全局监控租约、计划槽唯一键、北京时间09:00至15:00边界、失败保留重试状态、事务outbox和可恢复分段投递已实现。显式单目标补查1～24小时，最多10条，真实写入需确认通知上限。
- 企业微信加密回调、企业/应用校验、管理员白名单、持久MsgId/事件去重、明确指令、成员上下文、异步自然语言结构校验、版本冲突及原子多操作已实现。配置/审计/inbox/回执同事务，管理回执只给发起人。
- 自然语言批量删除先确认范围与版本；模型失败不修改。领取任务与投递使用独立租约标识，重启恢复不会再次应用已完成修改。
- CLI已实现初始化、配置/指令、一次监控、dry-run、显式补查、worker、状态、测试消息、旧seen导入、online备份、dead消息重试及临时库演示。所有模拟monitor/worker自动隔离正式数据库，fake worker不调用真实模型。
- Compose固定stock-watch项目、app/worker同镜像同持久目录、Caddy证书固定卷、非root UID10001、只读根文件系统、日志轮转、health/ready/版本接口已配置。

## 实际执行的验证（2026-10-21）

| 检查 | 实际结果 |
| --- | --- |
| `.venv\\Scripts\\python.exe -m pytest -q` | **151 passed**，完全离线，包含加密新增方向→保存配置→下一轮实际检索/分析→模拟定向回执及预警闭环 |
| 原服务 `python -m unittest discover -s legacy\\selfhost-wechat-push -p test_relay.py -v` | **17 tests OK**，原始模拟推送基线 |
| `python -m compileall -q stock_watch` | 语法检查通过 |
| `python -m stock_watch init-db` | schema2、SQLite3.50.4、journal=delete、monitor_enabled=false |
| `python -m stock_watch demo` | 独立临时库：动态方向实际进入查询，后续轮次产生并模拟投递分析结果 |
| `python -m stock_watch monitor --dry-run --fake` / `backfill --target 603986.SH --hours 24` | 模拟预览通过，不改变正式seen/任务，不投递真实消息 |
| Compose CLI解析生产/开发配置 | 固定项目名stock-watch；生产app/worker无发布端口；开发override仅127.0.0.1:8000，Caddy80/443 |
| Git check-ignore | 真实.env、私钥、虚拟环境、数据库、备份、日志均被排除 |

环境：uv创建的CPython3.12.12，cryptography50.0.2、tzdata2026.5、pytest9.1.1；没有调用真实微信、DeepSeek或东方财富接口。当前SQLite3.50.4未含WAL-reset修复，已采用DELETE journal；容器SQLite具体版本仍待实际构建检查。

## 本地启动验收

- 业务代码提交：`3169b1bb4f2b9c75d56df1593b166d7856f6d041`；初始输入基线：`10ba929`。无remote，未推送。
- 已启动本地API：`http://127.0.0.1:8000`，实际监听PID12236（2026-10-21启动记录），仅绑定127.0.0.1。`GET /health` 返回code0/status=ok/version0.1.0，`GET /ready` 返回code0/status=ready；两者build_sha均为上述业务提交。
- 日志在已忽略的 `data/app.stdout.log` 与 `data/app.stderr.log`。本地SQLite schema2、monitor_enabled=false，正式outbox/管理任务/监控轮次均为空。
- `python -m stock_watch worker --once --fake` 实际执行成功；其心跳和队列处理只发生于临时快照，正式库无任务/投递副作用。未启动常驻真实worker；正式worker心跳因此不应被标为已验通。
- 自动审批拒绝了常驻普通worker启动：即使MONITOR_ENABLED=false，普通worker仍可能消费正式队列并调用DeepSeek/投递企业微信，与本轮不调用真实外部接口的边界不符。已采用只启动API、隔离模拟worker的安全替代，无需为本轮继续开发操作平台后台。
- 本次启动是本地开发服务，不是生产WSGI/容器/HTTPS部署；文档后续提交不改变当前运行的业务代码SHA。

## 阶段 0～2 当时的执行边界（历史记录）

- 不部署生产服务器，不启用真实定时监控，不停用旧云函数，不发送真实微信消息，不消耗 DeepSeek 额度。
- 阶段 3/4 的真实服务器安装、迁移切换和 Git 自动部署留待后续阶段。

## 阶段 0～2 结束时的待验证项（历史记录）

- 企业微信 CorpID/AgentID/Secret、回调 Token/AES Key、管理员与测试收件人。
- HTTPS 域名、企业微信后台回调保存、客户端双向消息、DeepSeek 与东方财富真实网络表现。
- 服务器资源、Docker Compose、重启持久化和真实迁移切换。
- 本机Docker CLI可用，但Docker daemon未运行；本轮未构建/运行容器。现有Compose只完成静态解析，不能据此声称容器验收通过。
- 阶段3首次部署入口/发行包与完整服务器实操手册、阶段4bare Git/部署队列/状态/回滚脚本未进入本轮实施；`docs/DEPLOY_FIRST_TIME.md`明确为后续边界说明。

后续账号后台页面、字段及预期结果见 `docs/WECOM_SETUP.md`；当前无需为离线开发操作平台后台。官方资料核对见 `docs/REFERENCES.md`。

## 阶段 3：首次服务器部署（2026-10-21）

操作者在本聊天推进首次部署，并明确提出由助手通过本机现有服务器连接接手。普通 Windows 沙箱进程启动持续失败；经沙箱外只读检查恢复命令通道，使用已有 `stock-server` SSH 配置与严格主机密钥校验连接，实际主机名为 `vultr`。未读取或输出私钥、真实账号密钥或完整消息。

### 本轮新增与执行范围

- 新增 `compose.host-nginx.yaml`、`deploy/nginx/stock-watch.conf`、`scripts/deploy-host-nginx.sh`、`scripts/package-release.py`，更新首次部署说明。原 Compose 的 Caddy 模式保留；本次服务器使用已有宿主机 Nginx，app仅绑定127.0.0.1:8000。
- 服务器确认 Ubuntu20.04.6/x86_64、Docker28.1.1、Compose2.35.1；已有 Nginx/MySQL/trojan-go。仅修改 stock-watch 专用站点并留备份，原域名配置及这些服务保留，未重启整机。
- 操作者已执行 HTTP 校验和 acme.sh 签发。本轮直接接手启用 HTTPS/HTTP跳转及应用反向代理，证书副本路径 `/srv/platform/certs/stock.janettepa.uk/`；检测到 root acme.sh `--cron` 任务且 cron active。已验证签发/安装/reload，未强制续期演练。
- 服务器发布目录 `/srv/stock-watch/releases/aa24ee28ab3b69fd133c3b0cca3f3176012ad6f2`，`current`指向该目录；配置 `/srv/stock-watch/shared/.env` 为0600，数据 `/srv/stock-watch/shared/data/` 为UID/GID10001持久目录。
- 服务器创建空外部凭证模板，仅生成本服务随机 PUSH_TOKEN 并写入私有文件，不输出其值；企业微信和DeepSeek凭证尚未填写。MONITOR_ENABLED=false，正式outbox/管理任务/监控轮次为空，未访问真实新闻/模型或发送真实微信。
- 镜像 `stock-watch:aa24ee28ab3b69fd133c3b0cca3f3176012ad6f2`，实际镜像ID `sha256:e0a01819f8f99750b72441a780ce12021c0330f8a67e9686ea8ac12011d4d4ef`。Python3.12镜像内SQLite3.46.1，schema2，journal=delete。
- 白名单发行包 SHA256 `0a009a3e07ff98b478e3604bf8ea249bb9463fd9690dd81a07262bf8e3f45a9e`；归档manifest记录代码SHA和部署文件SHA，部署文件尚未提交，因此代码SHA不代表部署配置也已提交。首次SHA256侧文件因Windows换行校验失败，在解包/执行前停止；已修正为LF并重新打包，实际校验OK后执行。

### 实际命令及验收

| 实际执行 | 结果 |
| --- | --- |
| `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes stock-server hostname` | 返回vultr，现有SSH密钥登录成功 |
| `.venv\Scripts\python.exe -m pytest -q` | 部署前及交付前均151 passed，完全离线 |
| `.venv\Scripts\python.exe scripts/package-release.py`；`scp`上传归档和`.sha256`；服务器`sha256sum -c` | 白名单归档校验OK，无.env/数据库/私钥/日志/legacy |
| 发布目录下 `bash -n scripts/deploy-host-nginx.sh` 和 `bash scripts/deploy-host-nginx.sh aa24ee28ab3b69fd133c3b0cca3f3176012ad6f2` | shell与Compose解析、镜像构建、初始化、空队列检查、app/worker就绪、Nginx配置及HTTPS验证通过 |
| 本机 `Invoke-RestMethod https://stock.janettepa.uk/health`、`.../ready`；`curl.exe -sSI http://stock.janettepa.uk/deployment-check` | 公网HTTPS证书校验成功，health/ready均code0，SHA一致；HTTP301指向对应HTTPS路径 |
| 服务器 `bash /srv/stock-watch/incoming/host-acceptance.sh` | SQLite在线备份、临时目录恢复、integrity_check和schema/config_version比对通过；随后两个容器force-recreate并重新healthy，持久元数据一致 |
| `docker exec stock-watch-worker-1 python -m stock_watch worker-health`、`status` | worker ready；schema2/config_version1；monitor=false；outbox/tasks/runs为空 |
| 指定字段`docker inspect`与文件权限检查 | app/worker均10001:10001、readonly=true、内存上限192MiB、restart=unless-stopped；环境文件0600 |
| `systemctl is-active nginx mysql trojan-go cron` | 四者均active |
| `docker stats --no-stream`、`free -h`、`df -h /` | 一次采样app45.08MiB/worker20.16MiB，主机available481MiB、磁盘剩余6.2GB；不是负载峰值测量 |

实际备份 `/srv/stock-watch/shared/data/backups/acceptance-20261021T143505Z.db`，文件名使用UTC。容器重建前后schema2、config_version1、outbox/tasks/seen均0；恢复测试使用独立临时数据库，没有覆盖正式库。

Docker提示宿主机缺少swap限制能力；已核对内存上限生效，但未把swap限制或负载压力验收标为完成。原宿主机swap和其他服务不变。

### 剩余项与并行修改

- 真实企业微信应用/管理员/指定成员/回调后台保存、DeepSeek及新闻网络联调，尚未执行。需要密钥只写共享.env并重建容器，不能粘贴进聊天或提交。
- 自动监控保持关闭；旧系统是否仍在运行、最终seen迁移及启用切换仍需操作者确认。没有停用旧云函数。
- 容器重建、首次备份与隔离恢复已通过；整机重启、长期负载峰值、异地备份、自动续期和数据库回滚演练尚未验证。
- 本轮未实施或验证Git自动部署。执行期间本地出现非本轮创建的`.github/`和`compose.registry.yaml`，保持原样，未加入本次发行包，也未提交任何文件。

## 阶段 4：GitHub 构建与镜像部署（2026-10-21，进行中）

- 操作者要求由助手直接完成可执行的项目工作，仅在缺少账号权限、凭证或业务决定时询问。目标仓库为 `https://github.com/qinyinxiaoyun/stockWatcher`；SSH 只读查询远端成功，初次查询为空仓库，已设置 `origin`。
- 新增 `.github/workflows/build-deploy.yml`、`compose.registry.yaml`、`scripts/deploy-image.sh`、`tests/test_image_deploy.py` 和 `docs/GITHUB_DEPLOY.md`。路线改为 GitHub Actions + GHCR，替代原阶段4服务器 bare Git 构建计划。
- PR仅离线测试；main推送先测试、构建amd64/arm64镜像、用无网络容器初始化验收，随后才按开关部署。官方构建Actions已通过GitHub SSH只读查询固定到具体提交。部署使用镜像digest、生产串行锁，并拒绝被main新提交替代的过期构建覆盖生产。
- 复用首次部署的project、共享.env、data、UID/GID和Nginx。部署校验镜像revision、先备份再迁移，待消费队列默认阻断worker启动；app SHA和worker本轮新心跳分别检查。schema变化时不自动恢复旧数据库或旧应用。
- 新增19个离线部署场景，验证输入拒绝、真实队列阻断、可读备份、幂等重跑、镜像/就绪SHA错误、旧心跳拒绝及schema变化禁回退。测试通过本机Git Bash和Docker替身执行，不访问服务器或外部业务接口。

### 本轮实际验证与命令

| 实际执行 | 结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q` | **170 passed in 42.18s**，包含151项原有测试与19项部署测试，完全离线 |
| Git Bash `bash -n scripts/deploy-image.sh` | 通过 |
| `tmp/actionlint/actionlint.exe -shellcheck= .github/workflows/build-deploy.yml` | 官方actionlint v1.7.12校验通过；临时工具位于已忽略tmp并核对发行校验和 |
| `docker compose --env-file tmp/compose-validation.env -f compose.registry.yaml config --format json`（通过子进程提供无凭证插值变量） | 实际解析通过；app/worker同镜像、无build、app仅127.0.0.1；不依赖本机Docker daemon |
| `git diff --check`；已跟踪源码及原始ZIP常见私钥/API-key标记和敏感归档路径扫描 | 通过；未输出敏感内容；原ZIP保留 |
| 公网 `GET https://stock.janettepa.uk/ready` | code0/status=ready，仍为首次部署SHA aa24ee28ab3b69fd133c3b0cca3f3176012ad6f2；本聊天未重写首次部署 |

### 尚待真实验证

- GitHub实际构建、容器SQLite版本、镜像首次切换与故障回退演练，以实际工作流与服务器结果为准，不能以离线测试替代。
- SSH推送权限已可用；管理仓库Secrets/Variables需要GitHub API授权。首次官方CLI设备登录因网络超时未保存，正在使用官方设备授权协议重试；凭证仅保存本地已忽略.env，不进入代码、日志或聊天。
- `DEPLOY_ENABLED` 尚未开启；首次监控保持false，真实微信/模型/新闻联调与旧系统迁移切换沿阶段3单独验收。

## 阶段 4 续作：受限自动部署（2026-10-22）

- 已提交并推送 `bcfb607547d6a5d214379bf95f1a0eeae9b9097e` 到操作者指定的公开仓库。首次真实工作流 <https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/39620179381> 的test与image任务均成功；deploy因尚未配置开关而跳过。
- GitHub真实构建已发布amd64/arm64镜像并通过无网络容器初始化；日志确认容器SQLite3.46.1。不能再把镜像构建标为未验证。服务器此时仍运行阶段3首次部署版本。
- 操作者已完成设备授权；本地凭证只存已忽略的 `.env.github`，API核对账号为qinyinxiaoyun且对该仓库具有admin权限；不在聊天或日志输出Token。
- 原先不限制命令的root专用密钥方案被自动审批拒绝，未执行。已改为 `restrict,command="/usr/bin/python3 -I /srv/stock-watch/deploy-tools/github-gate.py"`：仅接收固定deploy命令、16KiB内的严格JSON、本仓库digest和完整SHA；不接受脚本、路径、额外字段或队列审批豁免。
- 新增root控制的 `deploy/github-gate.py`（宿主机工具兼容Python3.8，业务仍Python3.12）及86项纯离线测试；固定读取root不可组写的Compose/脚本，净化执行环境，Docker临时登录目录结束清理。安装时按公钥本身移除该密钥的重复记录，保留其他管理密钥。
- `.github/workflows/build-deploy.yml` 已改为只向固定deploy命令发送结构化请求，不再SCP上传脚本或执行通用远程shell。部署工具更新通过原有可信管理连接独立审核安装。

### 本轮实际验证

| 实际执行 | 结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest tests/test_github_gate.py -q` | 86 passed，包含命令/JSON/注入/路径权限/软链接/登录失败/凭证清理 |
| `.venv\Scripts\python.exe -m pytest -q` | **256 passed in 44.01s**，完全离线 |
| `tmp/actionlint/actionlint.exe -shellcheck= .github/workflows/build-deploy.yml`；`git diff --check` | 通过 |
| 既有严格SSH连接检查目录权限及 `docker ps` | /srv和项目根目录root755，shared为root700；原app/worker持续healthy |

### 受限入口与仓库配置实际结果

- 经既有可信管理SSH安装root控制的gate、固定Compose和部署脚本，文件统一LF。GitHub专用公钥使用restrict和Python隔离模式forced command；没有授予通用root shell。
- 已设置仓库Secrets `DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS` 及Variables `DEPLOY_HOST`、`DEPLOY_PORT`、`DEPLOY_USER`；设置过程使用标准输入传递凭证且不显示内容。配置核对时 `DEPLOY_ENABLED=false`。
- 使用该新密钥、关闭SSH代理/其他身份后实际测试：请求 `id` 返回1并明确拒绝非deploy命令；请求 `deploy` 加空对象返回1并明确拒绝无效请求。这两项证明实际SSH限制生效，不只是配置文本检查。
- Windows首次验证因本地私钥继承了额外ACL被SSH拒绝；已只收紧本项目两个忽略凭证文件的ACL到当前用户，再次验证通过。没有为此放宽SSH权限。

接下来开启仓库部署开关并推送本提交，实际镜像切换及验收结果继续记录；自动监控开关仍保持false。

### 自动部署实际验收完成（2026-10-22）

- 已开启仓库 `DEPLOY_ENABLED=true` 并推送 `9c49a76f5462ec5d433e0f310ef3b113b8e35298`。工作流 <https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/39703307482> 的测试、镜像和部署最终全部成功。
- 首次deploy在SSH认证时报告 `error in libcrypto`：Windows文本模式向CLI传递Secret时引入CRLF。该次未进入gate，原服务未被停止。已用原始LF字节重设同一受限密钥与主机公钥，仅重试失败部署；本次工作流修订另增加CR清理以避免再发。
- 首次成功镜像切换为 `ghcr.io/qinyinxiaoyun/stockwatcher@sha256:995d1c4662dfe9186874b38e37636b8143b290839d8b9d21d612da33d8640dae`；app和worker使用同digest、均healthy、UID/GID10001、readonly=true。`current-image`实际指向该完整提交的image-releases目录；源码首部署current和现有Nginx保留。
- 公网 `/ready` 返回code0/status=ready/build_sha=9c49a76…；容器只读SQL核对worker心跳SHA一致，MONITOR_ENABLED=false、schema2、config_version1、outbox0、async_tasks0，数据库integrity_check=ok。
- 自动生成备份 `pre-image-20261022T010744322836Z.db`；只读核对integrity_check=ok、schema2/config_version1，与正式库相同。文件名用UTC；没有覆盖恢复正式库。
- 项目AGENTS新增执行偏好：在已授权范围由助手直接配置、测试、推送、部署并核实，仅在确需操作者时询问；监控/真实投递/权限扩张仍保留原约束。
- 后续仅凭证换行兼容与文档修订会再经同一流程发布；生产故障回退、整机重启、长期资源峰值、异地备份、真实微信/模型/新闻、旧系统切换仍不标为完成。

### 最终普通推送验证（2026-10-22）

- 普通推送 `1561161b3153d3c720c1947781fe66b12290968a` 后，无需手工服务器命令触发发布；工作流 <https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/39704277693> 的test、image、deploy全部success。Windows凭证换行兼容修复已进入正式工作流。
- 公网 `/ready`、app版本、worker本轮心跳均为1561161…；两个容器healthy、10001:10001、readonly=true，同镜像digest为 `sha256:fff4a68c5484da99e50c599dfc10cea0814584d76334d11c52f7d7b019f1aca8`。
- 实际执行只读验收 `tmp/verify-live-deployment.py`：MONITOR_ENABLED=false、schema2/config_version1、outbox0/tasks0；正式数据库integrity_check=ok。最新自动备份 `pre-image-20261022T011438255779Z.db` 的完整性、schema和配置版本均一致；未恢复覆盖正式库。
- 阶段4的推送→离线测试→GitHub镜像构建→受限自动部署主流程已真实验通。末次记录只更新本进度文档，以 `[skip ci]` 提交避免验收记录自身反复触发发布；运行版本仍以上述1561161业务发布为准，后续普通代码推送继续自动部署。

## 企业微信资料准备（2026-10-22）

- 操作者确认DeepSeek API Key已有、旧云函数仍在运行。保留旧流程进行并行联调；停旧/导seen仅适用于后续明确选择新系统接管的切换方案，不是创建应用或联调前置条件。
- 已核对保留旧代码使用COS seen与ServerChan，新服务使用独立SQLite与企业微信；同时启用相同目标可能产生重复分析/通知，但不会互相覆盖去重状态。新监控继续关闭，未修改旧函数。
- 已创建本机 `.env.production.local`：只允许当前用户读取、git check-ignore确认排除；回调Token/AES Key自动生成且未显示内容。企业微信应用资料、成员UserID和已有模型密钥由操作者填入空项，尚未上传服务器或调用真实接口。
- 已更新 `docs/WECOM_SETUP.md` 的创建应用、字段来源和并行/接管边界；本轮仅文档和忽略配置模板变化，无业务代码修改，不重复运行离线测试。

## 企业微信资料同步与无投递预检（2026-10-22）

- 操作者确认私密资料已填写。`tmp/wecom-config.py` 使用项目Settings/WeComCrypto实际校验必填项、正数AgentId、43字符AESKey、UserID及监控关闭；结果通过，管理员1名、收件人1名，未显示任何值。
- `tmp/apply-wecom-config.py` 通过既有严格SSH在共享deploy.lock下执行：确认无待消费任务→停止app/worker→备份SQLite及0600私密环境文件→仅白名单合并凭证→原子替换0600环境文件→重建同版本app/worker→用容器内摘要逐项核对实际配置值。保留原PUSH_TOKEN、数据库路径、镜像版本及其他环境配置。
- 已生成数据库备份 `pre-wecom-20261022T024416649543Z.db`，完整性检查通过；环境备份保留在服务器root私密共享目录，不展示内容。正式库仍schema2/config_version1，待消费outbox/tasks均0；未恢复覆盖数据库。
- 已实际执行 `tmp/probe-wecom-config.py`：公网加密回调GET精确回显随机挑战，错误签名返回403，不创建管理任务；这只是服务端协议验证，不等同于企业微信后台已保存回调。
- 企业微信gettoken鉴权成功，应用信息只读接口返回60020。`tmp/wecom-trust-ip.py` 从错误信息仅提取并确认来源IP `66.135.8.221`，需要管理员后台加入“企业可信IP”。不打印原始上游响应、access_token或带凭证URL。
- DeepSeek仅调用官方 `GET /models`：鉴权成功，配置模型deepseek-flash在可用列表中；没有发起模型推理或消耗推理额度。未发送微信消息，未发起实际新闻分析。
- `tmp/verify-live-deployment.py 1561161b3153d3c720c1947781fe66b12290968a` 只读验收通过：公网服务ready、worker心跳版本一致、MONITOR_ENABLED=false、schema2/config_version1、outbox0/tasks0、数据库完整性ok。文档提交未触发部署，故按实际发布SHA校验。
- 当前需操作者在企业微信管理员后台放行上述IP并保存 `https://stock.janettepa.uk/wecom/callback`，Token/EncodingAESKey使用私密文件既有值。若后台要求域名校验，交由助手处理服务器验证文件。真实指定成员投递、双向指令、付费模型推理和新闻联调仍未验收；旧云函数保持运行，新监控保持关闭。
- 本轮未修改业务代码或测试，仅修改私密运行配置及文档；未重复运行已通过256项的离线测试。

## 企业微信后台字符限制兼容修正（2026-10-22）

- 操作者后台提示：配置可信IP前须先设置可信域名或接收消息服务器URL；EncodingAESKey只允许字母数字。仅格式检查确认原AESKey长度43，但非纯字母数字，原因是助手此前一次性生成标准Base64时可能包含+/，与后台UI不兼容。
- 已采用随机32字节→完整标准Base64去padding→仅接受ASCII字母数字43位候选的拒绝采样，并验证严格解码32字节及重新编码一致。仅轮换AES字段，保留Token、企业/应用/成员资料、DeepSeek、PUSH_TOKEN等其他值；不修改底层协议解密逻辑或官方向量兼容性。
- 实际执行 `tmp/rotate-wecom-aes.py`，更新既有本机私密文件以保留ACL，再复用空队列/私密备份/SQLite备份/原子环境更新/容器重建流程同步服务器，摘要核对本地与app/worker实际配置一致。未显示新旧密钥。新备份为 `pre-wecom-20261022T030254596469Z.db`。
- `tmp/wecom-config.py` 校验通过，并新增后台字符范围检查；`tmp/probe-wecom-config.py --callback-only` 的公网新Key加密GET回显和错误签名403均通过；只读运行验收仍schema2/config_version1、数据库完整性ok、outbox0/tasks0、worker心跳正常、监控false。
- 实际执行完整离线回归 `.venv\Scripts\python.exe -m pytest -q`：**256 passed in 43.24s**。未发送微信消息、未调用模型推理，旧云函数未修改。
- 当前需管理员重新读取本机私密文件的新AES值，先保存API接收回调（URL、Token不变），成功后再添加可信IP66.135.8.221。服务端合成验证已通过，企业微信后台保存与真实收发仍待用户反馈验收。

## 企业微信真实双向收发验收（2026-10-22）

- 操作者确认API接收回调及可信IP已保存。实际执行 `tmp/probe-wecom-config.py`：加密回调精确回显、错误签名403；企业微信token鉴权及应用信息接口通过（code0），应用匹配且指定收件人在可见范围内。DeepSeek仍只请求模型列表，鉴权通过且deepseek-flash可用。
- 操作者在“股票新闻助手”企业微信应用会话发送“帮助”，并确认客户端收到回复。实际执行 `tmp/verify-wecom-roundtrip.py`，只查询状态、计数及内部收件人一致性，不读取/输出完整消息、成员ID或凭证。
- 正式库inbox done1、回复outbox sent1，完成的接收/投递配对1条且收件人仅为消息发起人；pending outbox0、模型意图任务0、monitor_runs0。schema2、config_version1、integrity_check=ok，worker心跳与实际发布1561161…一致，MONITOR_ENABLED=false。
- 本轮完整离线验证 `.venv\Scripts\python.exe -m pytest -q`：**256 passed in 42.08s**。
- 已实际执行 `tmp/prepare-single-news.py`：从生产worker按现有首个启用目标兆易创新、单条查询、page_size1、max_attempts1抓取并保留1篇公开新闻，未调用模型/投递/写正式库。输入为证券时报网《两融余额九连降 累计缩水950.28亿元》，发布时间2026-10-22 10:14（Asia/Shanghai），URL为 <http://finance.eastmoney.com/a/202610223903781079.html>；上游返回的是新闻摘要，尚未读取全文。
- 操作者明确批准以该篇进行最多1次DeepSeek分析及向既有唯一测试成员发送1条“联调测试”结果；自动监控保持关闭，旧云函数继续运行。实际执行结果在后续条目记录，不能仅凭授权将真实模型/业务投递标为通过。

## 授权单篇真实业务接口验收（2026-10-22）

- 实际执行 `.venv\Scripts\python.exe tmp/run-single-news-acceptance.py`。直接复用已部署版本的Eastmoney样本、DeepSeekAnalyzer与WeComClient.send_chunk，不运行MonitorService/CLI monitor，不触发调度或初始化正式数据库。
- 固定同一已审阅新闻和目标；DeepSeek模型deepseek-flash、max_tokens1200、max_attempts1，传输计数硬限1次。输入为上游新闻摘要，未假称读取全文；输出通过项目结构及目标约束校验，分类“普通／不确定”。本条测试消息用于展示接口结果，不代表这类普通新闻符合正式预警阈值。
- 唯一收件人与此前“帮助”成功回执的发起人内部一致；发送请求硬限1次，遇token失效、超时或结果不确定也不补发。企业微信返回code0/accepted_chunks1，消息911 UTF-8字节，含“联调测试”和监控关闭提示；未记录或输出成员ID、完整消息、msgid、Token或上游原始响应。客户端是否收到这条测试消息尚未另行确认，之前“帮助”回执已由用户确认收到。
- 实际结果model_requests1、send_requests1、messages_accepted1，run phase=completed。持久化防重账本位于 `/srv/stock-watch/shared/data/acceptance/news-cf0d3073f4911460a078dda4.json`，目录0700、文件0600；该样本同一run_id再次执行会拒绝调用，不允许删除账本后重试。本次单次推理/投递授权已使用，后续真实任务按新授权范围执行。
- 本脚本SQLite连接全部mode=ro；执行前后关键表计数与版本一致：seen_articles0、outbox1（此前帮助回执）、monitor_runs0、async_tasks0、config_version1，PRAGMA及app_meta schema均2，integrity_check=ok。该比较是计数及版本检查，不是逐行内容一致性证明；worker常规心跳继续写入monitor_state。
- MONITOR_ENABLED=false，实际发布仍1561161…，旧云函数未修改。已完成真实接口小范围联调；自动调度、首次基线/正式筛选与持久投递、真实配置修改及自然语言管理、生产故障回退/整机重启/长期负载/异地备份、旧系统切换仍未验收或启用。

## 发布恢复修复与上线决定（2026-10-22，已验证并发布）

- 操作者授权处理发布恢复问题；关注名单及现有配置保持到上线，不导入旧系统seen，计划非交易时段切换，系统升级暂缓。自动监控保持关闭，旧系统本轮不改动。
- 修复目标：队列非空在停服务前拒绝发布；停前验证旧app/worker身份、健康、版本及数据库；停后队列竞态或候选失败时，在结构兼容前提下恢复原先运行的旧worker。GitHub入口仍不能提交队列审批豁免。
- 补充恢复边界：数据库不仅比较user_version，还比较稳定DDL指纹；备份失败也有停前结构证据；发布记录写入失败恢复旧state/marker；原本停止、不健康或无法确认身份的服务不自动接管。
- 当前正在补充离线回归和无业务网络、独立Compose/临时数据库的Docker隔离演练；验证和真实发布结果将在完成后记录，未提前标为完成。

### 修复验证完成，准备发布

- `scripts/deploy-image.sh` 已实现上述保护，并确保EXIT trap临时文件清理失败不会中断旧服务恢复；发布成功前不会记录成功，失败不覆盖正式数据库。
- 实际执行 `.venv\Scripts\python.exe -m pytest tests/test_image_deploy.py -q` 首轮35 passed；随后补齐候选app启动时入队、旧app版本/worker心跳不符、清理失败与原软链接恢复边界。最终完整 `.venv\Scripts\python.exe -m pytest -q`：**277 passed, 1 skipped in 133.04s**。跳过仅为Windows不能可靠创建真实Linux软链接的用例，交由Linux CI验证；其余测试全离线。
- 实际执行Git Bash `bash -n scripts/deploy-image.sh`、Python `-m py_compile scripts/verify-deploy-recovery.py`、`git diff --check` 均通过；并行只读复审确认没有扩大GitHub gate权限、队列审批豁免或正式数据恢复权限。
- 在服务器实际执行 `tmp/release-recovery-ops.py upload-probe` / `run-probe /srv/stock-watch/recovery-checks/tool-pf0jj6md`（经原有严格管理SSH）。工具使用真实旧镜像9c49a76…和新镜像1561161…，`network_mode:none`、不开端口、独立环境和临时库，覆盖五项真实Docker案例：停前pending拒绝且原容器ID不变；候选ready版本错误后旧app/worker恢复；SIGTERM期间入队后旧worker恢复且pending保留；原worker停止时拒绝且不启动它；空队列成功更新并保留配置/seen/已投递outbox。**五项全部通过**。
- 本次隔离项目为 `stock-watch-recovery-jenv2_sd`；已清理自身容器和临时业务文件（均为可丢弃fixture），保留摘要 `/srv/stock-watch/recovery-checks/run-jenv2_sd/summary.json`。待测部署脚本SHA256为 `5b5249a6abb1ec490c3935053eb6de6be2f65b94c8a8d6c4f56fcb2b8a1596a4`。未向生产注入故障、没有运行真实监控/模型/投递；给定镜像的Docker/GHCR拉取属于部署控制面，不将其宣称为完全离线。
- 演练前只读生产核对：app/worker仍为1561161…且healthy、MONITOR_ENABLED=false、config_version1、三只股票不变、themes0、seen0、baseline0、monitor_runs0、async_tasks0、outbox sent1且待消费0、integrity_check=ok；业务表指纹为 `e15149082b0f3b1e6187ace2ab73549033bf4360213dac091729039a1a6eeb29`。接下来通过可信管理连接在deploy.lock下备份并原子更新固定部署脚本，再提交普通代码发布；实际结果另记，不将“已验证”混同“已部署”。
- 演练后再次只读核对生产容器ID、版本、数据计数及业务表指纹完全未变。已实际执行 `tmp/release-recovery-ops.py install-tools`：在deploy.lock下校验原脚本SHA256为 `b672ea31e9323ea8f022d5b4d806fc950a1fe4f8a63758f7023df2951c7318b9`，原件备份到 `/srv/stock-watch/deploy-tools/deploy-image.pre-recovery-20261022T060828486042Z.sh`，经语法检查后原子替换为上述已演练的5b5249a6…版本（root0640）。没有改动gate、密钥、权限范围或生产环境文件；此时业务容器仍是原版本，等待普通推送触发部署。

### 普通推送、自动部署及最终验收完成

- 已提交并普通推送 `f104a7b7ddcd8025adb8c00020f76fbd1d729f7a`。工作流 <https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/39732211177> 的test、image、deploy均success；没有手工执行生产容器切换命令。GitHub Linux CI实际 **278 passed in 45.08s**，包含Windows跳过的真实软链接恢复用例；双架构镜像构建、实际镜像禁网初始化检查均通过，容器SQLite3.46.1。
- 正式镜像为 `ghcr.io/qinyinxiaoyun/stockwatcher@sha256:23b40bfc8ab96933426ab0f5c8e5d0d8cfb44f3b3e0393f57aec8d500dd53a74`。实际执行 `tmp/release-recovery-ops.py verify-release f104a7b7ddcd8025adb8c00020f76fbd1d729f7a`：两个容器同digest、healthy、10001:10001、readonly=true，镜像revision、公网 `/health` / `/ready`、本轮worker心跳均为f104a7b…；`image-release.env`、`current-image`和运行版本一致，发布目录脚本与受控脚本逐字节一致且为已演练SHA256。
- 正式库及本次备份 `pre-image-20261022T061243285627Z.db` 的integrity_check均ok、schema2/config_version1。配置、股票/别名/关注主题、审计、seen、outbox逐行选取后计算的业务表指纹均保持上述e1514908…不变；seen0、baseline0、monitor_runs0、async_tasks0、outbox仍仅此前帮助回执sent1，待消费0。worker心跳验收时18.34秒新鲜；没有恢复覆盖数据库。
- **本轮发布恢复修复已完成并上线到服务代码；正式监控切换未进行。** MONITOR_ENABLED仍false，三只股票和关注配置未变；不导入旧seen，不修改旧云函数，非交易时段切换由操作者安排，系统升级暂不纳入。没有新增真实模型、新闻或消息投递调用。
- 五项故障注入发生在独立Docker项目，不宣称对生产实例做过故障注入、整机重启或长期压力验收。正式调度/首轮基线/筛选投递、真实配置变更与自然语言管理、异地备份和整机重启仍沿原计划分别验收；它们未被本轮发布恢复修复悄然启用。
- 本节最终只更新交付记录，以 `[skip ci]` 提交避免记录自身再次部署；实际运行SHA仍为上述f104a7b业务发布，后续普通代码推送继续走同一受限自动发布流程。
