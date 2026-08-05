# dbt-doris 异步物化视图：当前实现与使用指南

dbt Model 配置 `materialized='materialized_view'` 后，dbt-doris 会把编译后的
Model 查询创建为 Doris Async Materialized View，并管理
CREATE/REPLACE/DROP、刷新策略和失败恢复。对于 `ON MANUAL`，定义未变化的
后续 `dbt run` 会提交 Doris Refresh；`ON SCHEDULE` 和 `ON COMMIT` 的后续
触发由 Doris 管理。

本 Materialization **只管理 Doris 异步物化视图**。Doris Sync Materialized
View（Rollup）具有不同的 DDL 和生命周期，不在本实现范围内。

## Async MV 版本门禁与验证状态

dbt Core 的开发与测试基线是 1.12.x。每次管理 Async MV 前，Adapter 会从
`SHOW FRONTENDS` 优先读取当前连接 FE 和 Master FE 的版本；如果返回结果无法
标出这两个角色，则退回校验第一行。被选中行的版本无法解析或未通过 Gate 时
直接失败。当前代码 Gate 接受 2.x 中不低于 2.1.5 的版本、除 3.0.0 外的
3.x，以及主版本 4 及以上。

Async MV 生命周期已经在以下五个 Doris 官方发行版本上直接运行同一组 21 项
专项 Functional Test；每个版本均全部通过且没有 Skip：

| Doris | FE/BE 完整 Version | MV 专项结果 |
| --- | --- | --- |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 21 passed / 121.07s |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 21 passed / 118.73s |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 21 passed / 105.66s |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 21 passed / 105.94s |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 21 passed / 109.19s |

专项测试直接运行
`test_doris_materialized_view.py`、`test_doris_materialized_view_basic.py` 和
`test_doris_materialized_view_complete.py`，覆盖 Adapter 自有生命周期场景和 dbt
官方 Materialized View 基础 Contract。被测 Adapter 提交为
`f5e30c64ef7eb8320cf359c3d96cf62b595faf00`、测试开始时 `dirty=false`；环境为
dbt Core 1.12.0、dbt-doris 1.0.0、Python 3.11.15、pytest 8.4.2。五个版本均使用
单 FE/BE、`replication_num=1`，FE/BE 完整 Version 一致且 `Alive=true`；测试后
对应数据库残留均为 0。共执行 105 项 MV Functional Test，105 项通过。

逐项测试步骤、21 个 Case 的完整断言、复现命令、原始证据位置和未覆盖边界见
[异步物化视图专项测试说明与执行记录](materialized-view-test-plan.zh-CN.md)。

这组边界是代码中人为设置的运行条件，不是多版本 E2E 得出的最低/排除版本结论。
当前证据证明的是上表五个精确版本，仍没有证明 2.1.5 是准确最低版本或 3.0.0
一定不兼容，因此本文不把 Gate 放行范围整体称为“支持版本范围”。此前
2.1.11、3.0.8、3.1.4 的 View DDL 重放结果，以及 FE 4.1.2/BE `0.0.0` 的
混合集群结果，仅保留为历史证据。2.1.11 曾发现旧 View 查询受调用 Session 当前
`sql_mode` 影响；Pre-model Ordering 修复后，其完整 Functional 套件已通过。

生产定时任务支持 `minute`、`hour`、`day` 和 `week`。Adapter 会拒绝
`second`，因为 Doris 只通过测试专用设置开启秒级 Schedule。

## 完整示例

下面以按日期汇总订单销售额的 `ON MANUAL` 异步物化视图为例。假设项目中已经
存在 `orders` Model：

```sql
-- models/daily_sales.sql
{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual',
    wait_for_refresh=true,
    refresh_wait_timeout=300,
    refresh_poll_interval=1,
    on_configuration_change='apply'
) }}

select
    order_date,
    sum(amount) as sales
from {{ ref('orders') }}
group by order_date
```

这个示例声明了本 Materialization 的创建、刷新、Task 等待和配置变化策略。

正常创建和更新流程中，用户不需要手写 CREATE、Replace、生命周期 Drop、Task
轮询或失败恢复 SQL。Adapter 根据 Config 生成对应语句。`ON MANUAL` 的后续
刷新由定义未变化时每次选中该 Model 的 `dbt run` 提交；用户仍可按需直接执行
Doris 原生 Refresh SQL。

