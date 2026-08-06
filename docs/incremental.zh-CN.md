# dbt-doris Incremental 用户指南

本文是 `materialized='incremental'` 的用户入口，说明可用版本、策略选择、
配置方式、临时关系和失败重试语义。它与 Doris Async Materialized View 是两种
不同能力；本文只说明 Incremental。

发布验证、SQL 次数判定和失败注入清单见
[Incremental 测试方案](incremental-test-plan.zh-CN.md)；当前全部 pytest 节点、
参数化分支和覆盖语义见
[Incremental 自动化测试清单](incremental-test-inventory.zh-CN.md)。

dbt-doris 内置支持四种 Incremental 策略：`append`、`merge`、
`insert_overwrite` 和 `microbatch`。已有目标表且
`on_schema_change='ignore'` 时，每个普通增量批次都只执行一条最终 DML，
不会先把同一批数据写入物理临时表。

## 源候选已验证版本（PR #2 待复验）

以下五个 Doris 官方发行版本已经在同一份干净源实现候选上完成真实集群验证。
本实现选择性移入 PR #2 时排除了独立的 Grants/MV 改动；加入 Microbatch 前的
移植工作树曾通过 252 项 Unit 和 40 项本地 Doris Incremental Functional，但
正式发布前仍应在 PR Head 上重跑下表矩阵，不能把源提交日志当作新提交产生的日志：

| Doris | FE/BE 完整 Version | 完整 Functional | 聚焦 Incremental | 状态 |
| --- | --- | --- | --- | --- |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 98 passed / 106 warnings / 290.51s | 36 passed / 27 warnings / 45.20s | 源候选通过；PR #2 待复验 |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 98 passed / 106 warnings / 143.87s | 36 passed / 27 warnings / 52.49s | 源候选通过；PR #2 待复验 |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 98 passed / 106 warnings / 150.81s | 36 passed / 27 warnings / 43.94s | 源候选通过；PR #2 待复验 |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 98 passed / 106 warnings / 138.82s | 36 passed / 27 warnings / 39.69s | 源候选通过；PR #2 待复验 |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 98 passed / 106 warnings / 135.13s | 36 passed / 27 warnings / 39.48s | 源候选通过；dirty PR Head Microbatch 聚焦通过；完整复验待运行 |

五个版本都覆盖 `append`、MOW/MOR `merge`、可见 Sequence 列、整表/静态分区/
动态分区 `insert_overwrite`、Schema Change、Full Refresh、默认策略路由、现有
目标表模型与物理 Key 前置校验、Schema Change 冻结批次失败重试、已有 View
转换为 Incremental Table 时的 Snapshot/Pre-hook 失败重试，以及临时/备份对象
清理。每个版本的 FE/BE
完整 Version 一致且 `Alive=true`，测试数据库和 Helper Relation 残留均为 0。

PR #2 在该源候选之后又补了两类保护：Schema Change/自定义策略的 batch staging
改为 keyless Duplicate + RANDOM/AUTO，以及已有目标的物理 Sequence mapping
一致性校验。它们已通过 Unit 和本地开发集群 E2E，但尚未进入上表五个官方版本的
PR Head 复验，不能从源候选结果推导为已通过。

本 PR 后续新增的 `microbatch` 也不在上表历史结果中。它依赖的命名分区
`INSERT OVERWRITE` SQL 能力覆盖这些目标版本。当前 dirty PR Head 已在 FE/BE
均为 `doris-0.0.0-ebec9530ba` 的本地开发集群完成 99 项完整 Functional 和 42 项
聚焦 Incremental，并在带精确版本 Gate 的 Doris 4.1.3 上完成静态分区与 Dynamic
Partition 两项 Microbatch 聚焦用例。后者不是 4.1.3 的完整 PR Head 矩阵证据；
五个精确发行版本仍须完整重跑后，才能标记 Microbatch 兼容通过。

