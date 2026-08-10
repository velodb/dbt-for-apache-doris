# dbt-doris 总体测试方案

## 1. 目的

本文定义 dbt-doris Adapter 的总体测试策略、覆盖范围、执行分层和发布准入标准。
它是项目级测试方案，不替代 Incremental、异步物化视图等功能的专项测试文档。

总体测试由两条主线组成：

1. **dbt 官方 Adapter 合约测试**：证明 dbt-doris 对 dbt 公共行为和 Adapter
   接口的兼容性；
2. **dbt-doris 专项功能测试**：证明 Doris SQL 方言、表模型、增量策略、异步物化
   视图等 Doris 特有能力的正确性。

单独执行其中一条主线都不能作为发布依据。官方测试不能覆盖 Doris 特有行为，专项测试
也不能替代 dbt 官方兼容性合约。

## 2. 测试基线

| 项目 | 当前基线 |
| --- | --- |
| Adapter 包 | `dbt-doris 1.0.0` |
| dbt Core | `1.12.x`，以 `setup.py` 中的依赖约束为准 |
| Python | 3.10、3.11、3.12、3.13、3.14 |
| Doris 正式版本矩阵 | 2.1.11、3.0.8、3.1.4、4.0.7、4.1.3 |
| 功能测试框架 | `pytest` + `dbt-tests-adapter` |
| 默认测试库 | `dbt_test`，单副本 |

Doris 版本矩阵覆盖上表五个精确发行版本，不代表这些系列当前仍处于维护状态，也
不自动承诺同系列其他 Patch 版本兼容。新增或删除已验证版本时，必须同步修改本表、
CI/执行脚本和发布兼容性声明。

## 3. 总体分层

| 层级 | 主要目标 | 是否需要 Doris | 典型执行时机 |
| --- | --- | --- | --- |
| L0 静态与收集检查 | 风格、导入、用例可发现性 | 否 | 每次提交 |
| L1 单元测试 | Python 逻辑、宏渲染、版本门禁和异常分支 | 否 | 每个 PR |
| L2 官方 Adapter 合约测试 | dbt 公共语义兼容性 | 是 | 功能 PR、发布候选 |
| L3 Doris 专项功能测试 | Doris 特性和端到端行为 | 是 | 功能 PR、发布候选 |
| L4 多版本兼容矩阵 | 发现 Doris 版本差异和回归 | 是 | 发布候选 |
| L5 制品验证 | wheel/sdist 内容、依赖和安装可用性 | 否 | 发布候选 |

## 4. dbt 官方 Adapter 合约测试

### 4.1 接入原则

- 优先继承 `dbt.tests.adapter` 提供的官方测试类，避免复制官方实现；
- 只在 Doris 语法或对象生命周期确有差异时覆盖 fixture 或测试方法；
- 每个覆盖都要保留官方测试所验证的合约，并在代码中说明 Doris 差异；
- 不能以自研的同类测试为理由跳过官方套件；
- 暂时无法通过的官方套件必须记录为“待接入”或“不适用”，不得静默
  `skip`/`xfail`；
- dbt Core 升级时，重新审查 `dbt.tests.adapter` 新增、删除和变更的合约。

### 4.2 当前已接入

| 官方测试域 | 官方基类/套件 | 本地入口 | 当前 Item |
| --- | --- | --- | ---: |
| Basic | Simple Materializations、Singular、Ephemeral、Empty、Generic、Adapter Methods | `test_basic.py` | 7 |
| Constraints | Table/View/Incremental Columns Equal | `test_doris_contract.py` | 12 |
| Utils | `BaseCurrentTimestampNaive` | `test_doris_freshness.py` | 2 |
| Grants | Model、Incremental、Seed、Snapshot、Invalid Grants | `test_doris_grants.py` | 5 |
| Incremental | `BaseIncrementalOnSchemaChange` | `test_doris_incremental.py` | 4 |
| Materialized View | `MaterializedViewBasic` | `test_doris_materialized_view_basic.py` | 8 |
| Persist Docs | `BasePersistDocs*` | `test_doris_persist_docs.py` | 7 |
| Store Test Failures | `BaseStoreTestFailures*`、官方交互/项目级场景 | `test_doris_store_failures.py` | 9 |
| **合计** |  |  | **54** |

