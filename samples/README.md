# 真实采集样本

## 当前 v2 格式

`parquet_v2_20260914_1500_2100/` 是 0.3.0 的完整数据包，协议为 `schema_version=2`。
当前读取器仅支持 v2，其他协议版本均明确报错。
从本机 `runs/history-20260914-1500-2100/` 的 SQLite 离线导出，没有重新请求 Grafana。
时间和文件系统仍为北京时间 **2026-09-14 15:00–21:00**、`cfs_echo_hl_qa_fs`。
该次运行有 205 个面板（65 个 success、140 个 empty），206,492 个点、4,053 条曲线，
含 8,684 个 null；相对更早样本多一个无数据面板，不能把旧样本面板数当作本次清单。

复制整个目录即可交付：`points.parquet`、`series.parquet`、`manifest.json`、
`provenance.json.gz`、`README.md`。完整来源另存压缩文件，普通点值读取不需要解压它。
相同源运行的 v1/v2 点值、曲线身份、完整标签、单位、质量信息及查询配置逐项相同。
五文件总计 1,038,201 字节，比对应 v1 的四文件 5,951,166 字节减少 82.55%。

```bash
python examples/read_parquet.py samples/parquet_v2_20260914_1500_2100 --panel 166 --limit 5
```

核对记录见 `validation/parquet-v2-audit.json`、`validation/parquet-v2-comparison.json`。
原始 SQLite 和 HTTP 响应仍在采集运行目录，不包含在此数据包中。

## v1 历史对照

`parquet_20260914_1500_2100/` 是 0.2.0 的完整 Parquet 快照，采集范围为北京时间 **2026-09-14 15:00–21:00**，文件系统 `cfs_echo_hl_qa_fs`。来自此前真实采集记录的离线转换，未重新发起查询。

数据包包含 points.parquet、series.parquet、manifest.json 和读取说明。全部 **204 个面板、30 个分组**在清单中；65 个面板有数据，139 个合法无数据。实际有 **206,492 个点、4,053 条曲线**，其中 **8,684 个点的值为 null**，其余 197,808 个为数字。缺失时间点没有补零或新增记录。

数值采用 Grafana 原单位；series 表保存完整标签、原单位及显示换算信息。吞吐率保留 B/s 数值，显示元数据可转 GiB/s；比例保留原值，1 表示 100%。详见数据包 README。

此目录仅保留作历史核查和体积对照，当前读取器不支持 v1；向下游交付请使用上面的 v2 目录。数据包不包含浏览器登录状态、SQLite 或压缩接口响应。清单里的源运行绝对路径及 raw 响应路径是来源记录。原始 SQLite/响应仍在本机 `runs/sdkv2_20260914_1500_2100/`；完整数值核对报告见 `validation/parquet-audit.json`。

历史 0.1.1 Excel 样本和压缩包保留在本机原目录；本仓库当前样本使用 Parquet。主仓库位于内部 Codebase。`validation/` 同时保留早期采集验证记录，需按记录版本区分；当前格式以本数据包 manifest 为准。