验证环境是 dbt Core 1.12.0、dbt-doris 1.0.0、Python 3.12.13；被测 Adapter
提交为 `7f6d9701140188f347e9f68a25ef9013551e4e48`，`dirty=false`。每份测试日志
开头的 `DORIS_E2E_VERSION_EVIDENCE` JSON 都记录了目标发行版、实际 FE/BE
Build、Gate 状态和 Adapter 身份。Unit 为 327 passed / 9 warnings / 57.99s，
Flake8 与 diff check 通过。

安装约束是 Python 3.10+ 和 dbt Core 1.12.x；上表的正式矩阵使用 Python 3.12.13
与 dbt Core 1.12.0。

“已验证”只承诺表中这五个**精确发行版本**。它不表示其他 2.1.x、3.x 或 4.x
一定不可用，也不把未实测的 Patch 版本自动视为兼容；生产使用其他版本前应运行
同一套 Functional 和聚焦 Incremental 测试。2.1.11 暴露的调用 Session
`sql_mode` 问题已通过 Pre-model Ordering 修复，并由该版本两套测试验证。

## 策略选择

| 配置 | Doris 目标表 | 普通增量语句 | 结果语义 |
| --- | --- | --- | --- |
| `append` | Duplicate Key | `INSERT INTO` | 追加本批全部行 |
| `merge` | MOW 或 MOR Unique Key | `INSERT INTO` | 按 Unique Key 完整行 Upsert |
| `insert_overwrite` | 可写 Doris 表 | `INSERT OVERWRITE` | 覆盖整表或指定分区 |
| `microbatch` | 按 `event_time` 单列 RANGE 分区的 Duplicate Key 表 | 命名分区 `INSERT OVERWRITE` | 按 dbt Core 时间窗口完整替换一个精确分区 |

没有显式配置 `incremental_strategy` 时：

- 配置了 `unique_key`：使用 `merge`；
- 没有 `unique_key`：使用 `append`。

`delete+insert` 和 `delete_insert` 均不受支持。需要按 Key 更新或插入时使用
`merge`；需要按时间窗口重算完整分区时使用 `microbatch`。

## `append`

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id']
) }}

select id, value, loaded_at
from {{ ref('source_events') }}

{% if is_incremental() %}
where loaded_at > (
    select coalesce(max(loaded_at), '1970-01-01 00:00:00') from {{ this }}
)
{% endif %}
```

首次运行创建 Duplicate Key 目标表；后续运行通过一条 `INSERT INTO` 追加。
`is_incremental()` 只负责让 Model SQL 选出“本批数据”，Adapter 不会替用户自动
推断水位。未加过滤时，后续运行会再次追加 Model 返回的全部行。

## `merge`

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['tenant_id', 'id'],
    distributed_by=['tenant_id', 'id']
) }}

select tenant_id, id, value, updated_at
from {{ ref('source_changes') }}

{% if is_incremental() %}
where updated_at > (
    select coalesce(max(updated_at), '1970-01-01 00:00:00') from {{ this }}
)
{% endif %}
```

这里的 `merge` 是 dbt 的结果语义，不代表当前会生成 Doris 原生
`MERGE INTO`。Adapter 向 Unique Key 表执行完整行 `INSERT INTO`，由 Doris
存储模型完成 Upsert。包括 Doris 4.1.3 在内，当前 Adapter 仍走这条可跨版本的
Unique Key 路径，没有启用 4.1+ 原生 `MERGE INTO`：

- 同时支持 Merge-on-Write（MOW）和 Merge-on-Read（MOR）Unique Key 表；
- 新建 Unique Key 表默认使用 MOW；显式配置
  `properties={'enable_unique_key_merge_on_write': 'false'}` 可创建 MOR；
- 已有目标必须是 Unique Key 表，且 Doris 物理 Key 与 `unique_key` 的顺序完全
  一致；不一致时先用 Full Refresh 重建；
- 模型 SQL 中 Key 列不必写在最前面；首次创建和 Full Refresh 会按
  `unique_key` 配置顺序将 Key 投影为 Doris 物理 Schema 的前缀；
- `function_column.sequence_col` 使用模型返回的可见列，并继续由 Doris 按
  Sequence 规则决定新旧版本；