运行 Model：

```bash
dbt run --select daily_sales
```

首次 `dbt run` 创建定义并等待 `BUILD IMMEDIATE` 产生的首次任务成功，不额外
提交 Refresh。以后定义未变化时不重复创建：显式配置的 `ON MANUAL` 每次都会
提交一次
`REFRESH MATERIALIZED VIEW ... AUTO` 并等待；`ON SCHEDULE` 或 `ON COMMIT`
则 Skip，由 Doris 按 DDL 中的策略管理。

## 刷新方式怎么选择

### `refresh_trigger`

| 配置 | 谁触发后续刷新 | 适用场景 |
| --- | --- | --- |
| `manual` | 定义未变化的 `dbt run` 提交 Doris Refresh；也可直接执行原生 SQL | 由 dbt Job 或外部调度精确触发 |
| `schedule` | Doris 内置 Schedule | 固定时间间隔刷新 |
| `commit` | Doris 根据底表提交触发 | 希望底表变化后自动刷新，且满足 Doris `ON COMMIT` 约束 |

刷新动作直接由 `refresh_trigger` 决定，不提供额外的 `refresh_on_run` 开关。
对于已存在且定义未变化的 MV，`manual` 每次选中运行都提交 Refresh；
`schedule` 和 `commit` 都 Skip，后续触发交给 Doris。

默认是 `manual`，同时默认 `build_mode='immediate'`。使用这两个默认值时，第一
次 `dbt run` 只创建并等待首次构建；第二次及以后定义未变化的 `dbt run` 会
提交 Refresh。

#### Manual

Model 配置：

```sql
{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual'
) }}

select ...
```

第一次运行创建 MV。以后定义未变化时，每次执行：

```bash
dbt run --select daily_sales
```

Adapter 会提交：

```sql
REFRESH MATERIALIZED VIEW `analytics`.`daily_sales` AUTO;
```

配置 `refresh_method='complete'` 时提交：

```sql
REFRESH MATERIALIZED VIEW `analytics`.`daily_sales` COMPLETE;
```

具体使用 `AUTO` 还是 `COMPLETE` 取决于 `refresh_method`。默认
`wait_for_refresh=true`，Adapter 会等待这次新 Task；设置为 `false` 时只提交
Refresh SQL，不轮询结果。

也可以从 MySQL Client、Doris SQL Console、Airflow 或其他调度系统直接执行上述
Doris 原生 SQL；这不改变 dbt Model 的生命周期语义。

刷新是 Doris 异步任务，可以在 Doris 中查看：

```sql
select TaskId, Status, ErrorMsg, LastQueryId
from tasks('type'='mv')
where MvDatabaseName = 'analytics'
  and MvName = 'daily_sales'
order by CreateTime desc, TaskId desc;
```

#### Schedule

```sql
{{ config(
    materialized='materialized_view',
    refresh_method='auto',
    refresh_trigger='schedule',
    refresh_schedule={
        'interval': 1,
        'unit': 'day',
        'start_time': '2026-08-01 02:00:00'
    }
) }}

select ...
```

这会把类似下面的策略写入 CREATE DDL：

```sql
REFRESH AUTO
ON SCHEDULE EVERY 1 DAY
STARTS '2026-08-01 02:00:00'
```

`start_time` 可省略。生产 Schedule Unit 支持 `minute`、`hour`、`day` 和
`week`。定义未变化的后续 `dbt run` 不会再提交 Refresh，而是 Skip 并把触发
交给 Doris Schedule。

#### Commit

```sql
{{ config(
    materialized='materialized_view',
    refresh_method='auto',
    refresh_trigger='commit'
) }}

select ...
```

Adapter 只生成 `REFRESH AUTO ON COMMIT`；是否以及何时产生刷新任务由 Doris
判断，Model 和底表必须满足 Doris 的 `ON COMMIT` 使用约束。定义未变化的
后续 `dbt run` Skip，不额外提交 Refresh。

### `refresh_method`

`refresh_method` 与触发方式相互独立：

