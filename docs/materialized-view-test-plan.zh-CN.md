# dbt-doris 异步物化视图专项测试说明与执行记录

## 1. 被测代码与环境

### 1.1 被测代码

| 项目 | 值 |
| --- | --- |
| Adapter Git SHA | `f5e30c64ef7eb8320cf359c3d96cf62b595faf00` |
| 测试开始时工作树 | `dirty=false` |
| Adapter 包版本 | `dbt-doris 1.0.0` |
| dbt Core | `1.12.0` |
| Python | `3.11.15` |
| pytest | `8.4.2` |

后续提交到当前分支的内容没有修改 MV Adapter 源码、MV Macro、三个 MV
Functional 文件或 `test/unit/test_materialized_view.py`，因此本报告仍对应当前
MV 实现；如果以后这些文件发生变化，必须重新执行本测试。

### 1.2 Doris 集群形态

每个版本都重新解压官方二进制包并启动一套隔离集群：

| 项目 | 配置 |
| --- | --- |
| 集群节点 | 1 FE + 1 BE |
| FE Query Port | `21030` |
| FE HTTP/RPC/Edit Log | `20030` / `21020` / `21010` |
| BE Heartbeat/BE/Web/BRPC | `21050` / `21060` / `20040` / `20060` |
| `priority_networks` | `172.20.32.0/24` |
| 测试副本数 | `replication_num=1` |
| dbt 线程数 | `1` |
| 2.1.11 JDK | Temurin `8u502-b07` |
| 3.0.8—4.1.3 JDK | Temurin `17.0.19+10` |

使用偏移端口是为了避开机器上其他 Doris 进程，不改变 MV SQL 或功能语义。每个
版本开始测试前都要求 `SHOW FRONTENDS` 和 `SHOW BACKENDS` 中的节点
`Alive=true`，且 FE/BE 完整 Version 一致。

## 2. 测试入口与用例数量

五个版本都执行相同的三个文件：

| 文件 | 来源 | pytest Case 数 |
| --- | --- | ---: |
| `test_doris_materialized_view.py` | dbt-doris Doris 专项生命周期测试 | 11 |
| `test_doris_materialized_view_basic.py` | 继承 dbt Core `MaterializedViewBasic` 官方合约 | 8 |
| `test_doris_materialized_view_complete.py` | dbt-doris Docs、Source、Alias、Schema 补充测试 | 2 |
| **合计** |  | **21** |

## 3. E2E 使用的 Doris 观测面

测试不是只判断 `dbt run` 退出码。每个场景按需要使用以下 Doris SQL 验证真实
对象、任务和数据：

```sql
SHOW CREATE MATERIALIZED VIEW <schema>.<mv>;

SELECT Id, Name, State, RefreshState, QuerySql
FROM mv_infos("database"="<schema>")
WHERE Name = '<mv>';

SELECT TaskId, Status, ErrorMsg
FROM tasks('type'='mv')
WHERE MvDatabaseName = '<schema>'
  AND MvName = '<mv>'
ORDER BY CreateTime DESC, TaskId DESC;

SELECT table_name
FROM information_schema.tables
WHERE table_schema = '<schema>'
  AND table_name LIKE '<model>__dbt_%';
```

另外通过 Adapter `list_relations_without_caching` 验证 dbt 看到的真实 Relation
Type，通过普通 `SELECT` 验证 MV 聚合数据，而不是仅检查 DDL 字符串。

Refresh Task 的测试识别方法是：动作前读取已有 `TaskId` 集合，提交 Create 或
Refresh 后查询 `tasks('type'='mv')`，选择不在旧集合里的新任务，并等待状态离开
`PENDING/RUNNING`。Functional Test 只有在新任务最终为 `SUCCESS` 时通过；故意
制造的失败任务必须为 `FAILED` 且带有预期错误。

## 4. 21 个真实 Doris Functional Case

### 4.1 dbt-doris 生命周期专项：11 个

#### MV-E2E-001：Deferred Manual 创建、重复运行与数据刷新

- pytest：`TestDorisMaterializedViewLifecycle::test_create_repeat_and_manual_refresh`
- 初始数据：三行订单，按日期聚合结果应为 `300`、`50`。
- 第一次运行：创建 `BUILD DEFERRED / REFRESH COMPLETE ON MANUAL` MV。
- 断言：Relation Type 是 `materialized_view`；`mv_infos` 的 State/RefreshState
  为 `INIT/INIT`；DDL 包含 Deferred、Complete、Manual 和 Definition Hash；没有
 产生 Refresh Task。
- 第二次运行：只选择该 MV，再次执行 `dbt run`。
- 断言：MV Id 不变，没有重建；Adapter Response 为
  `REFRESH MATERIALIZED VIEW`；产生新的 `TaskId`，状态为 `SUCCESS`；查询结果为
  `300`、`50`。
