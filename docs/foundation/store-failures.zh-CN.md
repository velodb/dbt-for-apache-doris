# Doris Store Failures

Store Failures 将 dbt Data Test 返回的失败明细保存为 Doris Relation，便于排查具体哪几行
数据违反了约束。没有开启时，dbt 只返回失败数量，不保存明细。

## 开启方式

可以为单个 Data Test 开启：

```yaml
version: 2

models:
  - name: orders
    columns:
      - name: order_id
        data_tests:
          - unique:
              config:
                store_failures: true
```

也可以在命令行对本次选择的全部测试开启：

```bash
dbt test --store-failures
dbt build --store-failures --select orders
```

默认失败表位于 `<target.schema>_dbt_test__audit`。可以在项目中指定较短、易管理的后缀：

```yaml
data_tests:
  +schema: audit
```

使用 dbt 默认的 Schema 生成规则时，上例生成 `<target.schema>_audit`。Doris Database 名称
最长 64 个字符，Target Schema 较长时应缩短 Audit 后缀，或在项目中自定义
`generate_schema_name`。

## Table、View 与 Ephemeral

`store_failures_as` 可以直接决定保存方式，并优先于 `store_failures`：

```yaml
data_tests:
  +store_failures_as: table
```

- `table`：保存为 Doris Table；重复运行会用本次失败明细替换旧表
- `view`：保存为 Doris View；查询 View 时读取当前源数据
- `ephemeral`：不创建 Relation，等同于关闭失败明细落库

也可以只在某个测试的 `config` 中设置。支持 Singular Test 和 Generic Data Test。
`limit` 同时限制 dbt 统计到的失败数量和实际保存的失败行数。

## Doris 存储属性

失败 Table 是 `data_tests` 资源，不继承 `models` 下的 Doris Properties。单 BE 开发环境
需要单独配置副本数：

```yaml
data_tests:
  +store_failures: true
  +properties:
    replication_num: "1"
```

多 BE 生产集群可以使用适合自身拓扑的 `replication_num`。其他 Doris Table 配置也可以放在
`data_tests` 下；由于不同测试返回的列不相同，通常让 Doris 自动选择 Failure Table 的
Distribution 更安全。

## 查看与清理

测试输出中的 `relation_name` 和 “See test failures” SQL 会指向保存的 Relation，也可以直接
查询：

```sql
select *
from analytics_audit.unique_orders_order_id;
```

关闭 Store Failures 不会自动删除以前生成的 Relation。如不再需要，应显式 Drop 对应
Table/View 或整个 Audit Database。
