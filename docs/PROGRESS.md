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

## 未来功能可行性与路线规划（2026-10-22）

- 操作者提出组织内多微信成员独立订阅、个人微信接收和更多信息源，要求先分析并加入未来计划。本轮只修改文档，不实现业务功能，不访问生产、不推送/部署、不修改成员/名单/凭证、不启用监控或真实接口投递；当前服务状态仍以上次验收为准，未冒充本轮重新在线核查。
- 已读取现有配置/数据库、回调权限、抓取、模型分析、seen/outbox及worker源码，并行只读复审。确认当前是全局名单+全局seen+文章级outbox+多收件人同内容，尚无个人订阅隔离，抓取和分析也尚未并行。
- 新增 `docs/FUTURE_ROADMAP.md` 并纳入PLAN阶段5～7：个人微信渠道最小验证→成员订阅/共享抓取和关注组分析→逐源审核启用。AI首版按真实分析需求分组复用，不按成员机械重复调用；分别记录成员投递、基线、撤权和崩溃恢复要求。
- 已查腾讯官方API/订阅消息资料、Tencent/openclaw-weixin官方README与协议、公告/正式数据服务及HKEX条款/IIS。微信插件账号能力和真实收发仍待确认；ClawBot有官方实现，但客户端字段可选不等于主动推送服务端保证；未采用第三方固定额度作承诺。部分企业微信帮助/开发页面本次读取受限，明确留待实际账号核验。
- 信息源按用途许可和技术接口双重门槛规划：优先合规A股公告/IR/可选SEC，HKEX等先取得适用授权；不把公开网页或robots许可等同于可抓取、AI处理和多人分发权。未申请服务、调用付费接口或承诺供应商费用。
- 实际执行 `.venv\Scripts\python.exe -m pytest -q`：**277 passed, 1 skipped in 114.49s**（Windows真实Linux软链接用例跳过，非新增失败）；`git diff --check`通过。仅docs变更，未提交/推送或触发CI/部署。未来需求状态均为规划，不能替代现有正式切换或称为已实现。

## 新监控正式启用与自然首轮验收（2026-10-22）

- 操作者明确要求“现在打开新的监控”，并在确认问题中回答“已停用，且没有运行中的任务”，确认旧服务全部定时触发器已停用、无运行任务。该授权允许既定监控流程的真实抓取、模型调用及原配置成员投递，不包括随意测试广播或未来多成员/信息源功能。旧平台停用依据操作者确认，助手未登录腾讯云后台独立核查，未删除旧函数或COS。
- 保留前一任务尚未提交的未来计划文档。本轮未改业务代码、未推送/部署镜像、未升级系统、未导入旧seen、未增加来源/成员/股票/关注点；只把生产共享环境MONITOR_ENABLED=false改为true。
- 启用前实际执行 `tmp/release-recovery-ops.py snapshot` 和 `tmp/activate-monitor.py status`：app/worker均f104a7b…/healthy/monitor=false，schema2/config_version1、3只股票、themes0、seen0/baseline0、monitor_runs0、async_tasks0，outbox仅之前帮助回执sent1且待消费0。原始目标effective_at为初始epoch，满足静默基线语义；既有凭证和唯一收件人只做存在/格式检查，没有打印内容或发送测试消息。
- 已实际执行 `.venv\Scripts\python.exe tmp/activate-monitor.py enable`：通过原有严格管理SSH，在共享deploy.lock下核对可信发布与旧健康/新鲜心跳→复查空队列→停止旧app/worker→备份环境及SQLite→原子改一行开关→同镜像重建app及唯一worker→核对两服务全部实际环境除MONITOR_ENABLED外逐项相同。没有改GitHub受限入口、密钥、反向代理或数据库内容来强行过检。
- 运行切换时间为UTC `2026-10-22T06:39:04.414491+00:00`（北京时间14:39:04），属于既定工作日监控窗口。此前“非交易时段切换”的计划由本轮明确的立即启用指令取代；未伪称这次发生于非交易时段。数据库备份 `pre-monitor-20261022T063857380383Z.db` 完整性ok、配置指纹一致，环境原件备份root0600；正式库未从备份覆盖。
- app/worker仍使用相同不可变镜像 `ghcr.io/qinyinxiaoyun/stockwatcher@sha256:23b40bfc8ab96933426ab0f5c8e5d0d8cfb44f3b3e0393f57aec8d500dd53a74`，BUILD_SHA仍f104a7b…，MONITOR_ENABLED在二者内均true、healthy、UID10001、只读根文件系统，新心跳已核对。公网 `/health` 和 `/ready` 均code0/SHA匹配。
- **自然首轮14:35时槽**按5分钟宽限机制在14:39:05认领，14:39:20成功完成：fetched239、eligible63、baseline_seen63、analyzed0、queued0、error_count0、耗时15.584秒。3个股票的基线键逐个存在。基线覆盖首次实际返回窗口新闻，不宣称覆盖全网历史；未执行强制monitor、dry-run real、补查或额外定时器。
- **下一自然14:40时槽**14:40:00开始、14:40:14成功：fetched239、eligible63、baseline_seen0、analyzed0、queued0、error_count0、耗时13.764秒。两个自然轮次均无模型分析或新预警；这验证了基线和定时去重，但不能称为已真实命中并投递新的重大/重要新闻。
- 最终只读状态：schema2/config_version1、configuration_fingerprint始终 `d492eb01aaa3f17359f037f590aa926242b51792a8b01e192b4cf945aa97828b`，名单和关注点/等级未变，seen63、baseline3、outbox仍sent1、pending/tasks0，两个monitor_runs均succeeded，integrity_check=ok。日志只提取轮次、计数和耗时，不打印文章正文、收件人或凭证。
- 调度继续按Asia/Shanghai周一至周五09:00～15:00每5分钟，15:05后不执行，当前不自动排除法定休市日；首次启用后不补跑服务离线期间所有历史时槽。下一时槽按实际时钟计算，不为验收伪造时间。
- 本轮完整离线 `.venv\Scripts\python.exe -m pytest -q`：**277 passed, 1 skipped in 113.07s**；既有Windows软链接用例跳过，无新增失败。运行配置和当前状态已同步PLAN/WECOM_SETUP/GITHUB_DEPLOY，未来需求仍为规划，不随监控启用变成已实现。