- 每一批 Source 中，同一个单列或复合 `unique_key` 只能出现一次。Adapter
  在同一条 Upsert 语句内校验重复 Key；失败时不会先改写目标表。内部校验列从
  n+1 个保留候选中选择不与本批 n 个目标列重名的 Alias，因此用户列名不会与
  Merge Guard 冲突；Sequence 只决定跨批版本顺序，不能用来绕过这项批内重复
  Key 检查。

`merge_update_columns`、`merge_exclude_columns` 和
`incremental_predicates` 暂不支持，因为它们需要条件或局部列更新。

裸 `sequence_col` 不是 dbt-doris 配置。请使用 Doris 表属性，例如：

```python
properties={
    'enable_unique_key_merge_on_write': 'true',
    'function_column.sequence_col': 'updated_at'
}
```

此时 Model 必须返回可见的 `updated_at` 列。即使较低 Sequence 的同 Key 行更晚
到达，Doris 也不会用它覆盖较高 Sequence 的版本。

Sequence mapping 是 Doris 表的物理属性，必须在目标表首次创建时配置。每次已有
Unique Key 目标的 `merge` 都会读取 `SHOW CREATE TABLE`，确认模型配置与物理
`function_column.sequence_col` 完全一致；配置缺失、物理属性缺失、列名不一致或物理
表使用隐藏 `function_column.sequence_type` 时，都会在 Hook、staging 和 DML 前失败。
给既有目标新增、更换或删除 mapping 时应执行 `--full-refresh`；普通增量运行不会
ALTER 这项属性。

`insert_overwrite` 可以覆盖已有 Unique Key 表，但若该表使用隐藏
`function_column.sequence_type`，同样会在 Hook 和 DML 前拒绝；Adapter 无法在
显式目标列列表中安全写入 `__DORIS_SEQUENCE_COL__`。已有表使用可见
`function_column.sequence_col` 时，完整覆盖批次必须返回该可见列。

`function_column.sequence_type` 依赖写入 Doris 隐藏列
`__DORIS_SEQUENCE_COL__`，当前 Incremental 列映射不暴露该隐藏列，因此会在
执行前明确拒绝；请改用 `function_column.sequence_col`。

## `insert_overwrite`

`insert_overwrite` 不能同时配置 `unique_key`。这是迁移保护：旧版曾把该组合
当成 Unique Key Upsert；新版若直接接受为原生覆盖，可能静默删除本批未出现的
旧行。需要 Upsert 时改用 `merge`；确实需要覆盖时删除 `unique_key`，显式选择
以下原生语义。

不配置 `overwrite_partitions` 时覆盖整表：

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    duplicate_key=['id'],
    distributed_by=['id']
) }}

select id, value, event_date from {{ ref('replacement_data') }}
```

静态覆盖指定分区：

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    duplicate_key=['event_date', 'id'],
    partition_by=['event_date'],
    partition_type='RANGE',
    partition_by_init=[
        'PARTITION p20260801 VALUES LESS THAN ("2026-08-02")',
        'PARTITION p20260802 VALUES LESS THAN ("2026-08-03")'
    ],
    overwrite_partitions=['p20260801', 'p20260802'],
    distributed_by=['event_date', 'id'],
    properties={'replication_num': '1'}
) }}
```

