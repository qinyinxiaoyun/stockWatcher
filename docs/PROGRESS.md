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

## 独立订阅自然扫描与单次模型校验异常排查（2026-10-23）

- 操作者提供09:00和09:05的worker日志；本轮只读核对源码及正式库聚合数据，没有手动抓取新闻、重跑模型、发送消息、改阈值/订阅/成员或重启部署。原唯一worker继续按既有授权运行。Docker日志的01:00/01:05为UTC，与北京时间09:00/09:05一致。
- 实际执行`.venv/Scripts/python.exe tmp/diagnose-analysis.py`：通过既有严格SSH管理别名，在worker内使用SQLite URI `mode=ro`、`query_only=ON`和短读事务读取计数。输出仅版本、轮次、等级/方向分组、数量和心跳，不读取或打印失败原响应、文章/模型正文、个人关注文本、UserID或凭证。
- 北京时间09:08快照：生产仍0c988da…、MONITOR_ENABLED=true、独立订阅开启、唯一有效成员；当前个人config_version2、3只股票及1个主题，推送等级为重大/重要，三只股票均双向/important。worker心跳SHA匹配且新鲜。监控日志中的config_version1来自兼容全局settings，不表示个人配置回退；成员一致性检查实际使用各自个人版本。
- 09:00轮成功，10次新闻模型请求；09:05轮partial，10次请求中1次AnalysisValidationError。两轮共持久缓存19个合规结果：普通/不确定12个、普通/利空7个，should_notify均true；member_evaluations为evaluated19/baseline11，新闻outbox0，既有回执等outbox均sent。故queued0符合当前仅重大/重要的通知门槛，不是微信投递失败；没有据此评价模型重要性判定本身是否正确。
- analyzed计数是实际发起的分析请求，失败也占每轮10次上限；fetched是各查询返回条数之和，可能包含重复，eligible是本轮待评估候选数量，不表示全部已分析。baseline_seen11与迁移继承去重绑定当前内容版本有关，不能推断重新建立了所有目标的首轮基线。失败项目不写有效分析缓存/已完成评估，下个自然轮次仍有资格重试，但须满足24小时窗口、预算和当前订阅；不承诺无限重试或必然推送。
- 当前错误日志仅保留异常类型，失败原响应没有持久化，无法追溯到底是JSON结构、字段类型/枚举、长度还是目标键校验失败；不凭经验宣称某个原因已被证实。后续可补受控校验原因码与匿名任务身份来改善排障，不应记录完整敏感输入/输出或放宽校验来消除报错。本轮未修改业务代码、推送或部署。
- 实际完整离线`.venv/Scripts/python.exe -m pytest -q`：**368 passed, 1 skipped in122.56s**；跳过仍为Windows真实Linux软链接用例。`git diff --check`通过。只追加本节排查记录，辅助工具在忽略的tmp内；没有把只读诊断当作已修复模型输出格式或自然预警已真实投递。

## 与旧服务对比的降噪、重复与成本整改计划（2026-10-23，待实施）

- 操作者反馈新服务token、通知频率明显增多，具体包括阿里巴巴港股卖出重复、日内推送昨晚新闻，以及过多行情/买卖事实，要求分析原因并给计划。本轮限于对比、生产只读取证与计划文档，不改业务代码、个人配置/阈值/成员、监控开关或线上队列，不重启、部署、补跑、试发或额外调用新闻/模型。原唯一worker继续按既有授权运行。
- 并行源码复核均使用gpt-6.1-sol：旧新成本/聚合差异、版本/事件去重/时效缺口、降噪计划及边界。实际只读检查legacy/tencent-scf/index.py、news/config/analyzer/subscription_monitor/delivery/scheduler和相关测试；未把legacy接入运行路径或修改迁移证据。
- 实际执行`.venv/Scripts/python.exe tmp/news-noise-audit.py`，通过既有严格SSH在worker内用SQLite mode=ro/query_only/短读事务汇总，服务器内解析已有Docker轮次日志，只输出受控时间/数量/匿名指纹/字段相等性。09:46冻结证据保存在忽略的`tmp/news-noise-audit-0946.json`：1成员、3股+1主题、16词查询；09:00～09:45共10轮100次真实分析请求，成功97（普通91/重要6）、校验失败3。未读取/输出凭证、关注文本、完整新闻/分析或推送正文。
- 同一快照新闻outbox共6条sent、均attempts1/accepted_chunks1；5条来自前一天，全部入队年龄>9小时、最大14.83小时，入队至发送仅5～19秒。发现两组各2条同URL/上游ID/规范标题重复；阿里一组标题空格及摘要不同，另一组同目标摘要仅空白变化。09:47追加核对：阿里组两个不同股票目标，修正过程中未核实的“股票/主题”猜测。去重缺陷是检索呈现差异进入版本哈希导致，不是同一outbox网络重试；不将完整正文未经核实的“相似事件”混作已证实重复。
- 池中178个文章版本对应140个上游身份，32个身份有多个版本；摘要中位122字符、最大154、无超过350者。故不把理论4000字符输入上限当作当批成本增加的实际原因。两版均未记录模型usage，旧实际SCF触发频率无保留证据，本轮不能量化真实token/账单倍数。
- 确认差异：旧公司名3次检索、新当前16词；旧每轮最多10篇一次AI、新逐版本/目标分析；旧整轮合并消息、新逐文章版本通知；新原始摘要/格式/元数据微改触发新版本；两版原本都是24h/+1h及最旧优先，扩大候选池与持久积压使隔夜回顾持续处理。两版均缺少基本面硬闸门，不能称旧版原本有语义事件去重/严格经营过滤，也不将单成员实耗解释为多人线性放大。
- 新增`docs/ALERT_QUALITY_PLAN.md`并放入PLAN当前优先级：规范身份和格式去重/基本面预筛/失败冷却及usage→2小时实时、09:00～09:15最多一次隔夜经营摘要、重要级每30分钟合并与发送到期→30天事件账本和最多5项小批量。重大新事实、实质更正、经营风险有独立路径；不同股票/关注点仍独立判断，不能为了零请求错用他人/其他目标结果。规则变更不能清库重发历史，先离线回放再发布；目标≥50%降低模型任务/token需实际验证，并非已达收益。
- 查阅DeepSeek官方Chat Completions用量字段和上下文缓存说明，计划保存输入/输出及cache-hit/miss等计数；API缺usage时标未知，不能用请求数/字符数代替token，失败已产生用量也计入。未查阅或调用账号账单/密钥API，没有承诺供应商缓存固定命中或固定节费。
- 实际完整离线`.venv/Scripts/python.exe -m pytest -q`：**368 passed, 1 skipped in129.96s**；唯一跳过仍是Windows真实Linux软链接用例。`git diff --check`通过。保留上一轮未提交的PROGRESS追加内容；本轮只有计划/记录变更，未提交、推送或部署，生产仍为0c988da…。旧平台真实历史账单、改后模型质量/费用、事件去重效果和新推送策略均未验收。

### 2026-10-23 新闻质量与成本整改实施

