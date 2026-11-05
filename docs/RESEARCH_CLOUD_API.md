# 云端研究更新接口与配置

三个HTTP接口、worker更新、历史归档和网页状态已于2026-11-05部署生产，业务版本6a21afa、schema6。生产已开启受限REST接口及正式保存开关，三家公司dry-run回执已验证，未制造新研究版本。**ChatGPT网页云端定时任务尚未创建**：等待Auth0租户和用户授权，MCP服务尚未启用。执行计划见 [RESEARCH_CLOUD_PLAN.md](RESEARCH_CLOUD_PLAN.md)，任务正文见 [RESEARCH_DAILY_PROMPT.md](RESEARCH_DAILY_PROMPT.md)，连接清单见 [RESEARCH_AUTH0_SETUP.md](RESEARCH_AUTH0_SETUP.md)。时间为周一至周五05:00、Asia/Shanghai。

## 运行与迁移

运行时Python3.12；新增数据库schema6，包括 `research_update_runs`、`research_update_items`、`research_update_checks`。正常 `init-db` 从schema5迁移，保留研究历史、成员配置、seen和通知证据。部署沿用已有备份/迁移/镜像流程，不在请求中自动初始化数据库。

`app` 只接收HTTP。唯一 `worker` 每轮处理一个研究任务项，调用SQLite一致性备份、版本比较和追加、原子导出；不调用模型、新闻源或企业微信。研究处理不依赖 `MONITOR_ENABLED`，但必须显式开启研究接口。

研究默认关闭：

```dotenv
RESEARCH_API_ENABLED=false
RESEARCH_APPLY_ENABLED=false
RESEARCH_ALLOWED_COMPANIES=gigadevice,alibaba,smic
RESEARCH_API_PRINCIPAL=cloud-research
RESEARCH_API_TOKEN=
RESEARCH_MAX_REQUEST_BYTES=4194304
```

`RESEARCH_REPORT_DIR` 默认 `DATA_DIR/research/reports`，必须处于持久 `DATA_DIR` 内。worker备份在数据库目录下 `research/backups`。备份含原数据库数据，仅留服务器受保护持久目录，不返回云端。报告目录和信任文件禁止链接路径。

HTTP直接调用需要独立、至少32字符ASCII且无空白的随机Bearer凭证；保存在未跟踪环境文件，不能使用网页密码、企业微信、模型或部署密钥。`RESEARCH_API_PRINCIPAL` 是稳定ASCII服务身份；轮换token时保持它，以便仍可查询原回执。固定白名单仅允许三家公司，可缩小不能扩大。

验收时先开启API但保持APPLY=false；提交时固化 `dry_run=true`，处理结果为 `validated`，不增基线/水位/导出。之后开启正式保存也不会执行旧试运行任务；需要重新提交新run_id。正式保存要求提交时和worker处理时均开启APPLY。

## REST调用合同

所有请求使用HTTPS及 `Authorization: Bearer <专用凭证>`；响应禁缓存。未授权请求不读取正文/数据库。不支持SQL、Shell、Python或由服务器代抓任意URL。

| 方法与路径 | 输入与输出 |
| --- | --- |
| `GET /api/research/v1/snapshot` | 默认摘要。`company_ids=gigadevice,alibaba`选子集；`full=true`返回完整研究。公司项含版本、hash、semantic_hash、as_of、last_checked、last_updated、covered_through、window_from、lookback_start、gaps和report_path。 |
| `POST /api/research/v1/updates` | 严格JSON，受理返回202与执行回执。拒绝重复键、NaN、越界公司、超深/超大输入。HTTP202不表示研究已更新。 |
| `GET /api/research/v1/runs/{run_id}` | 仅返回本调用身份的任务；他人/未知任务404。返回各公司旧新版本/hash、committed/exported、状态、错误码和report_path。 |

提交文档字段：

| 层级 | 字段 |
| --- | --- |
| 顶层 | `contract_version=1`、`run_id`、`as_of`、`items`。 |
| 每家公司 | `company_id`、`expected_base_hash`、`result`、`coverage`、`summary`；仅UPDATE必须附 `document`。 |
| coverage | `from`、`through`、`complete`（布尔）、`sources`（`url`及`status=ok/failed`）、`gaps`（文本数组）。 |

`run_id` 为1—100字符ASCII标识（首字母/数字，其后可用字母数字及`_.:-`），同一次请求重试沿用；同身份/编号但内容不同返回409。`expected_base_hash` 使用快照的 `hash`，不是semantic_hash；只有基线不存在时允许null。任务按公司独立提交，支持部分成功。