- 第三次运行：先向底表插入金额 `75` 的新日期，再运行同一 MV。
- 断言：再次产生新的成功 Task，MV 数据变为 `300`、`50`、`75`。

#### MV-E2E-002：Immediate 配置/SQL/Full Refresh 变更及异步失败原子性

- pytest：`TestDorisMaterializedViewChanges::test_config_sql_full_refresh_and_failure_are_atomic`
- 初始运行：创建 `BUILD IMMEDIATE` MV，断言数据为 `300`、`50`。
- 配置变更：把 `buckets` 从 1 改为 2。
- 断言：MV Id 改变，`SHOW CREATE` 包含 `BUILD IMMEDIATE` 和 `BUCKETS 2`。
- SQL 变更：把聚合改为 `sum(amount) + 1`。
- 断言：MV Id 和 QuerySql 都改变，数据为 `301`、`51`。
- Full Refresh：执行 `dbt run --full-refresh`。
- 断言：即使定义相同也重新部署，MV Id 再次改变，数据仍为 `301`、`51`。
- 失败注入：新定义在构建时执行非法 JSON Parse，使临时 MV 的异步 Build Task
  失败。
- 原子性断言：线上 MV 的 Id、QuerySql 和数据保持为失败前版本；临时 MV Task
  状态为 `FAILED`，ErrorMsg 同时包含 JSON 和 Parse 信息。
- 恢复：恢复有效 SQL 后再次运行。
- 断言：运行成功，`daily_sales__dbt_tmp%` 临时 MV 全部清理。

#### MV-E2E-003：ON COMMIT 由 Doris 触发，dbt 不重复刷新

- pytest：`TestDorisMaterializedViewOnCommit::test_commit_trigger_refreshes_the_deferred_materialized_view`
- 创建：`BUILD DEFERRED / REFRESH AUTO ON COMMIT`。
- 断言：DDL 与配置一致。
- 定义未变时再次 `dbt run`。
- 断言：Adapter Response 为 `skip`，TaskId 集合不变，dbt 没有提交 Refresh。
- 向底表插入金额 `75` 的新日期。
- 断言：Doris 自动产生新的成功 MV Task，查询结果变为 `300`、`50`、`75`。

#### MV-E2E-004：`on_configuration_change=continue`

- pytest：`TestDorisMaterializedViewContinue::test_continue_keeps_the_existing_definition`
- 操作：先部署 MV，再把 SQL 改为 `sum(amount) + 1` 并配置 `continue`。
- 断言：运行不重建 MV，部署前后的 MV Id 和 QuerySql 完全一致。

#### MV-E2E-005：`on_configuration_change=fail`

- pytest：`TestDorisMaterializedViewFail::test_fail_rejects_the_change_and_keeps_the_existing_definition`
- 操作：先部署 MV，再修改 SQL 并配置 `fail`。
- 断言：`dbt run` 失败；线上 MV 的 Id 和 QuerySql 保持不变。

#### MV-E2E-006：Pending 首次部署恢复优先于 Continue

- pytest：`TestDorisMaterializedViewPendingRecovery::test_pending_deployment_recovers_even_with_continue_policy`
- 失败注入：首次创建成功后让 Post-hook 查询不存在的表，整个 Model 失败。
- 断言：线上 DDL 带有 `dbt-doris:deployment-pending=` Marker。
- 恢复：删除失败 Hook，把策略设为 `continue` 后再次运行。
- 断言：Adapter 不因 Continue 忽略未完成部署，而是完成恢复；DDL 改为
  `dbt-doris:definition-hash=`，Pending Marker 消失。

#### MV-E2E-007：Replace 后 Post-hook 失败、旧 MV 保存与下次恢复

- pytest：`TestDorisMaterializedViewReplaceRollback::test_failed_post_hook_preserves_and_restores_previous_mv`
- 初始状态：保存旧 MV Id 和 QuerySql。
- 失败注入一：修改 SQL，同时让 Post-hook 失败。
- 断言：新 Canonical MV 带 Pending Marker；旧 MV 被保留为
  `daily_sales__dbt_tmp`，其 Id 和 QuerySql 与原版本一致。
- 失败注入二：下一次重试改成会造成异步 Build Task 失败的 SQL。
- 断言：Adapter 先恢复旧 MV；Canonical Id/QuerySql 回到原版本，Definition Hash
  完整且没有 Pending Marker。
- 最终恢复：使用有效的新 SQL 再次运行。
- 断言：新 MV 成功发布、Id 改变、临时 MV 全部清理。

#### MV-E2E-008：ON SCHEDULE DDL 与重复运行分流