- 操作者在审阅计划后要求“按计划修改”，并在审批服务额度恢复后要求继续。使用gpt-6.1-sol子agent完成质量规则、批量分析、通知、迁移与文档的分工及交叉审查。
- schema4新增规范来源身份/原始观察/可信修订、保守确切事实事件账本、成员候选与摘要窗口、持久尝试和模型usage。标题高亮、空白、检索摘要和追踪参数不再制造修订；明确事实更正保留独立版本，摘要省略或历史摘录不能回退可信修订。旧评估、投递与原始记录通过映射保留，不清库重发；显式init-db可在非扫描时段幂等补建历史证据。
- 高置信纯资金流、涨跌成交回顾、两融、技术指标与普通评级模板先于AI拦截；经营/行业供需、业绩订单、事故监管等事实保留。普通实时仅可信披露后2小时；来源未知/冲突/未来时间隔离；隔夜经营事实09:00收集，09:10至09:15前最多一次3事件晨报。疑似重大新披露风险在证据约束下保留24小时例外。
- 普通重要事件每成员每30分钟最多1条摘要、每条最多3事件；到期和成员/订阅校验覆盖每段发送，重试不续期。尚未接受的同一事件允许补齐新命中目标的独立影响，已经接受后仅保存分析证据，不再提醒；已成功目标不被同篇其他失败目标阻塞。
- 成员扫描每轮最多10个新逻辑任务，DeepSeek按同组织/公开或私有成员域/重大预算资格分批，每批最多5项；逐项校验/缓存/恢复。失败退避5、15、60分钟，提供方熔断，预算不足不写成已完成。新闻预算默认每小时60000、北京时间自然日300000 token，普通最多使用80%，其余留给疑似重大。真实usage与未知实耗估算分列，配置管理intent另计；真实成员DeepSeek dry-run只预览缓存。
- 东方财富平时主词检索，:00/:15/:30/:45完整覆盖别名和主题词；主词空结果或失败回退全部查询，不修改成员配置。发布脚本新增未发送候选队列检查，仍保留原outbox/管理任务/备份/schema恢复限制。
- 150条合成回放：40条经营/重大/更正全部进入独立分析，40条纯行情、40条过期回顾在AI前拦截，30条格式/摘要变体未引起重复分析；这是规则回归，不是实际模型召回率或实测token节省。实际`.venv/Scripts/python.exe -m pytest -q tests/test_query_policy.py tests/test_image_deploy.py`为61 passed/1 skipped；CLI/成员CLI/批量用量33 passed；迁移/CLI13 passed；完整回归与发布结果在下节追加。
- 实际`.venv/Scripts/python.exe -m compileall -q stock_watch`、`git diff --check`及`.venv/Scripts/python.exe -m stock_watch monitor --dry-run --fake`通过，演练无真实模型或投递。生产只读核对：仍为0c988da…、schema3，唯一成员MaTianYe为admin/generation1，个人v2、3股1主题，环境和全局/个人配置指纹不变，app/worker健康且监控保持既有true授权。
- 在现有可信管理SSH上校验旧脚本哈希后，实际执行任务内`tmp/deploy-subscriptions.py install-tools`原子更新固定deploy-image.sh：39d5917b…→928f1fc1…；原脚本备份为deploy-image.pre-subscriptions-20261023T024659583778Z.sh。未修改受限发布入口、服务器权限或私密环境。自动审批服务曾因账号额度无法完成最后一次本地读取审查，该操作未执行；用户继续后已恢复，无安全绕过。
- 当前已过工作日15:05扫描截止；发布验收不强制补跑、不额外请求新闻/DeepSeek/企业微信。真实成本变化、模型召回率及手机收件质量需下个正常扫描时段观察，≥50%节费仍是目标，旧服务缺usage不能反推实际费用倍数。
- 最后交叉审查修复：历史已发/已评估只绑定它本身的事实修订，不能误标后来的更正已发；新修订时间未知/冲突不得借用旧稿可信日期，也不覆盖当前可信代表。补齐目标升级为重大时，未接受摘要可安全抽出立即提醒；整批输入超限或预算不容纳时自适应拆小，单项超限可查且不拖住其他任务。
- 增补用例定稿期间一次完整运行读到超出200字名称限制的中间fixture，结果1 failed/532 passed/1 skipped；修为合法配置后专用流水线28 passed。最终冻结全部业务代码和测试后，实际完整`.venv/Scripts/python.exe -m pytest -q`：**538 passed, 1 skipped in148.08s**。唯一跳过仍为Windows真实Linux软链接测试，交由Linux CI执行；`git diff --cached --check`、`compileall`通过。发布前与origin/main比较0/0、生产无待发送outbox或管理任务，配置与环境指纹均保持原值。
- 改动提交并推送为`72b72152ff6a777b68e171aa7559e2d2635c27e0`，触发现有发布[run39917582769](https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/39917582769)。实际使用任务内`github-ops.py jobs/test-evidence`只读取状态与脱敏汇总；Linux CI完整**539 passed in65.18s**，脚本语法检查通过。此时正在构建双架构镜像，生产验收待下节记录；没有为了验收新增真实新闻/模型/投递请求。

### 2026-10-23 15:30 新闻质量整改生产验收

- 发布run39917582769的test/image/deploy均success，业务SHA为`72b72152ff6a777b68e171aa7559e2d2635c27e0`；生产app/worker实际使用同一不可变镜像`ghcr.io/qinyinxiaoyun/stockwatcher@sha256:2bba91a7ae66b9450b6f514aefae9a1b378e0e2144c2677acb24059a8cd92b31`。镜像真实离线自检通过，容器SQLite3.46.1，仍沿用DELETE journal；没有启用WAL。
- 实际执行任务内`.venv/Scripts/python.exe tmp/deploy-quality.py verify 72b72152ff6a777b68e171aa7559e2d2635c27e0`：app/worker均healthy、UID10001:10001、只读根文件系统，worker新SHA心跳在07:30:27Z；公网`/health`、`/ready`均code0及同一新SHA。MONITOR_ENABLED=true沿用原授权。
- 数据库schema3→4、integrity_check=ok；显式init-db历史迁移marker已完成，236个旧key映射到179个规范报道，保留246条来源观察，8组已发送成员事实证据。8条迁移候选为queued且指向原已sent outbox，只作为去重证据；pending候选/outbox/管理任务均0。升级前后outbox均sent18，没有新增投递或重放。当前模型usage0，未人为补跑已结束时槽或发测试消息。
- 唯一成员MaTianYe仍为admin/generation1，个人配置v2、3只股票/1个主题，完整全局配置及成员表/配置JSON指纹和环境文件哈希与发布前完全相同。没有恢复旧v1配置、扩收件人或重置订阅生效时间。
- 发布自动生成`shared/data/backups/pre-image-20261023T072858443757Z.db`：schema3、integrity_check=ok，全局及成员配置指纹均与发布前匹配。升级schema4后禁止直接运行schema3旧应用；保留备份，不自动恢复覆盖共享库。
- 验收边界：已验证离线决策、批量请求结构/usage、迁移/幂等、通知时效/资格/半段恢复与部署健康；未在本轮新增真实DeepSeek/新闻/投递验收调用。真实模型召回、每100候选token及重复率/手机提醒体验仍需下个正常扫描时段观察，不能将Fake回放或0调用空窗当作实测节费。候选历史按成员全量读取的规模优化留待后续数据增长时处理。
- 15:34重复只读核对：新SHA心跳07:34:27Z，生产PRAGMA journal_mode=delete，app/worker最近10分钟ERROR/CRITICAL/Traceback计数均0；公网健康与配置/环境指纹再次通过，未发送/调用模型。只补文档的后续提交带`[skip ci]`，不重新部署已验收业务镜像。

## 2026-10-30 兆易创新消息偏向只读诊断

