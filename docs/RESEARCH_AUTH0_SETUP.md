# Auth0托管授权配置清单

研究资源：`https://stock.janettepa.uk/mcp/research`。2026-11-06已配置租户 `prod-stockwatch.jp.auth0.com` 的API、客户端、scope、刷新与研究账户身份，并通过真实JWT/旋转刷新验证。生产MCP网关已部署2e28e5b，公开发现200、未认证401、真实JWT三工具读取与身份隔离通过；操作者已确认ChatGPT网页调用返回三家公司摘要，并授权该插件自动执行操作，插件权限更新成功。操作者现已确认网页云端任务创建完成；实际任务配置及首次运行回执待核，不重复创建。

## ChatGPT连接参数

- MCP地址：`https://stock.janettepa.uk/mcp/research`。
- 身份验证：OAuth；静态公共客户端ID：`F1fDSnothRIHmUk5DKNkSdwPBxCJwrw4`，Client Secret留空。
- 回调：`https://chatgpt.com/connector_platform_oauth_redirect`，取自已实际获取的OpenAI公开客户端元数据，同时核实Auth0 issuer identification支持。
- OAuth权限：`research:read research:write offline_access`；登录使用此次已验证的研究账户，与管理员CLI登录分开。
- 实际issuer：`https://prod-stockwatch.jp.auth0.com/`（保留尾斜杠）；访问令牌3600秒，研究用户身份由已验证签名JWT取得，不用email猜sub。

在ChatGPT网页插件页添加自定义MCP，选择OAuth并显式填写上述静态Client ID；此租户不支持自动DCR，不能把Client ID也留空。登录用此次验证的同一研究账户邮箱密码。连接完成后应能看到research_get_snapshot、research_submit_update、research_get_run_status，先读取快照，再验证自动任务的写入批准策略；OAuth有write scope不等于ChatGPT产品已允许无人值守写操作。

## 操作者最少步骤

1. 注册Auth0并建立租户，告知租户域名即可，不发送密码或密钥。
2. 助手启动CLI设备登录后，在官方verification URL核对设备码并登录批准管理权限。此步骤不可免交互；这是管理员登录，不是研究用户身份。
3. 最后在ChatGPT网页创建/连接研究MCP并以专用研究用户完成授权，确认读写及无人值守刷新。助手可配置API，但不能替代用户登录或同意。

## CLI取得及信任校验

官方CLI支持Windows/macOS/Linux，入口 https://auth0.github.io/auth0-cli/ ，发行包仅从 https://github.com/auth0/auth0-cli/releases 获取，选择明确固定稳定版本与匹配系统架构。Windows使用Windows_x86_64.zip，其他系统取对应Darwin/Linux架构包。不要执行网络下载脚本管道。

执行前比较官方同版本checksum文件与本地SHA256；若发行物提供发布者签名或GitHub构建attestation，核对其身份绑定auth0/auth0-cli及固定版本。普通checksum不能冒称签名。本轮经明确授权已下载并运行官方CLI1.39.0，ZIP SHA256与官方同版本checksums匹配，未验证发布者签名。

## 已核实命令参数（供助手执行，当前未执行）

官方CLI文档支持：

```text
auth0 login --agent-mode --scopes "read:resource_servers,create:resource_servers,update:resource_servers,read:clients,create:clients,update:clients,read:tenant_settings,update:tenant_settings,read:users"
auth0 tenants list
auth0 apis create --tenant <已核对租户> --name stockWatcher-research --identifier https://stock.janettepa.uk/mcp/research --scopes "research:read,research:write" --token-lifetime 3600 --offline-access=true --signing-alg RS256 --no-input
```

agent-mode设备登录输出URL/code JSON并等待人批准，随后设默认租户。登录scope能否获批取决于账号角色；操作前检查选中租户与已有API，避免重复创建。CLI的默认管理scope可能更广，不能把额外scope列表称精确最小权限。通用Management API可经CLI `auth0 api`调用；使用前按实际安装版本help/schema核对方法与文件参数，不凭空假定tenant设置字段。

## Dashboard或Management API配置

- API identifier精确等于上述资源，不补尾斜杠；RS256；access token最长3600秒，browser-flow lifetime也不得超3600；允许Offline Access；定义research:read和research:write。
- Tenant Settings > Advanced > Settings开启Resource Parameter Compatibility Profile。Auth0官方明确：MCP用resource而非audience时，未开启会回退userinfo audience并失败。必须测试authorize和token请求的resource兼容，不靠设置名推断成功。
- 优先使用ChatGPT管理页实际给出的静态OAuth client配置能力；精确登记其真实redirect URI，不猜测。用户授权流使用authorization_code及PKCE S256，启用refresh_token，申请openid offline_access research:read research:write。public client token endpoint auth method为none；若ChatGPT明确要求confidential client，按其实际支持方式配置，不混用client secret与public client。
- 若ChatGPT仅支持动态注册，才按实际兼容性开启Auth0 Dynamic Client Registration；该开关允许任何人注册应用，必须使用专用租户、用户白名单和明确同意，不能把DCR注册等同授权访问。不要为研究用户错误建立client_credentials替代交互授权。
- 配置Refresh Token Rotation、期限及复用检测，确认客户端每次持久化新refresh token；API允许offline access还不够，客户端还必须请求offline_access。需要实测跨access-token过期刷新，而非只测首次连接。
- 专用研究用户可用已有登录连接建立；管理账号device登录的subject不能当研究用户subject。通过研究登录后经验证签名的access token sub或租户Users的user_id取得实际subject。仅将该值写入受保护的持久policy文件，不打印完整token或用户敏感资料。
- 从租户HTTPS OIDC discovery核对issuer及jwks_uri，获取公开JWKS，核对RS256、RSA至少2048位及kid，写入服务器受保护持久文件。issuer包含其实际尾斜杠，必须与token一致；不要根据JWT中的任意URL抓取密钥。生产定期更新JWKS须采用已有受控流程。

服务器配置RESEARCH_MCP_ISSUER、RESEARCH_MCP_RESOURCE、RESEARCH_MCP_JWKS_FILE、RESEARCH_MCP_POLICY_FILE；policy只允许研究用户subject。先读、再dry-run写、再正式写，核实主体隔离、scope、audience、最长1小时、刷新、撤权与回执。研究HTTP Bearer不能用来冒充OAuth。

## 官方依据

- https://auth0.github.io/auth0-cli/auth0_login.html
- https://auth0.github.io/auth0-cli/auth0_apis_create.html
- https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0
- https://github.com/auth0/docs/blob/master/articles/api-auth/dynamic-client-registration.md
- https://github.com/auth0/docs/blob/master/articles/tokens/guides/get-refresh-tokens.md

已验证：租户管理登录、API/客户端与回调、PKCE代码交换、真实研究subject/签名/audience/scope/1小时access token、真实旋转刷新、生产JWKS/白名单权限、MCP只读往返。操作者已确认网页连接实际返回三家摘要、网页任务创建完成，并明确批准仅StockWatcher自动执行操作；产品权限接口已返回full_access更新成功，其他插件和全局权限不变。尚未独立验证：任务ID/启用状态/实际时间配置、首次真实研究保存与无人值守持续运行；没有对CLI发布物签名作已验证声明。创建消息留档见 [CREATE_RESEARCH_CLOUD_TASK.md](CREATE_RESEARCH_CLOUD_TASK.md)。
