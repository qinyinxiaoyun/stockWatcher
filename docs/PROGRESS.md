# 项目进度

更新时间：2026-10-21（Asia/Shanghai）

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

## 明确不在本轮执行

- 不部署生产服务器，不启用真实定时监控，不停用旧云函数，不发送真实微信消息，不消耗 DeepSeek 额度。
- 阶段 3/4 的真实服务器安装、迁移切换和 Git 自动部署留待后续阶段。

## 待真实环境验证

- 企业微信 CorpID/AgentID/Secret、回调 Token/AES Key、管理员与测试收件人。
- HTTPS 域名、企业微信后台回调保存、客户端双向消息、DeepSeek 与东方财富真实网络表现。
- 服务器资源、Docker Compose、重启持久化和真实迁移切换。
- 本机Docker CLI可用，但Docker daemon未运行；本轮未构建/运行容器。现有Compose只完成静态解析，不能据此声称容器验收通过。
- 阶段3首次部署入口/发行包与完整服务器实操手册、阶段4bare Git/部署队列/状态/回滚脚本未进入本轮实施；`docs/DEPLOY_FIRST_TIME.md`明确为后续边界说明。

后续账号后台页面、字段及预期结果见 `docs/WECOM_SETUP.md`；当前无需为离线开发操作平台后台。官方资料核对见 `docs/REFERENCES.md`。