- 操作者要求先确定最近四五天兆易创新下跌与利好推送并存的原因，随后明确“先不要修改代码”。本轮提前写入的analyzer/delivery本地改动已全部撤回，`git status --short`确认业务代码和测试文件均无变更；未提交、推送、部署或修改线上配置。仅保存诊断与本节记录。
- 实际执行`Get-Content`/`rg`审阅news/analyzer/quality/subscription_scan/subscription_monitor/delivery及既有只读工具；通过`.venv/Scripts/python.exe tmp/audit-gigadevice-bias.py`使用既有严格管理SSH，对生产SQLite以mode=ro、query_only读取并汇总，不抓取新闻、不调用模型、不投递消息。生产仍为72b72152/schema4/prompt3，监控保持已有true状态，没有改开关、成员、关注点或生效时间。
- 脱敏快照`tmp/audit-gigadevice-bias.json`初查北京时间10:16，补核10:18:47，核心计数一致；窗口10月26日至30日上午。兆易配置为both、重大/重要，非只收利好。入库188篇相关文章，检索摘要24～150字、中位115字；当前配置键能关联110个分析任务：利好30、利空2、不确定78。已发送15个报道事件为14利好/1利空，摘要内事件数不等于手机提醒次数；采集、分析与通知使用不同时间和粒度，不能据此计算模型准确率或来源全网覆盖率。
- 已证实重复强化：10月28日回购类送达5个报道事件；10月30日已送达8个重大利好财报报道事件，反复引用同轮前三季同比增长事实，并非8次独立经营改善。现有保守事件身份仍受不同标题/URL影响；只读证据不能证明所有相关报道完全没有新增细节。
- 已证实遗漏市场风险视角：10月26日AI算力可持续性担忧/板块抛售报道被模型标普通、利空、非基本面、不推送。10月30日09:30的Q3归母净利润环比下滑/公允价值变动转负报道则被正确标重要/利空并送达。正面财报片段主要单独解释同比、营收/利润，未与负面片段合并；尚无证据证明模型将同段明确利空硬判为利好。
- 5个实际前置行情过滤样本正负皆有，均为纯交易回顾，未证实本窗口误杀经营利空；离线合成内存探针另复现“出口限制/财务造假/被立案”等词表缺口，属潜在风险，不冒充本次线上根因。72条时间冲突审计仅对应1篇反复扫描，不是72篇被隔离。
- 外部公开核验：英为财情历史行情与每经10月30日静态报道显示近期为震荡回撤，三季报10月29日盘后披露，不能将它解释为此前四五天下跌的已知原因。逐页核对Q1、半年报、Q3原PDF，确认同比/环比、归母/扣非及投资损益口径不可混用；中国基金报一条快讯的累计归母金额与原报表存在十倍差异，说明需原文核验，不据此推断媒体蓄意报喜。来源：https://cn.investing.com/equities/gigadevice-semiconductor-beijing-historical-data ，https://www.nbd.com.cn/articles/2026-10-30/4630067.html ，https://pdf.dfcfw.com/pdf/H2_AN202610291831176278_1.pdf ，https://www.chnfund.com/article/54bd9326-cc46-181f-c39e-3a24991785bd 。
- 结论与待验证：目前能证实单检索渠道/短摘要、同事件正反片段分开判断、重复利好强化及市场风险信息不推送。不能证实来源故意报喜不报忧，也不能确定这轮回撤由估值、预期差或资金兑现中的哪项造成；未采集的全网负面覆盖、全文对照召回率、发布前一致预期仍未验证。建议优先评估事件合并与原公告核验、正反证据共同摘要、经营影响与市场风险分开报告，保持不强制正负各半；本轮不实施。
- 实际完整离线回归`.venv/Scripts/python.exe -m pytest -q`：**538 passed, 1 skipped in137.45s**；跳过仍为Windows真实Linux软链接用例。`git diff --check`通过；仅有PROGRESS记录变更，没有业务代码/测试变更。离线回归不证明模型效果或新闻覆盖率。

### 同日继续：媒体取向与预期定价分析（仅研究）

- 操作者要求跳出单只股票，比较国内财经来源与WSJ等媒体，并分析利好提前定价的处理方式。本轮继续遵守不改代码要求，未修改业务、配置或线上状态；通过web读取论文原始摘要、官方媒体说明和数据定义，未接入新新闻/行情接口。
- Piotroski/Wong/Zhang（2017）对177万篇企业报道的研究，在同公司/月及同财报事件控制后发现官方与非官方报纸语调差异，支持部分媒体存在结构性乐观倾向；不能外推为所有国内媒体或2026年东方财富与WSJ的总体排名。未找到这两个来源按同事件/时间/体裁匹配的直接对比证据。原论文：https://chicagounbound.uchicago.edu/jle/vol60/iss1/6/ 。
- Pollach/Hansen（2021）对相同财报事件的公司新闻稿、研报与财经报道比较发现，公司稿/研报偏积极，财经记者在其样本中削弱公司积极语调；提示应区分原创采写、公司转述、研报与聚合，不能只按国别评价。原论文：https://journals.sagepub.com/doi/10.1177/0267323121999524 。WSJ官网明确新闻与评论分开，但自述规范不等于实证无偏：https://www.wsj.com/about-us 。
- 2021年财经信息整治原文同时涉及谣言、哄抬和恶意唱空等，作为监管环境的原始背景，不直接声称所有负面事实被禁止或量化当前平台的语调：https://www.cac.gov.cn/2021-08/27/c_1631652531513374.htm 。
- Hand等（2021/2022）支持同时考虑业绩与未来指引相对预期的差异：https://link.springer.com/article/10.1007/s11142-021-09597-6 。LSEG官方数据说明区分业绩预期差、公布前预测变动及公布后未来季度修订：https://lipperalpha.refinitiv.com/2022/11/product-insights-how-to-monitor-analyst-revisions-during-earnings-season/ 。
- 建议但未实施：按事件关联首次披露与原文、经营影响、公布前同口径预期、价格/估值背景及公布后相对表现；保存预期时间快照，缺数据标未知，分析师共识只作代理，不让模型臆造预期；已提前上涨或公布后下跌均不能单独证明充分定价或某条新闻的因果影响。媒体对比应按同事件去重后检查负面事实遗漏、证据链、时效和更正记录，不用利好标题占比或后续股价涨跌替代可靠性评估。
- 以上为概念框架和文献证据，未实测跨媒体语料、未实现新评分/去重/预期或估值模型。业务代码未变，沿用本轮上节完整离线回归结果，不重复执行同一套测试。

## 2026-10-30 新成员命令无回复：准入修复

