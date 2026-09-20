# Grafana Parquet 数据包（schema_version=2）

`points.parquet` 是长表：每行一条真实面板曲线数据点，包含 `time`、
`panel_id`、`series_id`、`value`。值来自已完成 Math/面板转换的存储结果，
保留 Grafana 原单位，未乘显示系数。时间类型为 `timestamp[ms, tz=UTC]`。
`series.parquet` 每条曲线一行，以 `(panel_id, series_id)` 连接；完整标签
为 `map<string, string>`，数字形状的标签仍是字符串，名称不用于主键。
面板标题和分组只保存在 `manifest.panels`，按 `panel_id` 关联。
曲线不再保存重复的 `source_metadata_json`；`quality_json` 只保存原始质量
信息，`aggregate_tags` 是可空字符串列表，`extra_metadata_json` 只保存其他
未结构化的扩展属性。JSON 列缺省为空，不重复标签、单位、名称或 ID。

```python
from pathlib import Path
import json
import pyarrow.parquet as pq
from zoneinfo import ZoneInfo

manifest_path = Path("manifest.json")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
base = manifest_path.parent
points = pq.read_table(base / manifest["files"]["points"]["path"])
series = pq.read_table(base / manifest["files"]["series"]["path"])
panels = {row["panel_id"]: row for row in manifest["panels"]}
by_id = {(row["panel_id"], row["series_id"]): row for row in series.to_pylist()}
for point in points.slice(0, 5).to_pylist():
    curve = by_id[(point["panel_id"], point["series_id"])]
    display_value = None if point["value"] is None else point["value"] * curve["display_scale"]
    print(point["time"].astimezone(ZoneInfo("Asia/Shanghai")),
          curve["name"], point["value"], curve["value_unit"],
          display_value, curve["display_unit"], dict(curve["labels"]))
```

`value_unit` 描述原值单位；`grafana_unit` 保留 Grafana 配置代码。
例如 `binBps` 的原值单位是 `B/s`，`kbytes` 是 `KiB`，`percentunit`
是比例 `ratio`（1 表示 100%），后者 `display_scale=100`、`display_unit=%`。
`short`/`none` 的 `value_unit=1` 表示无物理单位的数字格式；`string`
不声明数值物理单位。未知单位显式标记 `unit_status=unsupported`，原代码
保留，单位为空且系数为 1；该系数只保留原值，不表示已推断出单位。
显示单位按本次选择的面板/时间窗固定；不同导出窗口可能选择不同前缀。
显示元信息不包含 Excel 数字格式，数值没有提前四舍五入。

`value` 是可空 float64。整数必须能精确转换为 float64，否则整个导出失败；
非有限数值也会明确报错。源存储此前已归一为空值的异常数值仍为空值，
相关批次质量计数见 provenance，曲线质量信息见 quality_json。
实际空值有一行 null；缺少数据点不产生行，
不补时间、不补零、不插值。不能仅凭表中没有行判断正常无数据或失败：
请检查 manifest 的面板 collection_status、range_status 和 display_coverage。

快照主键为 `(panel_id, series_id, time)`，不是采集事件流水；重叠修订只
保留当前存储结果。series_id 不能代替跨运行的 run 身份。
manifest 保留面板状态、范围、精简采样信息及文件索引；完整冻结看板、采样
配置、查询定义、原始响应路径和批次状态保存在 `provenance.json.gz`。
`provenance.queries[query_key]` 保存一份 definition 和该查询的采集状态；
`provenance.panels[str(panel_id)]` 保存转换、面板配置及 query_keys 引用。
实际采样间隔见 queries[query_key].definition.interval_ms 及 metadata，
其中 interval_ms=0 代表后端表达式决定采样，不能当作零秒采样。
没有声称逐点对应某个原始响应批次。
默认结果为面板显示层；网络查询输入未另行导出。

manifest 的所有产物路径都相对它所在目录；外层和 generation 内均可独立
读取。每次导出写入新的不可变目录，全部文件写完后才原子更新外层 manifest。
交付时复制整个 generation 目录，包括 provenance.json.gz。只读取数据点
不需要解压 provenance；需要追溯时：

```python
import gzip

with gzip.open(base / manifest["files"]["provenance"]["path"], "rt", encoding="utf-8") as stream:
    provenance = json.load(stream)
assert provenance["generation"] == manifest["generation"]
```

项目的 grafana_collector.dataset.read_points/read_provenance 只接受协议 v2。
已有运行结果可从原 SQLite 离线导出到新目录，无需重新采集。
