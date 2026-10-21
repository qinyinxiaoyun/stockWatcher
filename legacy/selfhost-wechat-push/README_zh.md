# 自建微信推送服务：替换 Server酱

本包只替换推送环节：腾讯云函数继续抓新闻、调用 DeepSeek、在 COS 去重；它把告警 POST 到你的服务器，由服务器调用企业微信自建应用消息接口，再经微信插件在个人微信接收。

这是一个轻量 Python 3 WSGI 服务，采用 Gunicorn 运行；可选 Caddy 自动配置 HTTPS。服务不使用 Server酱的 API、SendKey 或服务器。

## 1. 先确认企业微信账号条件

先完成以下设置，再投入服务器部署工作：

1. 在企业微信电脑端管理后台创建/选择你有管理权限的企业。
2. 创建一个自建应用，例如“新闻预警”，将你自己的成员账号加入应用可见范围。
3. 记录企业的 CorpID、应用的 AgentID 和应用 Secret，以及你自己的**成员 UserID**。UserID 是企业微信成员标识，不是普通微信号。
4. 在该应用的企业可信 IP 中添加**自有服务器访问企业微信接口时使用的公网出口 IP**。调用企业微信的是推送服务器，腾讯云函数的出口 IP 无需加入这套服务的可信 IP。
5. 在后台找到“微信插件”，用你自己的微信扫码关注并确认成员身份。检查允许微信插件接收应用消息的相关设置。

账号是否能注册、创建自建应用、启用微信插件，以及是否需要额外企业/域名验证，由当前账号权限和企业微信规则决定。本包不假定“个人未认证账号一定可用”。如果后台没有这些能力，单独部署服务器无法补齐微信通道资格。

如果配置可信 IP 前要求先验证可信域名，请按官方要求验证你拥有的域名。后台提供的 `WW_verify_*.txt` 可以原样放到本包 `verification/` 目录，再通过 `https://你的域名/WW_verify_实际文件名.txt` 访问验证。本包不实现企业微信加密回调 URL 验证；单向主动推送也不包含接收用户聊天的功能。

相关资料：

- 企业微信发送应用消息：https://developer.work.weixin.qq.com/document/path/90236
- 企业微信 access_token：https://developer.work.weixin.qq.com/document/path/91039
- 微信插件帮助：https://open.work.weixin.qq.com/help2/pc/18121
- Wecom酱的账号接入说明：https://github.com/easychen/wecomchan

## 2. 准备服务器

以下默认 Linux、已安装 Docker Engine 和 Docker Compose v2。

- 一个可用的固定公网出口 IP，并能访问 `qyapi.weixin.qq.com`。
- 一个你拥有的子域名，例如 `push.example.com`，DNS 指向服务器。若发布 AAAA 记录，IPv6 也必须正确可达。
- 若使用本包 Caddy，服务器的 80/443 端口需要可供域名验证和 HTTPS 使用，且没有其他程序占用。已有 Nginx/Caddy 时请使用后面的“已有反向代理”方案，不替换现有站点配置。

Caddy 的自动 HTTPS 说明：https://caddyserver.com/docs/automatic-https

## 3. 配置并启动

解压后进入 `selfhost-wechat-push/`：