- 操作者反馈新成员已加入企业及应用allowed user，但“帮助”“查看监控”无回复。代码复核与生产只读检查确认：独立订阅模式下，该成员缺少系统准入。应用可见范围不自动同步MemberRegistry；未准入成员会被HTTP入口拒绝，不生成inbox/回执。未取得该成员历史请求到达服务器的日志证据，因此不将此结论写成已观察到其实际请求返回403。
- 操作者提供精确UserID后，已通过现有可信运维通道为指定单个成员准入，默认subscriber，创建v1空订阅；操作前已备份并核对完整性。短事务内确认原成员记录、个人配置和全局配置保持原值；未重启、部署、更改监控开关或发送测试消息。成员身份参数及详细生产标识不写入本记录。
- 实际执行`.venv/Scripts/python.exe tmp/diagnose-member-access.py`；执行`tmp/admit-member.py`和`tmp/verify-member-access.py`时均传入操作者指定的单个UserID（此处省略身份参数）。后者以只读连接分别验证app与worker的现有授权判断，均通过。已请求成员重新发送命令；记录时尚无新收件/回执，客户端实际收到回复仍待验收。
- 在`docs/WECOM_SETUP.md`补充两层权限、准入和重发验收步骤；未改业务代码或测试，保留本轮开始时已有的研究记录。实际Python3.12.12；相关离线`.venv/Scripts/python.exe -m pytest tests/test_subscriptions.py tests/test_member_cli.py tests/test_member_callbacks.py`：**55 passed in3.21s**；完整离线`.venv/Scripts/python.exe -m pytest -q`：**538 passed, 1 skipped in140.34s**。跳过仍为Windows真实Linux软链接用例；测试通过自动fixture禁止外部网络。

### 同日继续：查询成功与帮助输入核对

- 操作者确认“查看监控”正常，但报告帮助收到模型结构错误。实际再次执行`.venv/Scripts/python.exe tmp/verify-member-access.py`（单个成员身份参数省略），只读核对入站字符特征与回执类型：查询命中view、处理完成且回执sent，仅发给本人；与帮助对应的失败输入存在字形误写，未命中固定帮助命令，随后走自然语言路径并返回结构错误。仅比对白名单词及状态，不在本记录保存原始成员消息。
- 进一步在app与worker的只读连接中，用现有CommandService分别执行固定“帮助”及`help`，均返回help状态且内容等于固定HELP_TEXT，个人版本保持原值；没有触发模型、外部投递或配置修改。已提示重新发送准确命令，真实帮助收件待成员确认。此轮未修改业务代码、测试或线上状态，沿用同一代码此前完整离线538 passed/1 skipped结果；不将拼写更正视为已修复自然语言模型输出质量。

## 2026-11-01 两位成员当前订阅查询

- 按操作者要求，实际执行`.venv/Scripts/python.exe tmp/read-member-subscriptions.py`，通过既有严格SSH，在生产容器内以SQLite mode=ro/query_only/短读事务读取成员注册及各自的MemberConfigStore快照，不读取消息历史或凭证。查询时两位成员分别为4股/1方向与1股/0方向；目标均启用，股票均为双向，推送等级均为重大/重要，无额外股票关注点。详细成员身份和订阅内容仅向操作者展示，不存入本记录。
- 本轮仅查询和更新记录，未修改业务代码、测试、线上订阅、监控开关或部署；未发起新闻/模型/投递请求。真实收件及新闻覆盖效果不在本次查询验收范围。

## 2026-11-02 “已定价”当前实现核查与设计建议（未实施）

- 操作者要求核对目前“已定价”逻辑并说明股票分析及代码实现方式。沿用此前先分析、不改业务代码的要求；只读审阅当前源码，不访问生产、凭证、新闻/模型/行情业务接口，不推送或部署。开始时已有PROGRESS和WECOM_SETUP改动均保留，后者本轮未修改。
- 实际执行`git status --short`、`rg -n`及`Get-Content`。当前analyzer提示v3/结构v2，输入仅标题/摘要/来源/披露日期/URL与目标关注点；AnalysisResult、白名单校验、推送规则没有定价状态、预期快照、行情或估值输入。event_type中的pricing为事件类别，不是股价已定价指标；seen、事件去重和2小时新鲜度也不表示市场已吸收信息。现有模型若写出“已定价”，没有独立数据计算或校验支持。
- 建议分开保存四类证据：事实已公开程度、相对公告前预期的差异、事件前后相对行情、当前估值隐含的经营要求。不将“没涨/先涨过”转换为priced_in布尔值或价格消化百分比；共识只是预期代理，来源过期/覆盖不足/口径不符均返回未知。
- 事件先按公司/市场、公告编号或报告期建立稳定身份，新增实质事实建立版本。每一事实版本的首次可信公开时间作为本次t0；转载沿用原时点。最终财报的新细节与此前预告属于同事件族但不机械共用预告前的旧预期基准。
- 所需快照字段包括as_of、available_at、ingested_at、source/evidence ID、财务期间、单位币种、归母/扣非或GAAP/调整口径、预测人数/分歧/日期，以及价格所属交易所、交易日历、基准和公司行为口径。公告前预期和事后修订分开，结果只使用截止时点已可得的证据；事后行情仅进入后续观察，不倒填原判断。
- 建议新增pricing_context（数据适配/时间快照）和pricing_assessment（Python纯函数计算/枚举与证据校验）模块；模型提取受约束事实和解释，不能自由决定已消化比例。事实分析缓存与动态市场评估缓存分离，后者纳入事件版本、市场/预期快照、as_of、规则版本及估值假设摘要；不能仅沿用目前15天新闻缓存。
- 复评需独立worker入口：目前已评估成员/文章/目标会被跳过，单改缓存键不自动重算。市场快照更新不制造新事件、不清理已发证据、不延长提醒期限，也不默认产生新推送；数据缺失不得阻断已有重大经营风险提醒。采集及远程请求继续在写事务外，由唯一worker调度，测试继续全离线。
- 量化上先计算同口径实际值减公告前共识，亏损或接近零时不硬算百分比，指标方向与指引分别解释。进阶才在明确假设网格下做反向估值/事件两端情景；若展示(P−V_base)/(V_full−V_base)，仅称条件模型情景位置，非客观已定价比例或成功概率，分母退化/超界不截断伪装为0至100%。
- 公开一手核验通过web读取：Hand等财报预期差研究 https://link.springer.com/article/10.1007/s11142-021-09597-6 ；FactSet历史时点共识方法 https://insight.factset.com/resources/at-a-glance-factset-estimates-point-in-time-consensus?hs_amp=true ；Damodaran反向隐含增长讲义 https://pages.stern.nyu.edu/~adamodar/pdfiles/eqnotes/dcfveg.pdf 。未取得或购买具体股票的历史共识/行情数据，未拟合定价模型或验证预测准确率。
- 实际完整离线`.venv/Scripts/python.exe -m pytest -q`：**538 passed, 1 skipped in140.06s**，跳过仍为Windows真实Linux软链接用例。未新增业务代码/测试；该结果仅验证现有基线，不表示上述定价设计已经实现或通过效果检验。

## 2026-11-02 今日成员投递条数只读查询

- 按操作者要求，实际执行`.venv/Scripts/python.exe tmp/count-today-deliveries.py`，通过既有严格SSH，在生产SQLite使用mode=ro/query_only/短读事务，仅查询投递元数据，不读取新闻正文、模型解释、回执正文或凭证。窗口为北京时间当日00:00至17:43查询时点，按sent状态及sent_at归日，新闻与指令回执分别统计；accepted_chunks为成功接受的消息段，不用摘要items数量或计划分段数替代。
- 两位成员的新闻通知分别为11次/4次，对应15条/4条企业微信接受的消息段；前者4次各分2段。指令回执分别0条/1条，消息段合计15条/5条。逐行累加与SQL分组结果一致，共16条成功投递记录、20个已接受消息段；未归属记录、收件作用域不一致及非sent的已接受分段均为0。详细成员身份不写入本记录；接口接受不等同于客户端已读，系统不保存逐段接受时间，不能据此精确重建跨日发送时刻。
- 本轮仅查询及更新记录，未修改业务代码、测试、订阅、监控开关或部署；未触发新闻/模型/消息投递请求。该结果为截至查询时点的部分日统计。

