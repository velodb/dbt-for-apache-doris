# dbt-doris 异步物化视图测试矩阵

本文只回答三个问题：测试矩阵是什么、测试了哪些功能、测试怎么执行。

## 1. 测试矩阵

### 1.1 当前测试数量

| 层级 | 测试入口 | 主要内容 | pytest 用例数（参数展开后） |
| --- | --- | --- | ---: |
| Functional | `test_doris_materialized_view.py` | Doris MV 生命周期、刷新、变更和恢复 | 11 |
| Functional | `test_doris_materialized_view_basic.py` | dbt Core 官方 Materialized View 合约 | 8 |
| Functional | `test_doris_materialized_view_complete.py` | Docs、Source、Alias、自定义 Schema | 2 |
| Functional | `test_doris_grants.py::TestDorisMaterializedViewGrants` | MV Grants | 1 |
| **Functional 小计** |  | **连接真实 Doris 执行** | **22** |
| Unit/Adapter | `test/unit/test_materialized_view.py` | DDL、动作判断、配置校验、失败恢复和版本门禁 | 119 |
| Unit/Adapter | `test/unit/test_adapter_config.py` | MV 配置注册、默认值和反序列化 | 4 |
| Unit/Adapter | `test/unit/test_relation.py::test_materialized_view_to_view_replacement_updates_one_cache_key` | Relation Cache 类型切换 | 1 |
| **Unit/Adapter 小计** |  | **不连接 Doris** | **124** |
| **合计** |  |  | **146** |

### 1.2 Doris 版本矩阵

| Doris | 当前套件：22 项，包含 Grants | 历史核心套件：21 项，不含 Grants |
| --- | ---: | ---: |
| 2.1.11 | 未执行 | 21/21 passed |
| 3.0.8 | 未执行 | 21/21 passed |
| 3.1.4 | 未执行 | 21/21 passed |
| 4.0.7 | 未执行 | 21/21 passed |
| 4.1.3 | 22/22 passed | 21/21 passed |

当前代码的 124 项 Unit/Adapter 测试为 `124/124 passed`。它们不依赖具体 Doris
版本，因此不在每个 Doris 版本上重复执行。

## 2. Functional 测试了什么

| Case | 功能 | 怎么测试 | 主要通过条件 |
| --- | --- | --- | --- |
| MV-E2E-001 | Manual 创建和刷新 | 首次创建、重复 `dbt run`、修改底表后再次运行 | 首次不刷新；后续产生成功 Refresh Task；数据更新 |
| MV-E2E-002 | 配置、SQL 和 Full Refresh 变更 | 修改 Buckets、模型 SQL，执行 `--full-refresh`，再注入 Build 失败 | 应重建时 MV Id/定义变化；失败时旧 MV 和数据不受影响 |
| MV-E2E-003 | ON COMMIT | 重复运行模型后向底表 Insert | dbt 不主动刷新；Doris 在底表提交后刷新 MV |
| MV-E2E-004—005 | `on_configuration_change` | 分别使用 `continue` 和 `fail` 修改模型定义 | Continue 保留旧定义；Fail 阻止运行且旧 MV 不变 |
| MV-E2E-006—007 | 失败恢复 | 在首次部署和 Replace 后注入 Post-hook/Build 失败，再重试 | Pending/Backup 可恢复；旧 MV 不丢失；临时对象最终清理 |
| MV-E2E-008 | ON SCHEDULE | 创建定时刷新 MV 后重复运行 | DDL 中 Schedule 正确；dbt 不主动提交 Refresh |
| MV-E2E-009—010 | Doris 配置 | 设置顶层 `replication_num` 和单元素 `partition_by` | Doris 返回的真实 DDL 包含预期副本数和分区 |
| MV-E2E-011 | Relation 类型切换 | 按 Table、View、MV 多个方向切换，并注入 MV → Table Rename 失败 | 最终类型和数据正确；失败可重试；无 Helper 残留 |
| MV-E2E-012—019 | dbt 官方基础合约 | 执行官方创建、幂等、Full Refresh、类型切换和数据刷新用例 | 8 个 dbt Core Materialized View 合约全部通过 |
| MV-E2E-020 | Persist Docs | 分别开启/关闭 Relation 和 Column Docs，再修改 Description | 注释按配置写入；文档变更触发更新 |
| MV-E2E-021 | Source、Ref、Alias、Schema | MV 同时引用 `source()` 和 `ref()`，配置 Alias 和自定义 Schema | 数据、DDL、`dbt ls` 和 `manifest.json` 元数据正确 |
| MV-E2E-022 | Grants | 创建授权、切换授权用户、修改定义并重复运行 | 授权正确更新；定义变化后授权仍在；不重复 Grant/Revoke |

