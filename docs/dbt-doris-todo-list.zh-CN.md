# dbt-doris TODO

## 已确定的方案

- 只以 **dbt Core 1.12.x** 为开发和测试基线，Python 使用 3.10+。
- 当前只开发 Python dbt Adapter，不同时开发 Fusion Adapter。
- 覆盖 dbt Core 1.12 官方五种 Model Materialization。
- Incremental 有意支持 dbt Core 1.12 中适合 Doris 的三种内置策略；
  `microbatch` 留到 P1，`delete+insert` 不支持：

| 策略 | Doris 实现 | 阶段 |
| --- | --- | --- |
| `append` | `INSERT INTO` 追加 | P0 |
| `merge` | Unique Key 表 + `INSERT INTO` Upsert | P0 |
| `insert_overwrite` | Doris 原生 `INSERT OVERWRITE` | P0 |
| `microbatch` | 按 `event_time` 拆分时间批次 | P1 |

旧实现曾把 Unique Key `INSERT INTO` Upsert 命名为 `insert_overwrite`：同 Key
行更新、新 Key 行插入、未出现的旧 Key 保留。当前已把这条路径归到 `merge`，
`insert_overwrite` 则使用 Doris 原生覆盖语义。

这里的 `merge` 指结果语义，不要求 SQL 文本必须是 `MERGE INTO`。当前复用
Doris Unique Key Upsert，在 2.1.11、3.0.8、3.1.4、4.0.7 和 4.1.3 的 E2E
矩阵中都不依赖原生 `MERGE INTO`。原生 `MERGE INTO` 仅 Doris 4.1+ 提供；以后
需要局部列更新等能力时，再建立独立的 4.1+ 路径和版本门禁。

## P0：升级到 dbt Core 1.12

- [ ] 将依赖、Adapter 版本和测试环境统一到 dbt Core 1.12.x。
- [ ] 更新已变化的 Adapter API 和宏接口。
- [x] 验证 sdist/wheel 构建与 Twine、Python 3.12 全新 venv wheel 安装、
  `site-packages` 导入、关键 Macro 文件、合法策略列表和 `pip check`。
- [ ] CI 运行 Unit Test 和真实 Doris Functional Test。
- [ ] Functional Test 至少覆盖 `dbt debug/seed/run/test/snapshot`。

## P0：覆盖官方 Model Materialization

| Materialization | 当前状态 | 下一步 |
| --- | --- | --- |
| `view` | 已实现 | 验证 dbt 1.12 生命周期、Docs、Grants 和对象类型切换 |
| `table` | 已实现 | 完善安全替换、Contracts 和 Doris Table 配置 |
| `incremental` | 部分实现 | 完善官方策略，见下一节 |
| `ephemeral` | dbt Core 提供 | 验证 CTE 编译和 `ref()`，不新增 Doris DDL |
| `materialized_view` | 已实现 | Doris Async Materialized View 生命周期已闭环；Sync MV 不在范围内 |

- [x] 测试 `view`、`table` 和 `materialized_view` 之间的
  Relation 类型切换。
- [ ] 补 Incremental 与其他 Materialization 的 Relation 类型切换覆盖。
- [ ] 将现有自定义 `partition` Materialization 的能力并入
  `incremental_strategy='insert_overwrite'`，保留兼容迁移说明。

Snapshot、Seed 和 Data Test 是独立 dbt Resource，不属于 Model
Materialization；其 Doris 兼容工作放在 P1。

## P0：完善 Incremental 基础策略

- [x] 接入 dbt 1.12 标准 Incremental Strategy Dispatch 和对应策略宏。
- [x] 未支持的策略或配置在执行 SQL 前明确报错。
- [x] 完善策略与 Doris 表模型的映射：
  - `append` 使用 Duplicate Key；
  - `merge` 使用 MOW 或 MOR Unique Key；
  - `insert_overwrite` 使用 Doris 原生整表或分区覆盖。
