# 成员订阅与研究网页

2026-11-03当前运行017c2ae…：入口为`https://stock.janettepa.uk/`。前版39b61f2的验收手动补Origin，漏掉原生表单在no-referrer下的失败；现已修复策略及多标签Cookie覆盖，并按实际响应策略回放HTTPS登录/退出和六股票页。此回放不是实际浏览器自动化，浏览器工具仍受环境限制。已有登录页需刷新加载新策略，现有凭据不变。

网页复用现有 `stock.janettepa.uk` HTTPS入口及Python app，保留`/health`、`/ready`、`/push`、`/wecom/callback`和域名校验文件。没有修改DNS或另外部署静态站点；app仍只处理HTTP，唯一worker继续原调度。

## 页面

- `/`：管理员登录后的组织总览，展示本组织所有成员（包括已停用）、股票与主题订阅、方向/关注点/等级、研究覆盖及更新时间，支持搜索。
- `/stocks/<symbol>`：证券身份、研究状态与四项独立能力、十部分基线、财务期间/单位/币种/口径、假设与反证、事件监控映射、原始证据和历史版本。`?version=N`查看旧版本，不把版本编号大小当作截止日期先后。
- 最新财报未核对的资料明确标记待核，不把片仔癀2026Q3检查目标当成已核财务期间。研究可用与收益预测能力分开显示。
- 未建研究的关注股票显示待建立；资料损坏或不可读取时返回安全错误，不把读取失败伪装成零用户。

页面只读，使用SQLite只读连接、query_only与短读事务。成员信息按配置的组织ID隔离，只展示关注字段；不读取消息、确认令牌、API密钥、私有背景或其他组织资料。网页不会修改订阅、生效时间、seen、通知或研究版本，也不会触发新闻/模型/推送。

## 访问与凭据

成员UserID及MemberRegistry角色不自动授予网页权限；网页使用独立管理员账号，能够查看全部成员关注信息，因此默认登录保护。配置四个专用环境变量：

```dotenv
DASHBOARD_USERNAME=
DASHBOARD_PASSWORD=
DASHBOARD_SESSION_SECRET=
DASHBOARD_ORIGIN=https://stock.janettepa.uk
```

密码至少16字符、会话签名密钥至少32字符，应使用独立随机值，不能复用PUSH_TOKEN或企业微信凭据。未配置或明显占位值时页面关闭私密资料访问，原业务API保持工作。生产环境文件仍root600；初始网页登录信息单独保存在操作者本机未跟踪的`.env.dashboard.local`，签名密钥仅保存在服务器环境文件，不进Git或镜像。

使用HTTPS、8小时签名会话、Secure/HttpOnly/SameSite=Strict Cookie、同源及CSRF校验、有限登录失败冷却；更新服务器环境中的用户名、密码或签名密钥并重建app后，旧会话失效。本机凭据文件是登录信息保管副本，修改它不会自动修改服务器。访问凭据不放在URL、页面源码或日志里。登录保护适用于总览和所有股票子页面，禁止公共缓存、索引和嵌入；来源链接经过协议检查，页面文本转义，不执行原始新闻或研究里的HTML。

## 验收边界

原生表单响应使用`Referrer-Policy: same-origin`：同源表单保留Origin，跨站链接仍不发送Referer。不能改成no-referrer再由服务端要求非空Origin，否则浏览器普通登录/退出表单会被误拒；详见[Fetch规范](https://fetch.spec.whatwg.org/#append-a-request-origin-header)。缺失、null和异源Origin继续拒绝，CSRF Cookie与隐藏字段仍须签名有效且匹配。

同一浏览器打开多个登录页时复用经验证、尚未到期的CSRF Cookie，避免新标签覆盖旧表单；签名有效期仍为600秒，过期后刷新生成新值。验收工具必须根据页面实际Referrer-Policy回放原生表单的Origin规则，不能手动固定正确Origin而掩盖浏览器问题。该HTTP/规范回放仍不替代真实浏览器自动化测试。

离线覆盖组织隔离、成员/研究状态、历史版本、只读指纹、输入转义、登录/退出、Cookie/CSRF/Origin、时效/限流和已有API兼容。UI以合成成员加公开研究资料做HTTP预览检查，不把真实成员或密码写入fixture。

当前自动浏览器控制工具未能启动，无法声称已完成逐像素或移动设备浏览器实测；响应式样式、HTML和HTTP行为通过程序检查。正式发布后需验收真实HTTPS登录、总览、六个股票页及退出后的访问隔离。实际执行与发布记录见PROGRESS.md。
