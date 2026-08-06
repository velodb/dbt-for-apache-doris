# dbt-doris 异步物化视图专项测试说明与执行记录

本文档同时记录两类信息：

- **当前测试清单**：以功能实现基线 `7a362c89d234c0f3e6d4798a523ef7a05a57e163`
  为准，共有 22 个真实 Doris Functional Item 和 124 个直接相关 Unit/Adapter
  Item，合计 146 个；
- **历史多版本证据**：以 `f5e30c64ef7eb8320cf359c3d96cf62b595faf00`
  为准，在五个 Doris 版本上执行当时的 21 个 Functional Item，共 105 次通过。

本文的“Item”指 pytest 完成参数化展开后实际收集到的测试节点。只把直接验证异步
MV 行为的测试计入清单；共享的 Relation Namespace 等通用测试不重复计入。测试计划、
当前单版本执行和历史多版本证据分别列示，不能互相替代。

## 1. 被测代码与环境

### 1.1 当前测试清单与定向执行基线

| 项目 | 值 |
| --- | --- |
| Adapter Git SHA | `7a362c89d234c0f3e6d4798a523ef7a05a57e163` |
| 测试开始时工作树 | `dirty=false` |
| Adapter 包版本 | `dbt-doris 1.0.0` |
| dbt Core | `1.12.0` |
| Python | `3.12.13` |
| pytest | `9.1.1` |
| Doris FE/BE | `doris-4.1.3-rc02-7126cf65d96` |

本次定向执行使用当前 main 的干净工作树。本文档自身的后续文档提交不改变运行时代码
或测试逻辑；如果 MV Adapter、Macro 或本文列出的测试文件发生变化，必须重新收集并
执行清单。

### 1.2 历史五版本证据基线

| 项目 | 值 |
| --- | --- |
| Adapter Git SHA | `f5e30c64ef7eb8320cf359c3d96cf62b595faf00` |
| 测试开始时工作树 | `dirty=false` |
| Adapter 包版本 | `dbt-doris 1.0.0` |
| dbt Core | `1.12.0` |
| Python | `3.11.15` |
| pytest | `8.4.2` |

该历史批次早于 MV Grants Functional Case 及其相关实现，不代表当前 22 个
Functional Item 已在五个版本上全部执行。

### 1.3 历史五版本 Doris 集群形态

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

## 2. 当前测试入口与数量

### 2.1 完整清单

| 文件或 Selector | 类型 | pytest Item 数 |
| --- | --- | ---: |
| `test_doris_materialized_view.py` | dbt-doris Doris 专项生命周期测试 | 11 |
| `test_doris_materialized_view_basic.py` | 继承 dbt Core `MaterializedViewBasic` 官方合约 | 8 |
| `test_doris_materialized_view_complete.py` | dbt-doris Docs、Source、Alias、Schema 补充测试 | 2 |
| `test_doris_grants.py::TestDorisMaterializedViewGrants` | dbt-doris MV Grants 测试 | 1 |
| **Functional 小计** |  | **22** |
| `test/unit/test_materialized_view.py` | MV Macro、生命周期和 Adapter 单元测试 | 119 |
| `test/unit/test_adapter_config.py` | MV 配置数据契约测试 | 4 |
| `test/unit/test_relation.py::test_materialized_view_to_view_replacement_updates_one_cache_key` | MV Relation Cache 测试 | 1 |
| **Unit/Adapter 小计** |  | **124** |
| **当前直接相关测试合计** |  | **146** |

`test/unit/test_relation.py` 还有 4 个参数化 Item 验证通用 Doris Database/Schema
Namespace；它们会在完整 Unit Suite 中执行，但不直接验证 MV，因此不计入上述 146 项。

### 2.2 精确执行命令

Functional 只能选择 Grants 文件中的 MV Class；直接执行整个 `test_doris_grants.py`
会额外收集 5 个非 MV Grants Item：