## 3. Unit/Adapter 测试了什么

| 范围 | Item 数 | 主要内容 |
| --- | ---: | --- |
| SQL、Definition Hash、动作判断、Docs | 26 | Create/Refresh/Drop/Rename、Hash、Pending Marker、Docs |
| 生命周期、Task、Hook、Grants、恢复、类型切换 | 24 | Immediate/Deferred、等待任务、失败恢复、原子替换 |
| DDL 配置渲染、规范化和校验 | 54 | Refresh、Distribution、Partition、Replication 等合法和非法配置 |
| Relation、版本门禁、Drop/Rename | 15 | Relation 类型、Catalog、FE 版本选择和版本 Gate |
| Adapter Config | 4 | 字段注册、默认值、完整配置和 Schedule 类型 |
| Relation Cache | 1 | MV → View 后只保留一个 Cache Key |
| **合计** | **124** |  |

## 4. 怎么测试

### 4.1 执行 Functional

```bash
DORIS_TEST_HOST=127.0.0.1 \
DORIS_TEST_PORT=9030 \
DORIS_TEST_USER=root \
DORIS_TEST_PASSWORD='' \
DORIS_TEST_SCHEMA=dbt_adapter_mv_e2e \
DORIS_TEST_REPLICATION_NUM=1 \
DORIS_TEST_EXPECTED_VERSION=4.1.3 \
PYTHONPATH=. python -m pytest -q \
  test/functional/adapter/test_doris_materialized_view.py \
  test/functional/adapter/test_doris_materialized_view_basic.py \
  test/functional/adapter/test_doris_materialized_view_complete.py \
  test/functional/adapter/test_doris_grants.py::TestDorisMaterializedViewGrants
```

Functional 测试不是只看 `dbt run` 是否成功，还会直接查询 Doris：

```sql
SHOW CREATE MATERIALIZED VIEW <schema>.<mv>;

SELECT Id, State, RefreshState, QuerySql
FROM mv_infos("database"="<schema>")
WHERE Name = '<mv>';

SELECT TaskId, Status, ErrorMsg
FROM tasks('type'='mv')
WHERE MvDatabaseName = '<schema>' AND MvName = '<mv>';
```

每个 Case 根据需要检查：

1. MV Relation 类型和 Doris DDL；
2. Refresh/Build Task 是否产生及最终状态；
3. MV 查询数据是否正确；
4. 配置或 SQL 变化后是否正确重建或跳过；
5. 故障后旧对象是否保留、能否重试；
6. Grants、`__dbt_tmp`、`__dbt_backup` 和测试 Schema 是否符合预期。

### 4.2 执行 Unit/Adapter

```bash
PYTHONPATH=. python -m pytest -q \
  test/unit/test_materialized_view.py \
  test/unit/test_adapter_config.py \
  test/unit/test_relation.py::test_materialized_view_to_view_replacement_updates_one_cache_key
```

把 `-q` 改成 `--collect-only -q`，可以核对实际收集数量和 Node ID。

## 5. 结论

多版本测试使用 `1 FE + 1 BE`、`replication_num=1`，未覆盖多 FE/BE 拓扑。

当前未覆盖两个客户端并发刷新同一个 MV 的场景。现有 Manual Refresh 测试在单线程下，
通过刷新前后的 TaskId 集合差识别新任务。

当前代码的 22 项 Functional 和 124 项 Unit/Adapter 已全部通过，测试 Schema 残留为
0；Doris 4.1.3 完成了当前 22 项验证，其余四个版本只有旧代码基线的历史 21 项记录，
当前代码的 22 项尚未补跑。
