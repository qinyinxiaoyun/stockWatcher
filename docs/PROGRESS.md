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