```bash
DORIS_TEST_HOST=127.0.0.1 \
DORIS_TEST_PORT=9030 \
DORIS_TEST_USER=root \
DORIS_TEST_PASSWORD='' \
DORIS_TEST_SCHEMA=dbt_adapter_mv_record_e2e \
DORIS_TEST_REPLICATION_NUM=1 \
DORIS_TEST_EXPECTED_VERSION=4.1.3 \
PYTHONPATH=. python -m pytest -q \
  test/functional/adapter/test_doris_materialized_view.py \
  test/functional/adapter/test_doris_materialized_view_basic.py \
  test/functional/adapter/test_doris_materialized_view_complete.py \
  test/functional/adapter/test_doris_grants.py::TestDorisMaterializedViewGrants
```

直接相关 Unit/Adapter 测试：

```bash
PYTHONPATH=. python -m pytest -q \
  test/unit/test_materialized_view.py \
  test/unit/test_adapter_config.py \
  test/unit/test_relation.py::test_materialized_view_to_view_replacement_updates_one_cache_key
```

把 `-q` 替换为 `--collect-only -q`，可以核对完整 Node ID 与本文统计是否一致。

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

Refresh Task 的测试识别方法是：动作前读取已有 `TaskId` 集合；提交 Create 或
Refresh 后，每轮查询 `tasks('type'='mv')`，选择不在旧集合中且按
`CreateTime DESC, TaskId DESC` 排在最前的任务，再读取其状态。当前实现不会锁定首次
观察到的 TaskId。Functional Test 只有在所选任务最终为 `SUCCESS` 时通过；故意制造
的失败任务必须为 `FAILED` 且带有预期错误。

## 4. 当前 22 个真实 Doris Functional Case

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

### 4.4 Grants：1 个

#### MV-E2E-022：授权变更、定义替换与重复运行

- pytest：`TestDorisMaterializedViewGrants::test_grants_update_without_rebuilding_the_materialized_view`
- 初始运行：创建 `BUILD DEFERRED` MV，并把 `SELECT` 授给第一个测试用户。
- Grants 变更：把授权对象改为第二个测试用户，再次运行同一模型。
- 断言：第一个用户的直接授权被回收，第二个用户获得直接 `SELECT`。
- 定义变更：修改模型 SQL 后再次运行，再验证第二个用户的 `SELECT` 仍存在。
- 重复运行：定义和 Grants 都不变时再次运行。
- 断言：运行日志不再出现重复的 `GRANT` 或 `REVOKE`。
- 证据边界：该 Case 直接验证 Grants 状态和运行日志，没有比较 Grants-only 或 SQL
  变更运行前后的 MV Id；因此本文不把它单独作为“对象一定未重建”或“必然执行
  Replace”的证据。定义 Replace 本身由 MV-E2E-002 验证。

## 5. 实际执行结果

### 5.1 当前 main 定向结果

以下结果直接在功能实现基线 `7a362c89d234c0f3e6d4798a523ef7a05a57e163`
的干净工作树执行：

| 范围 | 环境 | 结果 | 耗时 |
| --- | --- | ---: | ---: |
| 当前 22 个 MV Functional Item | Doris `4.1.3`、dbt Core `1.12.0`、Python `3.12.13` | 22 passed | 56.54s |
| 当前 124 个 MV Unit/Adapter Item | dbt Core `1.12.0`、Python `3.12.13` | 124 passed | 23.00s |
| **当前直接相关清单合计** |  | **146 passed** | — |

Functional 日志同时记录 FE/BE 都是
`doris-4.1.3-rc02-7126cf65d96`、节点 `Alive=true`、当前连接 FE 是 Master，版本
Gate 为 passed。22 项产生的 22 个 Warning 都是 pytest 对 Class-scoped Instance
Fixture 的弃用提醒；124 项产生的 9 个 Warning 都来自 Logbook 的 datetime 弃用
提醒，不是测试 Skip 或 Xfail。

本次原始日志保存在：

```text
/mnt/disk1/chenjunwei/dbt-doris-mv-version-e2e/evidence/current-main-7a362c8/
├── evidence-manifest.txt         # 完整命令、Selector、Cleanup SQL 和文件说明
├── environment.log               # Git、Python、dbt、pytest、FE/BE 版本和 Alive 状态
├── collection.log                # 带范围标签的全量与 MV 定向收集数量
├── mv-functional-collection.log  # 当前 22 个 Functional Node ID
├── mv-unit-collection.log        # 当前 124 个 Unit/Adapter Node ID
├── mv-functional.log             # 22 项执行结果及 Version Evidence JSON
├── mv-unit.log                   # 124 项执行结果
├── cleanup.log                   # Endpoint、Cleanup SQL、Schema 残留数 0
└── checksums.sha256              # 上述八个证据文件的 SHA-256
```