- [x] 首次建表、普通增量和 Full Refresh 共用 Duplicate/Unique Key DDL，
  并保留 Key、Partition、Distribution 和 Properties。
- [x] 普通三策略仅使用逻辑 `__dbt_tmp` View 获取列元数据，最终执行一条
  DML；Schema Change 和自定义策略才创建物理 batch staging table。Active View
  正向类型切换的 Pre-model CTAS Snapshot 与 Incremental/Partition 失败恢复的
  Durable Marker 是独立例外，不改变普通策略契约。

### `append`

- [x] 保留当前 Duplicate Key 表 + `INSERT INTO` 实现。
- [x] 测试首次创建、重复运行和 Full Refresh。

### `merge`

- [x] 将当前错误命名的 `insert_overwrite` 路径改为 `merge`。
- [x] 要求配置 `unique_key`，支持单列和复合 Key。
- [x] 首次运行默认创建 Merge-on-Write Unique Key 目标表，并允许显式 MOR。
- [x] 后续运行继续使用一条 `INSERT INTO`，利用 Unique Key 完成完整行
  Upsert；可见 `function_column.sequence_col` 继续由 Doris 存储层裁决，
  需要隐藏列的 `function_column.sequence_type` 明确拒绝。
- [x] 校验现有目标表的 Key 类型和 Key 列；不兼容时提示
  `--full-refresh`。
- [x] 测试更新已有行、插入新行、保留本批未出现的旧行、MOW/MOR、
  复合 Key、重复 Source Key 和 Sequence。

### `delete+insert`

- [x] 有意不实现。`delete+insert` 和 `delete_insert` 都在 Hook 或写入前拒绝，
  并提示使用 `merge`。因此 Adapter 不包含两语句事务、物理批次表或 Doris
  版本门禁。

### `insert_overwrite`

- [x] 不再要求 `unique_key`。
- [x] 实现整表覆盖：

```sql
INSERT OVERWRITE TABLE target
SELECT ...;
```

- [x] 实现指定分区和 `PARTITION(*)` 动态覆盖：

```sql
INSERT OVERWRITE TABLE target PARTITION (p1, p2)
SELECT ...;
```

- [x] 测试覆盖范围内旧数据被删除、非覆盖分区保持不变。
- [x] 明确失败清理和重试行为。

### 兼容和迁移

- [x] 旧项目的 `insert_overwrite + unique_key` 组合在写入前拒绝，避免从
  Upsert 静默变成会删除缺失行的覆盖语义；按 Key Upsert 时改为 `merge`。
- [x] 旧项目若想覆盖整表或分区，继续使用 `insert_overwrite`，并删除
  `unique_key` 以显式选择原生覆盖。
- [x] 在 README 和 Incremental 指南中写明迁移保护与原生覆盖的删除语义。

## P0：实现 Materialized View

实现独立的 `materialized='materialized_view'`，对应 Doris Async Materialized
View，不作为 Incremental Strategy。

- [x] 根据 Model SQL 生成 `CREATE MATERIALIZED VIEW ... AS ...`。入口：
  `materialized='materialized_view'`，支持 `ref()`、`source()`、Alias 和目标
  Schema。
- [x] 支持 `BUILD IMMEDIATE/DEFERRED`。入口：`build_mode`；Immediate 默认
  等待首次构建任务完成，Deferred 不发起首次构建。
- [x] 支持 `REFRESH AUTO/COMPLETE` 和
  `ON MANUAL/SCHEDULE/COMMIT`。入口：`refresh_method`、
  `refresh_trigger`、`refresh_schedule`；生产 Schedule Unit 为
  minute/hour/day/week，Adapter 拒绝测试专用的 second。
- [x] 支持刷新周期、`PARTITION BY`、Distribution、Buckets 和 Properties。
  入口：`refresh_schedule`、`partition_by`、`distribution_type`、
  `distributed_by`、`buckets`、`properties` 和 `replication_num`。
- [x] 正确识别、删除和重建 Materialized View Relation。入口：
  `mv_infos` Relation 补全、MV 专用 Drop/Rename，以及 Table/View/MV
  类型切换。