动态覆盖本批数据涉及的分区：

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    duplicate_key=['event_date', 'id'],
    partition_by=['event_date'],
    partition_type='RANGE',
    partition_by_init=[
        'PARTITION p20260801 VALUES LESS THAN ("2026-08-02")',
        'PARTITION p20260802 VALUES LESS THAN ("2026-08-03")'
    ],
    overwrite_partitions='*',
    distributed_by=['event_date', 'id'],
    properties={'replication_num': '1'}
) }}
```

`overwrite_partitions` 只能与 `insert_overwrite` 和分区表一起使用。`'*'`
不能与静态分区名混用。整表覆盖时，Model 必须返回想保留的**完整目标数据**；
静态或动态分区覆盖时，Model 必须返回想保留的**完整分区数据**。覆盖范围内没有
出现在本批结果中的旧行会被删除。

## `microbatch`

`microbatch` 使用 dbt Core 1.12.x 的批次编排。Core 将时间轴拆成
`hour`、`day`、`month` 或 `year` 批次，并把配置了 `event_time` 的上游
`ref()` / `source()` 自动过滤到 UTC 半开区间 `[event_time_start,
event_time_end)`；dbt-doris 再用一条命名分区 `INSERT OVERWRITE` 完整替换
目标表中同一精确区间。

一个静态分区的日批模型示例：

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='microbatch',
    event_time='event_time',
    batch_size='day',
    begin=modules.datetime.datetime(2026, 1, 1, 0, 0, 0),
    duplicate_key=['id', 'event_time'],
    partition_by=['event_time'],
    partition_type='RANGE',
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select id, event_time, value
from {{ ref('source_events') }}
```

`source_events` 自身也必须配置 `event_time='event_time'`，Core 才能在这个
`ref()` 上自动注入批次过滤。最终 Model 输出必须是当前批次的完整数据，且所有行
都落在当前 `[start, end)`；不要再用 `is_incremental()` 自行维护另一套水位。

Microbatch 配置约束如下：

- 仅支持 dbt Core 1.12.x；必须配置 `event_time`、`batch_size`、`begin`、
  单列 `partition_by` 和 `partition_type='RANGE'`；`partition_by` 必须与
  `event_time` 是同一个未加引号的普通列名；
- 目标必须是 Duplicate Key 表。不能配置 `unique_key`、Sequence mapping、
  `overwrite_partitions`、`partition_by_init`、`predicates` 或
  `incremental_predicates`；初始精确分区由 Adapter 从 `model.batch` 创建；
- `lookback` 和命令行 `--event-time-start` / `--event-time-end` 仍由 dbt Core
  决定要重跑哪些批次；Core 按当前 UTC 时间和配置计算窗口，不读取目标表的
  `max(event_time)` 水位。每个批次的结果语义相同，都是完整替换；
- Adapter 不声明 `MicrobatchConcurrency` 能力，因此批次顺序执行；配置
  `concurrent_batches=true` 也不会让 Doris 批次并行；
- Core 的边界是 UTC aware datetime，Doris `DATETIME` / `DATETIMEV2` 没有时区。
  Adapter 会渲染不带 Offset 的 UTC 字面量；模型和上游数据必须把这些值按 UTC
  naive 时间保存，避免 Session 时区造成批次偏移。

### 静态分区模式（默认）

未配置 `dynamic_partition.enable='true'` 时，dbt-doris 管理每个批次的精确
RANGE 分区：

1. 首批用 CTAS 建表，并创建
   `dbt_mb_<Core batch id 去掉 T>`，例如日批 `dbt_mb_20260804`；
2. 后续批次先读取 `information_schema.partitions`。已有精确 `[start, end)`
   分区时使用其真实名称，因此既有分区不必遵循 `dbt_mb_` 命名；
3. 精确分区不存在且与现有分区不重叠时，执行一次
   `ALTER TABLE ... ADD PARTITION` 创建空分区，再执行一条命名分区
   `INSERT OVERWRITE`；
4. 若只找到更粗粒度、重叠或无法安全解析的分区，则在覆盖前失败，避免误删
   其他批次。

`ADD PARTITION` 只创建分区元数据和空 Tablet，不会运行 Model 查询，不是同批数据
的第二次物化。`on_schema_change='ignore'` 时，已有目标仍只使用逻辑
`__dbt_tmp` View 和一条目标 `INSERT OVERWRITE`。静态模式不会自动删除过期
分区，保留策略由用户管理；如果 ADD 成功后、最终 DML 前失败，可能留下空分区，
重试会解析并覆盖该分区。

### Doris Dynamic Partition 模式

若希望 Doris 调度器管理分区，可加入：