合入前的分支提交 `79ad341eb5f48f4c8697d66c5a0281f17dae02bd` 还执行了完整
Unit Suite `314 passed` 和 Doris 4.1.3 完整 Functional Suite `142 passed`。该次
工作树另有一项与运行时代码和测试无关、未提交的 Incremental 文档差异，因此不把它
称为 clean run；提交本身与 main 的 `7a362c8` 具有相同 Git Tree
`2bdcbead666d01673e276fd3634526757497eeda`；这里把它作为相同代码内容的全套回归
证据，不误写成直接在 Merge Commit 上执行。

### 5.2 历史五版本结果

以下矩阵来自历史基线 `f5e30c64ef7eb8320cf359c3d96cf62b595faf00`。每个版本
执行当时的 21 项套件，不包含 MV-E2E-022 Grants Case：

| Doris | FE/BE 完整 Version | 结果 | Skip | 耗时 |
| --- | --- | ---: | ---: | ---: |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 21 passed | 0 | 121.07s |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 21 passed | 0 | 118.73s |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 21 passed | 0 | 105.66s |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 21 passed | 0 | 105.94s |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 21 passed | 0 | 109.19s |
| **合计** |  | **105 passed** | **0** | **560.59s** |

每个版本的历史证据目录都包含：

```text
/mnt/disk1/chenjunwei/dbt-doris-mv-version-e2e/evidence/<version>/
├── environment.log       # Git、Python、dbt、pytest、FE/BE 版本和 Alive 状态
├── mv-functional.log     # 历史 21 个 Case 的逐项结果及 Version Evidence JSON
└── cleanup.log           # 对应测试 Schema 前缀的残留检查
```

## 6. 清理验证

当前 Doris 4.1.3 定向测试完成后执行：

```sql
SHOW DATABASES LIKE 'dbt_adapter_mv_record_e2e%';
```

返回 0 行。历史五版本批次则分别执行：

```sql
SHOW DATABASES LIKE 'dbt_adapter_mv_<version>_e2e%';
```

五个版本的返回行数也均为 0。历史检查完成后关闭对应 FE/BE。以上结论只针对专项
测试创建的 Schema，不表示扫描或删除机器上其他用户的 Doris 数据。

## 7. 当前 124 个 Unit/Adapter Item 完整清单

### 7.1 文件与覆盖分组

| 范围 | Test Function | pytest Item | 覆盖 |
| --- | ---: | ---: | --- |
| `test_materialized_view.py` | 62 | 119 | SQL/Hash/动作/Docs 26，生命周期/Task/Hook/Grants/恢复/类型切换 24，DDL 配置 54，Relation/版本/Drop/Rename 15 |
| `test_adapter_config.py` | 4 | 4 | 字段注册、默认值、完整配置反序列化、Schedule 类型校验 |
| `test_relation.py` MV Selector | 1 | 1 | MV → View 时 Relation Cache 只保留一个 Key |
| **合计** | **67** | **124** |  |

其中配置注册测试还明确确认已删除的 `refresh_on_run` 和 `refresh_partitions` 不会重新
进入 dbt Model Config。以下清单按“展开 Item 数 + Test Function”记录全部 67 个
直接相关 Test Function；参数化明细可用第 2.2 节的 `--collect-only -q` 命令展开：