| 配置 | 含义 |
| --- | --- |
| `auto` | Doris 根据能够获取的底表快照和分区版本判断刷新范围；对于可跟踪的底表尽量只刷新变化分区 |
| `complete` | 不检查分区是否已同步，强制刷新 MV 的全部分区 |

`AUTO` 只表示“由 Doris 选择刷新范围”，不表示“自动触发刷新”。是否自动触发由
`refresh_trigger` 决定。对于 Doris 无法感知数据变化的外表（例如 JDBC），
`AUTO` 可能把 MV 视为已经同步，而不是可靠地退化为全量刷新；这类场景应使用
`COMPLETE`。

## 生命周期与刷新配置

| Config | 默认值 | 支持值或格式 | 作用 |
| --- | --- | --- | --- |
| `materialized` | 无 | `materialized_view` | 使用 Doris Async MV Materialization |
| `build_mode` | `immediate` | `immediate`、`deferred` | 创建后立即构建，或推迟到以后刷新 |
| `refresh_method` | `auto` | `auto`、`complete` | 刷新范围；同时用于 DDL 和 Adapter 提交的 Manual Refresh |
| `refresh_trigger` | `manual` | `manual`、`schedule`、`commit` | 刷新触发方式；Manual 由定义未变的 dbt run 提交 |
| `refresh_schedule` | 无 | `interval`、`unit`、可选 `start_time` | 仅用于 `schedule`；生产 Unit 为 minute/hour/day/week |
| `wait_for_refresh` | `true` | Boolean | 是否等待首次构建或 Adapter 提交的 Manual Refresh Task；不影响是否提交 Refresh |
| `refresh_wait_timeout` | `300` | 正整数秒 | 等待本次 Refresh Task 的总超时 |
| `refresh_poll_interval` | `1` | 正整数秒 | 查询本次 Task 状态的间隔，不能大于总超时 |
| `on_configuration_change` | `apply` | `apply`、`continue`、`fail` | 已部署定义发生变化时的策略 |

`refresh_schedule` 不能用于 `manual` 或 `commit`，`unit='second'` 会在执行
DDL 前被拒绝。

## 首次构建与 Manual Refresh Task

| `build_mode` | 创建时行为 |
| --- | --- |
| `immediate`（默认） | Doris 立即构建新 MV；Adapter 默认等待成功后再暴露目标 |
| `deferred` | 创建时不构建；后续由 Manual、Schedule 或 Commit 产生第一次刷新 |

组合后的行为是：

| Build + Trigger | 首次构建 | 后续刷新 |
| --- | --- | --- |
| `IMMEDIATE + MANUAL` | 第一次 run 创建并等待 BUILD Task，不额外 Refresh | 定义未变的后续 run 提交 Refresh |
| `IMMEDIATE + SCHEDULE` | 第一次 run 创建并等待 BUILD Task | 后续 run Skip，Doris 按 Schedule 刷新 |
| `IMMEDIATE + COMMIT` | 第一次 run 创建并等待 BUILD Task | 后续 run Skip，Doris 按底表 Commit 刷新 |
| `DEFERRED + MANUAL` | 第一次 run 只创建，不产生 Task | 第二次定义未变的 run 提交第一次 Refresh |
| `DEFERRED + SCHEDULE` | 第一次 run 只创建 | 后续 run Skip，等待第一个 Schedule 周期 |
| `DEFERRED + COMMIT` | 第一次 run 只创建 | 后续 run Skip，等待第一次符合条件的底表提交 |

Adapter 会等待两类由当前 Model Action 产生的 Task：

- 创建或替换新定义时，`BUILD IMMEDIATE` 自己产生的首次 Task；
- 定义未变化且 Trigger 为 `ON MANUAL` 时，Adapter 显式提交 Refresh 产生的新
  Task。

创建和替换不会在等待 `BUILD IMMEDIATE` 后再提交第二个 Refresh。任务识别流程
为：

1. 在提交 CREATE 或 Manual Refresh 前记录该 MV 已有 Task ID；
2. 动作提交后轮询 `tasks('type'='mv')`，选择排序后第一个不在旧 ID 集合中的
   Task；
3. `SUCCESS` 才完成动作，并在 dbt Adapter Response 中返回 Task ID、Status，
   以及 Doris 提供时的 Last Query ID；
