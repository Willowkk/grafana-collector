# 验证记录

验证日期：2026-09-17；Mac、Google Chrome、Python 3.12.14。独立安装另外使用 Python 3.11.16。目标为 SDKv2 大盘 `inyUtsTSk`，版本 319；固定历史窗口为北京时间 2026-09-14 15:00–21:00，文件系统 `cfs_echo_hl_qa_fs`，其余筛选见示例配置。

## 真实历史采集

`runs/sdkv2_20260914_1500_2100/` 保存完整 SQLite、469 个压缩原始响应、manifest 和 204 个面板 Excel。已完成全部 30 个分组：65 个面板有数据、139 个合法空数据、0 个失败或未支持面板。469 条实际网络查询均完成整个 6 小时时间窗；隐藏且无公式依赖的输入没有额外执行。

使用专用登录 profile，通过共享认证状态的接口请求。数据源目录管理接口对当前账号返回 403，实际通过 Grafana 前端设置目录解析数据源：OpenTSDB `bytetsd`、Bosun `bosun`。ID 来自该次发现结果，不是程序常量。当前 OpenTSDB 配置没有显式最小间隔；自动间隔使用已核对的 Grafana 7.5.3 算法及 1440×900 视口实测网格。6 小时窗口的主要示例面板使用 `1m`；Bosun 表达式没有 `$ds`，其采样记录为后端表达式决定。

## 数值与 Excel

独立验收报告位于 `runs/validation/excel-audit.json` 与 `query-audit.json`。

- 204 本 Excel 均只有一个工作表，首列为真实 Excel 日期，按北京时间显示；数值是数字，缺失保留空单元格。
- 逐格核对 1,184,348 个数据单元格：197,808 个数字、986,540 个空白。206,492 个 SQLite 显示数据点（含空值）均准确导出；没有文件覆盖。
- Throughput 的 10 个已知 Grafana 显示值与原始响应按页面精度取整后完全相等。原始小数在 SQLite 中完整保留；Excel 以自身浮点精度保存。
- QPS、平均时延、多曲线 TopK 和 Bosun API Fail Rate，通过同一历史窗、变量及正常面板宽度的浏览器实际请求/响应进行对照。Bosun 两次后端返回中少数浮点结果相差约 `2.22e-16`；首份页面响应与采集结果逐点相同。这不是 Excel 舍入或客户端插值。
- EIC MSet/MGet 成功率的真实输入为空，尚不能声称完成了这两个公式的非空生产数据数值验收；依赖、空值、除零和计算行为通过合成响应测试。

`raw.Queries` 等后端诊断字段可能与实际 Bosun 结果窗口不一致；验收依据实际浏览器请求和 Results 时间戳，不把诊断字段当作请求事实。

冻结历史样本的面板 414 有一处旧元信息：`filesystem` 仅用于图例，实际查询未按它过滤。样本的真实过滤器和原始响应正确，独立审计附件已明确该范围；最终代码已把图例变量与查询变量分开记录。没有为修正说明而篡改既有冻结数据库。

## 持续采集与恢复

已完成 **3 轮真实持续采集**，北京时间 20:10:08 启动，20:11:08、20:12:08、20:13:08 查询。实测显式设置 1 分钟轮询和 1 分钟回查，加速验收；产品默认仍为 5 分钟轮询、5 分钟回查。QPS 第二、三轮分别返回 13、14 条曲线，吞吐在此实时窗口返回空数据。首次未查询启动前历史，后续请求严格从各自成功边界减回查量续采；重复点为 0。报告为 `runs/validation/live-watch-audit.json`，运行目录为 `runs/live_validation/`。

持续模式的采样间隔始终由输入链接的展示跨度固定，本次为 1 分钟，与轮询周期独立。

另用真实历史接口执行 4 轮增量请求，每轮重新打开 SQLite 并重建采集器，最后一次逻辑时间跳过 38 分钟。请求从上轮游标减回查量补齐整个缺口，两面板四轮均成功，固定采样不变、重复点为 0。报告为 `runs/validation/historical-incremental.json`；这里注入了历史逻辑时间，没有实际等待 38 分钟。

**136 项自动化测试通过。** 覆盖独立查询失败与重试、401 登录失效和恢复、403 权限错误、超时/网络异常、成功空响应、迟到修订、TopK 曲线退出、停止/重启、超过 30 分钟的逻辑时钟中断、固定采样和 Excel 分文件限制。故障测试使用可控传输和时钟；没有主动断开整机网络、作废用户真实登录，或把模拟中断宣称为等待了 30 分钟。

## 交付与复现

中文使用说明见 `README.md`，配置见 `examples/sdkv2.toml`。`samples/real_20260914_1500_2100/` 是 6 个代表面板的真实 Excel 和 manifest；其中两个成功率面板的空数据是实际结果。

wheel 在全新 Python 3.11 环境以普通安装方式验证，运行目录在项目之外，没有源码路径或 editable 安装；执行版本、帮助及离线导出，并逐格核对 Throughput 样本。详细环境与结果见 `runs/validation/clean-install.json`。