```text
# test/unit/test_materialized_view.py：62 functions / 119 items
1  test_create_manual_materialized_view_uses_safe_defaults
1  test_definition_hash_changes_with_model_sql_or_ddl_config
12 test_materialized_view_action_is_idempotent_and_honors_change_policy
1  test_materialized_view_change_policy_can_fail_the_run
1  test_definition_match_reads_the_hash_from_show_create
3  test_manual_refresh_sql_uses_the_configured_refresh_method
1  test_core_materialized_view_dispatch_helpers_use_doris_ddl
1  test_replace_sql_uses_doris_atomic_materialized_view_swap
1  test_deployment_complete_sql_replaces_the_pending_hash_marker
1  test_relation_description_is_persisted_only_when_relation_docs_are_enabled
1  test_column_descriptions_are_rendered_in_create_materialized_view
1  test_column_description_changes_the_hash_only_when_column_docs_are_enabled
1  test_column_doc_quote_semantics_are_part_of_the_definition_hash
1  test_materialization_first_immediate_run_builds_before_exposing_the_target
1  test_materialization_first_deferred_run_creates_the_target_without_waiting
1  test_unchanged_manual_materialized_view_refreshes_and_waits
2  test_unchanged_database_triggered_materialized_view_is_a_no_op
1  test_outside_pre_hook_runs_before_show_create_definition_inspection
1  test_invalid_grants_fail_before_definition_inspection_or_target_ddl
1  test_materialization_definition_change_builds_then_atomically_swaps
1  test_materialization_does_not_swap_when_immediate_build_fails
1  test_wait_for_refresh_can_be_explicitly_disabled
1  test_manual_refresh_can_submit_without_waiting_for_the_task
1  test_wait_for_refresh_reports_when_the_new_task_never_appears
1  test_materialization_removes_a_stale_intermediate_before_recovery
1  test_pending_replace_rolls_back_preserved_old_mv_before_retrying
1  test_materialization_restores_a_backup_before_retrying_a_type_switch
1  test_failed_inside_post_hook_leaves_the_deployment_marker_pending
2  test_pending_deployment_forces_recovery_before_change_policy
1  test_pending_type_switch_backup_is_retained_until_recovery_completes
1  test_materialization_full_refresh_replaces_even_with_continue_policy
2  test_materialization_replaces_a_different_relation_type
1  test_table_materialization_preserves_existing_mv_until_table_is_ready
1  test_view_materialization_drops_an_existing_mv_through_the_adapter
1  test_create_scheduled_materialized_view_renders_doris_options_in_order
1  test_top_level_replication_num_is_rendered_as_a_materialized_view_property
1  test_top_level_replication_num_overrides_and_merges_with_properties
1  test_top_level_replication_num_changes_the_materialized_view_definition_hash
1  test_replication_num_integer_and_trimmed_string_are_canonical
1  test_equivalent_identifier_and_bucket_configs_have_one_definition_hash
1  test_single_partition_list_matches_string_in_sql_and_definition_hash
4  test_partition_list_requires_exactly_one_non_empty_string
6  test_partition_by_accepts_identifier_and_function_call_shapes
7  test_partition_by_rejects_non_identifier_or_function_call
1  test_create_on_commit_materialized_view_renders_doris_trigger
1  test_create_schedule_rejects_test_only_seconds
1  test_create_sql_escapes_comments_properties_and_identifiers
1  test_invalid_build_mode_fails_before_sql_execution
8  test_invalid_refresh_config_fails_before_sql_execution
5  test_invalid_distribution_config_fails_before_sql_execution
12 test_invalid_ddl_config_fails_before_sql_execution
1  test_grants_are_valid_materialized_view_config
1  test_relation_listing_queries_async_materialized_view_metadata
1  test_catalog_reports_async_materialized_views_as_materialized_views
1  test_relation_listing_returns_empty_when_schema_does_not_exist
1  test_adapter_maps_async_materialized_views_to_the_dbt_relation_type
5  test_materialized_view_version_contract_accepts_configured_gate_versions
2  test_materialized_view_version_contract_rejects_versions_outside_gate
1  test_materialized_view_version_contract_uses_the_connected_frontend
1  test_materialized_view_version_contract_also_validates_master_frontend
1  test_drop_async_materialized_view_uses_doris_two_word_relation_type
1  test_rename_async_materialized_view_uses_doris_ddl

# test/unit/test_adapter_config.py：4 functions / 4 items
1  test_doris_config_registers_materialized_view_fields_with_dbt
1  test_doris_config_materialized_view_defaults_match_macros
1  test_doris_config_accepts_complete_materialized_view_configuration
1  test_doris_config_refresh_schedule_must_be_a_mapping

# test/unit/test_relation.py：仅列直接相关 Selector，1 function / 1 item
1  test_materialized_view_to_view_replacement_updates_one_cache_key
```

## 8. Refresh Task 测试边界

### 8.1 当前已经覆盖