```bash
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

最后一条在你自己的终端生成随机 PUSH_TOKEN。将其填入 `.env`；不要使用示例占位符。编辑 `.env` 中每一项：

| 变量 | 内容 |
| --- | --- |
| PUSH_DOMAIN | 你的推送子域名，不带协议与路径，例如 push.example.com |
| PUSH_TOKEN | 至少 32 字符的随机令牌；调用方和服务器使用同一个值 |
| WECOM_CORP_ID | 企业 CorpID |
| WECOM_AGENT_ID | 自建应用 AgentID，数字 |
| WECOM_SECRET | 该自建应用的 Secret |
| WECOM_TOUSER | 接收消息的成员 UserID；个人使用填自己的 ID；多个成员按官方约定用竖线分隔 |

所有真实凭证只放在你自己的服务器与云函数环境变量中。`.env` 不进入 Docker 镜像或源代码包。PUSH_TOKEN 是你自建服务的调用令牌，不是原 Server酱 SendKey。

启动推送服务和自动 HTTPS 代理：

```bash
docker compose --profile https up -d --build
docker compose ps
curl --fail https://push.example.com/health
```

把命令中的 `push.example.com` 改成你的域名。健康检查应返回：

```json
{"code":0,"status":"ok"}
```

健康检查只证明服务在运行，不会调用企业微信，也不能证明微信收件通道已经开通。

### 已有 Nginx / Caddy

只启动服务：

```bash
docker compose up -d --build relay
```

服务监听宿主机 `127.0.0.1:8000`，将现有 HTTPS 站点反向代理到这个地址即可。若现有 Nginx/Caddy 也在容器中，容器内的 `127.0.0.1` 指向自身，请将反向代理与 relay 接到同一个 Docker 网络并使用 `relay:8000`。

## 4. 显式发送一条测试消息

以下命令会向 `.env` 中指定的成员发送一条测试消息；由你在自己的服务器上运行：

```bash
python3 test_push.py
```

这个脚本使用本机 `127.0.0.1:8000` 测试，读取 `.env`，不会把令牌放到 URL。成功返回 `code: 0` 和 `accepted_chunks`。

接着测试外部 HTTPS 链路：

```bash
TEST_PUSH_URL=https://push.example.com/push python3 test_push.py
```

最后在个人微信里确认是否收到了消息。企业微信接口接受请求与手机端实际展示是两个检查点。

## 5. 接入现有腾讯云函数

在云函数环境变量中增加：

```text
PUSH_URL=https://push.example.com/push
PUSH_TOKEN=与你服务器.env中相同的随机令牌
```

将本包 `scf_send_wechat.py` 中的 `send_wechat()` 函数复制到现有 `index.py`，替换原同名函数。它使用你原来的 `os`、`json`、`http_request()`、`json_bytes()`；不需要把完整服务器程序复制到腾讯云函数。

保留此前已修复的 Python 2.7 HTTP 辅助函数：URL、请求头名称、请求头值与请求正文在发送边界转为字节串，以避免 `unicode_literals` 与含中文 JSON 的拼接错误。客户端会按 UTF-8 发送 JSON。

新函数对非成功响应抛异常，因此原来的“推送成功后才写 seen 状态”逻辑继续生效。旧 `SERVERCHAN_SENDKEY` 和 `serverchan_url()` 不再被这条推送路径使用。新闻抓取、DeepSeek、COS 状态文件与已设置的定时触发器不需要因这次替换而调整。

新接口会按 UTF-8 字节数自动分段，分段前的内容最大 64 KiB；每段普通文本带序号，不保证 Markdown 在个人微信插件中渲染。每次转发最多使用约 70 秒的处理预算，云函数 HTTP 等待设置为 90 秒；云函数总执行超时需覆盖新闻、DeepSeek 和转发的总耗时。

## 6. 接口约定

```http
POST /push
Authorization: Bearer <PUSH_TOKEN>
Content-Type: application/json
```

```json
{"title":"股票重大新闻预警","content":"具体新闻和分析"}
```

- 200，`code: 0`：所有段落均被企业微信接口接受；无效收件人不会被视为成功。
- 401：调用令牌不匹配。
- 400 / 411 / 413 / 415：JSON、长度或内容类型不符合约定。
- 502，`code: 1`：企业微信拒绝请求或网络失败。返回脱敏后的 `wecom_errcode`、`detail` 和已接受的分段数。

每段成功后并不等待手机已读回执。本包不提供持久队列或跨请求幂等账本；如果多段中途失败，调用方重试整条消息时，之前成功的分段可能重复。应保留原有新闻去重状态，不把 HTTP 200 之外的结果标记为已处理。企业微信的通道能力、接口限额继续适用，自建不等于无限额。

## 7. 常见问题

| 情况 | 处理 |
| --- | --- |
| /health 正常，但发消息 502 | 查看响应中 wecom_errcode / detail；用下方日志确认是 token_rejected、message_rejected 或 recipient_rejected |
| 提示 IP 不受信任 | 将推送服务器的真实公网出口 IP 配置到该应用，而不是仅填写服务器内网 IP |
| 企业微信能收到，个人微信没收到 | 检查账号的微信插件能力、扫码成员绑定、应用可见范围及通知接收设置 |
| token_rejected | 核对 CorpID 和该应用的 Secret；AgentID 与 Secret 必须属于同一自建应用 |
| recipient_rejected | 核对成员 UserID、成员状态及应用可见范围 |
| HTTPS 证书配置失败 | 核对域名 A/AAAA、80/443 端口、已有站点占用与 Caddy 日志 |
| 腾讯云函数报 401 | PUSH_TOKEN 必须与服务器配置完全一致 |

```bash
docker compose logs --tail=100 relay
docker compose --profile https logs --tail=100 caddy
```

程序不记录 Secret、access_token、Authorization 或推送正文；排查时使用 request_id 和错误码。

## 8. 本包验证范围

```bash
python3 -m unittest -v test_relay
```

测试使用模拟企业微信响应，不会发出微信消息。覆盖鉴权前禁止访问上游、中文/emoji 分段、token 缓存与失效刷新、无效收件人、部分失败、凭证脱敏、请求限制和云函数客户端 JSON 构造。

已在 Python 3.12 下运行这些检查；生产 WSGI 使用 Gunicorn 的 HTTP 链路另行验证。Docker 镜像构建、DNS/证书和真实企业微信投递需要你自己的服务器及账号完成验证。Python 2.7 云端客户端未在本地原生 2.7 解释器运行。

服务端主要业务使用 Python 标准库，唯一额外依赖为 Gunicorn；不需要 Redis、数据库或 Kubernetes。部署中使用 1 个 worker、4 个线程共享内存 token 缓存。