统计口径是 pytest 展开后的 Item：51 项直接继承 `dbt.tests.adapter` 的测试方法，另有
3 项为 Doris 子类对同名官方方法的方言适配覆盖。只复用官方 Helper、但测试方法由
dbt-doris 自己定义的 Case 不算官方 Item，例如 MV-E2E-022。当前 142 个 Functional
Item 因而分为 **54 个官方合约 Item + 88 个 Doris 专项 Item**。历史的
`19 官方 + 79 专项` 只对应早期 98 项基线，不能继续作为当前统计。

### 4.3 官方套件补齐计划

官方套件按 Adapter 已声明能力映射，不追求无条件接入所有目录。

| 状态/优先级 | 官方测试域 | 处理要求 |
| --- | --- | --- |
| 已接入 | 第 4.2 节的 Basic、Constraints、Current Timestamp、Grants、Incremental、Materialized View、Persist Docs、Store Test Failures | 持续跟随 dbt Core 版本维护 |
| P0：已有相应能力，优先审计官方映射 | `simple_seed`、`simple_snapshot`、`unit_testing`、`catalog`、`hooks`、`relations` | 先跑官方原始套件；仅对确认的 Doris 差异做最小适配 |
| P1：通用兼容性扩展 | `caching`、`concurrency`、`column_types`、`query_comment`、`dbt_debug`、`dbt_show`、其余 `utils` | 逐项确认 Adapter 能力和 Doris 行为后接入 |
| 当前不适用 | `python_model`、`dbt_clone`、`sample_mode` 等未声明支持的能力 | 在能力进入支持范围时转为 P0/P1；此前记录不适用原因 |

每次新增 Adapter 能力时，必须先检查 `dbt.tests.adapter` 是否已有对应官方套件：有则接入
官方套件，再补充 Doris 专项用例；没有才完全使用自研用例。

## 5. dbt-doris 专项功能测试

专项测试覆盖官方合约无法描述或无法充分描述的 Doris 行为。当前主要入口如下：

| 功能域 | 重点验证内容 | 测试入口 |
| --- | --- | --- |
| 连接与元数据 | Profile、连接、`dbt debug`、Catalog、缺失数据库、多类型元数据 | `test_doris_connection.py` |
| Table/View | 建表、建视图、schema/alias、类型切换、替换失败 | `test_doris_table.py`、`test_doris_view.py` |
| Doris 表属性 | Duplicate/Unique/Aggregate Key、分桶、复制数、properties | `test_doris_table.py`、`test_doris_contract.py` |
| 跨数据库行为 | 自定义 database/schema、引用和对象隔离 | `test_doris_cross_database.py` |
| 合约与文档 | Contract、列类型、注释和 persist docs 行为 | `test_doris_contract.py`、相关模型测试 |
| Seed/Snapshot | Doris 导入语法、快照建表和更新 | `test_doris_seed.py`、`test_doris_snapshot.py` |
| Freshness/Hooks/Grants | source freshness、前后置钩子、授权与回收 | `test_doris_freshness.py`、`test_doris_hooks.py`、`test_doris_grants.py` |
| Partition | 分区定义、静态/动态分区操作 | `test_doris_partition.py` |
| Incremental | append、merge、insert overwrite、schema change、full refresh、失败恢复 | `test_doris_incremental.py` |
| 异步物化视图 | 创建、刷新、等待、配置变更、回滚、版本门禁、类型切换、Grants | `test_doris_materialized_view*.py`、`test_doris_grants.py::TestDorisMaterializedViewGrants` |
| dbt Unit Test | 用户项目中的 dbt unit test 能否通过 Adapter 执行 | `test_doris_unit_test.py` |