当前 Doris `REFRESH MATERIALIZED VIEW` 通过 MySQL Protocol 返回 OK，不直接返回
TaskId。Adapter 的现有实现先读取该 MV 的旧 TaskId 集合并提交 Refresh；等待期间
每轮重新扫描该 MV 的全部 Task，再选择旧集合之外排序最前的任务读取状态。当前测试
覆盖：

- Manual 定义未变时由 `dbt run` 提交 Refresh；Schedule/Commit 由 Doris 管理；
- `wait_for_refresh=true` 等待新 Task 出现并轮询到 Success；
- Running、Failed、Task 未出现超时，以及 `wait_for_refresh=false` 只提交不等待；
- Build/Refresh 失败时不发布错误的新定义，重试能够恢复。

当前测试均为单线程，没有覆盖两个客户端对同一个 MV 并发 Refresh。因此“旧/新 TaskId
集合差”不能作为并发场景下精确关联本次请求的保证；轮询期间还可能改选后来出现、排序
更靠前的新任务。`SHOW LAST INSERT` 返回当前 Session 最近一次 Insert 的
TransactionId，不是 MV Refresh TaskId，当前实现也没有使用它。

### 8.2 待 Doris 提供精确 TaskId 接口后补测

以下是未来验收项，不属于当前 146 个测试：

1. 如果新增可选的 `REFRESH ... RETURNING TASK_ID`，不带选项的旧语句仍返回 OK；
   带选项时 MySQL CLI、JDBC 和 Python DB-API 都能读取标准单行 ResultSet。
2. 返回的 TaskId 必须能在 `tasks('type'='mv')` 中精确查到，并匹配目标 Catalog、
   Database、MV 和本次 Refresh。
3. 两个客户端并发刷新同一 MV 时，各自得到自己的 TaskId；Adapter 只轮询
   `WHERE TaskId = <returned_id>`，不再依赖集合差。
4. 覆盖 Success、Failed、Canceled、未知状态、Task 暂不可见、等待超时和提交失败；
   提交失败不得返回或记录伪 TaskId。
5. `wait_for_refresh=false` 仍应取得并写入 Adapter Response 中的 TaskId，只是不等待
   任务结束。
6. 连接 Follower/Observer 并由 FE 转发给 Master 时，TaskId 必须原样返回；连接池和
   多 FE 场景不得串到其他请求。
7. Adapter 需要对旧 Doris 做明确的版本/能力判断；若继续回退到集合差，文档和测试
   必须保留其并发限制。
8. 精确 Refresh TaskId 只解决已有 Manual MV 的 Refresh。`BUILD IMMEDIATE` 的
   Create/Replace Build Task 仍需独立返回接口，否则该路径仍使用集合差。

如果 Doris 最终采用 Session 级 `SHOW LAST MATERIALIZED VIEW REFRESH` 而不是
ResultSet 返回，还必须增加“同一物理 Session、连续两次 Refresh、中间插入无关 SQL、
连接池换连接和 FE 转发”测试。

## 9. 版本通过标准与结论

从当前清单开始，一个 Doris 精确版本只有同时满足以下条件，才能标记为“当前完整 MV
测试清单已验证”：

1. FE/BE 完整 Version 与目标发行版一致且全部 `Alive=true`；
2. dbt Core 1.12 环境实际收集到当前 22 个 MV Functional Item；
3. 22 项全部 Pass，没有 Skip、Xfail 或重跑后通过；
4. 同一 Adapter Git Tree 实际收集到 124 个直接相关 Unit/Adapter Item，且全部通过；
5. 两次 pytest 的退出码都为 0；
6. 测试 Schema 残留为 0；
7. 日志能追溯到精确 Adapter Git SHA、Git Tree 和环境版本。

按该口径，当前 22 个 Functional Item 和 124 个 Unit/Adapter Item 已直接验证的
精确 Doris 版本是 `doris-4.1.3-rc02-7126cf65d96`。2.1.11、3.0.8、3.1.4 和
4.0.7 有历史 21 项 Functional 全通过证据，但尚未补跑新增的 MV Grants Case 和
当前 Unit/Adapter 清单，不能写成当前 146 项已全部验证。

这不自动证明所有更高版本都兼容。同一发行线的后续 Patch 版本可以视为预期兼容，
但必须重新执行当前 22 个 Functional Item 后，才能把该精确版本加入当前完整验证表。