## 2026-11-02 基本面与新闻驱动的收益预测结构设计（未实施）

- 操作者提出以预测关注股票股价变动为目标，要求说明程序结构、需要改变的方面和困难。本轮仅研究与设计，继续遵守此前不改业务代码要求；保留既有PROGRESS/WECOM_SETUP改动，没有访问生产、修改运行配置或触发真实新闻/模型/投递。
- 实际只读执行`rg --files`、`Get-Content`、`rg -n`、`git status --short`，核对worker、分析任务、订阅、ARCHITECTURE、FUTURE_ROADMAP及15天保留/5分钟扫描/半小时普通通知边界。新增`docs/FUNDAMENTAL_PREDICTION_DESIGN.md`，明确当前是文章级经营分类，目标结构应是公司基本面/预期状态与事实事件版本、固定时点收益预测及结果验证。
- 设计分列经营影响、相对事前预期增量、指定窗口绝对及相对收益预测；内部研究预测不冒充市场共识。建议首版5交易日主窗口、1/20日辅助，先财报/指引，再扩其他事件；不是已验证最优参数。来源证据、基本面、内部预测、共识与行情均版本化，研究历史不随15天热缓存删除。
- 预测时间使用系统实际可用判断时点，通知时延另记；公告后、系统分析前已发生涨幅不算预测命中。已有5分钟扫描/别名15分钟补查/半小时普通摘要不直接承诺公告瞬间优势。独立worker持久任务做研究刷新、事件更新、复评及结果采集，不新增Web调度器/cron；保留成员隔离、预算、短事务、outbox/分段恢复及已发去重。
- 规划离线比较行情/行业基线、基本面、再加入新闻/预期差后的样本外增量，并进行实时影子验证。重大事件事前定义、同事件/重叠窗口不混入训练测试两边、保存弃权与全部预测；模型概率需经校准，LLM自报confidence不算预测概率。未建立新模型或验证方向/幅度准确率。
- 外部一手资料通过web读取：Hand等财报预期差研究、FactSet PIT官方说明、CFA Institute回测与模型验证资料、Glasserman/Lin关于LLM历史回测偏差的工作论文，链接在设计文档内。困难包括可靠历史时点数据、市场预期不可直接观察、行业模板维护、样本稀疏/过拟合、延迟和市场环境变化；未采购数据、扩大权限或纳入自动交易。
- `git diff --check`通过；仅文档变更，没有业务代码/测试变更。沿用本聊天同日相同业务代码完整离线回归**538 passed, 1 skipped in140.06s**，未因仅设计文档重复运行；该基线不代表预测设计已实现或具有金融效果。

## 2026-11-03 新增关注时的个股/方向研究底座（设计补充）

- 操作者询问商业模式、竞争优势之外还应建立哪些分析。继续仅分析设计，不改业务代码、订阅或运行状态。只读核对已有设计与git状态，并在`FUNDAMENTAL_PREDICTION_DESIGN.md`的公司状态部分补充新增关注研究要求；保留已有PROGRESS/WECOM_SETUP改动。
- 个股补充十部分：商业模式、行业/价值链/周期、竞争优势证据、盈利驱动、财务现金与负债质量、治理与资本配置、内部情景/外部预期、估值隐含要求、风险/反证/证伪条件、催化与跟踪计划。初始产出为版本化研究基线、关键假设表及事件到经营变量的监控映射，事实/推断/预测/未知须区分。
- 方向另补边界、终端需求与利润池、产业链受益/承压映射、技术替代与商业化阶段、供需/库存/资本开支/政策执行及代表对象；概念关联不等于利润暴露，研究代表名单不自动新增成员订阅。基本研究可用与收益预测可用分别标记，不因基础未齐删除订阅或用模型补造数据。
- 通过web读取CFA Institute《Company Analysis: Past and Present》《Industry and Competitive Analysis》公开说明与Damodaran增长价值驱动资料，支持业务/财务/竞争/投资与现金流综合研究；未对具体股票或方向完成研究，未抓取新的企业数据或调用业务接口。来源：https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/company-analysis-past-and-present ，https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/industry-and-competitive-analysis ，https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/growthvaluedrivers.htm 。
- 本轮仅文档补充，无业务代码或测试变更；沿用此前相同业务代码完整离线538 passed/1 skipped基线，不将文档设计视为已实现或完成预测验收。

### 同日继续：新闻差异更新流程与重复提醒修复状态

- 操作者询问研究框架下的筛选/已有信息更新操作，并确认重复推送是否仍未解决。本轮仍不改业务代码，仅在`FUNDAMENTAL_PREDICTION_DESIGN.md`补充“事实差异→研究状态→通知资格”的独立决策流程和分类型更新策略；保留所有已有工作区文档改动，不提交、推送或部署。
- 实际执行`git status --short`、`git log -5 --format`、`rg -n`及`Get-Content`审阅quality、subscription_scan、notifications、delivery与已有诊断；HEAD仍e8734bf文档提交，关键业务代码最后变更72b7215（10月23日），本轮无业务修改。`event_identity`仍依赖主体/类型/报道披露日/完整规范标题；新候选默认article身份，跨标题同一业务事件未根治；现有目标分析合并和摘要限频不等于正反事实综合。
- 只读合成内存探针复现同回购/同财报换表述会产生不同事件；同标题摘要新增环比下降、但无明确更正证据时revision_key不变，存在漏重评风险。探针说明规则缺口，不冒充生产最近三日已发生的重复率或遗漏量。
- 经既有StrictHostKeyChecking管理SSH，以docker exec只读取得worker版本/schema和四模块标准化LF的SHA256；北京时间2026-11-03 10:18:15，生产BUILD_SHA仍72b72152ff6a777b68e171aa7559e2d2635c27e0/schema4，quality/notifications/subscription_scan/subscription_monitor均与本地当前文件一致，没有新修复悄然上线。未查询最近三日文章、写库或请求新闻/模型/消息投递。10月30日的回购5个报道事件、财报8个重大利好事件仍只作为当时证据，不称为今日统计。
- 状态结论：报道级、相同确切事件键/成员候选及成功分段检查点保护已存在；不同标题/跨日的同一业务事实重复提醒尚未完成修复；发送成功但结果不明或落盘前中断的网络窗口仍不保证绝对一次。不能把测试通过称为事件语义去重已经解决。
- 设计要求追加证据及事实版本、冻结旧预期、只更新受影响驱动/预测，冲突待核实不覆盖确定值；重复补来源不通知，实质新指标/更正/确认才按成员上次已通知状态评估增量。已接受分段后冻结原文，必要更正另发增量；不清seen、不改订阅生效、不用转载续期。
- 实际完整离线`.venv/Scripts/python.exe -m pytest -q`：**538 passed, 1 skipped in140.14s**（既有Windows真实Linux软链接用例跳过）。本轮仅文档变更；测试验证现有规则的回归基线，不证明上述尚未实现的改进有效。

## 2026-11-03 业务事件归并、事实差异和成员已通知记录修复