## TrendRadar 源码调研与本项目对照（2026-10-22）

- 本轮为分析任务，核对公开仓库 `sansan0/TrendRadar` 的 master；GitHub API 返回提交 `68c8916c450f8c8dd36cb88a189f7c2e46681ab5`，提交时间为2026-10-20T11:21:52Z。以源码为主要依据，并检查现有news/analyzer/monitor/db/config/delivery/scheduler及ARCHITECTURE/FUTURE_ROADMAP；不把规划当作已实现。
- 抓取结论：热榜通过NewsNow统一API按平台ID请求，不是TrendRadar自身逐站维护爬虫；支持自定义API与预期域名校验。RSS直接请求feed URL，解析RSS/Atom/JSON Feed并保存feed自带摘要；主采集流程没有逐文章正文抓取。热榜与RSS采用SQLite持久化，保存首次/末次发现与排名等历史。源码：https://github.com/sansan0/TrendRadar/blob/68c8916c450f8c8dd36cb88a189f7c2e46681ab5/trendradar/crawler/fetcher.py 、同版本 `crawler/rss/fetcher.py`、`parser.py`、`storage/sqlite_mixin.py`。
- 个性化结论：关键词匹配和AI兴趣分类是可选筛选路径，AI按兴趣文件提取标签、分批分类、存储已分析状态并处理兴趣变更；分析简报是另一步，主要输入标题/来源/排名/时间，RSS简报输入也没有使用已保存的summary。多兴趣文件和多账号推送不等于成员独立订阅；dispatcher接收同一份报告分发给配置渠道/账号，未见该主链路具有每成员订阅、权限与定向筛选。源码：同版本 `ai/filter.py`、`ai/filter_pipeline.py`、`ai/analyzer.py`、`notification/dispatcher.py`。
- 推送结论：incremental/current/daily分别对应新增、当前、当日范围，不能将模式描述中的“零重复”等同于端到端严格一次。多账号发送任一成功即返回成功；开启once_push时任一渠道成功即记录时段执行，不能替代每成员/每渠道的持久重试。独立AI分析模式与推送模式不同时，当前主流程为避免时间窗混用，会排除RSS/独立展示区输入。源码：同版本 `__main__.py` 的 `_run_ai_analysis`、`_send_notification_if_needed`，及 `notification/dispatcher.py`。
- 借鉴建议（尚未实施）：先扩展统一来源身份/内容版本/游标与共享文章池，再做成员订阅隔离、按实际关注需求复用AI分析、成员+渠道+事件独立投递；采集/分析/推送时段解耦作为后续配置能力。保留现有Python3.12、唯一worker、短事务和持久outbox/分段检查点。股票预警优先补公告/IR等有权使用的来源，NewsNow热榜适合作为舆情补充；不以热度代替重要性，不照搬标题简报作为证券事实判断。与FUTURE_ROADMAP现有方向一致。
- 实际执行：公开源码经web工具读取；只读执行 `rg --files`、`rg -n`、`Get-Content`、`git status --short`，以 `Invoke-RestMethod` 查询公开提交、`Invoke-WebRequest` 阅读固定版本公开源码；执行 `.venv\Scripts\python.exe --version` 为Python3.12.12，完整离线 `.venv\Scripts\python.exe -m pytest -q` 为 **277 passed, 1 skipped in 112.76s**（既有Windows/Linux软链接差异用例跳过）。普通隔离进程因helper_unknown_error无法启动，改用已获自动审查允许的隔离外只读/离线测试命令完成。
- 未验证项：未部署或运行TrendRadar，未实际请求NewsNow/RSS/东方财富、DeepSeek、企业微信或生产服务器；本轮不验证线上即时状态、来源覆盖率、抓取延迟、模型效果及新增来源用途许可。仅追加本节调研记录，保留工作区已有文档变更；未修改业务代码、生产配置、监控开关、订阅或收件人，未真实投递、提交或推送。

