# 本地公司研究基线

2026-11-03：落实基本面设计阶段A的研究基线、关键假设表与事件监控映射。当前只有本地归档、人工核实导入及复核入口；不启动研究采集调度，不调用付费模型，不自动修改新闻判断或发送通知。预期增量、行情估值及经过验证的收益预测仍属后续阶段。

## 已建立的研究

以只读取得的当前启用股票并集建立六家公司研究，不复制成员身份、个人关注背景或消息。公司与证券分开保存，人民币财务口径不因港股证券以港币交易而改变。

| 公司 | 证券 | 正式报告覆盖 | 基线状态 |
| --- | --- | --- | --- |
| 兆易创新 | 603986.SH | 2026三季报，当季与前三季度 | 基础研究证据门槛通过 |
| 中芯国际 | 688981.SH | 2026第二季度，含上季及上年比较 | 基础研究证据门槛通过 |
| 阿里巴巴 | 9988.HK | 截至2026-06-30季度，财务期间与财年名称分开说明 | 基础研究证据门槛通过 |
| 江波龙 | 301308.SZ | 2026三季报及半年报，含现金、存货和资本开支 | 基础研究证据门槛通过 |
| 生益科技 | 600183.SH | 2026三季报及半年报，含多期经营/财务 | 基础研究证据门槛通过 |
| 片仔癀 | 600436.SH | 已核正式2026半年报；三季报仅核到官方公告目录 | 部分覆盖，待取得三季报原件 |

材料、数据库及导出报告均留在被Git忽略的`data/`中。人工阅读从`data/research/reports/index.md`进入；每家公司有十部分研究、精确财务指标、至少两条核心假设及三条监控映射。正式来源链接、报告期间、公开/可得/取得时间和页码/位置随版本保存。来源只有日期精度时不冒称公告秒级时点。

“基础研究证据门槛通过”只表示具备有来源的业务、驱动、财务和风险等最低材料，不等于完整尽调或金融效果验证。缺项仍逐项列出；六家公司都没有合格历史共识/市场快照或经过验证的收益模型，相关两项能力全部为false。

## 代码与数据约定

- `research.py`提供公司/证券身份、独立准备任务、追加基线版本及复核请求。schema5只增加研究表，保留原配置、seen、候选、outbox及审计；研究不随15/30天新闻热数据清理删除。
- 公开研究不带成员ID或私有关注背景；证券symbol、市场和币种不能任意重绑。准备阶段的无历史占位公司允许原子归属到正式公司，已有历史不能静默合并。
- 每个基线有材料截止`as_of`、实际本地建立`created_at`及内容hash；重复导入相同文件幂等，改变内容追加版本。查询历史同时约束材料截止和建立时间，今天导入的旧报告不能冒充过去程序已形成的判断；迟到旧截止版本不覆盖当前基线或取消待复核。
- 财务值用整数或精确十进制字符串，逐项保存期间、单位、币种、会计口径及证据ID。禁止非有限值、浮点财务值、无来源事实、未知引用、未来材料与任意额外可执行字段。缺失内容使用unknown而不填造数据。
- 十部分分别为商业模式、行业周期、竞争优势、盈利驱动、财务质量、治理、内部情景/外部预期、估值、风险、催化。事实、推断、假设与未知分开。最低基本面能力由程序核对四项核心事实、同期间财报证据、假设与映射，不强迫无行情的估值章节假装已知。
- `research_jobs`记录pending、partial、usable、needs_update；复核原因另存，不改旧研究版本。公开或取得新材料后人工追加新版本；重复导入不能擦掉待复核状态。尚未接入新增订阅自动采集或worker研究刷新，使用显式prepare同步新增关注。
- 所有远程资料核实在数据库写事务外完成。CLI导入仅处理本地JSON，整批失败回滚；没有新闻/模型/投递接口调用。研究模块不能设置监控开关、改订阅生效时间或清空通知证据。

## 本地入口

```powershell
# 从本地库已有启用订阅建立准备任务；不会拉取生产配置或请求模型
.\.venv\Scripts\python.exe -m stock_watch research prepare --from-subscriptions
# 或显式使用仅有证券清单的JSON
.\.venv\Scripts\python.exe -m stock_watch research prepare --watchlist data/research/watchlist.json
.\.venv\Scripts\python.exe -m stock_watch research import data/research/baselines
.\.venv\Scripts\python.exe -m stock_watch research list
.\.venv\Scripts\python.exe -m stock_watch research show gigadevice
.\.venv\Scripts\python.exe -m stock_watch research show gigadevice --as-of 2026-11-03T12:00:00+08:00
.\.venv\Scripts\python.exe -m stock_watch research jobs
.\.venv\Scripts\python.exe -m stock_watch research review gigadevice --reason "下一份财报披露后复核库存与现金流"
.\.venv\Scripts\python.exe -m stock_watch research export --output data/research/reports
```

如使用独立本地数据库，在子命令之前指定`--database PATH`。`--as-of`查询早于实际建立的时点时返回空，不能用旧报告发布时间倒填研究完成时间。

输入JSON合同可查`tests/test_research.py`中的最小合成示例。真实基线放在`data/research/baselines/`，不作为离线测试fixture。测试统一阻止外部网络；导出使用安全文件名及转义，原始材料不会执行SQL、Shell或Python。

## 后续边界

下一步是建立有核验记录的财报增量更新及事前预期快照，再考虑行情、反向估值和固定期限收益实验。当前研究保存不等于这些分析已接入提醒，不能把静态基线称为自动研究、已定价判断或收益预测。片仔癀补齐原件后追加新版本，不覆盖现有来源与未知记录。