4. `FAILED`、`CANCELED`、未知状态或超时都会让 Model 失败，并携带任务错误。

当前等待器没有把 Refresh 语句的 Query ID 与 Task 做强关联。同一 MV 在 dbt
等待期间如果又被其他客户端并发刷新，Adapter 可能识别到另一项新 Task。因此
同一个 MV 应避免并发提交 Manual Refresh。

只有明确配置 `wait_for_refresh=false` 时才只提交、不等待。
`refresh_wait_timeout` 和 `refresh_poll_interval` 同时控制首次构建与 Manual
Refresh 的等待。等待依赖 Doris 保留 MV Task History；若任务历史被关闭或过早
清理，Adapter 会超时并给出提示。等待超时只会让 dbt Model 失败，不会取消已经
提交到 Doris 的异步 Refresh Task。

配置 `BUILD IMMEDIATE` 但设置 `wait_for_refresh=false` 时，Adapter 会在首次
任务完成前暴露新定义。下游 Model 可能看到尚未完成构建的 MV，只应在明确接受
该风险时使用。对于定义未变化的 Manual MV，关闭等待只提交 Refresh SQL，Model
成功不代表 Refresh Task 已完成。

## `dbt run` 如何处理已有对象

| 场景 | 行为 |
| --- | --- |
| 目标不存在 | 创建异步物化视图；Immediate 只等待 BUILD Task，Deferred 只创建 |
| 定义未变化 + `ON MANUAL` | 提交 `REFRESH MATERIALIZED VIEW ... AUTO/COMPLETE`；默认等待新 Task |
| 定义未变化 + `ON SCHEDULE/COMMIT` | Skip，不提交 Refresh，后续触发由 Doris 管理 |
| Model SQL 或生命周期与刷新配置变化 | 按 `on_configuration_change` 处理 |
| `on_configuration_change='apply'` | 构建临时 MV；Immediate 默认等首次构建成功后原子 Replace，关闭等待时提前暴露，Deferred 不产生首次任务 |
| `on_configuration_change='continue'` | 保留 Doris 中的旧定义并给出警告，不提交 Manual Refresh |
| `on_configuration_change='fail'` | 终止运行，不修改已有对象 |
| 使用 `--full-refresh` | 即使定义未变也重新部署完整定义；只处理 BUILD，不额外提交 Manual Refresh |
| Table、View 与 MV 互相切换 | 交给目标 Materialization 按真实 Relation Type 处理；各方向的安全边界见下文 |

### MV → MV 原子替换与失败恢复

dbt-doris 在 MV Comment 中保存部署状态和定义 Hash。Hash 根据编译 SQL 和参与
MV 定义的配置生成；SQL 大小写或内部空白变化仍可能改变 Hash，并触发重建。
部署开始时写入 `deployment-pending`，完整流程成功后改为
`definition-hash`。如果进程在中途失败，下次运行会识别未完成部署并恢复。
如果原子 Replace 已完成但流程中断，旧 MV 会保留在临时名称下；下一次运行先把
旧 MV 原子换回线上目标，再重试新定义，避免过早删除最后一个完整版本。

已有 MV 的结构变化不会先删除线上对象。Adapter 先创建临时 MV；Immediate 在
默认 `wait_for_refresh=true` 时等待新定义的首次构建成功，再用 Doris 原子
Swap 暴露新定义；Deferred 按其语义不发起首次构建。
残留的 `__dbt_tmp` 或 `__dbt_backup` 对象会在后续运行中按部署状态恢复或清理。

恢复边界：

- 首次创建失败时没有旧版本可恢复，但失败的临时对象不会被当成成功目标。
- MV → MV 的临时 CREATE 或首次任务失败时，现有线上 MV 不变。
- 原子 Swap 后流程中断时，下次运行先恢复旧 MV，再重试新定义。

`--full-refresh` 是重新部署 MV 定义，不等于只执行
`REFRESH MATERIALIZED VIEW ... COMPLETE`。它不会把 `refresh_method` 改成
`complete`，也不会覆盖 `build_mode`；配置为 `BUILD DEFERRED` 时仍不会发起或
等待首次构建。

## Relation 类型切换

