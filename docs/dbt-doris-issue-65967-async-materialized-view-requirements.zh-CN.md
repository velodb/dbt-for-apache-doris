
# 任务 4：Apache Doris #65967 dbt-doris 支持异步物化视图需求说明

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 任务编号 | 4 |
| 对应任务 | [Apache Doris #65967](https://github.com/apache/doris/issues/65967) |
| Issue 标题 | `[Feature] (dbt-doris) Support async materialized views as a dbt materialization` |
| Issue 状态 | Open |
| Issue 类型 | `kind/feature` |
| 创建时间 | 2026-07-23 |
| 文档日期 | 2026-07-27 |
| 文档目的 | 把 Issue 中的一句话需求展开成可讨论、可开发、可验收的需求 |
| 实现状态 | Async Materialized View 已按本文验收项完成；Sync MV 不在范围内 |

## 0. 一句话结论

这个任务要让用户在 dbt Model 中只写 Config 和查询内容：

```sql
-- models/mv_daily_sales.sql

{{ config(
    materialized='materialized_view',
    refresh_method='auto',
    refresh_trigger='schedule'
) }}

select
    order_date,
    sum(pay_amount) as sales_amount
from {{ ref('fact_orders') }}
group by order_date
```

其中，Config 告诉 dbt“把结果构建成异步物化视图”，下面的 `SELECT` 定义
“物化视图计算什么”。dbt-doris 负责把编译后的 `SELECT` 包装成类似下面的
Doris DDL：

```sql
CREATE MATERIALIZED VIEW `mv_daily_sales`
REFRESH AUTO
ON SCHEDULE ...
AS
select
    order_date,
    sum(pay_amount) as sales_amount
from `fact_orders`
group by order_date;
```

Hook 是通过 `pre_hook` 或 `post_hook` 配置，在 Model 构建前或构建后额外执行的
SQL。没有专用 Materialization 时，用户可能被迫使用下面这种临时绕法：

```sql
-- 仅用于说明 Hook 绕法，不是推荐写法

{{ config(
    pre_hook="DROP MATERIALIZED VIEW IF EXISTS mv_daily_sales",
    post_hook="CREATE MATERIALIZED VIEW mv_daily_sales REFRESH AUTO ON MANUAL AS SELECT ..."
) }}

select ...
```

这种方式要求用户自己拼接完整 DDL，并处理对象识别、重复运行、配置变化、重建和
删除。#65967 的目标就是让这些工作由 dbt-doris 完成，用户只维护正常的 Config
和 `SELECT`。

这里支持的是 **Doris Async Materialized View**，不是 Doris Sync Materialized
View，也不是把普通 dbt Table 改一个名字。

## 1. Issue 原文确认了什么

Issue 明确提出了以下需求：

1. 当前 dbt-doris 没有办法从 dbt 创建 Doris 异步物化视图；
2. 新增 dbt 的 `materialized_view` Materialization；
3. 该 Materialization 应生成 Doris
   `CREATE MATERIALIZED VIEW ... REFRESH ... AS ...`；
4. 最终用户目标是通过 dbt 管理 Doris 异步物化视图。

Issue 没有进一步定义：

- dbt Config 的具体名称；
- 默认刷新方式；
- 重复执行 `dbt run` 时如何处理已有定义；
- Model SQL 或刷新配置变化时怎样更新对象；
- `--full-refresh` 的行为；
- 如何从 Doris 元数据中识别异步物化视图；
- 支持哪些 Doris 版本；
- 是否等待 `BUILD IMMEDIATE` 产生的首次异步任务；
- 是否包含暂停、恢复、取消和刷新状态监控。

因此，“生成一条 CREATE SQL”是这个 Issue 的起点，不是完整的完成标准。

## 2. 为什么需要这个能力

### 2.1 当前三种常见 Model 物化方式不能替代它

| 物化方式 | 数据由谁维护 | 数据什么时候更新 | 与 Async MV 的差别 |
| --- | --- | --- | --- |
| View | 不保存结果数据 | 查询时读取最新底表 | 查询时仍要执行原始计算 |
| Table | dbt-doris | 每次 `dbt run` 重建 | 刷新调度仍由 dbt 或外部调度器负责 |
| Incremental | dbt-doris | 每次 `dbt run` 增量写入 | 增量边界和写入 SQL 由 dbt Model 负责 |
| Async Materialized View | Doris | 手动、定时或底表提交后触发 | Doris 保存预计算结果并管理刷新任务 |

Doris 异步物化视图适合：

- 对复杂聚合或 Join 结果做预计算；
- 加速报表和重复查询；
- 由 Doris 定时刷新，而不是依赖频繁执行 `dbt run`；
- 对分区表只刷新发生变化的分区；
- 参与 Doris 的透明查询改写。

### 2.2 dbt 在这里负责什么

dbt Core 负责：

- 解析 Model 和 `ref()` 依赖；
- 选择 `materialized_view` Materialization；
- 决定本次运行哪些 Model；
- 提供 `--full-refresh`、Hook 和配置变化等通用生命周期。

dbt-doris 负责：

- 识别 Doris 专用 Config；
- 生成 Doris Async MV SQL；
- 从 Doris 元数据中识别已有对象；
- 把 dbt 的创建、替换和删除动作以及刷新策略映射到 Doris DDL；
- 定义未变化且触发方式为 `ON MANUAL` 时，每次选中该 Model 的运行都提交
  Doris Refresh，并默认等待本次 Task。

Doris 负责：

- 创建并保存异步物化视图；
- 执行创建、Manual、Schedule 和 Commit 产生的刷新任务；
- 选择刷新分区并维护分区刷新状态；
- 执行查询和透明查询改写。

## 3. 期望的用户使用方式

下面是为了把 Issue 变成可实现需求而给出的**建议接口**。Issue 原文没有确定这些
Config 名称，合入前仍需由维护者确认。

### 3.1 使用 `ON MANUAL` 刷新策略的异步物化视图

```sql
-- models/mv_daily_sales.sql

{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual',
    partition_by='order_date',
    distributed_by=['customer_id'],
    buckets=8,
    properties={
        'replication_num': '3',
        'workload_group': 'dbt_mv'
    }
) }}

select
    order_date,
    customer_id,
    sum(amount) as sales_amount
from {{ ref('fct_orders') }}
group by order_date, customer_id
```

期望生成的 Doris SQL 类似：

```sql
CREATE MATERIALIZED VIEW `analytics`.`mv_daily_sales`
BUILD IMMEDIATE
REFRESH AUTO ON MANUAL
PARTITION BY (`order_date`)
DISTRIBUTED BY HASH (`customer_id`) BUCKETS 8
PROPERTIES (
    "replication_num" = "3",
    "workload_group" = "dbt_mv"
)
AS
select
    order_date,
    customer_id,
    sum(amount) as sales_amount
from `analytics`.`fct_orders`
group by order_date, customer_id;
```

第一次 `dbt run` 部署以上定义；`BUILD IMMEDIATE` 自己产生首次构建 Task，
Adapter 默认等待它，但不额外提交 Refresh。第二次及以后定义未变化的
`dbt run` 会提交：

```sql
REFRESH MATERIALIZED VIEW `analytics`.`mv_daily_sales` AUTO;
```

并默认等待提交后发现的新 Task。`AUTO` 来自 `refresh_method`，表示刷新范围；
`MANUAL` 表示由 dbt run 或其他外部操作触发。Adapter 不提供指定分区 Refresh，
也不区分同一 MV 上并发外部 Refresh 产生的新 Task。

### 3.2 定时刷新型异步物化视图

概念上还需要表达：

```sql
BUILD DEFERRED
REFRESH AUTO
ON SCHEDULE EVERY 1 DAY
STARTS '2026-08-01 02:00:00'
```

对应 Config 至少需要包含：

- 刷新间隔数值；
- 刷新时间单位；
- 可选的首次执行时间。

具体使用平铺 Config，还是一个结构化的 `refresh_schedule` Config，需要在实现前
确定。

## 4. Config 与 Doris SQL 的需求映射

| 功能 | 建议 Config | 可选值或格式 | Doris SQL |
| --- | --- | --- | --- |
| 对象类型 | `materialized` | `materialized_view` | `CREATE MATERIALIZED VIEW` |
| 首次构建 | `build_mode` | `immediate`、`deferred` | `BUILD IMMEDIATE/DEFERRED` |
| 刷新方法 | `refresh_method` | `complete`、`auto` | `REFRESH COMPLETE/AUTO` |
| 刷新触发 | `refresh_trigger` | `manual`、`schedule`、`commit` | `ON MANUAL/SCHEDULE/COMMIT` |
| 调度周期 | 待定 | 数值和 minute/hour/day/week 等单位 | `EVERY <n> <unit>` |
| 调度起点 | 待定 | Doris 可解析的时间 | `STARTS '<time>'` |
| Key | `duplicate_key` 或专用 MV Key Config | 列名或列名列表 | `DUPLICATE KEY (...)` |
| 分区 | `partition_by` | 列或 `date_trunc` 表达式 | `PARTITION BY (...)` |
| 分布 | `distributed_by` | Hash 列列表 | `DISTRIBUTED BY HASH (...)` |
| 随机分布 | 建议新增分布类型 Config | `random` | `DISTRIBUTED BY RANDOM` |
| Bucket | `buckets` | 正整数或 `auto` | `BUCKETS <n>/AUTO` |
| 说明 | Model Description | 字符串 | `COMMENT '<text>'` |
| 属性 | `properties` | 字典 | `PROPERTIES (...)` |

Config 设计必须满足：

1. 对枚举值做大小写归一和合法性校验；
2. 只在对应触发方式下接受调度参数；
3. 错误组合在编译或执行前给出清晰错误；
4. 不直接把未经校验的任意片段拼进 SQL；
5. 文档明确各配置要求的 Doris 最低版本。

例如，`ON COMMIT` 若存在 Doris 版本边界，必须结合对应 Release 的官方文档
和真实集群测试确认，不能从 Adapter Gate 或模拟版本字符串单测推导最低版本。

## 5. 功能需求

### R1. 注册 `materialized_view` Materialization

用户只需配置：

```sql
{{ config(materialized='materialized_view') }}
```

不需要复制自定义 Macro，也不需要通过 Pre-hook 或 Post-hook 手写 DDL。

本任务针对当前 dbt-doris 的 dbt Core v1 Python Adapter。Issue 中引用了带
Fusion/v2 参数的 dbt 文档链接，但这不表示任务要求把 dbt-doris 迁移到 dbt v2。

### R2. 正确编译 Model SQL 和依赖

Materialized View 的 `AS <query>` 必须使用 dbt 编译后的 SQL，因此：

- `ref()` 能正确解析上游 Model；
- `source()` 能正确解析 Source；
- Model 在 dbt DAG 中保留正常的上下游关系；
- Alias、Schema 和 Target 环境规则继续生效。

### R3. 生成合法的 Doris Async MV DDL

创建语句必须：

- 明确生成异步物化视图语法；
- 正确引用 Database 和对象名；
- 按 Doris 语法顺序输出刷新策略、Key、分区、分布、属性和查询；
- 正确引用标识符并转义注释、属性值；
- 不把 Doris Sync MV 误当成此 Materialization 的实现。

### R4. 正确识别已有异步物化视图

dbt-doris 必须把已有 Doris Async MV 识别成
`RelationType.MaterializedView`，而不是普通 Table。

这是完整生命周期的前提。只有正确识别后，dbt 才知道应该执行：

```sql
DROP MATERIALIZED VIEW ...
SHOW CREATE MATERIALIZED VIEW ...
ALTER MATERIALIZED VIEW ...
REPLACE WITH MATERIALIZED VIEW ...
```

而不是错误地执行 `DROP TABLE` 或 Table 替换逻辑。

### R5. 定义重复执行行为

至少要保证：

- 第一次 `dbt run` 创建对象；
- 第二次运行不会因对象已存在而失败；
- 不会把已有 Async MV 当作 Table 删除；
- 不会在每次运行中无条件 Drop/Create；
- 定义未变化且为 `ON MANUAL` 时，每次选中运行都提交一次
  `REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`；
- 定义未变化且为 `ON SCHEDULE/COMMIT` 时 Skip，把触发交给 Doris。

dbt 官方把 Materialized View 的 `dbt run` 主要视为定义和配置的部署动作，
而数据刷新通常由数据库管理。dbt-doris 针对 Doris Trigger 做明确分流：
`ON MANUAL` 把定义未变的 `dbt run` 作为刷新入口；`ON SCHEDULE/COMMIT`
定义未变时 Skip，后续触发由 Doris 管理。该分流只由 `refresh_trigger`
决定，不提供 `refresh_on_run`。Adapter 不提供指定分区刷新。

CREATE/Replace 新定义时，`BUILD IMMEDIATE` 自己产生首次构建 Task，Adapter
默认等待它成功后再把新定义视为可用，不会紧接着额外提交 Refresh。
`BUILD DEFERRED + ON MANUAL` 的第一次运行只创建；第二次定义未变化的运行提交
第一次 Refresh。`wait_for_refresh=false` 时，Adapter 对首次构建不轮询，对
Manual Refresh 只提交 SQL。

### R6. 处理 SQL 和配置变化

需要支持 dbt 的 `on_configuration_change` 语义：

| 配置 | 期望行为 |
| --- | --- |
| `apply` | 能安全 ALTER 的配置直接修改；不能 ALTER 的变化安全重建 |
| `continue` | 保留已有对象，给出警告并继续；不提交 Manual Refresh |
| `fail` | 检测到变化时明确失败，不修改已有对象 |

建议的变化处理原则：

- Refresh Method、Refresh Trigger 和部分 MV Properties 可评估使用
  `ALTER MATERIALIZED VIEW`；
- Model SQL、Key、Partition 和 Distribution 等结构变化通常需要重建；
- 重建应优先考虑创建临时 MV 后使用 Doris
  `REPLACE WITH MATERIALIZED VIEW`，减少直接删除旧对象的风险；
- 如果 Doris 版本不支持安全替换，行为必须被明确记录和测试。

### R7. 支持 `--full-refresh`

执行：

```bash
dbt run --full-refresh --select mv_daily_sales
```

应重新部署该异步物化视图的完整定义。

需要保证：

- 旧对象与新对象的类型判断正确；
- 构建失败时尽可能保留旧对象；
- 成功后不遗留临时或备份对象；
- Full Refresh 的含义是重建 dbt 对象定义，不要与 Doris
  `REFRESH ... COMPLETE` 混为同一个概念。

### R8. 支持正确的删除和类型切换

以下切换都必须有确定行为：

```text
不存在 -> Materialized View
Table -> Materialized View
View -> Materialized View
Materialized View -> Table
Materialized View -> View
Materialized View -> Materialized View
```

删除 Async MV 必须使用：

```sql
DROP MATERIALIZED VIEW IF EXISTS <name>;
```

不能依赖当前通用 `drop {{ relation.type }}` 在错误 Relation Type 下猜测对象类型。

View → Materialized View 不得重放 View DDL，也不得假设 View 保留创建时
SQL Mode/Session 语义。Doris 2.1.11 实测表明，查询旧 View 会受调用 Session
当前 `sql_mode` 影响。因此 Active Canonical View 的正向 Snapshot 必须发生在
新模型任何 Pre-hook、`sql_header` 或 DDL 之前，并通过专用物理 CTAS 建立当前
操作的新 Backup：固定
`DISTRIBUTED BY RANDOM BUCKETS AUTO` 和
`enable_duplicate_without_keys_by_default=true`，仅允许从当前模型配置额外携带
`replication_num` 或 `replication_allocation`，绝不从旧 View 推断副本属性。
当前操作新建的 Active View Backup 是物理 Table，只保存 Pre-model Session 当时
从旧 View 可查询的数据，不保存 View Definition、创建时 Session 状态、Comment、
Grant 或完全一致的 Schema 属性；这不表示历史遗留的 Backup 只能是 Table。

CTAS 失败时旧 View 保持在线，且新模型 Hook/Header/DDL 均未运行。CTAS 成功后也
不立即删除源 View；旧 View 继续服务 Canonical 名，直到 Replacement 构建完成，
然后才 Drop View 并 Rename Replacement。Snapshot Marker 保留到完整生命周期
成功后清理。Snapshot Helper 在源/目标同名时必须在执行任何 SQL 前失败；目标已
存在时只允许只读 Relation 元数据查询，必须在修改 SQL 或 Drop 前失败。Generic
View Rename/Exchange 明确拒绝。SQL Mode 用例必须按 Pre-model
Session 的实际查询结果断言，不能从 View 创建模式推导结果。

若 Replacement Build 失败而 Canonical 旧 View 仍在线，Retry 可清理或替换上次
物理 Marker 并重新 Snapshot；若失败发生在 Drop/Rename 窗口导致 Canonical
缺失，则必须保留物理 Backup 作为唯一旧数据副本。目标 Materialization 为
Table/MV 时，下一轮先把 Backup 恢复到 Canonical 再重试；只有
Incremental/Partition 使用下述 Durable No-restore 规则。

Incremental/Partition 在 Canonical 缺失且 `__dbt_backup` 存在时，不恢复或转换
Backup。该 Durable Marker 可以是 Legacy View、Table 或 Async MV；它保持原名，
Retry 不执行、Snapshot、Rename 或提前 Drop，而是直接从 Model SQL 完整构建
Canonical。连续失败保持 Canonical 缺失和 `is_incremental()=false`；旧数据只在
Backup 名下可查。只有完整生命周期成功后才删除 Marker，因此 Legacy View
Backup 不走 CTAS。

### R9. 保持 dbt 通用生命周期

Materialization 至少应正确处理：

- Pre-hook；
- Post-hook；
- Adapter Commit；
- 返回正确的 Relation；
- 失败清理；
- dbt 日志中的主 Statement。

当前实现已把 `persist_docs.relation/columns` 和 Doris 专用 `grants` 纳入该
生命周期；创建、替换和幂等运行路径都必须保持相同行为。

### R10. 提供文档、示例和兼容性说明

用户文档至少需要说明：

- 最小可运行示例；
- Manual、Schedule、Commit 三种刷新方式；
- AUTO 与 COMPLETE 的区别；
- `ON MANUAL` 的定义未变运行会触发 Refresh；`ON SCHEDULE/COMMIT` 不会；
- `--full-refresh` 与 `REFRESH ... COMPLETE` 的区别；
- Config 列表和默认值；
- Doris 与 dbt Core 的版本要求；
- 权限要求；
- 已知限制和排错方式。

## 6. 当前实现

### 6.1 Materialization 与 Relation 生命周期

Adapter 已注册 Doris 专用 `materialized_view` Materialization，并提供 Async MV
Create、Replace、Drop、Rename、Relation Type 识别和配置变化处理。Relation
Cache 会结合 `mv_infos` 把 Async MV 补全为 Materialized View，而不是依赖
`information_schema.tables` 中容易误判的 `BASE TABLE`。

Table、View 和 Async MV 之间切换时按真实对象类型使用对应 DDL。结构变化和
`--full-refresh` 先构建临时 MV；Immediate 首次构建成功后再使用 Doris
`REPLACE WITH MATERIALIZED VIEW` 原子替换，Deferred 按其语义不发起首次构建。
部署 Comment 的 Pending/Complete 标记与备份 Relation 用于识别和恢复中断部署。
Active Canonical View 正向切换时绝不重放 View DDL，也不依赖创建时 SQL Mode。
Adapter 先于新模型 Pre-hook、`sql_header` 和 DDL 执行源 View，把 Pre-model
Session 当时可查询的数据写入专用物理 CTAS Snapshot。Snapshot 固定 RANDOM/AUTO 与
Duplicate-without-keys，仅允许从当前
模型配置额外携带 `replication_num` 或 `replication_allocation`，绝不从旧 View
推断副本属性。当前操作新建的 Active View Backup 为 Table，只保存结果数据；
历史 Backup 仍可为 Legacy View、Table 或 Async MV。CTAS 失败时旧 View
在线且不运行新模型上下文；CTAS 成功后旧 View 也保持在线，直到 Replacement
构建完成才执行 Drop + Rename。Snapshot Helper 在源/目标同名时零 SQL；目标已
存在时只允许只读 Relation 元数据查询，零修改 SQL、零 Drop。Generic View
Rename/Exchange 明确拒绝。

Incremental/Partition 的 Durable Marker Retry 不 Restore、执行、Snapshot、
Rename 或提前删除 Backup，而是保持 Canonical 缺失，直接完整构建新 Canonical；
只有全流程成功后才清理 Marker。三轮 Functional 用例覆盖连续失败仍保持
`is_incremental()=false`，以及最终成功后清理 Backup。

若失败时 Canonical 旧 View 仍在线，下一次尝试可清理上次的物理 Marker 并重新
Snapshot；若失败发生在 Drop/Rename 窗口导致 Canonical 缺失，则必须保留物理
Backup 作为唯一旧数据副本。Table/MV 下一轮先恢复 Canonical 再重试；只有
Incremental/Partition 保持 Backup 原名并直接完整构建 Canonical。

### 6.2 Config、刷新语义与 Task 可观测性

`DorisConfig` 已声明 Build、Refresh Method、Refresh Trigger、Schedule、分区
定义、Distribution、Bucket、Properties、Task 等待超时和轮询间隔等类型。
Jinja Macro 会归一化枚举值，校验不合法组合，并引用标识符和转义字符串。

默认 `BUILD IMMEDIATE` 会等待 CREATE 新定义产生的 Doris 首次构建任务成功。
CREATE/Replace 不额外提交 Refresh。定义未变化时，`ON MANUAL` 提交
`REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`，`ON SCHEDULE/COMMIT` Skip。
是否提交只由 `refresh_trigger` 决定，不提供 `refresh_on_run`。
Adapter 从 `tasks('type'='mv')` 的 Task ID 差集选择并等待首次构建或 Manual
Refresh 后出现的新 Task；失败、取消、未知状态或超时会使 Model 失败，成功
Adapter Response 包含 Task ID、Status 和可用的 Last Query ID。关闭等待时只
停止轮询，不会跳过 Manual Refresh SQL。当前没有 Query ID 强关联；同一 MV
的并发外部刷新可能被误认，等待超时也不会取消 Doris 中已经提交的异步 Task。

### 6.3 Docs、Grants 与 Hook

`persist_docs.relation` 控制 Relation Description 是否进入 MV Comment，
`persist_docs.columns` 通过 CREATE MV 完整列定义写入 Column Comment。只有启用
的 Description 会进入定义 Hash。

Doris Grants 使用用户名或 `username@host`，裸用户名表示 `username@%`。项目级
`+grants` 可用于 MV；所有权限名和用户会在 MV DDL 前校验，避免无效授权暴露
新定义。当前不支持 Role，因为 `information_schema.table_privileges` 无法安全
区分用户直接授权和从 Role 继承的授权。

Outside Pre-hook 通常在 `SHOW CREATE` 和配置漂移检查前执行；Active Canonical
View 类型替换是安全例外，物理 Snapshot 必须早于 Outside/Inside Pre-hook、
`sql_header` 和任何新模型 DDL。Inside Hook 参与实际部署动作。Inside Post-hook
成功后才把部署标记从 Pending 改为 Complete；
Replace 后 Post-hook 失败时保留旧 MV，并在下次运行先原子回滚再重试。

### 6.4 版本 Gate 与验证状态

| Doris | FE/BE 完整 Version | 聚焦 Async MV | 状态 |
| --- | --- | --- | --- |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 21 passed / 121.07s | passed |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 21 passed / 118.73s | passed |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 21 passed / 105.66s | passed |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 21 passed / 105.94s | passed |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 21 passed / 109.19s | passed |

这里的 `passed` 只覆盖上表聚焦 Async MV 套件、精确版本身份和清理证据；
Incremental 的版本矩阵与用例证据独立记录在 Incremental 指南和测试方案中。

聚焦 Async MV 套件直接运行三个 MV Functional 文件，共 21 项，覆盖 Adapter
生命周期用例和 dbt 官方 Materialized View 基础 Contract。它使用 Adapter SHA
`f5e30c64ef7eb8320cf359c3d96cf62b595faf00`、测试开始时 `dirty=false`、dbt Core
1.12.0、Python 3.11.15、pytest 8.4.2；五个版本的 FE/BE 完整 Version 均完全
一致且 `Alive=true`，均无 Skip，测试数据库残留为 0。

Package 干净输出 `/tmp/dbt-doris-package-clean.tUhMxp` 中，75,660-byte wheel
SHA-256 为 `edcbc1bae94e440c7be25f71ec96b6c91e4a5e71af29604561f4d99264584725`，
119,127-byte sdist 为
`ffe4c9c41e8a7f6a24fb43935ec30535748095b2a807b634fe2266ede0b43ef9`；
Twine 7.0.0 双 PASSED。Python 3.12.13 全新 venv
`/tmp/dbt-doris-wheel-clean-py312.lPTWhm` 完成 wheel 安装、`site-packages`
导入、三个 Macro、策略列表和 `pip check` 验证，均为 passed。

Adapter 通过 `SHOW FRONTENDS` 优先校验当前连接 FE 和 Master FE；无法识别
角色时退回首行，被选中行无法解析或未通过 Gate 时失败。当前代码 Gate 接受
2.x 中不低于 2.1.5 的版本、除 3.0.0 外的 3.x，以及主版本 4 及以上。

这些边界是人为设置的运行条件，不是多版本 E2E 得出的最低/排除版本结论。当前
正式证据证明上表五个精确版本，仍没有证明 2.1.5 是准确最低版本或 3.0.0 一定
不兼容。旧 2.1.11、3.0.8、3.1.4 的 View DDL 重放结果与历史 4.1.2 FE / `0.0.0`
dev BE 混合集群结果仅作历史记录。2.1.11 曾暴露旧 View 查询依赖调用 Session
当前 `sql_mode`；Pre-model Ordering 修复后，该版本的完整与聚焦套件均通过。
生产 Schedule Unit 为 minute/hour/day/week；测试专用的 second 会被 Adapter
拒绝。

## 7. 交付范围结论

### 7.1 已交付

- Model SQL、`ref()`、`source()`、Alias 和环境 Schema；
- BUILD IMMEDIATE/DEFERRED；
- REFRESH AUTO/COMPLETE 与 ON MANUAL/SCHEDULE/COMMIT；
- BUILD IMMEDIATE 首次 Task 与 ON MANUAL 后续 Refresh Task 的等待和结果；
- 定义未变时 Manual Refresh、Schedule/Commit Skip，以及
  `BUILD DEFERRED + MANUAL` 第二次运行刷新；
- Key、Partition、Distribution、Buckets、Properties 和 `replication_num`；
- 定义 Hash、幂等运行、`on_configuration_change`、Full Refresh 和原子替换；
- Relation/Column Persist Docs、Doris User Grants 和 Hook 顺序；
- Unit Test、dbt 官方 Materialized View Contract Test、Doris Functional Test
  与用户文档。

### 7.2 不属于本 Issue 的内容

- Doris Sync Materialized View；
- dbt v2/Fusion Adapter 迁移；
- Doris Async MV 内核实现；
- 透明改写规则本身的增强；
- 通用任务调度平台；
- 全部 External Catalog 能力；
- Async MV 的可视化运维平台。

## 8. 验收标准

### AC1. 首次创建

给定一个引用上游 Model 的 `materialized_view` Model，执行：

```bash
dbt run --select mv_daily_sales
```

结果应为：

- 命令成功；
- Doris 中存在 Async MV；
- `SHOW CREATE MATERIALIZED VIEW` 包含期望的 Model SQL 和配置；
- `mv_infos` 能查询到该对象；
- dbt 返回的 Relation Type 是 Materialized View。

### AC2. 重复运行

连续执行两次相同的 `dbt run`：

- 第二次不因对象存在而失败；
- 不错误执行 `DROP TABLE`；
- 不遗留临时或备份对象；
- `ON MANUAL` 第二次提交 `REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`，
  默认等待本次新 Task；
- `ON SCHEDULE/COMMIT` 第二次 Skip，不提交显式 Refresh。

### AC3. 三种刷新触发

分别验证：

```text
ON MANUAL
ON SCHEDULE EVERY ...
ON COMMIT
```

`SHOW CREATE MATERIALIZED VIEW` 或 `mv_infos.RefreshInfo` 必须与 Config 一致。
定义未变化时，还要验证 Manual 提交并等待新 Task，而 Schedule/Commit Skip。

### AC4. AUTO 和 COMPLETE

分别配置 AUTO、COMPLETE：

- CREATE 和 `SHOW CREATE MATERIALIZED VIEW` 中的刷新策略正确；
- 定义未变化的 Manual Run 分别生成
  `REFRESH MATERIALIZED VIEW ... AUTO/COMPLETE`；
- 对 Doris 无法感知变化的外表不承诺 AUTO 自动退化全量，用户应选择 COMPLETE；
- 非法值在执行前失败并指出具体 Config。

### AC5. 配置变化

修改 Refresh、Property、Partition 或 Distribution：

- `apply`、`continue`、`fail` 行为分别符合文档；
- 需要重建的变化不被错误地当作数据刷新；
- 失败时不静默丢失旧对象。

### AC6. Model SQL 变化

修改 Model 的 SELECT：

- dbt 能检测定义变化；
- 新定义最终反映到 `SHOW CREATE MATERIALIZED VIEW`；
- 必须部署新定义，不能把定义变化当作数据刷新。

### AC7. Full Refresh

执行 `dbt run --full-refresh`：

- 对象定义被完整重建；
- 与 Doris `REFRESH ... COMPLETE` 的行为区分清楚；
- 不遗留临时对象。

### AC8. 类型切换

验证 Table、View 和 Materialized View 之间的切换：

- 使用正确的 Drop DDL；
- 最终对象类型正确；
- dbt Relation Cache 不保留错误类型。
- View → MV 使用物理 CTAS Snapshot，且 Snapshot 先于新模型所有
  Pre-hook/`sql_header`/DDL；CTAS 失败时旧 View 在线且不运行新模型上下文；
- CTAS 成功后旧 View 仍在线，直到 Replacement Build 完成才 Drop + Rename；
- Snapshot 固定 RANDOM/AUTO 与 Duplicate-without-keys，仅允许从当前模型配置携带
  `replication_num` 或 `replication_allocation`，绝不从旧 View 推断副本属性；
  当前操作新建的 Active View Backup 为 Table，不继承其他目标配置，也不声称
  保存 View 定义、Comment、Grant 或完全一致 Schema；
- Incremental/Partition 的 Legacy View/Table/MV Backup 作为 Durable Marker 时
  保持原名，不 Restore 或 Snapshot；Canonical 完整构建成功后才删除；
- Table/MV 在 Canonical 缺失且 Backup 存在时，先恢复 Canonical 再重试；
- Replacement Build 失败且旧 View 在线时清理/替换陈旧 Marker；Drop/Rename 窗口
  失败导致 Canonical 缺失时按目标 Materialization 选择上述恢复路径；
- 源/目标同名时零 SQL；目标已存在时只允许只读 Relation 元数据查询；两者均零
  修改 SQL、零 Drop。Generic View Rename/Exchange 明确拒绝。

### AC9. 依赖和环境

- `ref()`、`source()` 正确编译；
- Dev/Prod Schema 隔离正确；
- Alias 正确；
- `dbt ls` 和 Docs 中仍能看到正常依赖。

### AC10. 测试要求

至少包含：

- Macro SQL 生成 Unit Test；
- Config 合法值和非法组合 Unit Test；
- Relation Type 识别 Unit Test；
- 创建、重复运行、Manual Refresh、Schedule/Commit Skip、Deferred 第二次
  Refresh、等待/只提交、Full Refresh 和类型切换 Functional Test；
- View Snapshot CTAS 失败保留源 View 的 Unit Test 与真实 Doris E2E；
- CTAS 成功后 Rename 失败保留 Table Marker、Retry 完整构建 Canonical 并在成功
  后清理的真实 Doris E2E；
- SQL Mode 用例按 Pre-model Session 对 Active Canonical View 的实际查询结果
  断言，并证明 Snapshot 先于新模型 `sql_header`；不得假设 View 创建模式被保存；
- Incremental/Partition Persistent Marker 三轮 Functional Test：连续失败不发布
  Canonical、不触碰 Backup，成功完整构建后才清理；
- Snapshot Helper 必须验证源/目标同名时零 SQL，以及目标已存在时只读元数据查询、
  零修改 SQL、零 Drop；
- 有 Doris 版本边界的兼容性测试或明确跳过条件。

## 9. 已确定的实现决策

| 问题 | 当前决策 |
| --- | --- |
| 默认 Build Mode | `BUILD IMMEDIATE` |
| 默认 Refresh Method | `AUTO` |
| 默认 Trigger | `ON MANUAL` |
| 无变化的 `dbt run` | Manual 提交 AUTO/COMPLETE Refresh；Schedule/Commit Skip |
| Schedule Config | `refresh_schedule={interval, unit, start_time?}`；拒绝测试专用的 second |
| Async MV 识别 | 结合 `mv_infos` 补全 Relation Type |
| SQL/Config 变化 | 临时 MV 构建成功后原子 Replace |
| Create/Replace 完成语义 | Immediate 只等待 BUILD Task，不额外 Refresh；Deferred 只创建 |
| Manual 完成语义 | 定义未变时每次选中运行都提交 Refresh；默认等待新 Task，关闭等待时仍提交但不轮询 |
| Deferred + Manual | 第一次只创建，第二次定义未变的 run 提交第一次 Refresh |
| Schedule/Commit | 定义未变时 Skip，后续触发由 Doris 管理 |
| Refresh 分流 | 只由 `refresh_trigger` 决定，不提供 `refresh_on_run` |
| 分区选择 | 由 Doris 按 MV 定义和 Refresh Method 管理；Adapter 不指定分区 |
| Docs | 支持 `persist_docs.relation` 与 `persist_docs.columns` |
| Grants | 用户名或 `username@host`；Role 暂不支持 |
| Doris 版本验证 | 2.1.11、3.0.8、3.1.4、4.0.7、4.1.3 的正式版本矩阵均 passed；旧实现和开发混合集群结果仅作历史诊断 |
| 普通 View 类型切换 | Active Canonical View 正向 Snapshot 先于新模型 Hook/Header/DDL，旧 View 在线到 Replacement Build 完成；Incremental/Partition 使用 Durable No-restore Marker，Table/MV 先恢复 Canonical；Generic View Rename/Exchange 拒绝 |
| Doris 版本 Gate | 当前代码接受 2.x >= 2.1.5、3.x 排除 3.0.0、主版本 >= 4；这是运行条件，不是兼容性矩阵 |

## 10. 实现拆分结果

以下交付顺序已经完成：

1. Relation 元数据识别与 Drop；
2. 最小 `materialized_view` 创建；
3. Build、Refresh Method/Trigger、Schedule 和分区定义 Config；
4. 重复运行、Manual Refresh、Task 结果和 `--full-refresh`；
5. 配置差异、原子 Replace、Hook、Docs、Grants 和异常恢复；
6. Unit Test、官方 Contract Test、Doris Functional Test 和用户文档。

## 11. 参考资料

- [Apache Doris Issue #65967](https://github.com/apache/doris/issues/65967)
- [dbt Materializations](https://docs.getdbt.com/docs/build/materializations)
- [Doris CREATE ASYNC MATERIALIZED VIEW](https://doris.apache.org/docs/4.x/sql-manual/sql-statements/table-and-view/async-materialized-view/CREATE-ASYNC-MATERIALIZED-VIEW/)
- [Doris Async Materialized View 管理与查询](https://doris.apache.org/docs/4.x/query-acceleration/materialized-view/async-materialized-view/functions-and-demands/)
- [Doris MV_INFOS](https://doris.apache.org/docs/4.x/sql-manual/sql-functions/table-valued-functions/mv_infos/)
- [Doris REFRESH MATERIALIZED VIEW](https://doris.apache.org/docs/dev/sql-manual/sql-statements/table-and-view/async-materialized-view/REFRESH-MATERIALIZED-VIEW)
- [Doris ALTER ASYNC MATERIALIZED VIEW](https://doris.apache.org/docs/dev/sql-manual/sql-statements/table-and-view/async-materialized-view/ALTER-ASYNC-MATERIALIZED-VIEW)
- [Doris DROP ASYNC MATERIALIZED VIEW](https://doris.apache.org/docs/4.x/sql-manual/sql-statements/table-and-view/async-materialized-view/DROP-ASYNC-MATERIALIZED-VIEW/)
