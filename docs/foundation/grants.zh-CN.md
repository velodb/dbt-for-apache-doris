# Doris Grants

dbt Grants 在 Relation 构建完成后，把声明式权限配置与 Doris 当前权限比较，只执行必要的
`GRANT` 和 `REVOKE`。支持 Table、View、Incremental、Seed、Snapshot 和异步
Materialized View。

## 配置

在 Model Properties YAML 中配置：

```yaml
version: 2

models:
  - name: orders
    config:
      grants:
        select:
          - analytics_reader
          - finance_reader
        insert:
          - ingestion_writer
```

也可以在 `dbt_project.yml` 中按资源路径统一配置：

```yaml
models:
  analytics:
    marts:
      +grants:
        select: [analytics_reader]
```

Grantee 是 Doris 用户名。未指定 Host 时，dbt-doris 生成
`'username'@'%'`；需要指定 Host 时使用 `username@host`，例如
`analytics_reader@10.%`。Host 为 `%` 时应使用简写用户名，保证权限查询结果与配置一致。

## 权限映射

dbt-doris 接受以下稳定、可核对的 dbt 权限名：

| dbt 配置 | Doris DCL | `information_schema` 返回值 |
| --- | --- | --- |
| `select` | `SELECT_PRIV` | `SELECT` |
| `insert` | `LOAD_PRIV` | `INSERT` |
| `alter` | `ALTER_PRIV` | `ALTER` |
| `create` | `CREATE_PRIV` | `CREATE` |
| `drop` | `DROP_PRIV` | `DROP` |
| `show_view` | `SHOW_VIEW_PRIV` | `SHOW_VIEW` |

不在此列表中的权限会在执行 DCL 前报清晰的 Compilation Error，避免把未经验证的文本拼入
权限语句。

## 增量核对语义

adapter 从 `information_schema.table_privileges` 读取目标 Relation 的权限，转换成 dbt 的
`{privilege: [users]}` 结构，再计算差异：

- 配置与 Doris 一致：不执行 DCL
- 新增用户或权限：只执行必要的 `GRANT`
- 删除用户或权限：只执行必要的 `REVOKE`
- `grants: {}`：忽略权限，不改变已有状态
- `grants: {select: []}`：显式撤销当前 `select` 用户权限

Doris 每条 DCL 只处理一个用户，dbt-doris 也会逐条执行，避免 MySQL Protocol Connector
遗留多结果集。

Table 原子替换、View 重建、Incremental 普通运行与 Full Refresh、Seed 重载、Snapshot
合并都会在最终 Relation 上重新核对权限。异步 Materialized View 即使定义没有变化、跳过
重建，也仍会应用纯 Grants 配置变更。

## Doris 权限要求与限制

执行 dbt 的 Doris 用户需要有 `GRANT_PRIV`，并拥有要授出的相应权限。Doris 的权限语法和
范围见官方 [GRANT TO](https://doris.apache.org/docs/2.1/sql-manual/sql-statements/account-management/GRANT-TO/)
文档；当前权限可通过官方 [SHOW GRANTS](https://doris.apache.org/docs/2.1/sql-manual/sql-statements/account-management/SHOW-GRANTS/)
核查。

Doris Role 的 Table 权限不会以 Role 名称出现在
`information_schema.table_privileges`，因此 adapter 无法安全计算 Role 的撤销差异。
`role:<name>` 配置会明确失败。需要 Role 时，应在 Doris 中独立管理 Role，并在 dbt
`grants` 中配置可核对的直接用户权限。
