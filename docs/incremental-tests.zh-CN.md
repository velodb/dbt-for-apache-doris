# dbt-doris Incremental 测试矩阵

本文只回答三个问题：测试矩阵是什么、测试了哪些功能、测试怎么执行。

## 1. 测试矩阵

### 1.1 当前测试数量

| 层级 | 测试入口 | 主要内容 | pytest 用例数（参数展开后） |
| --- | --- | --- | ---: |
| Functional | `test_doris_incremental.py` | 四种策略、Schema Change、失败恢复、Full Refresh 和 View → Table | 42 |
| Shared Functional | `test_doris_table.py`、`test_doris_view.py`、`test_doris_partition.py` | Incremental 复用的 Relation 和 DDL 宏 | 12 |
| Unit/Adapter | `test/unit/test_incremental.py` | Schema Change、View Snapshot 和 Adapter Helper | 21 |
| Unit/Macro | `test/unit/test_macro_behavior.py` 中的 Incremental Class/Case | 策略配置、生成 SQL、Microbatch 和 physical staging | 82 |
| Unit/Adapter | `test/unit/test_adapter_api.py`、`test/unit/test_relation.py` 中的 Incremental Case | 策略接口、批次顺序和 UTC 边界 | 3 |
| Unit/Gate | `test/unit/test_macro_syntax.py -k incremental` | 三个 Incremental Macro 的解析与 License | 6 |
| **Unit/Adapter 小计** |  | **不连接 Doris** | **112** |
| **直接 Incremental 合计** |  | **42 Functional + 112 Unit/Adapter** | **154** |
| **含共享回归合计** |  | **直接 Incremental + 12 Shared Functional** | **166** |

当前代码的 112 项 Unit/Adapter 测试为 `112/112 passed`。完整 Unit 共 281 项，
完整 Adapter Functional 共 99 项；它们包含上表中的子集，因此不再计入合计。

### 1.2 Doris 版本矩阵

| Doris | 当前套件：42 项，包含 Microbatch | 历史核心套件：36 项，不含 PR #2 后续边界 |
| --- | ---: | ---: |
| 2.1.11 | 未执行 | 36/36 passed |
| 3.0.8 | 未执行 | 36/36 passed |
| 3.1.4 | 未执行 | 36/36 passed |
| 4.0.7 | 未执行 | 36/36 passed |
| 4.1.3 | 完整 42 项未执行；Microbatch 2/2 passed | 36/36 passed |

当前 42 项套件已在本地 Doris 开发集群通过，12 项 Shared Functional 和完整 99 项
Adapter Functional 也已通过。正式版本矩阵中，4.1.3 目前只有两个 Microbatch
Case 的当前代码聚焦证据；它不能代表完整 42 项通过。

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
| INC-060 | Full Refresh | 对已有 Incremental Table 执行 `--full-refresh` 并捕获 SQL | 只执行一次 intermediate CTAS、零二次 copy INSERT、一次元数据交换；配置和数据正确 |
| INC-061 | View → Table | 先创建 View，再改成 Incremental Table | 已有 View 成功替换为 Incremental Table；最终 Relation 类型、数据正确且 Helper 清理 |
| INC-062/063/072 | Durable Marker 和恢复 | Canonical 缺失时保留 Backup，注入失败后再次运行 | 失败期间不触碰 Backup；成功完整构建后才清理；陈旧 Temp/Intermediate 最终清理 |
| INC-065/067/069/073 | View Snapshot 失败边界 | 注入 Snapshot CTAS、Replacement Build、Pre-hook、Rename 失败，并切换 Session SQL Mode | 旧 View 或 Snapshot 数据不丢失；新模型 Header/DDL 不提前执行；修正后 Retry 收敛且无 Helper 残留 |
| INC-071 | Hook 失败 | 分别注入 Pre-hook 和 Post-hook 失败，再重试 | Pre-hook 失败零写入；Post-hook 失败后 DML 已可见；重试先清理并收敛 |
| INC-080 | 自定义策略 | 自定义 Macro 读取 dbt 标准五参数，并从冻结的 physical staging 读取批次 | staging 为 keyless Duplicate + RANDOM/AUTO；结果正确；成功后 Helper 清理 |

## 3. Unit/Adapter 测试了什么

| 范围 | Item 数 | 主要内容 |
| --- | ---: | --- |
| Python Helper | 21 | Doris 类型、Schema 比较、View Snapshot、Alter Job 等待与超时 |
| 策略配置校验 | 52 | 默认路由、四种策略、Key/Sequence、Microbatch 和危险配置 |
| 策略 SQL | 27 | 单语句写入、重复 Key Guard、Overwrite、Microbatch 分区和标准参数契约 |
| Adapter、UTC、Staging | 6 | 策略允许列表、顺序批次、UTC-naive 边界和 physical staging 属性 |
| Macro 解析与 License | 6 | `incremental.sql`、`help.sql`、`strategies.sql` |
| **合计** | **112** |  |

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

## 5. 结论

多版本测试使用 `1 FE + 1 BE`、`replication_num=1`，未覆盖多 FE/BE 拓扑。

Microbatch Functional 当前验证 day 粒度的静态和 Dynamic Partition 多批路径；
hour、month、year 的 Batch ID 和边界由 Unit 覆盖。Adapter 明确不声明 Microbatch
并发能力，所有批次顺序执行。

当前代码的 42 项 Functional 和 112 项 Unit/Adapter 已全部通过；12 项 Shared
Functional 和完整 99 项 Adapter Functional 也已在本地开发集群通过，测试 Schema
和 Helper 残留为 0。Doris 4.1.3 完成了当前代码的两个 Microbatch Case，其余四个
版本以及 4.1.3 的完整 42 项尚未补跑。
