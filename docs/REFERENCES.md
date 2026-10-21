# 协议与依赖核对

核对日期：2026-10-21。下列资料用于代码约束，不代表真实账号/服务器已验通。

- [企业微信接收消息](https://developer.work.weixin.qq.com/document/path/90237)、[回调配置](https://developer.work.weixin.qq.com/document/path/90238)、[发送应用消息](https://developer.work.weixin.qq.com/document/path/90236)。网页读取可能受站点限制，使用[企业微信团队 Python 示例](https://github.com/sbzhu/weworkapi_python)固定外部向量辅助校验。
- [PyCA cryptography](https://pypi.org/project/cryptography/)：采用50.0.2，提供Python3.12预编译轮子；自有代码仅负责企业微信协议封装，AES由该库实现。
- [Gunicorn](https://pypi.org/project/gunicorn/)：26.2.0，Linux生产WSGI；Windows本地用Python内置开发服务器。
- [DeepSeek JSON mode](https://api-docs.deepseek.com/guides/json_mode/)：JSON response_format和明确schema提示仍需服务端校验；空输出/截断均按失败处理。[当前模型入口](https://api-docs.deepseek.com/)采用deepseek-flash，支持环境覆盖。
- [Python3.12 zoneinfo](https://docs.python.org/3.12/library/zoneinfo.html)：Windows/精简镜像通过tzdata依赖补充IANA时区。
- [SQLite WAL与已知复位问题](https://www.sqlite.org/wal.html)：本机3.50.4未含修复，默认DELETE journal。[在线备份API](https://www.sqlite.org/backup.html)用于一致备份。
- [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)：环境变更需重建受影响容器；restart不能刷新env_file。[Caddy2.11.7](https://github.com/caddyserver/caddy/releases/tag/v2.11.7)用于Compose版本声明，真实镜像运行待验证。
