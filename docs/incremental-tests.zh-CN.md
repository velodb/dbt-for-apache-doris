# dbt-doris Incremental 测试矩阵

本文只回答三个问题：测试矩阵是什么、测试了哪些功能、测试怎么执行。

## 1. 测试矩阵

### 1.1 当前测试数量

| 层级 | 测试入口 | 主要内容 | pytest 用例数（参数展开后） |
| --- | --- | --- | ---: |
| Functional | `test_doris_incremental.py` | 四种策略、Schema Change、Grants 前置校验、失败恢复、Full Refresh 和 View → Table | 43 |
| Shared Functional | `test_doris_table.py`、`test_doris_view.py`、`test_doris_partition.py` | Incremental 复用的 Relation 和 DDL 宏 | 12 |
| Functional/Persist Docs | `test_doris_persist_docs.py` | 通用注释契约，以及 Incremental 的 Append、Merge、Sequence、失败恢复和 Microbatch | 14 |
| Unit/Adapter | `test/unit/test_incremental.py` | Schema Change、View Snapshot 和 Adapter Helper | 24 |
| Unit/Macro | `TestIncrementalStrategyValidation` | 策略配置、Microbatch、DDL Comment 解析和危险配置 | 53 |
| Unit/Macro | `TestIncrementalStrategySql` | 生成 SQL、分区覆盖和标准参数契约 | 27 |
| Unit/Macro | 三个 Incremental staging Case | keyless Duplicate、RANDOM/AUTO 和副本属性 | 3 |
| Unit/Adapter | `test/unit/test_adapter_api.py`、`test/unit/test_relation.py` 中的 Incremental Case | 策略接口、批次顺序和 UTC 边界 | 3 |
| Unit/Gate | `test/unit/test_macro_syntax.py -k incremental` | 三个 Incremental Macro 的解析与 License | 6 |
| **Unit/Adapter 小计** |  | **第一条命令 110 + Macro Gate 6** | **116** |
| **直接 Incremental 合计** |  | **43 Functional + 116 Unit/Adapter** | **159** |
| **含共享回归合计** |  | **直接 Incremental + 12 Shared Functional** | **171** |

当前代码的 116 项聚焦 Unit/Adapter 为 `116/116 passed`，完整 Unit 为
`355/355 passed`。完整 Adapter Functional 为 `153/153 passed`（207 warnings，
163.14s），其中 Persist Docs 为 `14/14 passed`。Persist Docs 和完整 Adapter
套件是更广的覆盖，不重复计入 159/171 合计。

### 1.2 Doris 版本矩阵

| Doris | 当前 PR Head 的 43 项 Incremental Functional | FE/BE Build |
| --- | ---: | --- |
| 2.1.11 | **43/43 passed** | `doris-2.1.11-rc01-97b77e6cda` |
| 3.0.8 | **43/43 passed** | `doris-3.0.8-rc01-09b0cc49a6` |
| 3.1.4 | **43/43 passed** | `doris-3.1.4-rc02-7f5ba43de6` |
| 4.0.7 | **43/43 passed** | `doris-4.0.7-rc02-35854e7e92a` |
| 4.1.3 | **43/43 passed** | `doris-4.1.3-rc02-7126cf65d96` |

五个精确版本均已完成当前 PR Head 的 43 项运行时验证，Version Gate、测试
Schema/Helper 清理均通过；历史 36 项结果不作为当前 43 项套件的通过证据。

## 2. Functional 测试了什么