- 操作者明确要求按方案实施这三部分。本轮本地修改代码及离线回放，未访问生产、凭证或真实新闻/模型/消息接口，未部署、推送提交、更改成员订阅/监控开关或触发真实投递。普通沙箱启动失败，文件读取与离线测试使用获准的沙箱外命令通道；未扩大服务器权限。
- 新增`events.py`的有限显式业务锚归并、稳定证券身份、事实版本/来源链/更正与当前指针；接入quality和scan。报告期、公告/合同/订单/回购计划标识可跨标题/跨日复用；无可靠锚继续保守确切处理，主题不伪装成已证明公司事件。原始版本保留，未知/冲突改文不能污染可信canonical绑定。
- 事实比较分列期间、币种单位、归母/扣非/扣非归母口径、绝对值/同比/环比、指引与实际、数值/完整区间及实施状态。新增指标重新评估；截短摘录不抹更正；矛盾不覆盖确定值；传闻/观点/预测及否认语气不写为已实施。晚发现的旧更正只归档，不能回滚current；可信首次公开时间只可收紧，观点或转载不续期。原始fact JSON/版本差异保留，独立时间索引与状态指针可收敛。
- 新增`notification_acceptances`，仅真实接口返回接受后随outbox检查点/最终状态在同一短事务落账；候选、已评估及研究最新状态不再冒充已通知。组织/成员/事件/实质变化幂等，完整事实子集及多条保留候选的A+B组合转载防重；已接受任意分段后冻结原文，digest未全部接受不谎称未覆盖条目完整送达。更正按成员实际接受值展示before/after，首次提醒明确区分原披露值。
- 提示升级v4；仅对有证明身份的材料附加程序整理的事实/变化上下文，要求按增量重要性判断。旧重大指标不能让新增普通细节自动变成重大通知。上下文不改源文章归档，不作为外部可执行意图；保留独立目标/关注点/隐私/模型版本缓存与预算边界。
- 升级使用独立`business_report_revisions_v1`和`business_event_receipts_v1`组织标记。真实旧v4哈希、已有quality_history_v1、基线/已评估/过滤/过期决策会映射到同一旧正文的新键，不借最新代表把旧决策迁到新指标。历史sent/部分接受才导入接受证据；旧pending/sending仅在候选侧附事件元数据，原outbox JSON不动，发送前恢复严格同成员/原文修订的元数据防重，实际接受后才记账。既有真实检查点时间不因迁移覆盖；历史partial无精确时点时导入时间只能视为证据观察时间，不用于投递时延分析。
- 新增`tests/test_event_facts.py`、`test_notification_ledger.py`、`test_business_event_pipeline.py`、`test_event_upgrade.py`，并修正跨目标合成独立事件fixture（不能复制另一事件的event_links）。新增`docs/BUSINESS_EVENTS.md`并同步架构说明。开始时已有PROGRESS/WECOM_SETUP和研究设计文档变更均保留；过程中出现的research/research_cli、db/cli及其schema断言并行修改不由本轮覆盖或提交。

### 实际执行与结果

- `git status --short --branch`、`rg --files`、`rg -n`、`Get-Content`：核对方案和扫描/投递/迁移调用点；确认原先事件身份依赖标题、候选默认article身份，以及入队即写quality_member*的混淆。后者保留兼容评估证据，实际防重使用接受账本。
- `.venv/Scripts/python.exe -m pytest tests/test_business_event_pipeline.py tests/test_event_upgrade.py tests/test_quality_migration.py -q --tb=short`：**27 passed in4.22s**，含旧pending不冒充已通知、正文保持、接受后强事件防重。
- `.venv/Scripts/python.exe -m pytest tests/test_notification_ledger.py tests/test_business_event_pipeline.py tests/test_event_upgrade.py tests/test_event_facts.py tests/test_quality_migration.py -q --tb=short`：中间稳定版**106 passed in7.39s**；后续增加旧格式发送前检查及组合口径负例。
- `.venv/Scripts/python.exe -m pytest tests/test_event_facts.py tests/test_notification_ledger.py -q --tb=short`：**80 passed in3.17s**，包含最终扣非归母口径分离。
- 完整回归初轮定位并修复迁移历史source_published_at为None的两例；一次与协作文件更新重叠的回归产生7个旧fixture定义失败，文件稳定后专项均通过。随后的完整`.venv/Scripts/python.exe -m pytest -q --tb=short`：**665 passed, 1 skipped in148.54s**。
- 最终完整`.venv/Scripts/python.exe -m pytest -q --tb=short`：**666 passed, 1 skipped in149.68s**。跳过项为既有Windows真实Linux软链接用例，不伪称Linux生产迁移/效果验收通过。该完整结果覆盖当前共享工作区，包括保留的并行研究模块变更；本轮不将其归为自身实施范围。
- `.venv/Scripts/python.exe -m compileall -q stock_watch`、`git diff --check`：通过；PROGRESS既有CRLF仅有Git换行提示，无空白错误。

### 剩余验收边界

- 有限模板不等于已解决全部跨语言/无标识语义归并，不能用合成测试声称实际线上重复率归零。缺乏可靠业务锚时保守回退；完整基本面、市场预期与收益模型不是本轮实现内容。
- 本地旧v4数据库fixture升级、通知失败/部分接受/隔离/版本恢复均有离线回放；生产库副本、真实成员收件及上线后重复/遗漏效果未验收。本轮没有操作生产，不能把旧聊天中的部署证据当成本次修复已上线。
- 接口已接受但本地落盘前中断、网络结果不明的窗口仍不保证绝对一次；已接受不等于已读。没有清空seen、修改订阅生效时间或为转载续期。

## 2026-11-03 本地研究基线代码与当前关注公司初始归档

