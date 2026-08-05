# dbt-doris Snapshot

> 状态：已实现并通过真实 Doris 与 `dbt-tests-adapter` Snapshot 用例验证。

## 1. Snapshot 是什么

Snapshot 用 SCD Type 2 方式保存业务记录的历史版本。源表中的一条记录发生变化时，
dbt-doris 会关闭旧版本的有效期并写入一个新版本，而不是覆盖旧数据。

Snapshot 表包含以下默认元字段：

| 字段 | 含义 |
| --- | --- |
| `dbt_scd_id` | 历史版本的唯一标识 |
| `dbt_updated_at` | 该版本对应的更新时间 |
| `dbt_valid_from` | 版本生效时间 |
| `dbt_valid_to` | 版本失效时间；当前版本默认为 `NULL` |
| `dbt_is_deleted` | `hard_deletes='new_record'` 时标记删除版本 |

Snapshot 保存的是历史事实，通常不能只靠当前源表重建。因此 dbt-doris 在更新已有
Snapshot 时先构造一张完整的新历史表，校验通过后再原子替换目标表。

## 2. Check Strategy

源数据没有可靠的更新时间字段时，使用 Check Strategy：

```sql
{% snapshot snap_customers %}

{{
    config(
        target_schema="analytics",
        unique_key="customer_id",
        strategy="check",
        check_cols=["level", "address"],
        hard_deletes="invalidate"
    )
}}

select *
from {{ source("ods", "customers") }}

{% endsnapshot %}
```

`check_cols` 中任一字段变化都会生成新版本。也可以使用 `check_cols='all'` 比较全部
业务字段。

Doris 的 `current_timestamp()` 默认只有秒级精度。为避免同一秒连续执行产生相同
`dbt_scd_id`，dbt-doris 会在 Check Strategy 的版本 Hash 中加入随机 nonce；无变化
记录仍不会产生新版本。

## 3. Timestamp Strategy

源数据有可靠、单调递增的更新时间字段时，使用 Timestamp Strategy：

```sql
{% snapshot snap_customers %}

{{
    config(
        target_schema="analytics",
        unique_key="customer_id",
        strategy="timestamp",
        updated_at="updated_at",
        hard_deletes="invalidate"
    )
}}

select *
from {{ source("ods", "customers") }}

{% endsnapshot %}
```

dbt-doris 保留 Doris `DATETIME(p)` 精度，并在修改历史前拒绝以下输入：

- `updated_at` 为 `NULL`；
- 同一个 `unique_key` 出现多行；
- 当前源记录的 `updated_at` 早于 Snapshot 中该 Key 的当前版本。

这意味着迟到数据不会静默改写已有历史。需要接纳时间倒退时，应先制定业务迁移规则，
而不是关闭保护后直接运行。

## 4. Hard Deletes

支持 dbt 1.12 的三种公开行为：

| 配置 | 源记录删除后的结果 |
| --- | --- |
| `hard_deletes='ignore'` | 保留原版本为当前版本 |
| `hard_deletes='invalidate'` | 关闭原版本，不写删除版本 |
| `hard_deletes='new_record'` | 关闭原版本，并写入 `dbt_is_deleted='True'` 的当前版本 |

旧配置 `invalidate_hard_deletes=True` 仍由 dbt Core 转换为 Invalidate 行为。

## 5. 原子替换与失败恢复

更新已有 Snapshot 的执行流程如下：

```text
清理上次失败留下的 staging/upsert
        |
校验源 Key 和 Timestamp
        |
生成 staging 变化记录
        |
CREATE TABLE upsert LIKE target
        |
写入完整历史并校验
        |
ALTER TABLE target REPLACE WITH TABLE upsert
PROPERTIES('swap' = 'false')
        |
Persist Docs / Grants 生命周期 / 清理 staging
```

替换前不会执行 `DROP target`。创建临时表、写入或最终替换失败时，已有目标表继续提供
完整旧历史；下一次运行会丢弃未知状态的临时对象并从目标表重新构建。

dbt-doris 还可以恢复旧版本实现遗留的一种状态：目标表已被删除，但完整的
`target__snapshot_upsert` 仍然存在。恢复前会先校验该表，校验失败则保持错误状态供人工
处理，不会把不完整数据改名为目标。