```python
properties={
    'replication_num': '1',
    'dynamic_partition.enable': 'true',
    'dynamic_partition.time_unit': 'DAY',
    'dynamic_partition.time_zone': 'UTC',
    'dynamic_partition.prefix': 'p',
    'dynamic_partition.start': '-30',
    'dynamic_partition.end': '1',
    'dynamic_partition.create_history_partition': 'true'
}
```

其中 `time_unit` 必须等于 `batch_size`，时区必须是 `UTC`、`Etc/UTC` 或
`+00:00`，`prefix` 必须是安全标识符，`end` 必须是正整数，且
`create_history_partition='true'`。还必须配置 `start` 或
`history_partition_num`，并让窗口覆盖 `begin` 和所有回填批次；月批的
`start_day_of_month` 只能省略或设为 `1`。

已有目标表会校验静态/动态模式不能漂移，并比较 `time_unit`、`prefix`、`end`、
`create_history_partition`、UTC 时区以及模型显式配置的 `start`、
`history_partition_num`、`start_day_of_month`。Dynamic Partition 模式下
Adapter 不执行 `ADD PARTITION`；如果调度器尚未创建当前精确区间，会在 Hook 和
写入前失败。扩大历史窗口并等待分区生成后再重试。长时间回填还受 Doris 动态
分区保留窗口和集群分区数量限制；这类场景通常更适合默认的静态模式。

### 为什么不用 `PARTITION(*)`

Doris 的 `INSERT OVERWRITE ... PARTITION(*)` 只替换本批结果实际触达的分区；
当查询返回 0 行时它是 No-op，会把目标中的旧批次错误地保留下来。Microbatch
必须支持“空结果也清空整个批次”，所以 Adapter 总是先解析一个精确物理分区名，
再执行：

```sql
INSERT OVERWRITE TABLE target PARTITION(`actual_partition_name`) (...)
SELECT ...;
```

命名分区在空结果下也会被完整替换。这正是 `microbatch` 与普通
`insert_overwrite` 的关键差别：用户不指定覆盖分区，Adapter 按 Core 批次边界
解析并保护覆盖范围。

## Schema Change

支持 dbt Core 的四种 `on_schema_change`：

| 配置 | 行为 | 本批是否使用物理 staging table |
| --- | --- | --- |
| `ignore`（默认） | 不同步列集合；仍允许安全的字符串宽度扩展 | 否，使用逻辑 View |
| `fail` | 检测到差异立即失败，目标 Schema 和数据不变 | 是 |
| `append_new_columns` | 添加 Source 新列后写入冻结批次 | 是 |
| `sync_all_columns` | 同步新增、删除和可变类型后写入冻结批次 | 是 |

Doris Unique Key 和可见 Sequence 映射列是物理关键列。增量运行不修改它们的
类型；需要改变这些列时使用 `dbt run --full-refresh --select <model>`。

## 临时关系与 Full Refresh

已有目标表的普通 `on_schema_change='ignore'` 增量运行会创建名为
`__dbt_tmp` 的普通逻辑 View。这个 View 只保存模型 SQL 定义，用于读取列名、
类型和字符串长度；它不保存查询结果，也不会造成一次数据物化。随后策略读取
该 View，向目标表执行唯一一条 DML，运行结束后删除 View。首次运行则直接
CTAS 创建目标表。

以下场景会创建物理关系：

- `on_schema_change` 不是 `ignore`：使用物理 staging table 冻结本批数据，
  避免修改目标 Schema 前后读取到不同批次。成功写入目标的运行会先物化 staging，
  再向目标写入，确实有两次物理数据写入；`fail` 检测到差异时只有 staging
  CTAS，目标表不会发生第二次写入；
- 自定义 Incremental 策略：使用物理 staging 保持 dbt 的标准策略参数契约；
- 普通策略的 Full Refresh：先创建 intermediate table，再用 Doris 元数据交换
  安全替换目标表。数据只写入 intermediate 一次，不会再写最终表一次；
- Microbatch 的首次运行或 Full Refresh：第一个时间批次用 CTAS 创建目标或
  intermediate 并完成交换，后续每个时间批次各执行一次命名分区
  `INSERT OVERWRITE`。每份批次数据仍只物化一次，不存在完整数据集的二次 copy。