- 操作者明确要求“先进行本地修改：按照方案实现研究基线代码，并建立已关注股票的研究基线”。本轮落实阶段A的研究归档、关键假设与监控映射；不部署、提交或推送，不接入收益预测/新采集调度或真实模型投递。保留开始时已有PROGRESS/WECOM_SETUP及研究设计改动；工作区并行出现的业务事件/通知账本修复不回退、不冒充本研究模块实施内容。
- 只读审阅AGENTS、设计与既有代码后，以原有StrictHostKeyChecking管理SSH/SQLite mode=ro/query_only核对当前启用证券并集；仅输出证券代码/名称/核对时间，未复制成员身份/私有关注或消息。得到301308.SZ、600183.SH、600436.SH、603986.SH、688981.SH、9988.HK，写入被Git忽略的data/research/watchlist.json；未修改任何线上状态或请求线上新闻/DeepSeek/微信。
- 新增research.py、research_cli.py及合成测试。schema5追加研究公司/证券、准备任务、不可覆盖基线和复核请求；保存十部分研究、来源时间/位置/币种口径、精确数值、核心假设/反证和事件到指标的映射。公共研究不复制成员背景，支持双上市证券独立币种、无历史占位公司的受控归属、同文件幂等、历史截止与实际建立时间双重约束。迟到旧基线不覆盖当前或清除needs_update，重复导入不能抹复核请求，新闻热数据清理不删除研究历史。
- 事实/推断/假设/未知分开；程序核对四项核心有据事实、最新期财报与同期间指标、支持假设和映射。估值/共识缺失不阻断基本面起点，但expectation_comparison和validated_prediction始终false，拒绝导入未经验证的能力true。无来源事实、任意执行字段、未来资料、无时区日期、NaN/Infinity及浮点财务值均拒绝；整数/精确十进制文本保留期间/币种/单位/会计口径，不执行模型生成代码。
- gpt-6.1-sol研究agent核对公司/交易所/巨潮正式报告并建立六份输入JSON，原始来源定位、事实与条件解释分别留存。兆易2026Q3、江波龙/生益2026Q3及半年报、SMIC2026Q2 HKEX报告、阿里2026六月季度官方报告均有核对；片仔癀只核到2026Q3官方目录，SSE原PDF反复不可达且巨潮公告页timeout，补入正式2026H1原件的业务与多期财务，明确最新期未对齐，不用第三方转载冒充核验。root另外通过web打开江波龙三季报及片仔癀半年报交叉检查；巨潮兆易链接root单次timeout，保留研究agent成功核对和URL而不虚报root重取成功。
- 六份基线均含十部分、至少两条假设及三条监控映射。五家通过基础研究证据门槛；片仔癀partial/latest_financial_period_not_reconciled。当前市场价、历史共识、完整债期等缺项仍为unknown，不生成目标价/上涨概率/“已定价比例”。材料截止保留研究采集阶段时点，实际本地建立为2026-11-03T04:34:41Z；不得在早于created_at的历史查询中冒称已形成判断。
- 本地数据写入前，以SQLite online backup建立data/backups/pre-research-20261103T043249Z.db并核integrity=ok。当时本地库schema2，随后仅本地升级5。实际执行 `.venv/Scripts/python.exe -m stock_watch research prepare --watchlist data/research/watchlist.json`、`research import data/research/baselines`、`research export --output data/research/reports`；六个占位任务原子归属为正式公司，最终6个版本/6家公司，5 usable/1 partial；原16个业务表（app_meta仅排除schema_version元数据）内容指纹全部不变，旧配置/seen/outbox未变化。生产库没有升级。
- 可阅读索引data/research/reports/index.md及六份Markdown/JSON，输入data/research/baselines/和验收data/research/acceptance.json均在持久data目录且不进Git。导出使用安全文件名/Markdown和HTML转义；片仔癀索引明确“三季报待核，已核历史指标见报告”，避免把检查目标期当已核期间。README与RESEARCH_BASELINE.md记录CLI入口、合同/能力/保留边界，研究设计状态仅更新本地研究阶段。
- 实际研究专用 `.venv/Scripts/python.exe -m pytest -q tests/test_research.py tests/test_research_cli.py tests/test_research_migration.py`：**41 passed in0.45s**；覆盖schema4→5配置/投递保留、历史时点、旧回填、事务回滚、准备→正式归属、复核、输入/导出安全、公开订阅并集不复制私有背景、40天新闻清理后研究仍存。最终完整 `.venv/Scripts/python.exe -m pytest -q --tb=short`：**676 passed, 1 skipped in148.29s**，包含共享工作区其他已存在改动，不将这些测试归为本研究模块独有成果；跳过仍为Windows真实Linux软链接用例。compileall和git diff --check通过。
- 实际再次逐份append相同文件核对ID和版本：仍为6版本，和只读证券清单完全一致；SQLite integrity=ok，本地MONITOR_ENABLED=false，收益预测false。后续待核事项：片仔癀Q3原件、各公司更多期财务和独立反证、行情/事前预期数据及后续研究更新接入。已有研究不会自动接入新新闻提示或触发推送，新增关注使用显式research prepare，尚未启用worker研究刷新。

## 2026-11-03 最新版本发布准备

- 操作者授权“将最新版本部署到环境”，发布范围包含当前已完成的业务事件/事实差异/实际接受账本及研究基线代码，研究数据单独进入持久目录。沿用现有组织、成员、订阅和监控授权，不启用新研究采集调度/预测模型，不做额外新闻/模型/试发请求。
- 本地git与origin/main起点均e8734bf；完整离线 `.venv/Scripts/python.exe -m pytest -q --tb=short` 再次 **676 passed, 1 skipped in165.06s**，compileall和diff --check通过。两名gpt-6.1-sol子agent只读审阅事件升级/通知及研究导入，未发现确认阻断；内存库实导6资料、5基础门槛通过/1部分覆盖、两预测能力false、重导仍6版本。
- 新任务内tmp/deploy-current.py替代只支持唯一成员/schema4的旧验收工具，输出只含计数/哈希/版本与状态。实际baseline只读核对：生产72b7215…/schema4，两位active成员、个人版本均3，app/worker健康，原环境哈希208531db…、固定部署脚本928f1fc1…，pending outbox/管理任务/候选均0，journal=delete，完整性ok。成员注册/配置/审计与全局配置分别保存匿名指纹供发布后核对，不在记录保存成员消息或凭证。
- 实际执行 `.venv/Scripts/python.exe tmp/deploy-current.py rehearse 72b72152ff6a777b68e171aa7559e2d2635c27e0`：服务器SQLite在线影子备份留在shared/data/release-checks，当前不可变旧镜像挂载新源码，在--network none/只读根/UID10001/降权能力下执行init_db+prepare_history及6份研究导入，schema4→5、integrity、配置/成员/seen/outbox原状态和正文指纹均保持，重复导入不增版本，导出成功。未下载生产库或把它变成fixture，未停止/写入正式业务库。
- 演练工具先后修正审计表名称、宿主Python路径API兼容及最小权限目录可读性，失败仅发生在读取或影子目录/容器，正式数据未修改；最终结果ok/schema5/6公司/5基础可用/1部分覆盖/idempotent/preserved。正式发布仍经过原队列保护、停机备份、初始化及健康/版本心跳链路；不设置WORKER_QUEUE_APPROVED，不清队列，不改.env或受限SSH入口。

### 2026-11-03 15:40 最新版本生产验收

- 发布业务SHA为f3085e9e113bce25034c3f6fbf6bfbe62f25c2af，经既有main发布[run41588319498](https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/41588319498)完成test/image/deploy，三项均success。Linux完整 **677 passed in76.40s**（本地Windows跳过的真实软链接用例在Linux已执行）；正式镜像为`ghcr.io/qinyinxiaoyun/stockwatcher@sha256:9d25cd3a134346eb838585a25611d46590110bea33d33e66df4128a561a8bdd0`。未更改原受限发布入口或固定部署脚本928f1fc1…，未绕过待办队列保护。
- 升级前自动备份shared/data/backups/pre-image-20261103T061543024085Z.db：schema4、integrity=ok，完整成员注册/配置/审计及全局配置指纹与发布前一致。schema5升级后不能直接运行低schema旧应用，不自动恢复覆盖共享库。
- 实际执行`tmp/deploy-current.py verify f3085e9e113bce25034c3f6fbf6bfbe62f25c2af`核对新镜像/版本：app/worker均healthy、只读根、UID10001:10001，新SHA心跳07:40:15Z；公网health/ready均code0与相同SHA，最近10分钟ERROR/CRITICAL/Traceback均0。正式SQLite schema5、integrity=ok、journal=delete；环境哈希208531db…、全局及两位active成员完整配置/审计指纹与发布前完全相同，个人版本仍均3。监控保持既有true授权，没有新增收件人或重置订阅。
- `business_report_revisions_v1`与`business_event_receipts_v1`标记各1，已重建业务事件507、事实271、版本529、实际接受证据91。升级后自然15:00及之前各正常时槽有成功记录，未强制补跑、试发或调用额外模型来验收。原sent outbox仍87；pending通知、候选及管理任务均0。事件表计数不等于手机提醒数，接受证据不代表已读，也不据空窗声称线上重复率归零。
- 实际执行`tmp/deploy-current.py import-research f3085e9e113bce25034c3f6fbf6bfbe62f25c2af`：先持原部署锁并生成pre-research-import-*在线备份及完整性检查，再从stdin接收本地六份公开JSON，使用短事务追加导入并导出到`/srv/stock-watch/shared/data/research/reports/`。输入资料没有放入Git/镜像层或混入成员私有背景；此过程未访问新闻/DeepSeek/消息接口，没有写seen或outbox。
- 实际导入结果6公司/6证券/6基线版本/6研究任务，5基础研究门槛通过/1部分覆盖；重复导入不增版本，预期比较和经过验证的收益预测均未启用。片仔癀保留已核半年报和最新三季报待核状态。导入后再次verify，库完整性、所有配置/环境指纹、健康与心跳均通过；研究资料处于生产持久目录，仍未接入自动研究刷新或实时新闻判断。
- 更新PLAN、业务事件说明和研究设计/使用文档标记实际发布。后续仅文档提交带[skip ci]，不重新部署已验收业务镜像；剩余研究缺口及真实事件归并/手机收件效果按自然运行观察，不将离线程序测试解释为金融预测有效性。