## 独立订阅与AI调用粒度设计澄清（2026-10-22）

- 本轮只讨论设计，未实施多成员功能。再次只读核对AGENTS、FUTURE_ROADMAP第2节、analyzer的结果与提示词、monitor分析入队及delivery收件人/检查点；执行Get-Content及git status --short，未读取凭证、运行库或生产服务器。
- 首版建议以“文章内容版本 × 目标 × 规范化分析需求”作为逻辑分析单位，同需求成员复用；推送等级、方向偏好、渠道、静默时间等只影响本地过滤/投递，不因这些字段变化重跑模型。不同公司、关注问题、分析时间范围或私有背景分别处理；不要把不同目标压成一个总等级/方向。关注点不是排除其他重大新闻的硬筛选条件，分析结构需区分关注相关性和目标整体重要性，并保存证据与缺失信息。
- 逻辑任务与API请求拆开：首版每文章/关注组独立请求便于验证、缓存及失败重试；后续可把同文少量公开关注组合并为一次请求，按组ID返回与独立验证，私有上下文隔离。10人×10篇且需求相同可约10次，3个关注组且每文均相关约30次，均不含重试；合并请求减少HTTP次数不等于token成本同比减少。“基础分析一次再个人补分析”可能增加调用和误差传播，作为实测后可选优化。
- 缓存按内容hash、目标、分析需求、模型/提示词/结构版本及隔离域识别，独立订阅版本用于并发一致性，不机械导致全局缓存失效。采集、分析、成员评估及成员+渠道+文章事件投递分层；原子生成通知或可恢复分发任务，发送前复核订阅资格，新订阅默认从生效时间开始，普通配置变更不重发旧闻。
- 外部只查阅公开工程文档以核对任务拆分/受约束输出的通用设计；不据此声称当前DeepSeek接口已具备其他供应商的特定能力。未新增测试或改业务代码；沿用上一轮同代码完整离线基线277 passed、1 skipped（112.76s），本轮未重复运行。未修改线上配置、订阅、收件人或监控开关，未调用新闻、模型或投递接口。

## 独立订阅实现与TrendRadar借鉴计划落地（2026-10-22）