同一个 Model 可以直接修改 `materialized`：

```text
table ↔ materialized_view
view  ↔ materialized_view
```

用户不需要先手动 Drop，Adapter 会识别现有 Relation Type 并使用对应 DDL。但
不同方向的失败保证并不完全相同：

- Table → MV：先构建临时 MV。只有 `BUILD IMMEDIATE + wait_for_refresh=true`
  时，首次构建失败才不会切换旧对象，构建成功后才备份旧对象并暴露 MV；
  `BUILD DEFERRED` 没有首次 Task，关闭等待也不具备这项首次任务失败保护。
- View → MV：先执行下一项所述的 Pre-model Snapshot，再运行新模型 Hook/Header
  并构建临时 MV；旧 View 在临时 MV 构建完成前一直在线。
- Adapter 绝不重放普通 View DDL，也不假设 View 保留创建时 SQL Mode/Session
  语义。Doris 2.1.11 实测表明，查询旧 View 会受调用 Session 当前 `sql_mode`
  影响。只有 Active Canonical View → MV 的正向类型替换执行专用物理 CTAS；该
  Snapshot 必须先于新模型任何 Pre-hook、`sql_header` 或 DDL，并固定
  `DISTRIBUTED BY RANDOM BUCKETS AUTO` 和
  `enable_duplicate_without_keys_by_default=true`，仅允许从当前模型配置额外携带
  `replication_num` 或 `replication_allocation`，绝不从旧 View 推断副本属性；
  Snapshot 不继承目标 MV 的 Key、Distribution、Partition、Contract 或
  `sql_header`。Snapshot 只保存 Pre-model Session 当时从旧 View 可查询的数据，
  不保存 View Definition、创建时 Session 状态、Comment、Grant 或完全一致的
  Schema 属性。
- CTAS 失败时旧 View 保持在线，且新模型 Hook/Header/DDL 均未运行。CTAS 成功后
  也不立即 Drop View；旧 View 继续占用 Canonical 名，直到临时 MV 完成构建，
  然后才 Drop View 并暴露 Replacement。物理 Snapshot Marker 保留到完整生命周期
  成功后再清理。SQL Mode 用例必须按 Pre-model Session 的实际查询结果断言，不能
  从 View 创建模式推导结果。
- 若 Replacement Build 失败但 Canonical 旧 View 仍在线，Retry 先清理或替换陈旧
  Snapshot Marker，再重新冻结旧 View；若失败发生在 Drop/Rename 窗口导致
  Canonical 缺失，则保留物理 Backup 作为唯一旧数据副本。目标仍是 Table/MV
  时，下一轮先把 Backup 恢复到 Canonical，再重试类型切换。若目标改成
  Incremental/Partition，则由目标 Materialization 的恢复规则接管，详见
  [Incremental 用户指南](incremental.zh-CN.md)。
- Snapshot Helper 遇到源/目标同名时会在执行任何 SQL 前失败；目标已存在时可能
  先做只读 Relation 元数据查询，但不会执行修改 SQL 或 Drop。Generic View
  Rename/Exchange 明确拒绝。
- MV → Table：先构建 Intermediate Table，再把旧 MV 以相同 Relation Type Rename
  到 `__dbt_backup`，随后把 Intermediate Table Rename 为 Canonical；只有完整流程
  成功后才删除 MV Backup。第二次 Rename 或后续流程失败时，同类型 MV Backup
  保留，下一次 Table 运行可先恢复它再重试类型切换。该过程不是 Doris 原子 MV
  Swap。
- MV → View：当前 View Materialization 会先删除 MV，再创建 View；如果 CREATE
  VIEW 失败，目标名可能暂时不存在。
- MV → MV 定义变化：使用 Doris `REPLACE WITH MATERIALIZED VIEW` 原子 Swap，
  这是当前失败恢复保护最完整的路径。

如果目标位置已经存在一个不是由本 Adapter 部署的 Async MV，因为没有
`dbt-doris` Definition Marker，第一次运行会把它视为定义变化。默认
`on_configuration_change='apply'` 会重建；设置 `continue` 会保留并警告，
设置 `fail` 会拒绝接管。

## 当前范围边界

当前实现明确不提供：