- [x] 重复执行 `dbt run` 时保持幂等；配置变化时明确更新或重建。入口：
  归一化定义 Hash、`on_configuration_change`、临时 MV 和 Doris 原子
  `REPLACE WITH MATERIALIZED VIEW`；Pre/Post Hook 与未完成部署恢复已覆盖，
  包括 Replace 后 Post-hook 失败时先原子回滚旧 MV 再重试。
- [x] 支持 `ON MANUAL` Refresh 并返回 Doris Task 状态。首次 Create/Replace
  只按 `BUILD IMMEDIATE` 等首次 Task，不额外 Refresh；定义未变时，每次选中
  Manual Model 都提交 `REFRESH MATERIALIZED VIEW ... AUTO/COMPLETE` 并默认
  等待，关闭等待时仍提交但不轮询；Schedule/Commit 未变时 Skip。
  刷新分流只由 `refresh_trigger` 决定，不提供 `refresh_on_run`。
  `BUILD DEFERRED + MANUAL` 第一次只创建、第二次运行刷新。入口：
  `wait_for_refresh`、
  `refresh_wait_timeout`、`refresh_poll_interval`；不提供指定分区刷新。
- [x] Functional Test 覆盖创建、查询、Manual Refresh、Schedule/Commit Skip、
  Deferred 第二次运行、Task 等待/只提交、配置变化和删除。入口：
  `test/functional/adapter/test_doris_materialized_view.py` 和
  `test_doris_materialized_view_basic.py`、
  `test_doris_materialized_view_complete.py` 和
  `test_doris_grants.py`；Unit Test 同时覆盖 DDL、配置校验、Docs、Grants、
  Hook、状态轮询和失败恢复。

## P0：跨 Materialization 验证与发布证据

- `persist_docs.relation/columns` 已覆盖 MV Relation/Column Comment，且仅在启用时
  纳入定义 Hash。
- Doris Grants 支持用户名和 `username@host`，并在写入前校验权限名与用户；
  Role 暂不支持，避免将继承权限误当作用户直接授权进行回收。
- 最终 CTAS Snapshot + Durable Marker + Pre-model Ordering 实现已完成正式矩阵：
  2.1.11、3.0.8、3.1.4、4.0.7、4.1.3 均为完整 Functional 98 passed、聚焦
  Incremental 36 passed。各版本 FE/BE 完整 Version 完全一致且 `Alive=true`，
  测试数据库与 Helper Relation 残留均为 0。
- Async MV 又在上述五个精确版本直接运行相同的 21 项专项 Functional Test：
  各版本均为 21 passed、无 Skip，耗时依次为 121.07s、118.73s、105.66s、
  105.94s、109.19s；测试后对应数据库残留均为 0。被测 Adapter SHA 为
  `f5e30c64ef7eb8320cf359c3d96cf62b595faf00`，测试开始时 `dirty=false`，环境为
  dbt Core 1.12.0、Python 3.11.15 和 pytest 8.4.2。
- INC-001、INC-002、INC-044、INC-053、INC-056、INC-063、INC-069、INC-071、
  INC-073 已补入五版本 E2E；INC-057 的三个确定性超时分支已由 Unit 覆盖；测试
  计划当前登记的 Incremental 场景全部自动化。
- 五版本完整 Functional 的 warnings/耗时依次为 `106/290.51s`、
  `106/143.87s`、`106/150.81s`、`106/138.82s`、`106/135.13s`；聚焦
  Incremental 依次为 `27/45.20s`、`27/52.49s`、`27/43.94s`、`27/39.69s`、
  `27/39.48s`。
  环境为 dbt Core 1.12.0、Adapter 1.0.0、Python 3.12.13；正式 Adapter SHA 为
  `7f6d9701140188f347e9f68a25ef9013551e4e48`、`dirty=false`。每份 Functional
  和聚焦日志开头的 `DORIS_E2E_VERSION_EVIDENCE` JSON 均记录对应
  `expected_release`、完整 `reported_build` 和 `status=passed`。Unit 为
  327 passed / 9 warnings / 57.99s，Flake8 与 diff check 通过；旧 dirty 工作树
  五版本运行仅作预验证和历史记录。