| Case | 功能 | 怎么测试 | 主要通过条件 |
| --- | --- | --- | --- |
| INC-001/002/010 | 默认路由和 Append | 分别运行无 Key、有 Key模型；首次建表后再次追加；另测 keyless Duplicate Target | 无 Key → append，有 Key → merge；Append 保留旧行、加入新行；普通运行无 physical staging |
| INC-020—026 | Merge、Key 和 Sequence | 在 MOW/MOR 上运行单 Key、复合 Key、保留字 Key、重排列 Key、重复 Key 和 Sequence 数据 | 只执行一条目标 `INSERT INTO`；Upsert 结果正确；重复 Key 原子失败；低 Sequence 不覆盖高 Sequence |
| INC-028/044 | Target Preflight | 构造策略、物理 Key 或 Sequence mapping 不匹配的已有目标表 | 在 Hook、ALTER、Helper 和 DML 前失败；目标 DDL、数据不变 |
| INC-030—032 | Insert Overwrite | 分别执行整表、静态命名分区和动态 `PARTITION(*)` 覆盖 | 整表旧行被替换；静态只改命名分区；动态只改本批触达分区；无 physical staging |
| INC-034—036/038—039 | Microbatch | 对静态和 Dynamic Partition 运行多批、空批、lookback、Backfill 和 Full Refresh | 每批覆盖精确命名分区；空批清空旧行；其他分区不变；Dynamic 模式不手动 ADD |
| INC-040—041 | 废弃或危险配置 | 配置 `delete+insert`，以及 `insert_overwrite + unique_key` | 在 Hook、Helper、DDL 和 DML 前失败；提示改用 merge 或删除 Key |
| INC-050—055 | Schema Change | 运行 `ignore`、`append_new_columns`、`sync_all_columns`、`fail`，并测试 VARCHAR 扩宽、Key 类型和大小写变化 | 安全变更成功；物理 Key 类型变化提示 Full Refresh；`fail` 不改变目标；只改大小写不误发 Add/Drop |
| INC-056 | Schema Change 失败与重试 | physical staging 冻结批次后执行 ALTER，再注入目标 DML 失败并重试 | staging → ALTER/等待 → DML 顺序正确；失败时目标不部分写入；重试先替换陈旧 staging、不重复 ALTER，并清理 Helper |
| INC-060 | Full Refresh | 对已有 Incremental Table 执行 `--full-refresh` 并捕获 SQL | 普通路径只执行一次 intermediate CTAS、零二次 copy INSERT、一次元数据交换；`persist_docs.columns` 使用下述明确例外 |
| INC-061 | View → Table | 先创建 View，再改成 Incremental Table | 已有 View 成功替换为 Incremental Table；最终 Relation 类型、数据正确且 Helper 清理 |
| INC-062/063/072 | Durable Marker 和恢复 | Canonical 缺失时保留 Backup，注入失败后再次运行 | 失败期间不触碰 Backup；成功完整构建后才清理；陈旧 Temp/Intermediate 最终清理 |
| INC-065/067/069/073 | View Snapshot 失败边界 | 注入 Snapshot CTAS、Replacement Build、Pre-hook、Rename 失败，并切换 Session SQL Mode | 旧 View 或 Snapshot 数据不丢失；新模型 Header/DDL 不提前执行；修正后 Retry 收敛且无 Helper 残留 |
| INC-070 | Incremental Grants 前置校验 | 给已有目标配置不存在的 Doris 用户并捕获 SQL | 在目标 DML、Helper 和 Schema 修改前失败；目标数据与权限不变 |
| INC-071 | Hook 失败 | 分别注入 Pre-hook 和 Post-hook 失败，再重试 | Pre-hook 失败零写入；Post-hook 失败后 DML 已可见；重试先清理并收敛 |
| INC-080 | 自定义策略 | 自定义 Macro 读取 dbt 标准五参数，并从冻结的 physical staging 读取批次 | staging 为 keyless Duplicate + RANDOM/AUTO；结果正确；成功后 Helper 清理 |
| Grants | Incremental 权限增删核对 | 在 `test_doris_grants.py` 中运行 dbt Core Incremental Grants 契约 | 已有/新增 Incremental Relation 的直接用户权限按声明 Grant/Revoke；无变化时不重复 DCL |
| Persist Docs | 14 项 Relation/Column Comment | 创建和更新 Append 注释；验证 Merge、可见 Sequence、首次/Full Refresh Copy 失败恢复，以及 Microbatch + Persist Docs | 注释正确；Unique Key 与 Sequence 保留；Copy 失败不发布坏目标且 Retry 清理 Helper；Microbatch 后续分区覆盖正确 |

`persist_docs.columns=true` 的首次创建和 Full Refresh 是明确的两次物理写入例外。
Doris CTAS 不能声明列注释，因此 Adapter 先把模型写入私有 keyless
`__dbt_docs_source`，再创建带列注释的 intermediate 并复制数据，成功后才发布或
交换目标。已有目标的普通后续增量不走这条路径；`append`、`merge`、
`insert_overwrite` 和 `microbatch` 仍使用逻辑 View，并且每批只有一条目标 DML。

## 3. Unit/Adapter 测试了什么

| 范围 | Item 数 | 主要内容 |
| --- | ---: | --- |
| Python Helper | 24 | Doris 类型、Schema 比较、View Snapshot、Alter Job 等待与超时 |
| 策略配置校验 | 53 | 默认路由、四种策略、Key/Sequence、Microbatch、DDL Comment 解析和危险配置 |
| 策略 SQL | 27 | 单语句写入、重复 Key Guard、Overwrite、Microbatch 分区和标准参数契约 |
| Staging | 3 | keyless Duplicate、RANDOM/AUTO 和副本属性隔离 |
| Adapter、UTC | 3 | 策略允许列表、顺序批次和 UTC-naive 边界 |
| Macro 解析与 License | 6 | `incremental.sql`、`help.sql`、`strategies.sql` |
| **合计** | **116** | **第一条命令 110，第二条 Macro Gate 6** |

