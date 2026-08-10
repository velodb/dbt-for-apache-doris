# dbt-doris 状态与 TODO

本文档同时记录当前实现已具备的能力和后续工作。迁入的代码审核基线为源仓
`d84328221e5413b587c94663cfae1141bb7bdd04`，包含 Incremental/Microbatch
[源仓 PR #2](https://github.com/xylaaaaa/dbt-doris-adapter/pull/2) 和 Failure-safe
Snapshot [源仓 PR #3](https://github.com/xylaaaaa/dbt-doris-adapter/pull/3)。

状态规则：

- `[x]`：代码已经合入 `main`，并有对应测试或明确的构建验证；
- `[ ]`：尚未进入 `main`，或者只有历史/分支证据，仍需完成；
- 分支或历史提交中存在代码不等于仓库已经完成；只有合入 `main` 并在合并态验证的
  能力才能打勾。

当前结论：核心 Adapter、四种 Incremental 策略、Async MV 和 Failure-safe Snapshot
均已进入 `main`。仓库仍处于 Beta 阶段，主要缺口已经转为真实 Doris CI、当前主线的
完整五版本矩阵、生产连接能力和可重复发布流程。

## 1. 当前基线

### 1.1 运行时、依赖与构建

- [x] 以 dbt Core 1.12.x 为开发和测试基线。
- [x] 支持 Python 3.10+，当前 CI 覆盖 Python 3.10 和 3.14。
- [x] 完成 Adapter 注册、Credentials、Connection Manager、Relation 和基础
  Catalog/Metadata 接口适配。
- [x] 更新 dbt Core 1.12 所需的 Adapter API、Macro Dispatch 和测试接口。
- [x] 构建 sdist/wheel，并通过 Twine、干净环境安装、关键 Macro、插件注册和
  `pip check` 验证。
- [x] GitHub Actions 执行 Flake8、Unit Test 和分发包构建。
- [ ] 在 GitHub Actions 中启动真实 Doris 并执行 Functional Test。
- [ ] 修复当前静态类型检查发现的问题，并将 mypy 加入 CI。

当前 Credentials 只覆盖 Host、Port、User、Password、Schema 和 Charset。SSL、
Timeout、Retry 与多 FE Failover 仍属于生产能力 TODO。

### 1.2 dbt Materialization 与 Resource

- [x] `view`：创建、重复运行、Docs、Grants 和基础 Relation 类型识别。
- [x] `table`：Duplicate/Unique Key 基础 DDL、Full Refresh、Contracts、Docs 和
  Grants。
- [x] `incremental`：`append`、`merge`、原生 `insert_overwrite`、`microbatch`
  和 `on_schema_change`。
- [x] `ephemeral`：使用 dbt Core CTE 编译和 `ref()` 行为。
- [x] `materialized_view`：映射 Doris Async Materialized View；Sync MV/Rollup
  不在当前支持范围。
- [x] `seed`：CSV 导入、类型配置及 Model 引用。
- [x] `snapshot`：Check/Timestamp Strategy、三种 Hard Delete 模式、复合 Key、
  自定义 Meta Column、保守 Schema Evolution、源数据校验、Persist Docs、Grants、
  原子替换和失败恢复。
- [x] Data Test 与 `--store-failures` 基础生命周期。
- [ ] 消除目标物化为 View 时 Table/MV 到 View 的先 Drop 后 Create 窗口，以及其他
  跨类型替换中 Canonical Relation 短暂不可用的窗口。

## 2. 已完成的功能

### 2.1 Incremental 基础策略

- [x] 接入 dbt 1.12 Incremental Strategy Dispatch。
- [x] `append`：Duplicate Key 表执行单条 `INSERT INTO`。
- [x] `merge`：MOW/MOR Unique Key 表执行完整行 `INSERT INTO` Upsert；支持
  单列/复合 Key、可见 Sequence 列和目标表模型校验。
- [x] `insert_overwrite`：Doris 原生整表、静态分区和动态分区覆盖。
- [x] 首次运行、普通增量、Full Refresh 和 Schema Change 复用一致的 Key、
  Partition、Distribution 与 Properties 处理。
- [x] `on_schema_change` 支持 `ignore`、`fail`、`append_new_columns` 和
  `sync_all_columns`。
- [x] 普通四种内置策略使用逻辑 `__dbt_tmp` View 和单条最终 DML，避免把源批次
  物理写入两次。
- [x] `delete+insert`/`delete_insert` 在 Hook、DDL 或 DML 前明确拒绝，并提示使用
  `merge`。
- [x] `insert_overwrite + unique_key` 在写入前拒绝，避免旧 Upsert 语义静默变成
  覆盖删除语义。
- [x] 在原生 Partial Merge 实现前明确拒绝 `merge_update_columns`、
  `merge_exclude_columns` 和 `incremental_predicates`。
- [x] `microbatch`：支持 dbt Core UTC `hour/day/month/year` 时间窗口，每个批次
  对精确命名 RANGE 分区执行一次 `INSERT OVERWRITE`，并正确清空空批次；支持
  Adapter 管理的静态分区和已有精确分区的 Dynamic Partition 表。
- [ ] Microbatch 并行批次；当前 Adapter 明确不声明
  `MicrobatchConcurrency`，即使项目启用并发配置也保持串行。
- [ ] 补齐 Incremental 与其他 Materialization 的全部 Relation 类型切换覆盖。
- [ ] 将旧 `partition` Materialization 的剩余能力收敛到
  `incremental_strategy='insert_overwrite'`，并保留兼容迁移说明。

Microbatch 的 `event_time`、`begin`、`batch_size`、`lookback` 和 CLI 时间边界由
dbt Core 编排。Adapter 负责把每个批次映射到 Doris 精确 RANGE 分区；并行批次在
正确性、失败恢复和连接隔离验证完成前不开放。

### 2.2 Async Materialized View

- [x] 支持 `CREATE MATERIALIZED VIEW ... AS ...`、`ref()`、`source()`、Alias
  和目标 Schema。
- [x] 支持 `BUILD IMMEDIATE/DEFERRED`、`REFRESH AUTO/COMPLETE` 和
  `ON MANUAL/SCHEDULE/COMMIT`。
- [x] 支持 Schedule、Partition、Distribution、Buckets、Properties 和
  Replication 配置。
- [x] 支持 MV Relation 识别、Drop/Rename，以及 Table/View/MV 类型切换。
- [x] 使用归一化定义 Hash 和 `on_configuration_change` 判断更新或重建。
- [x] 使用临时 MV 和 `REPLACE WITH MATERIALIZED VIEW` 完成配置变化时的原子
  替换，并覆盖 Hook 失败恢复与回滚。
- [x] `ON MANUAL` 重复运行时提交 Refresh，并支持等待、超时和轮询；Schedule/
  Commit 由 Doris 管理。
- [x] Persist Docs、Grants、Hooks 和刷新状态测试已覆盖 Async MV。
- [ ] 解决同一 MV 并发刷新时 TaskId 只能通过任务集合差推断的问题；多候选时不得
  静默关联错误任务。
- [ ] 补多 FE/BE 和两个独立 dbt 客户端并发刷新同一 MV 的验证。

### 2.3 dbt 通用能力

- [x] Contracts：Table、View 和 Incremental 的列名/类型合约。
- [x] Persist Docs：Relation/Column Comment 和 `catalog.json` 读取。
- [x] Source Freshness：Pass、Warn、Error 和时间戳处理。
- [x] Store Failures：失败行审计表、Schema 与清理生命周期。
- [x] Grants：Model、Incremental、Seed、Snapshot 和 MV 的授权/回收；支持
  `username` 与 `username@host`。
- [x] Pre/Post Hooks、`dbt debug`、`dbt docs generate` 和跨 Database-as-Schema
  基础行为。
- [x] 已接入 60 个 dbt 官方合约 Item，覆盖 Basic、Constraints、Current
  Timestamp、Grants、Incremental、Materialized View、Persist Docs、Store Test
  Failures，以及 Simple Snapshot 的 6 个官方 Item。
- [ ] 审计并接入 `simple_seed`、`unit_testing`、`catalog`、`hooks` 和 `relations`
  官方原始套件。
- [ ] 继续评估 `caching`、`concurrency`、`column_types`、`query_comment`、
  `dbt_debug`、`dbt_show` 和其余 Utils 官方套件。

## 3. 测试证据状态

测试数量只用于发现收集变化，不能替代通过状态。历史结果不能直接升级为迁入
实现的发布承诺；详细口径见
[总体测试方案](dbt-doris-test-plan.zh-CN.md)。

- [x] 迁入的源仓基线可收集 373 个 Unit Test；迁移前源仓最新 CI 在 Python
  3.10/3.14 均为 373 passed，Flake8、wheel/sdist 和 Twine Check 同时通过。
- [x] 迁入的源仓基线可收集 168 个 Functional Item：60 个官方合约 Item + 108 个
  Doris 专项 Item。
- [x] 迁移前源仓 `main` 的实现提交 `d843282` 与源仓 PR 3 验证 Head 的 Git Tree
  完全相同；Doris 4.1.3 上完整 Adapter Functional 为 168/168，Snapshot 专项和
  官方兼容套件为 16/16。
- [x] 迁移前源仓 Incremental 合并树在 Doris 2.1.11、3.0.8、3.1.4、4.0.7、4.1.3
  五个精确版本上分别通过 43/43 项聚焦 Functional，并完成 Version Gate 和资源
  清理验证。
- [x] 历史代码 `f5e30c64ef7eb8320cf359c3d96cf62b595faf00` 在上述五个
  版本分别通过 21 项 Async MV Functional；当前新增的第 22 项只在 4.1.3 验证。
- [ ] 对迁入的当前实现执行五个精确 Doris 版本的完整 168 项 Functional 矩阵；当前
  五版本证据只覆盖 43 项 Incremental，完整套件只覆盖 Doris 4.1.3。
- [ ] 将真实 Doris Functional 作为 PR 门禁或 Nightly CI，并保存精确 Commit、
  FE/BE 版本、测试日志和清理结果。
- [ ] 覆盖多 FE/BE、并发运行及失败注入后的资源清理。

## 4. 尚未完成的 Doris 原生与生产能力

### 4.1 Doris Table 原生能力

- [x] Duplicate/Unique Key、基础 Partition、Distribution 与 Properties 已由
  Table、Incremental 和 Full Refresh 共用同一组 DDL Macro。
- [x] 支持显式 RANGE/LIST Partition 定义，并有 Table Functional Test。
- [x] 支持 HASH Distribution 和整数 Buckets。
- [ ] 在现有基础上抽取统一、可校验的 Doris Table Config/DDL 抽象，供新增表模型
  能力复用。
- [ ] 新增 Aggregate Key 和聚合函数配置；只开放能够保证重试正确性的写入策略。
- [ ] 支持 Auto/Dynamic Partition，并补齐 Partition 配置校验与 Schema Change。
- [ ] 支持可配置 RANDOM Distribution 和 `BUCKETS AUTO`。
- [ ] 支持 Inverted、Bloom Filter、Bitmap 等索引。

### 4.2 连接、运维与可观测性

- [ ] SSL、Timeout、Retry 和多 FE Failover。
- [x] Adapter Response 返回基础 `rows_affected`；Async MV Task 可返回 Doris
  `query_id`。
- [ ] 补齐通用 Query ID、Invocation ID 和执行耗时，并明确各类语句的影响行数
  语义。
- [ ] Doris 服务端 Query Cancel；当前 `cancel()` 仅关闭客户端连接。
- [ ] External Catalog 元数据与命名空间支持。
- [ ] 明确事务边界和不支持事务时的失败语义，避免把客户端状态切换误认为 Doris
  事务回滚。

### 4.3 发布治理

- [x] CI 自动构建 wheel/sdist 并执行 Twine Check。
- [ ] 解决 PyPI 上已有 `dbt-doris==1.0.0` 与当前仓库同名同版本的问题，确定包
  所有权和下一版本号。
- [ ] 从同一 Commit 构建、测试并发布不可变制品。
- [ ] 建立 TestPyPI/PyPI、Git Tag 和 GitHub Release 流程。
- [ ] 启用 `main` 分支保护，并清理已合入的长期分支。

## 5. Milestone 路线图

核心功能和 Snapshot 安全性已经收口。后续执行顺序为：稳定性与真实 Doris CI →
可重复发布 → GA。

### M1：Beta 功能与安全性收口（已完成，2026-08-09）

目标：在迁移前源仓最新 `main` 上重建两个旧分支，形成唯一、可回归的功能基线。

- [x] 源仓 PR 2 在最新主线上重建并合入四种 Incremental 策略，不回退 Contracts、
  Grants、Persist Docs、Source Freshness、Store Failures、Async MV、测试或文档。
- [x] 源仓 PR 3 在源仓 PR 2 合并树上重建并合入 Failure-safe Snapshot：每次重新构建
  Helper/Staging Table，完整写入和验证后原子替换，并覆盖 Check/Timestamp、Hard
  Delete、Schema Evolution 和失败恢复。
- [x] 两个源仓旧 Draft PR 均转为 Ready、完成复核并合入源仓 `main`。
- [x] 源仓合并树通过 Flake8、373 项 Unit、wheel/sdist 与 Twine Check。
- [x] Doris 4.1.3 完整 Adapter Functional 168/168、Snapshot 16/16；五个精确
  Doris 版本的聚焦 Incremental 分别为 43/43。

验收结果：源仓合并态无旧功能回退；Lint、Unit、Build 全绿；Doris 4.1.3 全量
Functional 通过；Snapshot 失败注入不会丢失最后一次成功发布的 Canonical Relation。
远端开发分支清理不属于功能验收，仍保留在发布治理 TODO。

### M2：真实 Doris CI 与稳定性

目标：把人工和历史验证转化为持续门禁。

- [ ] 审计并修复目标物化为 View 时的先 Drop 后 Create，以及其他跨类型发布中的
  Canonical Relation 短暂不可用窗口。
- [ ] PR 至少运行一个当前声明支持的 Doris 版本；Nightly 运行完整精确版本矩阵。
- [ ] 补齐 P0 官方 Adapter 套件，并覆盖 Snapshot、Microbatch、Async MV 与五项通用
  能力。
- [ ] 将 README、总体测试方案和专项测试文档中的当前统计统一到
  `d843282 / 373 Unit / 168 Functional`，历史基线继续单独标注。
- [ ] 修复当前 mypy 错误并把静态类型检查加入 CI。
- [ ] CI 校验 FE/BE 版本一致，并保存 Commit、版本、日志与制品。
- [ ] 测试结束后数据库、用户、Helper Relation 和进程均无残留。
- [ ] 修复 MV Task 关联和并发运行中发现的确定性问题与 Flaky Case。

验收条件：最新 `main` 在声明矩阵全部通过；连续 Nightly 无未解释 Flake；任何失败都
能定位到精确 Commit、Doris 版本和日志。

### M3：Beta/RC 发布

目标：确保用户安装的制品与真实 Doris 验证的是同一份代码。

- [ ] 冻结 Beta API、配置名、dbt Core 依赖边界和已知限制。
- [ ] 确定 PyPI 包所有权、包名和版本策略；不得覆盖已有 `1.0.0`。
- [ ] 从同一 Commit 一次性构建 wheel/sdist，并使用该制品执行干净环境 Smoke E2E。
- [ ] 先发布 TestPyPI，验证 `dbt debug/run/test/snapshot/docs generate` 后再发布
  Beta/RC。
- [ ] 创建受保护分支、签名 Tag、GitHub Release、变更说明和回滚说明。

验收条件：Commit、Tag、包 SHA 和 CI 证据完全一致；全新环境只安装发布制品即可
通过真实 Doris Smoke Test。

### M4：GA

目标：从可发布 Beta 升级为可稳定采用的正式版本。

- [ ] 完成声明为 GA 范围的连接可靠性、服务端 Cancel 和可观测性能力。
- [ ] 关闭所有数据正确性、失败恢复和发布阻断问题。
- [ ] 明确 Aggregate Key、Sync MV、External Catalog 等能力是进入 GA 还是作为
  后续版本范围，并同步 README、兼容矩阵与限制说明。
- [ ] Beta/RC 观察期内完整版本矩阵持续稳定，无已知数据丢失或静默错误。

验收条件：支持范围、安装方式、升级/回滚路径和兼容矩阵都有可重复证据；完成正式
版本发布后，发布流水线能够从 Tag 重建相同制品。
