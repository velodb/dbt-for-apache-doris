# Doris Model Contracts

Model Contract 用 YAML 固定模型对外承诺的列名和数据类型。启用后，dbt 会在 Doris
执行建表或替换 View 之前检查编译 SQL 的输出；列缺失、出现未声明列或类型不一致时，
本次 `dbt run` 失败。

## 基本用法

模型 SQL：

```sql
{{ config(
    materialized='table',
    contract={'enforced': true}
) }}

select
    cast(order_id as bigint) as order_id,
    cast(amount as decimal(18, 2)) as amount,
    cast(created_at as datetime) as created_at
from {{ source('raw', 'orders') }}
```

`models/schema.yml`：

```yaml
version: 2

models:
  - name: orders
    config:
      contract:
        enforced: true
    columns:
      - name: order_id
        data_type: bigint
      - name: amount
        data_type: decimal(18, 2)
      - name: created_at
        data_type: datetime
```

也可以只在 YAML 中配置 `contract.enforced: true`，不必在 SQL 中重复配置。

## 支持范围

dbt-doris 对以下 Model Materialization 执行契约检查：

- `table`
- `view`
- `incremental`

增量模型还必须按 dbt Core 的要求配置：

```yaml
config:
  materialized: incremental
  contract:
    enforced: true
  on_schema_change: append_new_columns  # 或 fail
```

如果使用 Doris adapter 的 `insert_overwrite` 增量策略，还需要配置 `unique_key`；使用
`append` 时可以不配置。

## 类型与列名

契约比较的是 Doris 对查询结果推导出的类型，因此建议在模型 SQL 中显式 `cast`，尤其是
字符串、Decimal、日期时间和字面量。不要依赖字面量的隐式宽度或整数精度。

保留字或需要区分大小写的列使用 `quote: true`：

```yaml
columns:
  - name: order
    quote: true
    data_type: bigint
```

对应 SQL 也应引用同一个标识符：

```sql
select cast(order_number as bigint) as `order`
```

列在 SQL 与 YAML 中的顺序不同不会导致契约失败。Table 建表时会按 YAML 声明顺序投影；
View 保留查询本身的输出顺序。

## 失败语义

- Table 先构建中间表，契约检查或写入失败时不会替换已有目标表。
- View 在 `CREATE OR REPLACE VIEW` 之前检查契约，失败时已有 View 保持可查询。
- Incremental 在写入目标表之前检查本批 SQL，失败时不会插入本批数据。

Contract 只校验列集合和数据类型，不等同于 Doris 的 `NOT NULL`、唯一性、主键或外键
约束。业务数据质量仍应使用 dbt Data Test；Doris Key Model 仍通过 `duplicate_key`、
`unique_key` 等 adapter 配置选择。