冻结批次的 staging 是 Adapter 内部的 keyless Duplicate Table，固定使用
`DISTRIBUTED BY RANDOM BUCKETS AUTO` 和
`enable_duplicate_without_keys_by_default=true`，只从模型携带
`replication_num` 或 `replication_allocation`。它不会继承模型的 Key、
Distribution、Partition、Contract 或 `sql_header`，因此 Source 首列是 DOUBLE
等不可作 Key/Hash 的类型，或 Schema Change 删除原分桶列时，也不会让 helper DDL
失效。

另有一类与“冻结增量批次”不同的物理例外：已有 Canonical View 转换为
Incremental Table 时使用专用 CTAS Snapshot：

```sql
CREATE TABLE backup
DISTRIBUTED BY RANDOM BUCKETS AUTO
PROPERTIES (
  "enable_duplicate_without_keys_by_default" = "true",
  "replication_num" = "..."
)
AS SELECT * FROM source_view;
```

Incremental View → Table 类型切换路径绝不重放 View DDL，也不假设 View 保留创建时
的 SQL Mode/Session 语义。
Doris 2.1.11 实测表明，查询旧 View 的结果可能受调用 Session 当前 `sql_mode`
影响。因此 Snapshot 必须在新模型任何 Pre-hook、`sql_header` 或 DDL 之前执行，
使用尚未被新模型改变的 Pre-model Session。Snapshot 固定 RANDOM/AUTO 分桶和
`enable_duplicate_without_keys_by_default=true`；仅允许从当前模型配置携带
`replication_num` 或 `replication_allocation`，绝不从旧 View 推断副本属性，也不
继承新模型的 Key、Distribution、Partition、Contract 或 `sql_header`。这避免把
DOUBLE 等不可作 Key/Hash 的首列误选为物理 Key 或分桶列。该正向 Snapshot 是
物理 Table。Snapshot Helper 遇到源/目标同名时会在执行任何 SQL 前失败；目标已
存在时可能先做只读 Relation 元数据查询，但不会执行修改 SQL 或 Drop。
Incremental 类型切换不依赖 Generic View Rename/Exchange，而是显式执行
Snapshot、构建 Replacement、删除旧 View 和重命名 Replacement。

Snapshot 保存的是当时从旧 View 可查询的结果数据，不保存 View Definition、创建
时 Session 状态、Comment、Grant 或完全一致的 Schema 属性。

CTAS 失败时，Canonical 旧 View 继续在线，且新模型 Hook、Header、DDL 均未执行。
CTAS 成功后也不会立即删除旧 View：Adapter 先运行新模型上下文并完成 Replacement
构建，期间 Canonical 名仍指向旧 View；Replacement 就绪后才 Drop 旧 View 并将
Replacement Rename 为 Canonical。Snapshot Marker 保留到 Main Build、Index、
Docs、事务内 Post-hook 和 Commit 成功后才清理；事务外 Post-hook 在清理之后运行。
SQL Mode 用例必须断言 Pre-model Session 当时实际查询到的数据以及上述 Ordering，
不能再用 View 的创建模式推导查询结果。

这类 Snapshot 仅用于正向类型切换，不改变 `append`、`merge`、
`insert_overwrite` 或每个 `microbatch` 的逻辑临时 View + 一条最终 DML 契约。

Doris 执行 `INSERT OVERWRITE` 时内部使用的临时分区属于数据库实现细节，
不等同于 dbt-doris 的物理 staging table。

`__dbt_backup` 的恢复边界与正向 CTAS 不同。Incremental 发现 Canonical
缺失而 Backup 存在时，不会先把 Backup 恢复到 Canonical，也不会执行、Snapshot、
Rename 或提前删除它。Backup 保持原名作为 Durable Marker；当前自动化覆盖 Legacy
View 和 Table，Legacy View Backup 完全不走 CTAS。

