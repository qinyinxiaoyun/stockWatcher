# 企业微信应用准备与真实联调

服务器HTTPS和GitHub自动部署已验通，当前等待企业微信应用资料及已有DeepSeek密钥。应用创建、后台扫码/移动端确认由操作者完成；配置随机凭证生成、服务器配置、部署与检查由助手执行。不要仅凭收到推送就判断个人微信可以发送管理消息。

## 创建应用和准备资料

1. 管理员登录 <https://work.weixin.qq.com/wework_admin/frame>；如果还没有可管理的企业/组织，先按平台引导创建组织。
2. 进入“应用管理”的“自建”区域，点击“创建应用”。名称可填“股票新闻助手”，描述填“股票新闻监控、预警与配置管理”，上传标识；初次可见范围只选管理员本人/指定测试成员。
3. 创建后记录AgentId；Secret旁点击查看，按提示使用管理员企业微信移动端确认。进入“我的企业”查看企业ID。界面参考[腾讯官方图文说明](https://cloud.tencent.com/document/product/1831/138263)。
4. 在通讯录找到测试成员的账号UserID，明确是否同一成员同时作为命令管理员与预警收件人；不要用手机号或显示姓名代替UserID。
5. 本机已准备被Git忽略且只允许当前用户读取的 `.env.production.local`。将企业ID、AgentId、Secret、成员账号和已有DeepSeek API Key填入对应空项，保存即可，不粘贴到聊天。回调Token和EncodingAESKey已由助手生成，暂不修改。

本文件不会自动上传或启用监控。服务器先加载相同回调参数，再保存企业微信后台回调；本项目回调URL为 `https://stock.janettepa.uk/wecom/callback`。企业可信IP/域名校验按实际后台要求处理，助手可上传服务器验证文件。创建应用不需要购买新服务器或重复创建DeepSeek密钥。

| 执行页面/位置 | 填写/操作 | 预期结果 |
| --- | --- | --- |
| 企业微信管理后台 → 我的企业 → 企业信息 | 取得企业 ID，写入专用 `.env` 的 `WECOM_CORP_ID` | 与后续回调解密的 ReceiveId 一致 |
| 应用管理 → 自建应用 → 对应应用 | 取得 AgentId、应用 Secret；可见范围包含测试成员 | `WECOM_AGENT_ID`/`WECOM_SECRET` 对应该应用 |
| 通讯录 → 测试成员详情 | 取得成员账号 UserID，管理员填 `WECOM_ADMIN_USERIDS`，预警收件人填 `WECOM_TOUSER` | 两类名单独立；回执只发命令发起人 |
| 应用管理 → 对应应用 → 接收消息/设置 API 接收 | URL 填 `https://你的域名/wecom/callback`；生成 Token 与 EncodingAESKey，分别写 `WECOM_CALLBACK_TOKEN`/`WECOM_ENCODING_AES_KEY` | 服务器运行并加载相同变量后，点击保存成功，URL校验通过 |
| 应用管理 → 对应应用 → 企业可信 IP（账号要求时） | 填服务器实际公网出口 IP | 应用 token 和消息发送不再被 IP 限制拒绝 |
| 企业微信客户端 → 工作台 → 对应自建应用会话 | 测试成员发送 `帮助`、`查看监控`、`添加方向 半导体设备` | 收到仅发给该成员的回执，配置版本增加；下一轮读取新版本 |
| 同一应用会话 | 发送 `把中芯国际暂停，再加一个先进封装方向` | worker 解析后原子保存，或歧义/冲突时明确追问；失败不修改 |
| 个人微信插件/账号实际入口（如已配置） | 尝试发送 `查看监控` 并核对服务收到回调 | 仅当真实双向验通才标记支持；若只能收消息，继续使用企业微信会话 |

后台标签可能随企业账号能力变化，按对应自建应用的接收消息/API设置入口定位。`PUSH_TOKEN` 是自有 `/push` 接口的 Bearer 密钥，与企业微信回调 Token 完全独立，后台无需填写 PUSH_TOKEN。

真实投递先只给明确指定的测试成员：

```powershell
.\.venv\Scripts\python.exe -m stock_watch --env-file .env test-push --to TEST_USERID --real
```

预期返回 code=0 且客户端收到测试消息。若返回 invaliduser/IP/secret 错误，检查应用可见范围、成员账号和可信出口IP。不要在聊天或日志中粘贴 Secret、access_token 或完整回调正文。

真实模型配置在 DeepSeek 平台的 API keys 页面创建密钥，保存到 `.env` 的 `DEEPSEEK_API_KEY`，默认模型为 `deepseek-flash`；远程额度及能力仍待账号验证。真实新闻/模型只在显式 `monitor --dry-run --real` 时调用，不发微信、不修改正式seen。

旧云函数当前仍运行，联调期间保持不变，新服务 `MONITOR_ENABLED=false`。旧代码使用COS去重与ServerChan通知，新服务使用独立SQLite和企业微信，不会互相覆盖状态；同时监控同一新闻时，可能重复分析、分别通知并增加模型调用。

若操作者选择新服务接管旧流程，再暂停旧云函数定时触发器，确认无执行中任务后导出最终seen、备份/导入并正式启用；若选择长期并行，则明确接受独立去重及重复通知，或划分关注目标。不得把停旧流程作为创建应用或联调的前置要求。未完成真实联调前保持 `MONITOR_ENABLED=false`。