Incremental 的完整输入组合、失败注入和验收规则见
[Incremental 专项测试方案](incremental-test-plan.zh-CN.md)。异步物化视图当前 22 个
真实 Doris Case、124 个直接相关 Unit/Adapter Item、历史五版本执行记录和未覆盖边界见
[异步物化视图专项测试说明](materialized-view-test-plan.zh-CN.md)，配置和生命周期见
[异步物化视图使用与实现说明](materialized-view.zh-CN.md)。

### 5.1 专项用例最低要求

每个功能至少覆盖：

1. 首次创建或首次执行；
2. 重复执行及幂等性；
3. 配置变更或输入数据变更；
4. 关键边界值和 Doris 版本差异；
5. 明确失败路径、错误信息和失败后的对象状态；
6. 与已有 relation 类型之间的切换；
7. 数据结果和 Doris 元数据/DDL 状态，而不只检查命令退出码。

涉及备份、重命名或替换对象的功能，还必须验证失败恢复过程中不存在静默数据丢失、残留
临时对象或不可重复执行状态。

## 6. 单元、静态和制品测试

### 6.1 单元与静态检查

```shell
python -m pytest test/unit
python -m flake8 dbt test
python -m pytest --collect-only -q test/functional/adapter
git diff --check
```

单元测试重点覆盖：

- 宏生成 SQL 的精确结构；
- relation、column、credentials 和 connection manager 行为；
- Doris 版本解析、能力门禁和错误信息；
- 无需真实集群即可稳定复现的异常与恢复分支。

### 6.2 制品验证

```shell
python -m build
python -m twine check dist/*
```

发布前还应在干净虚拟环境中安装 wheel，执行 `pip check`，并确认 wheel/sdist 包含
Adapter Python 文件、宏、`LICENSE`、`NOTICE` 和必要元数据。

## 7. 功能测试执行方式

### 7.1 环境变量

```shell
export DORIS_TEST_HOST=127.0.0.1
export DORIS_TEST_PORT=9030
export DORIS_TEST_USER=root
export DORIS_TEST_PASSWORD=''
export DORIS_TEST_SCHEMA=dbt_test
export DORIS_TEST_REPLICATION_NUM=1
```

每个 Doris 版本必须使用独立且干净的测试库。测试账号应具备用例要求的建库、建表、建
视图、授权和物化视图权限；权限不足本身是测试目标时除外。

### 7.2 全量功能测试

```shell
python -m pytest -q test/functional/adapter
```

该命令同时执行已接入的官方合约测试和 Doris 专项测试，是单一 Doris 版本上的完整功能
门禁。

### 7.3 重点功能回归

```shell
# Incremental
python -m pytest -q test/functional/adapter/test_doris_incremental.py

# 异步物化视图
python -m pytest -q \
  test/functional/adapter/test_doris_materialized_view.py \
  test/functional/adapter/test_doris_materialized_view_basic.py \
  test/functional/adapter/test_doris_materialized_view_complete.py \
  test/functional/adapter/test_doris_grants.py::TestDorisMaterializedViewGrants
```

重点回归用于开发反馈，不能替代发布前的全量功能测试。

## 8. 执行节奏

### 8.1 普通 PR

- L0 静态与收集检查全部通过；
- L1 单元测试全部通过；
- 修改已有功能时，执行对应的官方测试和 Doris 专项测试；
- 新增功能时，同时增加官方合约映射和专项测试；
- 改动打包、依赖或资源文件时，执行 L5 制品验证。

### 8.2 功能验收

- 在至少一个目标 Doris 正式版本上执行全量功能测试；
- 对改动功能执行聚焦测试和故障/恢复测试；
- 核对执行前后的数据、DDL、Catalog 和临时对象状态；
- 若功能存在明确版本分支，在分支两侧至少各选择一个正式版本验证。

