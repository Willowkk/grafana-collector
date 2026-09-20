# SDKv2 Grafana 数据采集工具

从内部 Grafana SDKv2 大盘下载历史指标，或在 Mac 上持续增量采集，默认导出程序可直接读取的 **Parquet 长表数据集**。支持本实例 OpenTSDB、Bosun、Grafana Math 和现有面板转换；Excel 可通过 `--format xlsx` 选择。项目可独立安装运行，不依赖 Codex。

主仓库：[Codebase / jinpengbin/grafana-collector](https://code.byted.org/jinpengbin/grafana-collector)。

## 安装

采集需要 **Python 3.11 或以上**、Google Chrome，以及能访问 `grafana.byted.org` 的网络；离线导出和读取 Parquet 不需要 Chrome 或 Grafana 网络。macOS 的 `/usr/bin/python3` 可能是旧版本，请先检查版本。以下以 Python 3.12 为例：

```bash
git clone git@code.byted.org:jinpengbin/grafana-collector.git
cd grafana-collector
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
grafana-collector --help
```

普通安装只需 `python -m pip install .`。发布 wheel 可在另一台具备相同网络访问能力的 Mac 上安装；无需 Codex。`requirements.lock` 记录本次验证环境的版本。

克隆需要本机 SSH 身份已登记到 Codebase；可用 `ssh -T git@code.byted.org` 检查。已有本地项目时直接进入项目目录执行环境创建和安装步骤。

## 首次登录与历史下载

示例配置 `examples/sdkv2.toml` 已填入本次链接：文件系统 `cfs_echo_hl_qa_fs`，北京时间 **2026-09-14 15:00–21:00**，其余变量沿用链接。修改配置里的 URL 即可改变筛选条件。

```bash
grafana-collector login --config examples/sdkv2.toml
grafana-collector inspect --config examples/sdkv2.toml --out runs/query-plan.json
grafana-collector fetch --config examples/sdkv2.toml
```

工具会打开独立 Chrome 窗口。首次及登录失效时在该窗口完成登录，程序自动检测。`login` 不止检查页面，还会执行一条真实指标查询。登录状态保存在 `~/.local/share/grafana-collector/chrome-profile/`，不进入数据包。同一 profile 同时只能运行一个命令。

可以先采集少量面板，或另设历史时间窗：

```bash
grafana-collector fetch --config examples/sdkv2.toml --panels 166,150,70 --out runs/selected
grafana-collector fetch --config examples/sdkv2.toml \
  --from '2026-09-14T16:00:00+08:00' --to '2026-09-14T17:00:00+08:00' \
  --out runs/one-hour
```

命令行参数优先于 TOML 对应命令分节，分节优先于公共配置。无时区日期按北京时间解读，支持 ISO 时间、10 位秒时间戳、13 位毫秒时间戳及 `now-1h`。

## 持续采集与恢复

```bash
grafana-collector watch --config examples/sdkv2.toml
```

默认从本次命令启动时刻开始，首次在 5 分钟后查询此后产生的数据。随后每 5 分钟发起一轮，按每条查询上次成功的边界继续，并向前回查 5 分钟以更新迟到数据。查询起点始终不早于本次数据集起点。此默认回查量是可调工程参数，不代表后端承诺的最大上报延迟。

```bash
# 明确补采启动前的数据；后台追踪会沿用同一采样间隔
grafana-collector watch --config examples/sdkv2.toml --since 'now-1h' --out runs/backfill-live

# 自定义调度，仅影响何时请求，不改变数据点采样精度
grafana-collector watch --config examples/sdkv2.toml \
  --poll-interval 1m --lookback 10m --rounds 3 --out runs/three-rounds
```

按 **Ctrl+C** 正常停止并导出。`--duration 1h` 或 `--rounds 3` 也会自动停止。睡眠、断网或异常退出后，重新执行相同命令和 `--out`，工具使用已保存的看板、变量、采样设置及逐查询进度补齐缺口；中断超过 30 分钟也不丢弃更早的未采区间。恢复不重新解释 `now`，不要再传 `--since` 或修改起止时间。

同一输出目录冻结 URL、面板选择、轮询和回查配置。更改这些设置请使用新目录。真实失败不推进该查询的成功边界，成功空响应则记录为 `empty`。认证不能恢复时停止采集并保留进度；已有登录状态时可用 `--headless`，需要重新登录时重新运行有窗口的命令。

## 数据口径

- **轮询周期不等于采样间隔。** 历史下载按完整查询时间窗计算 Grafana 自动间隔；持续模式按输入链接的展示跨度计算一次并固定。缺少链接时间时使用看板默认展示跨度。
- 固定 1440×900 视口测量面板实际宽度，并校验当前 Grafana 7 的 24 列、8px 间距布局。随后冻结 `maxDataPoints` 和数据源/面板最小间隔。显式降采样和禁用降采样查询仍遵循原配置。
- 每轮是完整查询窗口。首版不自动切分历史长窗口，避免 TopK、rate 或 Bosun 计算因切片而改变含义。过大请求会明确失败；可以主动选择较小的业务时间段，但这些是独立窗口。
- 默认不采隐藏 target，公式需要的隐藏输入仍会获取。Math 和 `calculateField` 在标签与时间戳对齐后计算；缺失或零分母输出空值。
- 成功响应是其请求窗口的最新观察：替换该查询窗口内的旧标准化点，保留窗口外的历史。合法空响应同样替换对应窗口；失败不会删除历史。所有原始批次仍保留，可追溯修订。
- TopK 是每次查询窗口内的筛选。累计结果不是重新对累计大窗口求 TopK，曲线数量可能超过 K。需要完整时间窗的 Grafana 口径请用 `fetch`。
- 筛选变量是否实际作用于某面板，取决于原查询。某些面板固定通配符或没有 filesystem/task_id 条件，工具如实保存其实际范围。
- 单位跟随面板轴和曲线覆盖配置。API QPS 不会改名为磁盘 IOPS，已聚合的 P99 不会被宣称为全局请求 P99。

## 离线导出与文件结构

```bash
grafana-collector export --run runs/live --out runs/live
grafana-collector export --run runs/history --out runs/history-subset \
  --panels 166 --from '2026-09-14T16:00:00+08:00' --to '2026-09-14T17:00:00+08:00'
```

`fetch`、`watch`、`export` 默认 `--format parquet`，支持 TOML 的 `format` 设置，命令行优先。`export` 不需要 Chrome 或网络；旧版本采集的 SQLite 可以直接导出新格式，无需重新采集。运行目录中有：

```text
运行目录/
  collection.sqlite3          # 冻结配置、标准化时序、状态和恢复进度
  raw/                        # 每次请求对应的压缩原始响应
  manifest.json               # 最近一次完整导出的索引和状态
  exports/<导出批次>/
    points.parquet            # 全部所选面板的实际数据点
    series.parquet            # 曲线名称、完整标签及单位
    manifest.json             # 此批次独立清单，路径相对此文件
    README.md                 # 数据协议和读取说明
```

每次导出读取同一个 SQLite 快照，写入独立批次目录后再原子更新外层 `manifest.json`。旧批次保持可读，导出失败不会替换上次有效清单。交付数据时复制整个 `exports/<导出批次>/` 即可，里面的清单和文件可独立使用。外层清单通过 `files.points.path`、`files.series.path` 指向最近一次导出的文件，不要自行拼接文件名。

### Parquet 数据协议

协议标识为 `sdkv2-grafana-parquet`，`schema_version=1`，Parquet 使用 Arrow 类型和 Zstandard 压缩。一个数据集对应一次导出的固定快照，所有面板使用同一套列结构。

`points.parquet` 一行代表一条曲线在一个时刻的值：

| 字段 | Arrow 类型 | 含义 |
|---|---|---|
| `time` | `timestamp[ms, tz=UTC]` | 实际数据点时间；展示时可转北京时间 |
| `panel_id` | `int64` | 面板 ID |
| `series_id` | `string` | 曲线稳定 ID，与曲线信息表关联 |
| `value` | `float64`，可为 null | 面板公式及转换后的值，使用原单位，不按显示精度舍入 |

快照内 `(panel_id, series_id, time)` 唯一。曲线 ID 来自完整标签和查询等身份信息，不能只用图例文本识别曲线；跨大盘或不同采样/查询配置的数据不要直接混为同一数据集。新增 namespace 或 TopK 曲线只增加行，不增加列。

`series.parquet` 每条曲线一行，保存面板标题及分组、图例名称、完整标签（`map<string,string>`）、指标和引用 ID，以及 `grafana_unit`、`value_unit`、`display_unit`、`display_scale`。任务 ID 和 namespace 始终作为字符串标签保存。数据点只包含完成 Math/面板转换后的曲线，隐藏查询输入及详细请求来源见运行记录和清单。

单位来自 Grafana 配置；计算值与显示换算分开。例如 Throughput 的 `value=506523156.5333`、`value_unit=B/s`，`display_unit=GiB/s`、`display_scale=1/1024³`。需要展示时计算 `value * display_scale`，可显示为 `0.472 GiB/s`。比例保留原值，`0.01` 表示 1%；其 `display_unit=%`、`display_scale=100`。显示尺度按本次所选面板/时间窗确定，不能用它改变或解释其他批次的原值。未知单位会明确标识，不能猜测换算。

值不预先舍入；只有能精确表示为 `float64` 的整数才允许导出，不能精确转换的大整数会导致明确错误，旧导出保持有效。源浮点值保留 SQLite 中已保存的二进制浮点精度。时间戳保留毫秒，空表也有固定类型。

只导出实际数据点。明确返回的空值保留为 null，未返回的时间点不生成行，不补零、不插值。比如 Throughput 这次返回 11 条曲线、52 个实际点，长表就是 52 行；不同 namespace 的活跃时段本来可以不同。

`manifest.json` 保存看板版本、变量、查询定义、采样口径、来源及每个所选面板的状态。无数据的面板也在清单中，不能仅凭 points 中是否出现该面板判断成功或失败。

`collection_status` 是运行的采集状态，`range_status` 是本次导出范围的覆盖情况（`covered`、`partial`、`uncollected`）；`display_coverage` 列出实际已计算的时间区间。导出操作完成不代表该时间范围数据完整，应同时查看这些状态。

持续模式仍先写 SQLite，正常停止时导出累计快照。回查产生的新增、修订和删除先在 SQLite 中处理，因此下游不应把多个导出快照直接追加：同一批次内部已经去重，后一个完整快照用于替换前一个。首版不提供逐轮变更流。

### 读取示例

下面从运行目录或独立数据包的清单读取面板 166，得到 `pyarrow.Table`，无需 Pandas：

```python
import json
from pathlib import Path
import pyarrow.parquet as pq

manifest_path = Path("runs/history/manifest.json")
manifest = json.loads(manifest_path.read_text())
points = pq.read_table(
    manifest_path.parent / manifest["files"]["points"]["path"],
    filters=[("panel_id", "=", 166)],
)
series = pq.read_table(manifest_path.parent / manifest["files"]["series"]["path"])
print(points.schema)
print(points.slice(0, 5).to_pylist())
```

可执行示例 `examples/read_parquet.py` 还支持完整标签和时间筛选，并演示关联单位：

```bash
python examples/read_parquet.py runs/history --panel 166 --limit 5
python examples/read_parquet.py runs/history --panel 166 --tag method=cfs_pread \
  --from '2026-09-14T16:00:00+08:00' --to '2026-09-14T17:00:00+08:00'
```

### 可选 Excel

```bash
grafana-collector export --run runs/history --out runs/history-excel --format xlsx
```

Excel 保留 0.1.1 版规则：按分组每面板一个工作簿，白底表头，Time 加各曲线，缺失留空。同一面板统一 Grafana 显示单位，单元格仍为数字。Excel 的日期按北京时间显示；文件名为 `面板名-data-export.xlsx`，冲突或超出行列限制自动区分/拆分。Excel 使用独立清单结构；下游机器读取请选择 Parquet。

## 常见问题

- **HTTP 403：** 账号可能没有该接口权限。管理员数据源目录 403 时，工具自动读取页面使用的前端数据源目录；具体指标查询 403 仍明确记录为权限失败，不反复要求登录。
- **某些面板没有数据：** 先核对同时间、同变量的 Grafana。`empty` 与查询失败不同，缺点也不代表零。
- **启动 profile 失败：** 关闭其他使用同一专用 profile 的采集命令。不要将日常浏览器的默认用户目录传给 `--profile`。
- **持续模式数据稀疏：** 检查固定的降采样间隔、实际上报频率和回查范围，不能只调整轮询频率就期望新增更细的上报数据。
- **导出失败：** 查看错误信息及 `manifest.json` 中面板状态；已保存的数据可修复后重新离线导出。导出大整数精度错误时读取 SQLite/原始响应，不要自行转为浮点后忽略精度丢失。

退出码：`0` 命令操作完成，`1` 配置/认证/运行错误，`2` 为 fetch/watch 存在失败或未完成面板，或 inspect 发现不支持查询，`130` 被中断。离线 export 返回 `0` 表示导出文件已写入，数据完整性另见清单。正常 watch 停止会先导出。日志不输出 Cookie、密码或 Authorization。

## 验证与维护

```bash
python -m pytest -q
python -m pip wheel . --no-deps -w dist
```

测试使用合成响应，覆盖变量替换、真实版本自动间隔阈值、查询协议、公式、独立进度、迟到修订、TopK、断网重试、认证暂停、长时间中断恢复、停止与两种格式导出。真实采集核对见 `VALIDATION.md`。

模块职责：`query.py` 编译和计算；`transport.py` 登录与接口；`engine.py` 增量调度；`storage.py` 持久化；`exporting.py` 格式分发；`parquet_exporter.py` Parquet 协议；`exporter.py` Excel；`units.py` Grafana 单位与数字格式；`cli.py` 命令入口。若内部 Grafana 插件或布局变化，应先更新对应适配和对照测试，再使用新运行目录采集。
