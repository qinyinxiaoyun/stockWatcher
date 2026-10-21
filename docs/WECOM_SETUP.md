# 企业微信真实联调（待账号与 HTTPS 环境）

本轮离线开发不需要操作后台。以下步骤供后续真实联调使用，尚未执行；不要仅凭收到推送就判断个人微信可以发送管理消息。

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

自动监控开启属于迁移切换阶段：先暂停旧云函数全部定时触发器，确认无执行中任务，再导出最终 seen、备份/导入，最后开启新 worker。未完成真实联调前保持 `MONITOR_ENABLED=false`。