## 2026-11-03 成员订阅与研究网页实施/发布准备

- 操作者要求让stock.janettepa.uk展示所有用户及关注股票，并能进入股票基线分析状态子页。实际HTTP核对原首页404、health/ready均200且f3085e9…；域名已经承载回调/健康/推送，故在现有WSGI app加首页/登录/股票详情，保留原Nginx、DNS、证书、回调与推送路由，没有迁往新的托管平台。
- 参考Sites页面/预览规范，按用户指定的既有服务器部署方式实现；三名gpt-6.1-sol子agent分工只读数据、视图及专用会话鉴权。新增dashboard.py/dashboard_auth.py/dashboard_data.py/dashboard_views.py，settings增加4个独立后台配置字段（密码/会话密钥repr隐藏）。app没有调度器或研究采集，没有修改schema5。
- 总览显示本组织所有成员含停用状态、版本、股票/主题/关键词、方向/等级/关注点和研究覆盖；股票页显示证券币种、四项能力、最新期是否核对、十部分研究/精确指标、假设反证/监控映射、来源与历史。数据使用mode=ro/query_only/短读事务及字段白名单，不读消息/确认/凭证或其他组织。首次访问及所有未认证子页只显示登录，不提前打开资料库；私密响应no-store/noindex，HTML/来源URL转义。
- 独立管理员登录不复用推送密钥；8小时HMAC会话、HTTPS/HttpOnly/SameSite Strict Cookie、CSRF/固定Origin、密码与用户名轮换撤销、有限失败冷却/有界内存。语义占位/缺配置不允许私密页面。源码及测试不保存真实凭据。
- 实际相关离线测试69 passed（当时auth后续加固后全量覆盖增加）；最终完整 `.venv/Scripts/python.exe -m pytest -q --tb=short`：**735 passed, 1 skipped in155.20s**。compileall通过。真实生产库无网络影子演练 `.venv/Scripts/python.exe tmp/deploy-current.py rehearse f3085e9e113bce25034c3f6fbf6bfbe62f25c2af`：schema5保持、6公司/5基础门槛通过/1部分、重复幂等、旧配置/成员/seen/outbox指纹不变；没有写正式业务库。
- 本机loopback预览仅使用demo_admin/demo_member合成身份和公开研究文件；HTTP实际检查总览、兆易和片仔癀详情、登录和CSS均200，链接/表格存在，片仔癀最新期明确待核，不含inline脚本/样式。通过open_in_codex请求展示预览。自动cua浏览器两次初始化均因Windows沙箱helper/受信Node进程失败，未绕用其他浏览器驱动，不能声称视觉/移动端浏览器验收完成；后续按真实HTTPS功能验收记录。
- 实际任务内 `tmp/portal-ops.py provision` 仅在部署锁下原子追加DASHBOARD_USERNAME/PASSWORD/SESSION_SECRET/ORIGIN，原文件私密备份为shared/.env.before-dashboard-20261103T131254566507Z，原所有非后台参数逐行保持。环境哈希208531db…→897f484f…；root600不变，原回调/推送/DeepSeek和MONITOR_ENABLED未变。网页登录信息仅写本机未跟踪.env.dashboard.local，签名密钥仅在服务器；工具stdout只输出路径/哈希/状态，没有密码/会话值。
- provisioning后的生产baseline：仍f3085e9…，两位active、原配置/审计指纹cec5fb02…和全局d492eb01…，schema5/完整性ok、6研究版本；pending候选/outbox/管理任务均0。旧容器忽略新增网页配置，待新发布读取；现有worker授权保持true。本轮不强制扫描、试发、清队列或新增成员。

### 2026-11-03 21:28 网页生产验收

- 业务提交39b61f2521e88a10a125d3e61bd241979fe05d89经[run41643640789](https://github.com/qinyinxiaoyun/stockWatcher/actions/runs/41643640789)完成test/image/deploy，三项success。Linux完整 **736 passed in72.13s**（包含Windows跳过用例）。生产不可变镜像`ghcr.io/qinyinxiaoyun/stockwatcher@sha256:6b9eaf8fcb0b98b5e5f04656979908b47d4579c9433ab24a291c3af15c355e2f`，没有修改Nginx/DNS/原受限部署入口或固定脚本928f1fc1…。
- 原链路生成shared/data/backups/pre-image-20261103T132545994557Z.db，schema5/integrity=ok；成员注册/配置/审计及全局配置指纹与部署baseline一致。正式库仍schema5、DELETE journal、完整性ok，研究仍6公司/6证券/6版本/6任务，无研究或成员信息重置。
- 实际 `.venv/Scripts/python.exe tmp/deploy-current.py verify 39b61f2521e88a10a125d3e61bd241979fe05d89`：app/worker均healthy/UID10001:10001/只读根、新SHA心跳13:28:36Z；公网health/ready为200/code0/同SHA，最近10分钟错误计数0。新增网页环境哈希897f484f…与baseline相同，所有原非后台环境参数和两位active成员配置指纹保持原值，MONITOR_ENABLED既有true未变。
- 实际 `.venv/Scripts/python.exe tmp/portal-ops.py check 39b61f2521e88a10a125d3e61bd241979fe05d89` 通过：CookieJar在内存读取专用登录信息，真实HTTPS首页返回登录且no-store，正确CSRF/Origin登录进入总览；六个实际股票页均200并有研究章节及来源，片仔癀最新期保持待核；POST退出清Cookie，随后直接访问子页只见登录不见研究/成员数据。工具只输出成功布尔与页面数量，不记录密码、Cookie或私密页面正文。
- 当前pending通知/候选/管理任务均0，outbox仍sent87，模型usage仍275；本轮没有强制扫描、付费模型或实际消息测试。原微信/推送路径保持同一路由并通过离线回归；不将本轮网页HTTP测试称为重新验证手机真实收件。
- 使用open_in_codex请求展示正式入口https://stock.janettepa.uk/，替换本机合成预览；已停止本任务owned loopback预览进程。实际浏览器视觉/移动适配尚未通过自动浏览器实测，限制已写WEB_PORTAL.md，HTTP与离线功能验收已完成。
- 初始用户名/密码/入口仅保存在本机被Git忽略的.env.dashboard.local，服务器签名密钥没有复制到本机文件。没有把登录密码发入聊天、日志、提交、URL或研究fixture。后续仅文档提交带[skip ci]保存验收状态，不重新部署业务镜像。