- pytest：`TestDorisMaterializedViewSchedule::test_schedule_config_is_present_in_doris_ddl`
- 创建配置：`REFRESH AUTO ON SCHEDULE EVERY 1 DAY STARTS 2099-08-01 02:00:00`。
- 断言：`SHOW CREATE` 包含对应 Schedule 和 Starts。
- 定义未变时再次 `dbt run`。
- 断言：Adapter Response 为 `skip`，TaskId 集合不变，dbt 没有主动刷新。
- 说明：Starts 故意设到 2099 年，因此该 Case 验证 DDL 和 dbt/Doris 职责分流，
  不声称等待并观察了一次真实定时触发。

#### MV-E2E-009：顶层 `replication_num` 合并并覆盖 properties

- pytest：`TestDorisMvConfig::test_top_level_replication_num_is_present_in_doris_ddl`
- 配置：`properties.replication_num=3`，同时顶层 `replication_num=1`。
- 断言：Doris 返回的 DDL 最终包含
  `replication_allocation = tag.location.default: 1`，证明顶层值没有静默失效且优先。

#### MV-E2E-010：单元素 `partition_by` List

- pytest：`TestDorisMvConfig::test_single_partition_list_is_present_in_doris_ddl`
- 前置：创建按 `order_date` Range Partition 的底表。
- MV 配置：`partition_by=['order_date']`。
- 断言：Doris 实际 DDL 包含 `PARTITION BY (order_date)`，不是只验证宏渲染。

#### MV-E2E-011：Table/View/MV 类型切换及 MV → Table 失败恢复

- pytest：`TestDorisMaterializedViewTypeSwitch::test_table_materialized_view_view_and_table_switches`
- 顺序一：`Table → MV → View → MV`。
- 断言：每一步 Relation Type 正确，Table/View 数据行数为 2，没有
  `__dbt_tmp/__dbt_backup` Helper 残留。
- 失败注入：`MV → Table` 的 Intermediate Table Rename 人为失败。
- 断言：旧对象作为 `switchable__dbt_backup` 保留，Relation Type 仍是
  `materialized_view`。
- 重试：再次执行 `MV → Table`。
- 断言：最终类型为 Table、数据行数仍为 2、Helper 全部清理。
- 顺序二：继续验证 `Table → View → Table`，每步类型、数据和清理都正确。

### 4.2 dbt Core 官方 MaterializedViewBasic 合约：8 个

这八个 Case 直接继承 dbt Core 1.12 的 `MaterializedViewBasic`，Doris 子类只实现
插入数据、显式 Refresh、统计行数和 Relation Type 查询。每个 Case 开始前运行
Seed 并用 `--full-refresh` 建立干净 MV，结束后重建测试 Schema。

#### MV-E2E-012：官方创建合约

- pytest：`test_materialized_view_create`
- 断言：dbt 创建出的对象真实类型为 `materialized_view`。

#### MV-E2E-013：官方重复创建幂等合约

- pytest：`test_materialized_view_create_idempotent`
- 操作：已有 MV 上再次运行同一 Model。
- 断言：运行前后对象都保持 `materialized_view`。

#### MV-E2E-014：官方 Full Refresh 合约

- pytest：`test_materialized_view_full_refresh`
- 操作：执行 `dbt run --full-refresh`。
- 断言：对象仍为 MV，dbt Debug Log 明确出现对目标应用 Replace。

#### MV-E2E-015：官方 Table → MV 合约

- pytest：`test_materialized_view_replaces_table`
- 断言：相同 Model 名先为 Table，修改 Materialization 并运行后真实类型为 MV。

#### MV-E2E-016：官方 View → MV 合约

- pytest：`test_materialized_view_replaces_view`
- 断言：相同 Model 名先为 View，修改 Materialization 并运行后真实类型为 MV。

#### MV-E2E-017：官方 MV → Table 合约

- pytest：`test_table_replaces_materialized_view`
- 断言：相同 Model 名从 MV 切换后真实类型为 Table。

#### MV-E2E-018：官方 MV → View 合约

- pytest：`test_view_replaces_materialized_view`
- 断言：相同 Model 名从 MV 切换后真实类型为 View。

#### MV-E2E-019：数据只在 Refresh 完成后更新

- pytest：`test_materialized_view_only_updates_after_refresh`
- 操作：记录 Seed Table 和 MV 行数；只向 Seed Table 插入一行；再次记录行数；执行
  Doris 原生 `REFRESH MATERIALIZED VIEW ... COMPLETE` 并等待新 Task 成功；最后
  再次记录行数。
- 断言：底表行数在 Insert 后增加；MV 行数在 Refresh 前不变，只在 Refresh Task
  成功后增加。

### 4.3 Docs、Source、Alias、Schema：2 个

#### MV-E2E-020：`persist_docs` Relation/Column 开关及文档变更重建