- 操作者明确授权将前两轮分析及TrendRadar可取之处纳入计划，并按计划实现独立订阅；要求子agent优先6.1。本阶段三个实现子任务及最终复核均使用gpt-6.1-sol，主线程负责计划、CLI/worker路由、集成修复与完整验收。保留原工作区已有文档修改。
- 已更新PLAN/FUTURE_ROADMAP，新增INDEPENDENT_SUBSCRIPTIONS实施与使用说明，同步README/ARCHITECTURE/WECOM_SETUP。借鉴统一来源身份、共享文章池、兴趣分类缓存和投递适配；采集/分析/推送时段解耦、其他渠道、舆情日报、正式来源接入及受控并发明确留在后续阶段。没有把TrendRadar本身的多账号发送当作已实现成员订阅。
- schema3新增成员、完整个人配置、审计与确认，以及共享文章/分析、成员评估/基线表。个人配置保留stable ID、别名、隐藏topics、暂停状态、等级/方向、effective_at及版本，在隔离内存SQLite中复用已有ConfigStore领域规则，短事务CAS持久化，不建立每成员文件或调度器。新成员空订阅；显式指定唯一旧配置拥有者才迁移完整原配置/审计/基线，保留旧全局模式。
- 成员准入、撤权、恢复、私有分析范围由可信本地CLI管理；微信管理员和普通成员均只读写本人订阅。明确命令、上下文、确认、自然语言任务和回执绑定成员，模型调用前后校验资格代次与配置版本；确认不能串用。稳定成员ID+上游消息ID去重，撤权再恢复也不重复执行旧消息；恢复时仅刷新既有启用目标的生效时间，不自动恢复暂停条目或补发停用期间旧闻。
- 新增MonitorRouter，每轮读取持久运行模式；旧monitor在租约认领事务内再次检查模式，迁移时拒绝未完成旧队列及活跃监控租约，避免旧广播与新成员模式重叠。监控schema已存在时不再通过executescript隐式提交调用者事务。现有部署的schema/DDL指纹恢复保护继续保留，schema3升级不能直接切回schema2旧镜像。
- 成员检索词按目标取并集共享抓取，文章按来源命名空间、上游身份和内容版本持久化。AI以文章内容/来源、单目标、规范化关注点、组织/私有域、模型/端点/可用revision及提示词/结构版本分组缓存；不同公司不共用一个总利好利空，同需求成员复用。通知阈值由本地判断；每轮最多10次真实新闻模型请求，失败计入、成员模式内部单次尝试，持久轮转防超过10名成员时后排饥饿。文章/缓存/评估15天清理，outbox保留投递幂等证据。
- 成员评估与通知入队同事务；一人同文多目标合并一条定向消息，各目标分别展示分析。分析或发送失败不会吞掉其他成员通知；分段检查点保留。每段发送前复核运行组织、数据库组织、消息组织、成员代次和当前订阅/关注点/阈值，旧无作用域队列不得恢复广播。修复更换企业配置后旧消息发给另一组织同名UserID的边界。
- 迁移ID-only seen首次观察时绑定当前文章版本，不重发原内容，后续实质修订可重新分析。新增/恢复订阅按生效时间处理；显式历史补查必须指定成员和唯一目标，并保留真实订阅effective_at作为投递资格，避免临时回溯窗口使已授权补查被误取消。模拟/预览使用临时库，不触碰正式运行数据。
- 实际执行相关离线命令：`python -m pytest tests/test_subscriptions.py tests/test_config_commands.py -q`最终61通过；成员回调/API/旧端到端46通过；CLI/原监控相关47通过；新增成员端到端验证三成员共享一次AI、发送前收紧阈值取消、仅失败成员重试。最终组织隔离与稳定消息去重联测82通过，补查/CLI/成员端到端相关35通过。均使用`.venv/Scripts/python.exe`（Python3.12.12），autouse网络阻断生效，无真实模型或投递。
- 完整离线回归过程：较早实现345 passed/1 skipped；严格预算与修订后360 passed/1 skipped（117.16s）。补查后全套出现1项新端到端fixture失败，原因为模拟投递时刻早于扫描实际入队时刻；将fixture时间推进至入队后再验证重试，定向6项通过。**最终冻结全部代码后实际执行 `.venv/Scripts/python.exe -m pytest -q`：367 passed, 1 skipped in 120.62s**；唯一跳过仍为Windows环境的真实Linux软链接用例，非新增失败。随后`.venv/Scripts/python.exe -m compileall -q stock_watch`和`git diff --check`通过（仅既有PROGRESS行尾转换提示）。实际执行`python -m stock_watch --help`、`members --help`与`demo`成功；demo仅使用临时库/模拟抓取、模型及收件人。
- 未验证/未执行：未连接生产服务器、未调用真实新闻/DeepSeek/企业微信，未新增真实成员/来源/收件人，未修改线上MONITOR_ENABLED、原三只股票或关注配置，未导入旧系统seen、提交/推送或部署；本轮镜像构建、Linux CI及生产资源/管理回执时延尚未实测。生产仍是此前已授权版本，本轮不把本地schema3及离线测试称为生产迁移/新成员真实收件；上线前须明确旧配置唯一拥有者，按既有备份与队列保护执行迁移和实际账号验收。