同一个 Snapshot 节点不应被两个独立的 dbt 进程并发执行；物理 staging 名称遵循 dbt
的固定临时 Relation 命名，并发执行同一节点不属于支持的运行方式。

## 6. 构建前校验

默认启用 `snapshot_validate`，在原子替换前检查：

- Source `unique_key` 非空且唯一，复合 Key 的每一列都非空；
- Timestamp Strategy 的 `updated_at` 非空且不倒退；
- `dbt_scd_id` 和 `dbt_valid_from` 非空；
- `dbt_scd_id` 不重复；
- 每个业务 Key 最多有一个当前版本；
- `dbt_valid_from <= dbt_valid_to`。

校验会扫描 Source 和待替换历史表。对已经通过其他强约束保证输入质量、且确认可以承担
风险的超大表，可以设置 `snapshot_validate=false` 关闭这些附加校验。关闭后仍使用原子
替换，但不再阻止逻辑错误的完整表被安装。

## 7. Schema Change

Snapshot 历史采用保守的 Schema 演进策略：

- 新增业务字段：允许，向目标表增加可空列；旧历史该字段为 `NULL`；
- 删除历史字段：失败，要求显式迁移；
- 不兼容类型变化：失败，要求显式迁移；
- 安全拓宽：允许，例如较小整数写入较大整数、`DATE` 写入 `DATETIME`、较低精度
  `DATETIME` 写入较高精度目标。

新增列使用 Doris 异步 Schema Change。dbt-doris 会等待对应任务完成后再继续构建，避免
在列尚未可见时写入。

Snapshot 不支持通过 Full Refresh 自动丢弃历史；dbt Core 1.12 的 `dbt snapshot` 命令也
没有 `--full-refresh` 选项。破坏性变更必须由用户显式迁移历史表。

## 8. 其他配置

以下 dbt Snapshot 配置已经验证：

- 单列和复合 `unique_key`；
- `dbt_valid_to_current`；
- `snapshot_meta_column_names`；
- Relation 和 Column `persist_docs`。

Snapshot Materialization 保留 dbt 的 Pre/Post Hooks，并在原子替换成功后重新执行
`persist_docs` 和 `apply_grants`。当前 Grants 通用能力已经实现，Snapshot 可以对已存在的
Doris 用户声明并核对直接 Relation 权限；用户创建、支持的权限映射和 Role 限制见
[`grants.zh-CN.md`](grants.zh-CN.md)。

如果目标同名 Relation 是 View 等非 Table 类型，Snapshot 会直接报错，不会删除或替换
该 Relation。

## 9. 已验证场景

真实 Doris Functional Test 覆盖：

- Check 和 Timestamp Strategy；
- 新增、修改、删除、恢复和无变化重复执行；
- Ignore、Invalidate、New Record 三种 Hard Deletes；
- `DATETIME(6)` 精度、NULL 时间和时间倒退；
- 复合 Key、自定义元字段、当前版本哨兵值；
- 新增、删除和不兼容类型 Schema Change；
- 重复/NULL Source Key；
- Persist Docs 在原子替换后保留；
- 最终交换故障时旧历史保持可查询，重跑自动恢复；
- 旧实现遗留 Upsert 表恢复；
- `dbt-tests-adapter` 官方 Timestamp 和 Check Snapshot 套件。

相关代码与测试：

- [`materialization.sql`](../../dbt/include/doris/macros/materializations/snapshot/materialization.sql)
- [`snapshot.sql`](../../dbt/include/doris/macros/materializations/snapshot/snapshot.sql)
- [`strategies.sql`](../../dbt/include/doris/macros/materializations/snapshot/strategies.sql)
- [`test_doris_snapshot.py`](../../test/functional/adapter/test_doris_snapshot.py)

## 10. 实施状态

| 阶段 | 状态 | 验收结果 |
| --- | --- | --- |
| S1 原子替换 | 完成 | 故障注入后旧历史仍可查询 |
| S2 残留清理与恢复 | 完成 | 失败后重跑无重复、无残留 |
| S3 Timestamp Strategy | 完成 | 正常、精度、NULL、倒退场景通过 |
| S4 Hard Deletes | 完成 | 三种公开行为均有结果断言 |
| S5 Schema/Docs/Grants 生命周期 | 完成 | Schema 保护、Persist Docs 和 Snapshot Grants 通过 |
| S6 官方测试 | 完成 | 官方 Timestamp/Check 共 6 个用例通过 |