- Doris Sync Materialized View（Rollup）管理；
- 指定 Doris 分区的刷新配置；
- 内置的 `dbt run-operation` 手动刷新命令；
- 通过 dbt 暂停、恢复或取消 Doris MV Refresh Task。

定义未变化的 `ON MANUAL` Model 会由普通 `dbt run` 提交整项
`AUTO/COMPLETE` Refresh。用户或外部调度系统仍可直接执行 Doris 原生 SQL，
但 Adapter 不生成指定分区 Refresh。如果需要由 Doris 自动触发，应配置
`ON SCHEDULE` 或 `ON COMMIT`。

## 当前验证状态

当前分支已经通过：

- Async MV 配置、DDL、刷新分流、Task 等待和失败恢复 Unit Test；
- 创建、Manual Refresh、Schedule/Commit Skip 和配置变化 Doris Functional
  E2E Test；
- dbt 官方 Materialized View 基础生命周期 Contract Test；
- `Table ↔ Materialized View` 和 `View ↔ Materialized View` 类型切换测试。

## 排错

- 执行 dbt 的 Doris 身份需要读取 `SHOW FRONTENDS`，并具备查询、创建、刷新、
  修改和删除目标 Async MV 所需的权限。
- 首次构建或 Manual Refresh 超时时先检查 `tasks('type'='mv')`、Task History
  保留设置和 Doris 返回的 ErrorMsg/LastQueryId。
- `refresh_trigger='commit'` 仅在底表变更满足 Doris ON COMMIT 语义时触发，
  Adapter 不模拟 Commit 调度。
- 不要手动删除或修改 MV Comment 中的 `dbt-doris:` 部署标记；Marker 缺失或
  被修改会被识别为定义变化，并可能在默认 `apply` 策略下触发重建。

## 结论

当前实现把 `dbt run` 定义为 **MV 定义和配置的部署动作，以及 ON MANUAL 的
刷新入口**。

这里的 `ON MANUAL` 不是“dbt 只把策略写进 DDL，之后完全不管刷新”：首次
Create/Replace 完成后，只要已部署定义没有变化，之后每次选中该 Model 的
`dbt run` 都会由 Adapter 提交一次 Doris
`REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`。

是否提交 Refresh 只由 `refresh_trigger` 决定，没有 `refresh_on_run`：
`wait_for_refresh=false` 只关闭 Task 轮询，不会跳过 Manual Refresh SQL。

| 内容 | 由谁负责 |
| --- | --- |
| Model SQL、`ref()` 和 `source()` | 用户声明，dbt 编译 |
| CREATE、Replace、类型切换及部署流程所需的 Drop/失败恢复 | dbt-doris Adapter |
| `BUILD IMMEDIATE` 创建/替换的首次构建任务 | Doris 执行，Adapter 默认等待；不会额外提交 Refresh |
| `AUTO/COMPLETE` | 刷新范围：Doris 自动选择范围或执行完整刷新 |
| `MANUAL/SCHEDULE/COMMIT` | 刷新触发方式 |
| 定义未变的 `ON MANUAL` | Adapter 提交 Refresh，默认等待新 Task |
| 定义未变的 `ON SCHEDULE/COMMIT` | Adapter Skip，Doris 按 DDL 触发 |

因此：

- 首次运行会创建 MV；默认 `BUILD IMMEDIATE`，Adapter 等待首次构建成功。
- 首次创建或替换只等待 `BUILD IMMEDIATE` 自己产生的 Task，不会紧接着再提交
  一次 `REFRESH MATERIALIZED VIEW`。
- 定义未变化时，`ON MANUAL` 的后续 `dbt run` 提交
  `REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`；默认等待本次新 Task。
- 定义未变化时，`ON SCHEDULE` 和 `ON COMMIT` Skip，把后续触发交给 Doris。
- `BUILD DEFERRED + ON MANUAL` 第一次运行只创建；第二次定义未变的运行提交
  第一次 Refresh。
- SQL 或配置变化时按 `on_configuration_change` 处理；目标已经是 MV、使用默认
  `apply` 且保持默认等待时，Adapter 构建临时 MV，首次构建成功后原子替换。
- 当前不提供指定分区刷新或内置刷新 `run-operation`。