### 8.3 发布候选

- Python 3.10—3.14 的 L0、L1 和制品验证通过；
- 五个 Doris 正式版本分别执行全量功能测试；
- 每个版本额外执行 Incremental 和异步物化视图聚焦回归，便于生成专题证据；
- 不允许存在未解释的 `skip`、`xfail`、重跑后通过或环境污染；
- 保存精确代码提交、依赖锁定信息、Doris 版本、执行命令、结果统计和日志位置。

## 9. 用例设计模板

新增功能或回归用例应记录以下内容：

| 字段 | 要求 |
| --- | --- |
| 功能/需求 | 指向明确的 Adapter 能力和用户行为 |
| 官方合约映射 | 对应 `dbt.tests.adapter` 套件；没有则写明“无对应官方套件” |
| Doris 专项点 | SQL 方言、表模型、版本差异或生命周期差异 |
| 前置条件 | Doris 版本、权限、会话变量、初始 relation 和数据 |
| 操作 | dbt 命令、模型配置和必要的故障注入 |
| 结果断言 | 行数据、列定义、key、partition、properties、relation 类型和错误信息 |
| 清理/恢复断言 | 临时对象、备份对象、事务外 DDL 和再次执行能力 |
| 适用版本 | 全版本或明确的版本条件及原因 |

测试应使用唯一对象名或隔离 schema，测试开始时清理自己的历史对象；失败时保留足够日志
用于定位，但后续矩阵执行前必须恢复为干净环境。

## 10. 发布准入标准

发布必须同时满足：

1. 官方合约映射表中的“已接入”套件全部通过；
2. 与本次变更相关的 P0 官方套件已经接入，或有明确的阻塞原因和跟踪项；
3. Doris 专项全量测试在五个版本上全部通过；
4. 单元、静态、收集和制品验证全部通过；
5. 无未解释的跳过、预期失败、偶现失败和残留对象；
6. 结果报告能追溯到精确 Git commit 和 Doris 正式版本；
7. README 中的兼容性声明不超出实际矩阵证据。

用例数量只用于发现测试丢失或未被收集，不能代替通过状态和行为断言。

## 11. 当前状态与后续工作

截至 2026-08-06：

- 当前功能实现基线 `7a362c89d234c0f3e6d4798a523ef7a05a57e163` 可收集
  **314 个单元测试**和 **142 个 Adapter 功能测试**；当前 Incremental 文件仍为
  **36 项**；
- 当前 Async MV 直接相关清单为 **22 个 Functional Item + 124 个 Unit/Adapter
  Item**。两组都已在 Doris 4.1.3 对应基线上通过，精确命令、日志、清理结果和
  历史五版本边界见 MV 专项测试文档；
- 合入前分支提交 `79ad341eb5f48f4c8697d66c5a0281f17dae02bd` 与当时的源仓 main
  具有相同 Git Tree；该内容的完整 Unit Suite 为 `314 passed`，Doris 4.1.3
  完整 Functional Suite 为 `142 passed`。执行时工作树另有无关的未提交文档差异，
  因此该结果作为相同运行时代码的回归证据，不标记为 clean release run；
- `7f6d9701140188f347e9f68a25ef9013551e4e48` 上的 `327 Unit / 98 Functional /
  36 Incremental` 五版本矩阵，以及 `f5e30c64ef7eb8320cf359c3d96cf62b595faf00`
  上的 `21 Async MV × 5`，均保留为历史发布证据，不能写成当前代码的收集数量；
- 当前 142 个 Functional Item 按第 4.2 节口径分为 **54 个官方合约 Item +
  88 个 Doris 专项 Item**；新增或改写继承关系后必须重新审计，不能继续沿用历史的
  `19 官方 + 79 专项` 拆分。

测试执行结果应另存结果报告：方案文档只定义应如何测试，结果报告记录某个精确提交实际
执行了什么以及是否通过。