```bash
python -m pytest -q
python -m pip wheel . --no-deps -w dist
grafana-collector fetch --config examples/sdkv2.toml --out runs/new-history
grafana-collector watch --config examples/sdkv2.toml --panels 166,150 \
  --poll-interval 1m --lookback 1m --rounds 3 --out runs/new-live-check
```

数据包不包含 Chrome profile、Cookie 或登录凭证。真实样本保留内部指标和完整标签，属于本次本地交付内容。

## 0.1.1 参考版式与 Grafana 单位

根据后续反馈，重新读取提供的 `data.inf.hdfs_dancedn_seed_qa_test_dn` 目录（143 个 Excel）。其中 142 个文件使用白底加粗表头、秒级日期和普通数值格式；58 个文件本身有不规则时间间隔。因此导出改为对应白底样式、`面板名-data-export.xlsx` 命名，仍按真实时间戳合并。

单位规则核对了当前实例保存的 `app.05b1e2420ad003a5e4fe.js` 中 GNR5、dZjC 和图表格式化函数。Grafana 页面按每个值动态选择前缀；用户明确选择 Excel **同一面板统一单位、单元格带单位、保持数字可计算**。实现按 Grafana 单位族统一尺度：IEC 的 bytes/kbytes 按 1024 进制，SI 的 decbytes 按 1000 进制分别处理；时间 µs/ms 可统一，吞吐率和数据大小分开。

本次 Throughput 使用 GiB/s，API QPS 使用 Kreq/s，API Latency AVG 使用 ms，API Fail Rate 使用 %，IO Size 使用 MiB。例如 `506523156.5333 B/s` 换算为约 `0.471736 GiB/s`，单元格按显示精度呈现 `0.472 GiB/s`，底层数值没有按该显示精度舍入。百分比保留比例原值（1 表示 100%）并至少显示两位小数；实际超过 100% 的结果不截断。极小非零值使用科学计数法。

原始 SQLite 和压缩响应不变。新版 manifest 每列包含 `source_unit`、`display_unit`、`scale` 等信息，满足 `Excel 数值 = 原始数值 × scale`；百分比由 Excel `%` 数字格式负责显示。列名、时间戳、缺失空白和数据点数不变。

本版 **183 项测试通过**，包括 42 项单位测试。版式参考、单位转换、全量 Excel 核对及渲染记录保存在 `runs/validation/reference_style/`。早前 `excel-audit.json` 对应未换算的原始单位导出，本节的新审计对应统一显示单位后的结果。


## 0.2.0 Parquet 默认导出（2026-09-20）

将之前同一真实运行记录离线转换为 Parquet，没有重新请求 Grafana。默认 fetch/watch/export 使用 Parquet，可显式指定 `--format xlsx` 保留 Excel。持续采集和查询编译逻辑未改变。

交付样本位于 `samples/parquet_20260914_1500_2100/`。本次 generation 为 `20260920T070356-be9407e75776`，完整 204 个面板、30 个分组：65 个 success，139 个 empty。points.parquet 有 **206,492 行**（197,808 个数字、8,684 个显式 null）；series.parquet 有 **4,053 行**。Parquet 使用 ZSTD 压缩，两个数据文件合计约 1.43 MB，清单另存完整配置与来源。

`scripts/audit_parquet.py` 直接读取原始 SQLite，独立核对每个 `(panel_id,series_id,time)`：时间戳毫秒、浮点位模式、整数转换、显式 null、完整标签、面板名称/分组及源单位全部一致，缺失点没有被造出，曲线和点没有重复。全部文件 SHA-256 与 manifest 一致。冻结的变量及看板快照一致。结果 **0 个失败项**，见 `samples/validation/parquet-audit.json`。

本批整数共 4,820 个，绝对值最大为 1，可以精确转为 float64。针对超出可精确表示范围的整数另有拒绝测试；不声称任意大整数都可无损转 float64。Throughput 的原值保留 B/s，显示元数据为 GiB/s；百分比原值保留 ratio，程序显示系数为 100，未复用 Excel 的百分比格式来假定程序会自动乘 100。

本版 **231 项测试通过**，其中 29 项 Parquet 测试覆盖固定 schema、空表、精度错误、单位、筛选、null/缺失、空数据与失败、快照一致性、重叠修订/撤回及原子发布中断。CLI 测试覆盖默认 Parquet、显式 xlsx、配置优先级及恢复时切换导出格式。导出后的纯 PyArrow 示例实际运行，按 `panel=166` 读取 52 点，按 method=cfs_pread 和北京时间 16:00–17:00 筛选得到 6 点。

复现完整数值核对：

```bash
python scripts/audit_parquet.py --run runs/sdkv2_20260914_1500_2100 \
  --manifest samples/parquet_20260914_1500_2100/manifest.json
```

源运行 SQLite 仅保存在原采集电脑；仓库中的样本可以独立读取，重跑源值审计需要对应 SQLite。网络登录和采集行为沿用前述真实验证，本次未重复发起在线采集。

干净环境验证：新建独立 Python 3.11 虚拟环境，仅安装 0.2.0 wheel 及其声明依赖；在项目目录之外执行版本检查、真实 SQLite 的默认 Parquet 导出，以及交付样本读取示例，全部退出码为 0。未安装 Pandas，未启动 Chrome。记录见 `samples/validation/parquet-clean-install.json`。