本轮直接从 Model SQL 完整构建 Canonical。因为 Canonical 在编译时仍不存在，
`is_incremental()` 为 false；如果本轮再次失败，Canonical 继续缺失，下一轮仍走
完整构建分支。旧数据只在 `__dbt_backup` 名下可查询，Adapter 不保证失败期间
Canonical 名可用。只有 Main Build、Index、Docs、事务内 Post-hook 和 Commit 成功
后，才删除 Durable Marker。Incremental 三轮 Functional 用例覆盖“保留 Marker →
再次失败 → 成功构建后清理”的流程；事务外 Post-hook 在 Marker 清理后运行。

若失败后 Canonical 旧 View 仍在线，遗留的物理 Snapshot 只是上一次尝试的 Marker；
下一次运行在重新冻结旧 View 前先清理或替换该 Marker。若失败发生在 Drop View 与
Rename Replacement 的切换窗口，使 Canonical 缺失，则物理 Marker 是唯一旧数据
副本。Incremental 必须按上一段 Durable Marker 规则保留它，直到 Canonical
完整重建成功。

## Hook 失败与重试

对于已有 Table 目标的普通增量运行，Doris 的写入和 DDL 不会因为后续 Hook 失败
而由 dbt 回滚。`transaction: true` 只保留 dbt Hook 的执行阶段，不应理解为
Doris 上的 ACID 回滚边界：

- Pre-hook 失败发生在临时关系和目标 DML 之前，目标数据不变，也不会留下
  `__dbt_tmp`；
- Post-hook 失败发生在目标 DML 之后，已经写入的目标数据仍然可见。事务内
  Post-hook 失败时逻辑 `__dbt_tmp` View 可能保留；事务外 Post-hook 在 Helper
  清理之后运行；
- 普通策略的下一次运行会先清理同名陈旧 Helper；Microbatch Helper 名含 Batch
  ID，`dbt retry` 或重跑同一 event-time 窗口会清理同一批的 Helper。若后续窗口
  已前移，Adapter 不会通配删除其他 Batch ID，以免误删另一 Invocation 正在使用的
  对象；确认没有活跃运行后，可先精确回填失败批次使其收敛，再检查残留；
- `merge` 对同一批 Key 的重试可依赖 Unique Key Upsert 收敛；`append` 重试可能
  产生重复行，`insert_overwrite` 会再次覆盖所选范围；`microbatch` 会再次完整
  覆盖相同时间分区并收敛。因此 Post-hook 失败后应先确认策略和目标数据，再决定
  是否原样重跑。

前三种策略的这些状态已经在五个精确 Doris 版本上执行失败注入和成功重试验证；
Microbatch 的正式版本失败矩阵仍待 PR Head 复验。

## 从旧实现迁移

旧版 dbt-doris 把显式 `incremental_strategy='insert_overwrite'` 与
`unique_key` 的组合实现成 Unique Key `INSERT INTO` Upsert。新版不会静默
改变该配置的结果，而是在 Hook 和写入前拒绝并提示迁移：

- 需要保留旧 Upsert 语义：改用 `incremental_strategy='merge'` 并配置
  `unique_key`；
- 确实需要整表或分区替换：继续使用 `insert_overwrite`，但删除
  `unique_key`，明确选择覆盖范围内缺失行会被删除的语义；
- 旧的 `delete+insert` 模型：改用 Unique Key 目标和 `merge`，并通过一次
  Full Refresh 重建不兼容的现有目标表。

## 运行与进一步阅读

```bash
# 首次创建或普通增量运行
dbt run --select <model>

# 安全重建目标表
dbt run --full-refresh --select <model>
```

- [Incremental 测试方案](incremental-test-plan.zh-CN.md)：精确版本证据、SQL
  次数和失败注入矩阵；
- [Incremental 自动化测试清单](incremental-test-inventory.zh-CN.md)：逐项 pytest
  节点、参数化 case 数和覆盖语义；
- Doris 4.1+ 原生 `MERGE INTO` 尚未实现；`merge` 仍使用本文说明的跨版本
  Unique Key Upsert 路径，`microbatch` 使用命名分区 `INSERT OVERWRITE`。
