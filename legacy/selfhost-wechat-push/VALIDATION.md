# 验证记录（2026-10-20）

- Python 3.12.14：17 项 unittest 检查通过，全部使用模拟企业微信响应，不发送微信消息。
- Gunicorn 26.2.0：启动真实本地 HTTP 服务后，GET /health 返回 200，未鉴权 POST /push 返回 401，鉴权成功的中文/emoji 消息返回 200 并分为 6 段。
- 将云函数替换函数接到真实本地 HTTP 服务、后端使用模拟企业微信，UTF-8 JSON 链路通过。
- compose.yaml 通过 YAML 解析与关键网络配置检查；所有 Python 文件通过语法编译。
- 未在本环境构建 Docker 镜像、验证公网 DNS/TLS、调用真实企业微信凭证或运行原生 Python 2.7。

可复跑基础检查：`python3 -m unittest -v test_relay`。