- Doris 2.1.11 暴露的当前 Session `sql_mode` 问题已通过 Pre-model Ordering
  修复，并由该版本的聚焦与完整套件验证通过。此前 View DDL 重放和混合集群结果
  仅保留为历史证据。
- Package 干净验证已完成，输出目录为 `/tmp/dbt-doris-package-clean.tUhMxp`：
  75,660-byte wheel SHA-256 为
  `edcbc1bae94e440c7be25f71ec96b6c91e4a5e71af29604561f4d99264584725`，
  119,127-byte sdist 为
  `ffe4c9c41e8a7f6a24fb43935ec30535748095b2a807b634fe2266ede0b43ef9`，
  Twine 7.0.0 双 PASSED。Python 3.12.13 全新 venv
  `/tmp/dbt-doris-wheel-clean-py312.lPTWhm` 完成 wheel 安装、`site-packages`
  导入、三个关键 Macro、合法策略列表和 `pip check` 验证，均为 passed。
- Adapter 绝不重放 View DDL，也不假设 View 保留创建时 SQL Mode/Session 语义。
  只有 Active Canonical View → Table/MV/Partition 的正向类型替换使用专用物理
  CTAS；它必须发生在新模型任何 Pre-hook、`sql_header` 或 DDL 之前，并固定
  `DISTRIBUTED BY RANDOM BUCKETS AUTO` 和
  `enable_duplicate_without_keys_by_default=true`，仅允许从当前模型配置额外携带
  `replication_num` 或 `replication_allocation`，绝不从旧 View 推断副本属性。
  Snapshot 不继承新模型的 Key、Distribution、Partition、Contract 或
  `sql_header`。它保存 Pre-model 当前 Session 查询旧 View 得到的数据，不保存
  Definition、创建时 Session 状态、Comment、Grant 或完全一致 Schema 属性。
- CTAS 失败时旧 View 保持在线，且不能执行新模型 Hook/Header/DDL。CTAS 成功后也
  不立即 Drop 旧 View：Replacement 构建完成前 Canonical 仍由旧 View 提供；完成
  后才 Drop View 并把 Replacement Rename 为 Canonical。物理 Snapshot Marker
  保留到完整生命周期成功后再清理。
- Replacement Build 失败但旧 View 仍在线时，Retry 清理或替换陈旧 Marker 后重新
  Snapshot；Drop/Rename 窗口失败导致 Canonical 缺失时，物理 Backup 作为唯一
  旧数据副本保留。之后按目标 Materialization 分流：Incremental/Partition 不先
  Restore，Table/MV 则先恢复 Canonical 再重试各自的类型切换。
- Incremental/Partition 在 Canonical 缺失且 `__dbt_backup` 存在时，把原名 Backup
  当作 Durable Marker 保留；它可以是 Legacy View、Table 或 Async MV。Retry 不
  Restore、Snapshot、Rename、执行或提前删除 Backup，而是从 Model SQL 完整构建
  Canonical；只有整个生命周期成功后才删除 Marker。连续失败期间 Canonical 仍
  缺失，因此下一轮 `is_incremental()` 继续为 false。旧数据仅能通过 Backup 名
  查询，不保证失败期间 Canonical 名可用；Legacy View Backup 不走 CTAS。
- Snapshot Helper 在源/目标同名时会在执行任何 SQL 前失败；目标已存在时只允许
  只读 Relation 元数据查询，零修改 SQL、零 Drop。Generic View
  Rename/Exchange 明确拒绝。SQL Mode 用例必须按 Snapshot 当时
  Pre-model Session 对旧 View 的实际查询结果断言，不能再从 View 创建模式推导
  结果；各正式版本必须按新 Ordering 重新验证。