## 独立订阅生产发布与原拥有者迁移（2026-10-22，已完成）

- 操作者明确要求部署生产，指定原配置拥有者为当前唯一用户MaTianye。生产实际UserID与输入只存在大小写差异；已按casefold核实唯一身份，并沿用已部署的精确UserID拼写，不改变既有企业微信收件范围。原拥有者注册为admin，微信配置操作仍限定本人。
- 只读生产预检：app/worker均f104a7b…且healthy、UID10001/只读根文件系统，原授权MONITOR_ENABLED=true；schema2/config_version1、3只股票、themes0、seen68、基线3、outbox仅sent1、待投递/意图任务0，integrity_check=ok。配置指纹仍为`d492eb01aaa3f17359f037f590aa926242b51792a8b01e192b4cf945aa97828b`。环境仅做一致性指纹核对，未打印凭证或敏感消息。
- 发布前发现兼容问题：成员撤权产生cancelled终态通知，旧发布脚本将其误认为待发送。已最小修正queue_check并新增真实shell替身回归；完整离线`.venv/Scripts/python.exe -m pytest -q`为**368 passed, 1 skipped in126.72s**（既有Windows/Linux软链接差异跳过）。经可信管理SSH及deploy.lock备份原固定脚本，原子安装修正版，sha256为`39d5917b247e59bc05eb04f93222f74901ca6f6ee46485fa4155f02af7c2af0f`；未扩大受限GitHub入口权限或允许队列豁免。
- 已实际执行`git fetch origin`确认发布前main与远端一致，提交并推送业务版本`0c988dae7cbf1cd1e6ddedae44a0ad6e4dd79d1b`。正常[GitHub工作流39772647559](https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/39772647559)的test/image/deploy均success；Linux完整离线**369 passed in48.36s**，镜像烟测SQLite3.46.1。当前运行镜像为`ghcr.io/qinyinxiaoyun/stockwatcher@sha256:dd27d25c2846b8bb8223be54d271a7ee7df906fdbc8ef04ea523b6225b9df067`，app/worker、发布记录与current-image均对应同一业务SHA。
- 已实际执行`.venv/Scripts/python.exe tmp/deploy-subscriptions.py status`，核对标准发布完成schema2→3升级、旧全局模式、健康服务与空队列；再执行`migrate 0c988dae7cbf1cd1e6ddedae44a0ad6e4dd79d1b`。在原deploy.lock下停worker/app、复查空队列及配置指纹，另做SQLite online backup；使用同一不可变镜像、禁网/只读根文件系统/UID10001容器，在同一事务中准入唯一原拥有者并迁移配置、审计、seen和基线，再直接重启同一schema3镜像。没有绕过标准发布的旧服务健康要求，也未回退schema2旧应用或用备份覆盖正式库。
- 拥有者实际UserID为`MaTianYe`，与操作者指定MaTianye仅大小写不同且与原唯一收件人/管理员精确一致。现在member_count=active_members=1、role=admin、independent_subscriptions=true，个人配置与原配置逐表匹配、config_version1、股票3/themes0、继承seen68/基线3；全局原配置与审计指纹仍为`d492eb01aaa3f17359f037f590aa926242b51792a8b01e192b4cf945aa97828b`，环境文件指纹未变，MONITOR_ENABLED仍true。未复制配置给其他用户、增加收件人或变更凭证/应用可见范围。
- 已实际执行`verify 0c988dae7cbf1cd1e6ddedae44a0ad6e4dd79d1b`：app/worker均healthy、UID10001:10001/只读根，worker心跳SHA与新版本一致且新鲜；正式库integrity_check=ok，pending_outbox/pending_tasks均0，outbox仍只有原sent1。公网`https://stock.janettepa.uk/health`及`/ready`均HTTP200、code0、新SHA匹配。备份`pre-image-20261022T114624575303Z.db`（schema2）与`pre-subscriptions-20261022T114959417769Z.db`（schema3）均完整性ok、原配置指纹及seen68一致，位于持久数据目录backups内。
- 切换完成于北京时间19:50，属于原调度窗口之外，未手动补跑历史监控、调用真实新闻/DeepSeek或额外发送测试消息。新模式自然扫描/重大新闻真实命中投递、新增成员可见范围/收件、实际资源峰值和管理回执时延仍待实际运行验收；本轮健康及迁移检查不能代替这些结果。既有调度仍由唯一worker负责，首次部署默认监控关闭的约定不变。
- 发布后仅同步README/PLAN/FUTURE_ROADMAP/INDEPENDENT_SUBSCRIPTIONS/WECOM_SETUP/GITHUB_DEPLOY及本记录，执行`git diff --check`；以`[skip ci]`文档提交避免重复构建和重启已验收的同一业务版本。生产运行SHA仍为0c988da…，后续普通代码推送继续走既有完整发布链路。操作辅助文件及私有基线证据保留在忽略的tmp内，不进入提交。