## 4. 怎么测试

### 4.1 执行 Functional

```bash
DORIS_TEST_HOST=127.0.0.1 \
DORIS_TEST_PORT=9030 \
DORIS_TEST_USER=root \
DORIS_TEST_PASSWORD='' \
DORIS_TEST_SCHEMA=dbt_adapter_incremental_e2e \
DORIS_TEST_REPLICATION_NUM=1 \
DORIS_TEST_EXPECTED_VERSION=4.1.3 \
PYTHONPATH=. python -m pytest -q \
  test/functional/adapter/test_doris_incremental.py
```

Functional 测试不是只看 `dbt run` 是否成功，还会直接查询 Doris：

```sql
SHOW CREATE TABLE <schema>.<table>;

SELECT partition_name, partition_method, partition_expression,
       partition_description
FROM information_schema.partitions
WHERE table_schema = '<schema>' AND table_name = '<table>';

SELECT table_name, table_type
FROM information_schema.tables
WHERE table_schema = '<schema>';

SHOW ALTER TABLE COLUMN FROM <schema>
WHERE TableName = '<table>' ORDER BY JobId DESC;
```

每个 Case 根据需要检查：

1. 目标数据、Relation 类型、Key、Distribution、Sequence 和 Doris DDL；
2. 分区名称、边界，以及整表/静态/动态/Microbatch 的覆盖范围；
3. SQLQuery 事件中的 CTAS、目标 DML 数量和执行顺序；
4. 普通策略是否没有 physical staging，冻结批次场景是否正确使用 staging；
5. 故障后目标和 Backup 是否保留、能否重试；
6. `__dbt_tmp`、`__dbt_backup`、Intermediate 和测试 Schema 是否清理。

### 4.2 执行 Unit/Adapter

```bash
PYTHONPATH=. python -m pytest -q \
  test/unit/test_incremental.py \
  test/unit/test_macro_behavior.py::TestIncrementalStrategyValidation \
  test/unit/test_macro_behavior.py::TestIncrementalStrategySql \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_preserves_replication_allocation \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_prefers_top_level_replication_num \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_is_keyless_for_non_keyable_first_column \
  test/unit/test_adapter_api.py::test_incremental_strategy_allowlist_excludes_delete_insert \
  test/unit/test_adapter_api.py::test_microbatch_batches_remain_sequential \
  test/unit/test_relation.py::test_event_time_filter_renders_utc_as_naive_doris_datetime

PYTHONPATH=. python -m pytest -q \
  test/unit/test_macro_syntax.py -k incremental
```

把 `-q` 改成 `--collect-only -q`，可以核对实际收集数量和 Node ID。

### 4.3 执行共享与全量验证

```bash
PYTHONPATH=. python -m pytest -q \
  test/functional/adapter/test_doris_table.py \
  test/functional/adapter/test_doris_view.py \
  test/functional/adapter/test_doris_partition.py

PYTHONPATH=. python -m pytest -q \
  test/functional/adapter/test_doris_persist_docs.py

PYTHONPATH=. python -m pytest -q test/unit
PYTHONPATH=. python -m pytest -q test/functional/adapter
python -m flake8 dbt test
git diff --check
python -m build
python -m twine check dist/*
```

本轮结果为 Shared Functional `12/12`、Persist Docs `14/14`、完整 Unit
`355/355`，以及完整 Adapter Functional `153/153`（207 warnings，163.14s）。
Lint、Build 和 Twine Check 均通过。

## 5. 结论

多版本测试使用 `1 FE + 1 BE`、`replication_num=1`，未覆盖多 FE/BE 拓扑。

Microbatch Functional 当前验证 day 粒度的静态和 Dynamic Partition 多批路径；
hour、month、year 的 Batch ID 和边界由 Unit 覆盖。Adapter 明确不声明 Microbatch
并发能力，所有批次顺序执行。

当前代码的 Incremental Functional `43/43`、聚焦 Unit/Adapter `116/116` 和
Shared Functional `12/12` 均通过；直接合计 159 项，含共享回归合计 171 项。
Persist Docs 为 `14/14`，完整 Unit 为 `355/355`，完整 Adapter Functional 为
`153/153`（207 warnings，163.14s）。Lint、Build 和 Twine Check 均通过；
4.1.3 运行后的测试 Schema 和 Helper 残留为 0。

五个版本的 FE/BE 均通过精确 Version Gate，并完成当前 43 项 Incremental
Functional。每版使用 fresh `1 FE + 1 BE`、`replication_num=1` 的隔离运行时；
运行后的测试 Schema/Helper、临时进程、监听端口和 runtime 目录残留均为 0。
