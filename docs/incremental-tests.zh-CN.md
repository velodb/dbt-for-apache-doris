# dbt-doris Incremental 测试文档

本文只回答三个问题：当前有哪些测试、测试了什么功能、这些测试怎么运行。
表中的 case 数均为 pytest 参数化展开后的数量。

## 1. 当前测试矩阵

| 测试范围 | 主要用途 | 数量 | 最近结果 |
| --- | --- | ---: | --- |
| Incremental Unit / Macro | 配置校验、宏 SQL、Adapter Helper、UTC 边界、宏解析与 License | 112（106 行为 + 6 门禁） | 112 passed |
| Incremental Functional | 在真实 Doris 上验证 Incremental 数据、DDL/DML、失败与重试 | 42 | 42 passed（本地开发集群） |
| Table / View / Partition 共享回归 | 验证 Incremental 复用的 Relation 和 DDL 宏 | 12 | 12 passed（本地开发集群） |
| 完整 Unit | 防止 Adapter 其他逻辑回归；包含上述 112 项 | 281 | 281 passed |
| 完整 Adapter Functional | 防止其他物化与 Incremental 相互影响；包含上述 42 和 12 项 | 99 | 99 passed（本地开发集群） |
| Doris 4.1.3 Microbatch 聚焦 | 从 42 项中单独运行 2 项，在正式版本上验证静态与 Dynamic Partition Microbatch | 2 | 2 passed；仅为聚焦证据，不代表 4.1.3 完整兼容 |
| CI / Package | Python 3.10、Python 3.14、wheel/sdist 构建与检查 | 3 jobs | passed |

这些范围存在包含关系，不能把数量直接相加。PR Head 的五个 Doris 正式版本完整
矩阵仍待重跑；版本状态和历史证据见
[Incremental 测试方案](incremental-test-plan.zh-CN.md)。

## 2. 功能测试矩阵

| 功能 | 测试了什么 | 怎么测试 |
| --- | --- | --- |
| 默认路由与配置 | 无 `unique_key` 走 append，有 Key 走 merge；`delete+insert`、危险 overwrite、predicates 和 partial merge 配置提前失败 | Unit 覆盖路由和全部配置拒绝；Functional 对 `delete+insert` 和危险 overwrite 检查 Hook、DDL、DML 前失败 |
| Append | 首次建表、后续追加、旧数据保留、keyless Duplicate Target | 真实运行两轮 dbt，检查结果数据和 SQLQuery 事件 |
| Merge | MOW/MOR、单 Key、复合 Key、保留字 Key、批内重复 Key 原子失败 | Unit 检查单条 `INSERT INTO` SQL；Functional 检查更新、新增、保留和失败后目标不变 |
| Sequence | 后到的低 Sequence 不覆盖已有高 Sequence；模型配置必须匹配物理 Sequence mapping | 真实 Doris 数据结果 + 目标表属性前置校验 |
| Insert Overwrite | 整表覆盖、静态命名分区覆盖、动态 `PARTITION(*)`，未触达分区保持不变 | 捕获目标 `INSERT OVERWRITE`，查询各分区最终数据 |
| Microbatch | hour/day/month/year 的精确 `[start,end)`、UTC-aware 转 Doris UTC-naive、顺序执行、静态分区创建、Dynamic Partition 解析、空批清空、lookback、Backfill、Full Refresh | Unit 覆盖四种粒度、UTC 和不声明并发能力；Functional 运行 day 多批并检查分区 SQL 和数据 |
| 临时关系与写入次数 | 普通内置策略和 `on_schema_change='ignore'` 不使用物理 batch staging；非 ignore Schema Change、自定义策略使用 physical staging；Full Refresh 使用 physical intermediate | 捕获 SQLQuery 事件，统计 CTAS、目标 DML 和 Helper Relation |
| Target Preflight | 目标表模型、物理 Key 或 Sequence 不匹配时禁止写入 | 参数化 Functional，确认 Hook、ALTER、Helper 和 DML 均未执行 |
| Schema Change | `ignore`、`append_new_columns`、`sync_all_columns`、`fail`，VARCHAR 扩宽、Key 类型保护、异步 ALTER Job 等待 | dbt Core Contract 测试 + Adapter Unit + Doris Functional |
| 失败与重试 | Pre/Post Hook、重复 Key、Schema DML、View Build/Rename 失败后的数据状态和 Helper 清理 | 注入确定性失败，再次运行并检查目标、Backup、Temp 是否收敛 |
| Full Refresh 与 View → Table | 一次 intermediate CTAS、零二次 copy INSERT、元数据交换、旧 View Snapshot 和 Durable Backup Marker | 检查 SQL 顺序、Relation 类型、最终数据以及失败后的恢复行为 |
| 自定义策略 | 自定义 Macro 读取 dbt 标准五参数，并通过冻结的物理 staging 读取批次 | Functional 实际读取五参数，并检查 staging 属性、结果和清理；Unit 的五参数兼容测试覆盖内置策略 |

