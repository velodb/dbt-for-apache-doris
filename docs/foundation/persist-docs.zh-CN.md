# Doris Persist Docs

Persist Docs 将 dbt YAML 中的 Model、Seed 和 Column `description` 写入 Doris
Relation/Column Comment。这样在 `SHOW CREATE TABLE`、`SHOW FULL COLUMNS`、
`information_schema` 和 `dbt docs generate` 中都能看到同一份说明。

## 配置

可以在单个模型中启用：

```sql
{{ config(
    materialized='table',
    persist_docs={'relation': true, 'columns': true}
) }}

select order_id, amount from {{ ref('stg_orders') }}
```

也可以在 `dbt_project.yml` 中统一配置：

```yaml
models:
  analytics:
    +persist_docs:
      relation: true
      columns: true

seeds:
  analytics:
    +persist_docs:
      relation: true
      columns: true
```

说明写在 Properties YAML：

```yaml
version: 2

models:
  - name: orders
    description: 每日订单明细
    columns:
      - name: order_id
        description: 订单唯一标识
      - name: amount
        description: 订单金额，单位为元
```

`relation` 和 `columns` 可以分别启用。未配置 `persist_docs` 时，Description 只留在
dbt Manifest 中，不会写入 Doris。

## 支持范围

- Table：Relation 和 Column Comment
- View：在 `CREATE OR REPLACE VIEW` 中写入 Relation 和 Column Comment
- Incremental：首次创建和 `--full-refresh` 时完整写入；普通增量运行可以更新注释
- Seed：创建时写入 Relation 和 Column Comment

View 没有使用 Table 的 `ALTER ... MODIFY COLUMN COMMENT` 语法。dbt-doris 会先推导
View 查询的完整输出列，再生成包含所有列的 View 定义；只为 YAML 中存在 Description
的列添加 Comment，因此部分列文档不会改变 View 的输出列集合。

## 特殊字符

创建 Relation 时，dbt-doris 会对引号和反斜线进行 Doris 字符串转义。支持多行文本、
单双引号、dbt Docs Block、SQL 注释符号和 `$` 等普通文档内容。

部分 Doris 版本的 `ALTER ... COMMENT` 不能无损更新一个同时包含单引号和双引号的
Comment。首次创建和 Full Refresh 不受影响；如果普通增量或 Seed 重跑需要把现有注释
改成这种文本，adapter 会明确失败并提示：

```bash
dbt run --full-refresh --select <model>
# Seed 使用：
dbt seed --full-refresh --select <seed>
```

这样不会出现命令成功但 Doris 中实际保存了多余转义符的情况。

## 缺失列与 dbt Docs

当 YAML 声明了数据库中不存在的列时，运行会给出 Warning，并跳过该列的 Comment，
不会误写到大小写相近的其他列。`quote: true` 的列名按大小写精确匹配。

`dbt docs generate` 会从 Doris `information_schema.tables` 和
`information_schema.columns` 读取已经落库的注释并写入 `catalog.json`：

```bash
dbt run
dbt docs generate
dbt docs serve
```