- Incremental 与 Partition 都已增加三轮 Persistent Marker 用例：首次保留旧
  Backup、再次失败仍不发布 Canonical、最终完整构建成功后才清理 Backup。
- Snapshot 保存当时可查询的执行结果数据；Random/AUTO 与
  Duplicate-without-keys 避免把 DOUBLE 等不可作 Key/Hash 的首列误选为物理 Key
  或分桶列。
- Merge Guard 从 n+1 个保留候选中选择不与 Model 列冲突的 Validation Alias；
  Version Gate 拒绝 Expected `0.0.0`，并要求所有存活 FE/BE 的完整 Version
  字符串完全一致。
- Functional Schema Prefix 最长 14 字符，当前最长已知生成 Database 名为
  62 字符；5 位 Base-36 随机 Nonce 为每个配置 Schema 身份提供 60,466,176 个
  候选空间。
- 当前运行时 Gate 接受 2.x 中不低于 2.1.5 的版本、除 3.0.0 外的 3.x，以及
  主版本 4 及以上；模拟版本字符串的 Gate 单测只验证版本解析和 Gate 判断，
  不验证 Doris 功能兼容性。运行时 `SHOW FRONTENDS` 优先校验当前连接 FE 和
  Master FE，无法识别角色时退回首行。
- Sync Materialized View（Rollup）保持独立评估，本 TODO 不包含该能力。

## P1：完善 Incremental 高级能力

- [ ] `microbatch`：支持 `event_time`、`begin`、`batch_size`、`lookback`
  和并行批次。
- [x] `on_schema_change`：支持 `ignore`、`fail`、
  `append_new_columns`、`sync_all_columns`。
- [x] Merge 配置：`merge_update_columns`、`merge_exclude_columns` 和
  `incremental_predicates` 在原生 `MERGE INTO` 路径实现前明确报错。

## P1：补齐 dbt 通用能力

| 能力 | 大概功能 | 用户入口 |
| --- | --- | --- |
| Snapshot | 用 Check/Timestamp Strategy 保存数据历史版本，保证失败时旧历史仍可用 | `dbt snapshot` |
| Contracts | 建表前校验 Model 输出的列名和类型是否符合 YAML 声明 | `contract.enforced: true` |
| Persist Docs | 把 Model 和 Column Description 写入 Doris Comment | `persist_docs` |
| Source Freshness | 按源表最近加载时间产生 Pass、Warn 或 Error | `dbt source freshness` |
| Store Failures | 把 Data Test 失败的具体数据行保存到审计表 | `dbt test --store-failures` |
| Grants | 按 Model Config 授权并回收 Relation 的过期权限 | `grants:` |

- [ ] 接入适用的 `dbt-tests-adapter` 官方测试，作为上述能力的兼容性验收。

## P2：完善 Doris Table 原生能力

- [ ] 在 P0 已有的 Duplicate/Unique Key 支持上，抽取供 Table、Incremental
  和 Full Refresh 共用的配置与 DDL 层。
- [ ] 新增 Aggregate Key、聚合函数配置及测试；只开放能够保证正确结果的
  Incremental 策略，不默认套用 `append` 或 `merge`。
- [ ] RANGE/LIST/Auto/Dynamic Partition。
- [ ] HASH/RANDOM Distribution 和 `BUCKETS AUTO`。
- [ ] Inverted、Bloom Filter、Bitmap 等索引。

## P3：生产能力

- [x] 在最终 CTAS Snapshot + Durable Marker + Pre-model Ordering 实现上完成
  Doris 2.1.11、3.0.8、3.1.4、4.0.7 和 4.1.3 的精确版本 E2E；五个版本的完整
  Functional 98 项与聚焦 Incremental 36 项均通过，节点版本与清理证据符合要求。
- [ ] SSL、Timeout、Retry 和多 FE Failover。
- [ ] Query ID、Invocation ID、影响行数和执行耗时。
- [ ] Doris 服务端 Query Cancel。
- [ ] External Catalog 元数据支持和性能优化。
- [ ] 自动构建、测试和发布 wheel。