主要测试代码位于：

- `test/functional/adapter/test_doris_incremental.py`：42 项真实 Doris Functional；
- `test/unit/test_incremental.py`：Schema Change、View Snapshot 和 Adapter Helper；
- `test/unit/test_macro_behavior.py`：策略配置、SQL 和 staging；
- `test/unit/test_adapter_api.py`、`test/unit/test_relation.py`：策略接口和 UTC 边界；
- `test/unit/test_macro_syntax.py`：Incremental Macro 解析与 License。

## 3. 怎么运行

### 3.1 Unit 和 Macro

完整 Unit 不需要 Doris：

```bash
python -m pytest -q test/unit
```

Unit 通过 Jinja 宏渲染、Mock Adapter 元数据和确定性时钟检查生成 SQL、配置错误、
Schema Job 状态与时间边界。

<details>
<summary>只运行 112 个 Incremental Unit / Macro case</summary>

```bash
python -m pytest -q \
  test/unit/test_incremental.py \
  test/unit/test_macro_behavior.py::TestIncrementalStrategyValidation \
  test/unit/test_macro_behavior.py::TestIncrementalStrategySql \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_preserves_replication_allocation \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_prefers_top_level_replication_num \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_is_keyless_for_non_keyable_first_column \
  test/unit/test_adapter_api.py::test_incremental_strategy_allowlist_excludes_delete_insert \
  test/unit/test_adapter_api.py::test_microbatch_batches_remain_sequential \
  test/unit/test_relation.py::test_event_time_filter_renders_utc_as_naive_doris_datetime

python -m pytest -q test/unit/test_macro_syntax.py -k incremental
```

</details>

### 3.2 Doris Functional

先准备隔离的 Doris 测试 Schema：

```bash
export DORIS_TEST_HOST=127.0.0.1
export DORIS_TEST_PORT=9030
export DORIS_TEST_USER=root
export DORIS_TEST_PASSWORD=''
export DORIS_TEST_SCHEMA=dbt_incremental_test
export DORIS_TEST_REPLICATION_NUM=1
export DORIS_TEST_EXPECTED_VERSION=3.0.8  # 改成当前目标版本
```

然后运行：

```bash
# 42 项 Incremental 聚焦测试
python -m pytest -q test/functional/adapter/test_doris_incremental.py

# 12 项共享 DDL/Relation 回归
python -m pytest -q \
  test/functional/adapter/test_doris_table.py \
  test/functional/adapter/test_doris_view.py \
  test/functional/adapter/test_doris_partition.py

# 99 项完整 Adapter Functional
python -m pytest -q test/functional/adapter
```

Functional 会真实执行 dbt，查询最终数据和物理表属性，并捕获 SQLQuery 事件判断
写入次数、SQL 顺序以及是否创建过物理 staging。版本 Gate 会检查所有存活 FE/BE
均属于 `DORIS_TEST_EXPECTED_VERSION`。

### 3.3 Lint、Package 和 CI

```bash
python -m flake8 dbt test
git diff --check
python -m build
python -m twine check dist/*
```

GitHub CI 对 Python 3.10、Python 3.14 和发行包构建执行同类检查。
