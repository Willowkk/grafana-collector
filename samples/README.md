# 真实采集样本

`parquet_20260914_1500_2100/` 是 0.2.0 的完整 Parquet 快照，采集范围为北京时间 **2026-09-14 15:00–21:00**，文件系统 `cfs_echo_hl_qa_fs`。来自此前真实采集记录的离线转换，未重新发起查询。

数据包包含 points.parquet、series.parquet、manifest.json 和读取说明。全部 **204 个面板、30 个分组**在清单中；65 个面板有数据，139 个合法无数据。实际有 **206,492 个点、4,053 条曲线**，其中 **8,684 个点的值为 null**，其余 197,808 个为数字。缺失时间点没有补零或新增记录。

数值采用 Grafana 原单位；series 表保存完整标签、原单位及显示换算信息。吞吐率保留 B/s 数值，显示元数据可转 GiB/s；比例保留原值，1 表示 100%。详见数据包 README。

```bash
python examples/read_parquet.py samples/parquet_20260914_1500_2100 --panel 166 --limit 5
```

快照文件可直接交给下游读取，不包含浏览器登录状态、SQLite 或压缩接口响应。清单里的源运行绝对路径及 raw 响应路径是来源记录，不是读取此数据包的依赖。原始 SQLite/响应仍在本机 `runs/sdkv2_20260914_1500_2100/`；完整数值核对报告见 `validation/parquet-audit.json`。

历史 0.1.1 Excel 样本和压缩包保留在本机原目录；本仓库当前样本使用 Parquet。`validation/` 同时保留早期采集验证记录，需按记录版本区分；当前格式以本数据包 manifest 为准。