- pytest：`TestDorisMaterializedViewPersistDocs::test_relation_and_column_docs_follow_persist_docs_and_rebuild`
- 同时创建启用和关闭 `persist_docs.relation/columns` 的两个 MV。
- 断言：启用时 Relation Description 出现在 MV DDL，列说明出现在
  `SHOW FULL COLUMNS`；关闭时用户 Description/列注释都不存在，但 Adapter
  Definition Hash Marker 始终存在。
- 修改 YAML 中 `sales` 的 Description 后只运行启用 Docs 的 MV。
- 断言：MV Id 改变，`sales` 列注释更新为新值。

#### MV-E2E-021：`source()`、`ref()`、Alias、自定义 Schema 和 dbt 元数据

- pytest：`TestDorisMaterializedViewSourceAliasAndSchema::test_source_alias_and_custom_schema`
- 前置：在默认 Schema 建 Source Table，另建 Ref Model；MV 配置
  `alias='mv_source_alias'`、`schema='mv_custom'`。
- 数据断言：Source 的 `100+200` 与 Ref 的 `50` 聚合为 `350`。
- DDL 断言：`SHOW CREATE` 同时引用编译后的 Source Table 和 Ref Table 全限定名。
- dbt 断言：运行 `dbt ls --output json`，Alias、Materialization、Source/Ref
  Dependency 都正确；运行 `dbt docs generate`，`manifest.json` 中的 Alias、Schema
  和依赖关系与运行结果一致。
- 清理：Case 结束时强制删除自定义 Schema 并清理 Adapter Cache。

## 5. 实际执行结果

| Doris | FE/BE 完整 Version | 结果 | Skip | 耗时 |
| --- | --- | ---: | ---: | ---: |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 21 passed | 0 | 121.07s |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 21 passed | 0 | 118.73s |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 21 passed | 0 | 105.66s |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 21 passed | 0 | 105.94s |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 21 passed | 0 | 109.19s |
| **合计** |  | **105 passed** | **0** | **560.59s** |

每个版本的证据目录都包含：

```text
/mnt/disk1/chenjunwei/dbt-doris-mv-version-e2e/evidence/<version>/
├── environment.log       # Git、Python、dbt、pytest、FE/BE 版本和 Alive 状态
├── mv-functional.log     # 21 个 Case 的逐项结果及 Version Evidence JSON
└── cleanup.log           # 对应测试 Schema 前缀的残留检查
```

## 6. 清理验证

pytest 完成后，每个版本重新启动其 FE 元数据并执行：

```sql
SHOW DATABASES LIKE 'dbt_adapter_mv_<version>_e2e%';
```

五个版本的返回行数均为 0。检查完成后关闭 FE；之前运行 pytest 的 FE/BE 也都已
关闭。该结论只针对本专项测试创建的 Schema，不表示扫描或删除机器上其他用户的
Doris 数据。

## 7. 单元测试补充覆盖

真实 Doris E2E 之外，还执行：

```bash
PYTHONPATH=/tmp/dbt-doris-adapter \
python -m pytest -q test/unit/test_materialized_view.py
```

结果为 `118 passed in 38.23s`。文件中有 61 个 Test Function，其中参数化输入被
pytest 展开为 118 个 Item。它们补充覆盖：

- Create/Refresh/Drop/Rename/Atomic Replace SQL 的精确渲染；
- Definition Hash、Pending Marker、Docs、Identifier 和配置规范化；
- Immediate/Deferred、Manual/Schedule/Commit 的动作选择；
- Pre-hook Ordering、Post-hook Pending、Replace/Type Switch 恢复；
- `wait_for_refresh=false`、Task 从 Running 到 Success、Failed Task，以及新 Task
  不出现时的超时；
- replication、partition、distribution、refresh、build 等合法/非法配置；
- MV Relation Listing、Catalog Type、缺失 Schema；
- FE 版本解析、Connected/Master FE 选择和版本 Gate。

这些是无真实 Doris 的单元测试，不计入第 5 节的 105 个版本 E2E 结果。

## 8. 版本通过标准与结论

一个 Doris 版本只有同时满足以下条件，才能加入 MV “已验证版本”表：

1. FE/BE 完整 Version 与目标发行版一致且全部 `Alive=true`；
2. dbt Core 1.12 环境实际收集到 21 个 MV Functional Case；
3. 21 个 Case 全部 Pass，没有 Skip/Xfail/重跑后通过；
4. pytest 退出码为 0；
5. 测试 Schema 残留为 0，隔离进程已关闭；
6. 日志能追溯到精确 Adapter Git SHA。

按上述标准，当前已验证的精确 Doris 版本是：

```text
2.1.11、3.0.8、3.1.4、4.0.7、4.1.3
```

这不自动证明所有更高版本都兼容。同一发行线的后续 Patch 版本可以视为预期兼容，
但必须重新执行本文件的 21 个 Case 后，才能把该精确版本写成“已验证”。