`as_of`、coverage时间等使用带时区ISO8601。满足 `from <= through <= as_of <= 服务器当前时间`；UPDATE研究文档 `as_of` 必须等于顶层截止。document沿用现有schema1完整十部分格式、精确财务值/证据引用/时间顺序和能力限制。文档校验失败仅该公司失败，整体JSON/合同畸形则拒绝整个请求。

UPDATE保存前在短事务再次检查旧hash。旧指纹不匹配返回conflict，须读取新快照重新研究，不可强制覆盖。不能用不完整研究让既有fundamental就绪基线退化；基础就绪校验不代表自动证明研究深度与事实正确，云端仍须遵守 [研究标准](RESEARCH_BASELINE.md)。

`NO_CHANGE` 不刷新基线as_of，不增版本；同内容仅as_of/ingested_at或集合排序变化也按无变化处理。来源公开/可得时间、事实、财务期间/口径、假设与证据更正仍参与内容比较。无既有基线时NO_CHANGE记为incomplete。

完整覆盖需要至少一个成功来源、无failed来源/缺口，并从已成功水位（首次为基线as_of）或更早开始。INCOMPLETE、来源失败、覆盖断档不推进水位。周一或漏跑后从快照window_from继续，按lookback_start复查最近七天；不要固定仅查24小时，也不要把触发时间当成功水位。

状态包括 queued、running、validated、no_change、incomplete、conflict、failed、committed_export_pending、completed；混合终态可为partial_success。保存与待导出回执同事务提交，导出失败固定60秒退避重试，不重复增版。网页直接读DB，因此可能出现数据库已更新但导出待重试。

## 可选MCP与OAuth

本地/镜像支持官方Python MCP SDK 2.3.0及PyJWT 2.15.1。开发依赖包含二者；单独安装工具端使用项目 `research-mcp` extra。REST使用者无需启动MCP。

MCP是独立纯HTTP入口 `python -m stock_watch.research_mcp`，监听8001；Compose中为显式 `research-mcp` profile，默认不启动，不带任何调度。Docker镜像包含依赖，新增Nginx模板分别路由REST、`/mcp/research`与 `/.well-known/oauth-protected-resource/mcp/research`；研究路径正文限制4MiB。

MCP要求外部OAuth身份提供方，服务不自行实现登录、令牌签发或刷新。配置：

```dotenv
RESEARCH_MCP_ENABLED=false
RESEARCH_MCP_ISSUER=https://你的身份提供方
RESEARCH_MCP_RESOURCE=https://stock.janettepa.uk/mcp/research
RESEARCH_MCP_JWKS_FILE=/data/research/oauth/jwks.json
RESEARCH_MCP_POLICY_FILE=/data/research/oauth/policy.json
```

启用前由管理员把提供方的公开JWKS保存到上述持久文件，策略文件保存为：

```json
{"subjects":["提供方的研究账号subject"],"revoked_jtis":[]}
```

信任材料逐请求重新读取，允许替换JWKS轮换公钥、移除subject撤权或添加jti即时拒绝令牌；不会跟随令牌中的URL下载密钥。仅接受RSA至少2048位/RS256，验证签名、issuer、audience、sub、iat、exp，令牌有效期最多1小时。只读需要 `research:read`，提交还需 `research:write`。Bearer REST密钥不能认证MCP；OAuth主体以issuer+subject的不可逆指纹隔离任务，不向工具或页面返回原subject。

提供方需为上述resource颁发access token并提供标准OAuth发现、授权码/PKCE、客户注册及刷新/撤销能力。ChatGPT连接使用其管理页给出的实际redirect URI，不能猜回调地址或把网页会话当OAuth。工具声明分别为只读/写入，写工具权限按真实平台能力授权，不以只读标注或匿名写绕过。

本地离线SDK测试覆盖真实HTTP传输、资源发现、签名令牌、三工具往返、scope/主体/受众/期限校验、撤权/公钥更新与畸形JSON。生产REST快照、提交及回执已通过；用户选择ChatGPT网页任务并确认需要新建OAuth服务，Auth0租户注册仍待完成，因此外网MCP连接、OAuth账号绑定及无人值守持续写入仍待验收。

## 检查与排障

先查询run回执，再查询snapshot和网页；不要因网络超时直接换编号重复研究。冲突需重新读取；incomplete需补齐未覆盖来源；committed_export_pending只检查持久目录、导出锁/权限并等待worker重试，不重导版本或恢复全库。

导出包含跨进程非阻塞目录锁与一致读快照，逐文件原子替换，索引最后写；保留调用方事务。备份与远程研究均不占数据库写事务。

工程验收必须全离线；真实云端账号连接、生产迁移/新镜像/代理配置与定时任务创建分别登记。操作者后续已授权部署及创建任务；生产REST已部署，定时任务因账号与连接尚缺而未创建，不能将本地自动化或计划中的05:00冒称云端已运行。