## 个人微信接收与只读双向操作验收（2026-10-22，已完成）

- 操作者要求实现个人微信接收。只读检查当前投递/回调代码，确认现有企业应用text/touser和成员作用域即可复用微信插件，不需要新增业务渠道、修改凭证、开端口、部署代码或重启服务；不安装ClawBot或第三方转发平台，不向个人号好友/群开放任意群发。
- 先明确给出企业微信后台“我的企业→微信插件”、本人扫码关注、组织/应用应出现的操作路径。操作者确认“已关注并能看到应用，可发1条测试消息”；仅将该回复视为向当前唯一成员发送1条明确验收测试的授权，不扩大到其他成员/广播。
- 实际执行 `.venv\Scripts\python.exe tmp/deploy-subscriptions.py status`：生产仍0c988da…、app/worker均healthy、监控true、schema3、独立订阅开启且唯一有效成员与原拥有者一致，待发送/意图任务0、数据库完整性ok。个人配置已由此前用户操作变为版本2，本轮不覆盖回迁移时的版本1或旧全局配置。
- 实际执行 `tmp/wechat-plugin-acceptance.py preflight`：只读核验当前组织/唯一有效成员/现有收件人，企业微信token鉴权和应用信息接口成功，应用身份匹配且可见范围显式包含该成员；输出仅布尔、数量和配置指纹，不输出token、成员标识或完整消息。
- 随后执行 `tmp/wechat-plugin-acceptance.py send wechat-plugin-20261022T125620Z`：北京时间20:56:31，复用WeComClient.send_chunk，send_requests1、API接受1条；唯一收件人与既有已准入成员一致。持久账本 `/srv/stock-watch/shared/data/acceptance/wechat-plugin-20261022T125620Z.json` 记录启动/调用/接口结果，同一run_id再次执行会拒绝；网络结果不明不自动补发，也没有为两个客户端分别发送。
- 操作者明确确认“个人微信已收到，查看监控也能回复”，据此标记当前成员的个人微信文字收件与只读双向操作验收完成。服务器API无法识别最终展示客户端，不把code0或服务端字段当作手机确认；真实客户端结论来自本人反馈。
- 实际执行 `tmp/wechat-plugin-acceptance.py status 2026-10-22T12:56:20+00:00`：同一窗口新增inbox2，完成且已发送的定向回执配对2，接收身份、touser、成员ID及组织一致；未读取或打印完整正文，不独立断言这两条消息均来自某个客户端。本次人工测试投递只有1条，用户主动消息的正常回执另外处理，不将两者混淆。
- 验收前后个人配置版本2、配置指纹一致、integrity_check=ok，MONITOR_ENABLED始终true、代码/镜像版本不变；未修改名单、关注点、成员、应用可见范围或后台凭证。验收工具未调用新闻/模型接口、未强制运行监控，既有后台服务继续按原授权工作。
- 完整离线 `.venv\Scripts\python.exe -m pytest -q`：**368 passed, 1 skipped in 123.04s**；唯一跳过为既有Windows真实Linux软链接用例。个人微信接通属于账号绑定与既有协议路径验收，不因没有业务代码diff就虚构新适配器或部署。
- 新增PERSONAL_WECHAT使用/后续成员接入/故障排查说明，同步README、PLAN、FUTURE_ROADMAP和WECOM_SETUP。当前成员可直接使用个人微信现有应用入口；其他成员、媒体类型及自然新新闻命中投递仍按实际条件分别验收。
