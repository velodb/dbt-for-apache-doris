# dbt-doris Incremental 测试方案

## 1. 目标

本方案验证 dbt-doris 的 Incremental 行为与 Doris 写入语义一致，重点回答：

1. `append`、`merge`、`insert_overwrite` 是否生成正确的 Doris DML；
2. 普通增量是否只使用逻辑临时 View，而不会把同一批数据先写入物理临时表；
3. 必须冻结批次的 Schema Change、自定义策略是否按设计使用物理 staging；
4. Canonical View 正向类型切换是否只通过专用物理 CTAS Snapshot 保护数据，
   且 Snapshot 是否先于新模型 Pre-hook、`sql_header` 和 DDL；
   Incremental/Partition 的 Legacy Backup 是否作为 Durable Marker 原名保留而不
   被重放、恢复或 Snapshot；
5. 失败、重试、Full Refresh、Relation 类型切换是否保护已有目标数据；
6. 已移除或危险的旧配置是否在 Hook 和数据写入前失败。

这里的“物理写入次数”以 dbt-doris 向 Doris 提交的数据写入语句为边界。
Doris 在 `INSERT OVERWRITE` 内部创建临时分区、写 Rowset 或发布版本，属于存储
引擎实现，不计为 dbt-doris 的第二次物化。

## 2. 测试基线与矩阵

### 2.1 验证层级

| 层级 | 基线 | 用途 |
| --- | --- | --- |
| 每次代码提交 | Python 3.10+、dbt Core 1.12.x | Unit、宏、Lint 和 Package Test |
| 发布候选 E2E | 第 2.2 节的每一个精确 Doris 版本 | 完整 Functional、关键 Incremental 和清理验证 |

单个开发集群可以帮助发现回归，但只有使用已校验官方发行包、同版本 FE/BE，并
完成第 2.3 节全部证据的运行，才能填入正式版本矩阵。

### 2.2 发布前精确版本矩阵

矩阵固定为以下公开版本。`Latest` 和 `Stable` 是 2026-08-03 采用的 Doris 发布
标签；2.1、3.0 和 3.1 三行只记录“该系列最高公开 patch 且仍有官方包”，不据此
宣称这些系列仍被维护、属于 Stable 或具有 LTS 状态。

| Doris 版本 | 发布定位 | FE/BE 完整 Version | 当前状态 |
| --- | --- | --- | --- |
| 2.1.11 | 2.1 系列最高公开 patch；仍有官方包；不宣称维护状态 | `doris-2.1.11-rc01-97b77e6cda` | passed |
| 3.0.8 | 3.0 系列最高公开 patch；仍有官方包；不宣称维护状态 | `doris-3.0.8-rc01-09b0cc49a6` | passed |
| 3.1.4 | 3.1 系列最高公开 patch；仍有官方包；不宣称维护状态 | `doris-3.1.4-rc02-7f5ba43de6` | passed |
| 4.0.7 | **Stable** | `doris-4.0.7-rc02-35854e7e92a` | passed |
| 4.1.3 | **Latest** | `doris-4.1.3-rc02-7126cf65d96` | passed |

这里的 `passed` 表示该精确版本完成了本文登记的 98 项完整 Functional、36 项
聚焦 Incremental、版本身份和清理证据；第 5 节当前登记的用例均已有对应自动化
覆盖。它仍不是对任意未列场景或其他 Doris Patch 版本的泛化兼容承诺。

发布定位与环境基线分别以 Doris 官方
[下载页](https://doris.apache.org/download/)、
[版本选择说明](https://doris.apache.org/community/release-and-verify/release-versioning/)
和 [Java 环境矩阵](https://doris.apache.org/docs/4.x/install/preparation/env-checking/)
为准。

Doris 原生 `MERGE INTO` 只在 4.1+ 提供，但当前 dbt-doris 的 `merge` 不生成
该语句，也不依赖它。当前实现对所有矩阵版本都使用 Unique Key 表的完整行
`INSERT INTO` Upsert；只有未来的条件更新、条件删除或局部列更新路径才需要单独
建立 4.1+ 原生 `MERGE INTO` 版本门禁。

如果某个版本不支持当前策略依赖的 Doris 原生能力，应增加明确的版本门禁和错误
消息，不能通过跳过测试来暗示支持。

### 2.3 每个版本必须保存的证据

每个版本独立建集群、独立运行、独立归档，缺少以下任一项都不能标记 `passed`：

1. 官方发行包的下载地址和完整文件名；保存官方公布的 SHA-512 与本地计算值，
   并记录校验为 `passed`；
2. 完整 `SHOW FRONTENDS` 与 `SHOW BACKENDS` 输出。所有存活 FE、BE 的完整
   Version 字符串必须完全一致并属于矩阵中的目标版本；Expected `0.0.0` 直接
   拒绝。混用 RC、正式版、其他 patch 或 `0.0.0`/dev 构建均不算该版本的兼容
   证据；
3. 启动 FE、BE 时使用的 `JAVA_HOME`，以及 `java -version` 返回的 JDK Vendor、
   完整版本和架构；
4. Adapter Git SHA、工作树是否 dirty、dbt Core 版本、Python 版本、机器架构和
   测试 Endpoint；
5. 完整 `test/functional/adapter` 恰好 **98 passed**，不得用选测、跳过或 xfail
   替代；
6. 关键 Incremental 套件 **36 passed**，保存默认路由、SQL 次数、MOW、MOR、
   Sequence、整表/静态/动态 Overwrite、目标表前置校验、Schema Fail/Retry、Hook、
   View Replacement 失败、失败原子性、Helper 前置条件、陈旧对象清理和 Full
   Refresh 证据；
7. 测试 Schema、辅助 Relation、FE/BE 进程和占用端口均完成清理，并记录复查结果。

Functional Session 会输出一行
`DORIS_E2E_VERSION_EVIDENCE=<JSON>`，其中包含所有 FE/BE 的精确 build string、
Adapter SHA、dirty 状态、Python、dbt Core 和 Endpoint。SHA-512、JDK、测试结果、
清理记录及日志路径仍需写入第 8 节的版本记录。

## 3. 分层测试

### 3.1 Unit 与宏测试

Unit Test 不依赖 Doris，负责尽早发现：

- 策略允许列表与默认路由错误；
- Jinja 语法、宏 dispatch、参数契约错误；
- 一个策略宏意外生成多条 SQL；
- Key、Partition、Relation 名称未正确引用；
- Schema 类型、大小写匹配和异步 Alter Job 等 Adapter 逻辑错误；
- Schema Change 等待覆盖新 Job 持续 `RUNNING`、旧 `FINISHED` Job 仍可见以及
  最新 Job 暂不可见三种超时分支，且使用确定性时钟而不真实等待；
- View Snapshot CTAS 固定 RANDOM/AUTO 与 Duplicate-without-keys，仅允许从当前
  模型配置额外携带 `replication_num` 或 `replication_allocation`，绝不从旧 View
  推断副本属性，不泄漏新模型的 Key、Distribution、Partition、Contract 或
  `sql_header`，也不声称保存 View Definition、创建时 Session 状态、Comment 或
  Grant；
- Active View Snapshot 必须排在所有新模型 Pre-hook、`sql_header` 和 DDL 之前；
  Snapshot 成功后旧 View 仍在线，直到 Replacement 构建完成；
- Generic View Rename/Exchange 明确拒绝；Incremental/Partition 的 Canonical 缺失
  时保留任意类型 Backup Marker，并保证连续失败期间 `is_incremental()` 始终为
  false，完整成功后才清理；Table/MV 则先恢复 Canonical 再重试。

执行命令：

```bash
python -m pytest -q test/unit
python -m flake8 dbt test
git diff --check
```

### 3.2 Doris Functional Test

Functional Test 必须连接隔离的 Doris 测试 Schema，并捕获 dbt `SQLQuery`
事件。目录查询只能证明临时对象最终被清理；SQL 事件用于证明运行过程中是否
创建过物理 staging、执行过几条目标 DML。

```bash
export DORIS_TEST_HOST=127.0.0.1
export DORIS_TEST_PORT=9030
export DORIS_TEST_USER=root
export DORIS_TEST_PASSWORD=''
export DORIS_TEST_SCHEMA=dbt_e2e_3_0_8
export DORIS_TEST_REPLICATION_NUM=1
export DORIS_TEST_EXPECTED_VERSION=3.0.8

# 正式版本证据必须是完整 Adapter 套件，当前预期结果恰好为 98 passed。
python -m pytest -q test/functional/adapter

# 再单独运行关键 Incremental，便于归档聚焦日志和 SQL 事件。
python -m pytest -q test/functional/adapter/test_doris_incremental.py
```

每个版本必须保存两次运行的完整输出以及
`DORIS_E2E_VERSION_EVIDENCE=<JSON>` 行。第二条命令不能替代 98 项完整套件。
当前 `test/functional` 下只有 `adapter/`，所以 umbrella 路径也会收集同样的
98 项；正式证据仍记录本轮实际执行的 `test/functional/adapter` 命令。
`DORIS_TEST_EXPECTED_VERSION` 必须设置为当前矩阵行；Session Gate 会在测试前
检查所有存活 FE/BE 的完整 Version 字符串。Expected `0.0.0` 会被拒绝；所有
存活节点必须返回完全相同的 Version，且属于该精确版本，否则停止运行。

Functional Fixture 生成最长 14 字符的 Schema Prefix，为较长的测试模块后缀
预留空间；当前最长已知生成 Database 名为 62 字符。Prefix 使用 5 位 Base-36
随机 Nonce，同一配置 Schema 身份有 60,466,176 个候选值。

共享 Relation/DDL 宏发生变化时，可再追加聚焦回归；它同样不能替代完整套件：

```bash
python -m pytest -q \
  test/functional/adapter/test_doris_table.py \
  test/functional/adapter/test_doris_view.py \
  test/functional/adapter/test_doris_partition.py
```

### 3.3 Package Test

```bash
package_output=$(mktemp -d /tmp/dbt-doris-package.XXXXXX)
wheel_venv=$(mktemp -d /tmp/dbt-doris-wheel.XXXXXX)
python -m build --outdir "${package_output}"
python -m twine check "${package_output}"/*
python3.12 -m venv "${wheel_venv}"
"${wheel_venv}/bin/pip" install "${package_output}"/*.whl
"${wheel_venv}/bin/pip" check
```

Wheel 中必须包含以下三个文件：

- `materializations/incremental/incremental.sql`
- `materializations/incremental/help.sql`
- `materializations/incremental/strategies.sql`

### 3.4 证据归档与清理

每个版本使用独立证据目录，至少保存：发行包 SHA-512 校验、JDK 输出、FE/BE
配置与日志、`DORIS_E2E_VERSION_EVIDENCE`、完整 Functional 日志、关键
Incremental 日志、失败日志和清理记录。

清理必须作为测试步骤，而不是人工口头确认：

1. 运行前记录已有 Database、FE/BE 进程和端口，避免把其他任务的资源算入本轮；
2. 运行后先枚举本轮创建的精确测试 Schema 和其中的 `__dbt_tmp`、
   `__dbt_backup`、intermediate 等辅助 Relation；
3. 只删除刚才枚举出的本轮对象，再次查询并记录残留数为 0；
4. 停止本轮 FE/BE，确认对应 PID 已退出、端口已释放；
5. 保留证据目录，不把“删除测试环境”误做成“删除测试日志”。

清理失败时该版本保持 `running` 或改为 `failed`，不能标记 `passed`。

## 4. “不物理双写”的核心验收

### 4.1 已有目标表的普通内置策略

前置条件：目标表已存在，使用内置策略，且
`on_schema_change='ignore'`。

对 `append`、`merge`、`insert_overwrite` 分别运行第二次 dbt model，捕获该
节点的全部 SQL，并同时满足：

1. 恰好出现一次 `CREATE OR REPLACE VIEW ...__dbt_tmp AS ...`；
2. 不出现 `CREATE TABLE ...__dbt_tmp`；
3. 恰好出现一条写目标表的最终 DML：
   - `append`：一条 `INSERT INTO`；
   - `merge`：一条 `INSERT INTO`，由 Unique Key 完成 Upsert；
   - `insert_overwrite`：一条 `INSERT OVERWRITE`；
4. 不出现 `DELETE FROM`，也不通过 `BEGIN` 包装多语句删除与插入；
5. 运行结束后，`information_schema.tables` 中不存在同模型的
   `__dbt_tmp`、`__dbt_backup` 等辅助 Relation；
6. 最终数据符合各策略语义。

逻辑 View 只保存查询定义。创建 View 是一次元数据 DDL，不会执行模型查询，
因此不算一次数据物化。

### 4.2 首次运行

首次运行应直接执行一次目标表 CTAS：

- 不创建逻辑 View；
- 不创建物理 staging；
- `merge` 的 Key 列按 `unique_key` 配置顺序成为物理 Schema 前缀；
- Source Key 重复时，目标表不能发布部分数据。

### 4.3 Full Refresh

Full Refresh 允许创建 physical intermediate table，但应满足：

- 模型数据只写入 intermediate table 一次；
- 后续通过 `REPLACE WITH TABLE` 或 Rename 做元数据切换；
- 不再执行一次从 intermediate 到最终表的 `INSERT`；
- 新对象准备好之前，旧目标保持可查询；
- 成功后清理旧对象，失败重试时保留或恢复唯一的好副本。

### 4.4 允许物理 batch staging 的例外

以下场景有意把 Source 批次写入 physical staging，再写目标表：

- `on_schema_change` 为 `fail`、`append_new_columns` 或
  `sync_all_columns`；
- 自定义 Incremental Strategy。

Schema DDL 会改变 Source 与 Target 的可写列集合。冻结批次可避免两次读取
得到不同数据，并让自定义策略继续使用 dbt 标准 `temp_relation` 参数。

验收时必须确认：

- staging 继承必要的 Distribution，以及 `replication_num` 或
  `replication_allocation` 中的一项；
- `fail` 在修改目标 Schema 或数据前失败；
- `append_new_columns`、`sync_all_columns` 等待 Doris Alter Job 完成后才写入；
- 成功后立即清理 staging；失败可能遗留辅助对象，但下一次运行必须先清理或
  替换旧对象，且绝不能读取上一次未完成的批次；
- 测试报告明确把这类两次物理写入标记为设计内行为，不能与普通增量混为一谈。

### 4.5 Canonical View 正向 CTAS 与 Durable Backup Marker

Doris 2.1.11 实测表明，旧 View 的查询结果可能受调用 Session 当前 `sql_mode`
影响。因此验收基线是：**绝不重放 View DDL，也不假设创建时 Session 语义被
保存**。只有 Canonical View → Table/MV/Partition 的正向类型替换使用专用物理
Snapshot：

```sql
CREATE TABLE backup
DISTRIBUTED BY RANDOM BUCKETS AUTO
PROPERTIES (
  "enable_duplicate_without_keys_by_default" = "true",
  "replication_num" = "..."
)
AS SELECT * FROM source_view;
```

必须同时满足：

1. Snapshot 必须发生在新模型任何 Pre-hook、`sql_header` 或 DDL 之前，并使用
   当时未被新模型改变的 Pre-model Session；
2. Snapshot 固定 `DISTRIBUTED BY RANDOM BUCKETS AUTO` 和
   `enable_duplicate_without_keys_by_default=true`；仅允许从当前模型配置额外携带
   `replication_num` 或 `replication_allocation`，绝不从旧 View 推断副本属性，
   且不使用新模型 Key、Distribution、Partition、Contract 或 `sql_header`；
3. CTAS 失败时 Canonical 源 View 仍在线且可查询，并证明没有执行新模型的 Hook、
   Header 或 DDL；
4. CTAS 成功后 Canonical 旧 View 仍在线，直到 Replacement Relation 构建完成；
   之后才 Drop View 并把 Replacement Rename 为 Canonical；
5. 正向 Snapshot Relation 是物理 Table，只保存 Pre-model Session 当时从旧 View
   可查询的结果数据，不保存 View Definition、创建时 Session 状态、Comment、
   Grant 或完全一致的 Schema 属性；
6. 若失败后 Canonical 旧 View 仍存在，遗留 Snapshot Marker 可在下一次尝试前
   清理并重新冻结；若失败发生在 Drop/Rename 切换窗口导致 Canonical 缺失，则
   物理 Backup 必须作为唯一旧数据副本保留。Table/MV 下一轮先恢复 Canonical；
   Incremental/Partition 才进入下述 Durable No-restore 路径；
7. Incremental/Partition 遇到“Canonical 缺失、`__dbt_backup` 存在”时，Backup
   必须保持原名和原类型作为 Durable Marker；它可以是 Legacy View、Table 或
   Async MV，Retry 不得 Restore、执行、Snapshot、Rename 或提前 Drop 它；
8. Retry 直接从 Model SQL 完整构建 Canonical。连续失败期间 Canonical 保持缺失，
   使每轮编译的 `is_incremental()` 都为 false；旧数据只在 Backup 名下可查询，
   不保证失败期间 Canonical 名可用；
9. 只有 Canonical 的 Main Build、适用的 Index/Grants/Docs/Hook 和 Commit 全部成功
   后才能 Drop Marker；Incremental/Partition 的 Legacy View Backup 永远不走
   CTAS；
10. Snapshot Helper 在源/目标同名时必须在执行任何 SQL 前失败；目标已存在时
    只允许只读 Relation 元数据查询，必须在修改 SQL 或 Drop 前失败；Generic
    View Rename/Exchange 同样在破坏性 SQL 前明确拒绝；
11. SQL Mode 用例按 Pre-model Session 当时实际查询到的行断言，并证明后续
   `sql_header` 不会反向影响已经冻结的数据；不得从 View 创建模式推导结果；
12. 这条物理 CTAS 仅是正向类型切换 Snapshot，不得被统计为普通内置 Incremental
   的 batch staging 或第二次物化。

## 5. 功能用例矩阵

| 编号 | 场景 | 关键断言 | 当前自动化 |
| --- | --- | --- | --- |
| INC-001 | 默认策略，无 `unique_key` | 路由到 `append` | Unit 与五版本 E2E 已覆盖 |
| INC-002 | 默认策略，有 `unique_key` | 路由到 `merge` | Unit 与五版本 E2E 已覆盖 |
| INC-010 | `append` 首次与二次运行 | Duplicate Key；旧行保留，新行追加；普通运行无物理 staging | 已覆盖 |
| INC-020 | MOW `merge` | 同 Key 更新、新 Key 插入、未出现旧 Key 保留；一条目标 `INSERT` | 已覆盖 |
| INC-021 | MOR `merge` | 与 MOW 相同的结果语义 | 已覆盖 |
| INC-022 | 复合 Key | 所有 Key 共同参与 Upsert 与重复检查 | 已覆盖 |
| INC-023 | Key 不在 Source 首列 | 首次 CTAS 自动调整物理列顺序 | 已覆盖 |
| INC-024 | Key 是保留字 | Unique Key 与 Distribution 正确引用 | 已覆盖 |
| INC-025 | 批内重复 Key | 同一条 DML 原子失败，目标数据不变 | 已覆盖 |
| INC-026 | 可见 Sequence 列 | 后到达的低 Sequence 不覆盖高 Sequence 行 | 已覆盖 |
| INC-027 | 隐藏 Sequence Type | 写入前拒绝 `function_column.sequence_type` | Unit 已覆盖 |
| INC-030 | 整表 `insert_overwrite` | 本批缺失的旧行被删除；无物理 staging | 已覆盖 |
| INC-031 | 静态分区覆盖 | 只替换命名分区，其他分区不变 | 已覆盖 |
| INC-032 | `PARTITION(*)` | 只动态替换本批涉及的分区 | 已覆盖 |
| INC-040 | `delete+insert` / `delete_insert` | Hook 与 SQL 写入前拒绝；目标 Relation 不存在或数据不变 | 已覆盖 |
| INC-041 | `insert_overwrite + unique_key` | 写入前拒绝并提示迁移到 `merge` 或删除 Key | 已覆盖 |
| INC-042 | `merge` 无 Key | 编译失败并给出配置示例 | Unit 已覆盖 |
| INC-043 | 不支持的 Predicate/部分列 Merge | 写入前提示需要未来原生 `MERGE INTO` | Unit 已覆盖 |
| INC-044 | 已有目标表模型或物理 Key 与策略配置不一致 | `append` 只接受 Duplicate Key；`merge` 只接受与 `unique_key` 完全一致的 Unique Key；在 Hook、staging、DML/ALTER 前失败，DDL、数据和 Helper 均不变 | 三种不一致分支经五版本 E2E 覆盖 |
| INC-050 | `ignore` 下 VARCHAR 扩容 | 大小写不敏感匹配；无物理 staging；等待 Alter 完成 | 已覆盖 |
| INC-051 | Key/Sequence 类型变化 | 修改物理不可变列前失败，提示 Full Refresh | Key E2E、Key/Sequence Unit 已覆盖 |
| INC-052 | 仅列名大小写变化 | 不误发 Add + Drop，不删除 Key | 已覆盖 |
| INC-053 | `fail` | 目标 Schema、列元数据、数据和 DDL 均不改变；零目标 DML/ALTER/交换 | 五版本 E2E 已覆盖 |
| INC-054 | `append_new_columns` | 新列添加完成后写入冻结批次 | dbt Core 契约已覆盖 |
| INC-055 | `sync_all_columns` | Add/Drop/Type Change 后正确写入 | dbt Core 契约已覆盖 |
| INC-056 | Schema Change 冻结批次写入失败与重试 | 先物理 CTAS 冻结批次，再 ALTER + 等待，最后执行带重复 Key Guard 的 DML；JSON Parse 失败时目标数据不变且 staging 保留；重试先替换陈旧 staging，不重复 ALTER，成功后清理 | 五版本 E2E 已覆盖 |
| INC-057 | Schema Change Job 超时 | 新 Job 持续 `RUNNING`、旧 `FINISHED` Job 仍可见、最新 Job 暂不可见均给出确定性超时错误 | 三个 Unit 参数分支已覆盖 |
| INC-060 | Full Refresh | 配置保留；一次 intermediate CTAS、零 copy INSERT、一次元数据交换 | 已覆盖 |
| INC-061 | View → Table | 新模型上下文前 Snapshot；旧 View 在线完成 Replacement Build；再 Drop + Rename | 五版本正式 E2E 已覆盖 |
| INC-062 | Incremental/Partition Drop/Rename 窗口失败重试 | Canonical 缺失；物理 Table Marker 原名保留；Retry 完整构建成功后才清理 | Failure Injection 与五版本正式 E2E 已覆盖 |
| INC-063 | Incremental/Partition 陈旧 temp/intermediate/backup | Canonical 存在时清理陈旧对象；Canonical 缺失时不误删 Durable Marker | 五版本 E2E 已覆盖 |
| INC-064 | View → MV/Partition | 使用同一 Pre-model CTAS Snapshot 边界；旧 View 在线到 Replacement Build 完成 | 五版本正式 E2E 已覆盖 |
| INC-065 | View Snapshot CTAS 失败 | 源 View 保持在线；新模型 Hook/Header/DDL 均未执行 | CTAS Failure 与 Ordering 断言经五版本正式 E2E 覆盖 |
| INC-066 | Snapshot 配置与保真边界 | 固定 RANDOM/AUTO + Duplicate-without-keys；仅从当前模型携带 `replication_num` 或 `replication_allocation`；只保存结果数据 | 已覆盖 |
| INC-067 | 当前 Session SQL Mode + 首列 DOUBLE | 以 Pre-model Session 实际查询结果为准；Snapshot 后再运行新模型 `sql_header`；不声称保留创建模式 | 2.1.11 发现已修复；五版本完整与聚焦套件均通过 |
| INC-068 | Generic View Rename/Exchange | 在破坏性 SQL 前明确拒绝 | 已覆盖 |
| INC-069 | View Snapshot Helper 前置条件 | 源/目标同名时零 SQL；目标已存在时只允许只读元数据查询；两者均零修改 SQL、零 Drop | Unit 与五版本 E2E 已覆盖 |
| INC-070 | 无效 Grants | 权限名与用户校验先于目标 DML，目标数据不变 | 已覆盖 |
| INC-071 | Pre/Post Hook 失败 | Pre 失败零 staging/DML；Post 失败后 DML 可见、逻辑 View 保留；Retry 先清理并收敛 | 五版本 E2E 已覆盖 |
| INC-072 | Persistent Backup Marker 三轮运行 | Incremental/Partition 连续失败不发布 Canonical、不触碰 Backup；成功完整构建后才清理 | 真实 Doris E2E 已覆盖 |
| INC-073 | View Snapshot 后 Replacement Build 或 Pre-hook 失败 | Snapshot 先完成；旧 View 在线、物理 Backup 保留、零目标 DML；Pre-hook 失败时 Replacement CTAS 尚未开始；修正模型后 Retry 成功并清理 Helper | 两种失败分支经五版本 E2E 覆盖 |
| INC-080 | 自定义策略 | physical staging + dbt 标准五参数契约 | 已覆盖 |

## 6. 数据与失败注入

每个策略至少准备以下数据：

- 首批已有 Key、第二批更新 Key、新增 Key、第二批缺失 Key；
- 两列复合 Key，包含不同 Tenant 下相同业务 ID；
- 两行重复 Source Key；
- 高、低两个 Sequence 值；
- 两个静态分区，以及只触达一个分区的增量批次；
- `VARCHAR(5)` 目标与 `VARCHAR(40)` Source；
- 列名只改变大小写的 Source；
- 名称包含保留字、注释包含 ` AS ` 的 Relation 元数据。

失败注入至少覆盖：

- 缺失 Source Relation；
- 重复 Key；
- 不存在的 Grant User；
- Doris Schema Change Job `CANCELLED`，以及新 Job `RUNNING`、旧完成 Job 仍可见、
  最新 Job 暂不可见三类超时；
- Schema Change 已完成 ALTER 后，带重复 Key Guard 的目标 DML 因 JSON Parse
  失败；确认目标数据不变、冻结 staging 保留，Retry 替换它并收敛；
- View Snapshot CTAS 失败，以及 CTAS 成功后 Rename 失败留下 Durable Table Marker；
- Snapshot 成功、Replacement Build 或 Pre-hook 失败：旧 View 仍在线、Backup
  原名保留且零目标 DML；Retry 清理或替换陈旧 Marker 后重新 Snapshot；
- Incremental/Partition Canonical 缺失且 Legacy View/Table Backup 存在时再次
  失败，确认 Marker 原名保留、Canonical 仍缺失；随后成功运行完整构建 Canonical
  并清理 Marker；
- 无效策略和危险迁移配置。

每个失败用例都必须比较运行前后的目标数据，并检查辅助 Relation。只断言 dbt
返回失败不够，因为 Doris 的 DDL、DML 和 DCL 不由一个 dbt 事务统一回滚。

## 7. 退出标准

代码合并前必须满足：

1. Unit、Incremental Functional、受影响 Materialization 回归全部通过；
2. 三个普通内置策略均有 SQL 事件证据证明不存在 physical staging；
3. View 类型切换证明只有正向专用 CTAS Snapshot 例外，且 Snapshot 先于所有新模型
   Hook/Header/DDL，旧 View 在线直到 Replacement Build 完成；同时覆盖配置隔离、
   真实 CTAS 失败、Rename 失败和 Generic View 操作拒绝；
4. Incremental 与 Partition 的 Durable Marker 三轮用例证明 Retry 不 Restore 或
   Snapshot Backup、连续失败保持 `is_incremental()=false`、成功后才清理 Marker；
5. 本轮已覆盖的失败路径证明目标数据不发生部分更新；
6. 当前测试 Schema 与辅助 Relation 全部清理；
7. wheel/sdist 可构建，宏文件进入 wheel，`twine check` 与 `pip check` 通过；
8. 不新增 warning；现有 Pytest class-scope fixture deprecation warning 应在升级
   Pytest 10 前清理。

第 5 节当前登记的场景均已有自动化覆盖：表中标记 E2E 的项目进入下述五版本
Functional/聚焦矩阵，只标记 Unit 的项目由同一候选 SHA 的 327 项 Unit 结果覆盖。
新增场景时必须先补测试，再扩大对外声明范围。

对外声明一个 Doris 版本通过兼容验证前，该版本还必须满足：

1. 官方发行包 SHA-512 校验通过，且报告记录完整文件名和两个 Digest；
2. 所有 FE/BE 均是矩阵中的同一个精确版本，并保存完整 build string；
3. JDK、Adapter SHA、dirty 状态、dbt Core、Python、架构和 Endpoint 均已记录；
4. 完整 Functional 恰好 98 passed，聚焦 Incremental 恰好 36 passed；
5. 三个内置策略、MOW/MOR、Sequence、Overwrite 和 Full Refresh 关键断言通过；
6. 测试对象、进程和端口清理复查通过；
7. `DORIS_TEST_EXPECTED_VERSION` Gate 已拒绝 `0.0.0`，并证明所有存活 FE/BE
   返回完全一致的完整 Version 且属于目标精确版本；
8. 第 8 节对应行从 `running`/`pending` 更新为 `passed`，并登记证据目录或不可变
   归档位置。

## 8. 当前执行结果

### 8.1 历史混合集群记录

2026-08-02 基于实现提交 `aeacde2` 的历史运行实际使用了不同身份的 FE 和 BE：

| 字段 | 记录 |
| --- | --- |
| Doris FE | `doris-4.1.2-rc01-4536b29f712` |
| Doris BE | `doris-0.0.0-0a5ad292e3f`（development build） |
| Python / dbt Core | `3.12.13` / `1.12.0` |
| 完整 Doris Functional | **87 passed** |
| Unit Test | 292 passed |
| 聚焦 Incremental Functional | 25 passed |
| Table/View/Partition 受影响回归 | 12 passed |
| Flake8 / `git diff --check` | passed |
| wheel + sdist / Twine / Pip Check | passed |
| 官方包 SHA-512 / 同版本 FE-BE / JDK 记录 | 未形成正式版本证据 |

这 87 项通过证明代码路径在该精确混合开发集群上运行成功，但 FE 是
4.1.2-rc01、BE 是 `0.0.0` dev，且旧记录没有形成官方包 SHA-512、同版本节点和
完整 JDK/清理 Manifest，因此**不能**算作 Doris 4.1.2、4.1.2-rc01 或任何正式
版本的兼容证据，也不能填入下面任一正式矩阵行。

### 8.2 当前设计验证

Durable Marker、Pre-model Snapshot Ordering 与 Incremental 边界用例的最终候选
已完成本地验证：

| 套件 | 最终结果 |
| --- | --- |
| Unit Test | 327 passed，9 warnings，57.99s |
| Flake8 | passed |
| `git diff --check` | passed |
| `python -m build` | passed；在 `/tmp/dbt-doris-package-clean.tUhMxp` 生成 `dbt_doris-1.0.0` sdist 与 wheel |
| wheel | 75,660 bytes；SHA-256 `edcbc1bae94e440c7be25f71ec96b6c91e4a5e71af29604561f4d99264584725` |
| sdist | 119,127 bytes；SHA-256 `ffe4c9c41e8a7f6a24fb43935ec30535748095b2a807b634fe2266ede0b43ef9` |
| Twine 7.0.0 | sdist 与 wheel 均 PASSED |
| Python 3.12.13 全新 venv wheel 安装 | `/tmp/dbt-doris-wheel-clean-py312.lPTWhm`；passed；Adapter 从 `site-packages` 导入，三个关键 Macro 文件存在，合法策略为 `[append,merge,insert_overwrite]`，`pip check` 无损坏依赖 |

上述 Package Digest 绑定实现提交
`259b14e0ff77c1dac4c1963b918e0612b2901358` 的本轮审计，仅用于证明被测输入；后续
纯文档提交或正式发布构建会改变归档字节，不能把这里的 SHA-256 当成未来发行包
必须复现的 Digest。

Unit 与五版本 E2E 绑定测试提交
`7f6d9701140188f347e9f68a25ef9013551e4e48`；该提交只在上述实现上补充测试，不改变
打包进 wheel 的 Adapter 运行时代码。

运行环境为 dbt Core 1.12.0、Adapter 1.0.0、Python 3.12.13。完整 Functional
与聚焦 Incremental 的正式结果按版本登记在第 8.4 节。此前 Unit 321 与开发
混合集群结果早于最终调整，仅保留为历史记录。

### 8.3 旧精确版本运行（stale）

以下运行使用官方环境并曾跑绿，但验证的是已被替换的 View DDL 重放实现。CTAS
Snapshot 改变了类型切换与恢复路径，因此它们全部失效，不能作为当前候选的正式
兼容证据：

| Doris 版本 | FE / BE Build | 旧完整结果 | 旧聚焦结果 | 当前状态 |
| --- | --- | --- | --- | --- |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | `88 passed, 103 warnings, 94.25s` | `26 passed, 25 warnings, 20.06s` | stale；已由第 8.4 节结果取代 |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | `88 passed, 103 warnings, 96.13s` | `26 passed, 25 warnings, 19.61s` | stale；已由第 8.4 节结果取代 |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | `88 passed, 103 warnings, 110.53s` | `26 passed, 25 warnings, 20.62s` | stale；已由第 8.4 节结果取代 |

旧日志和 Digest 只保留审计价值，不得在 README、Release Note 或兼容矩阵中称为
当前实现 passed。

### 8.4 正式版本结果登记

截至 2026-08-03，最终 CTAS Snapshot + Durable Marker + Pre-model Ordering
实现及完整 Incremental 边界用例在五个正式版本上均已通过：

| Doris | FE/BE 完整 Version | 完整 Functional | 聚焦 Incremental | 状态 |
| --- | --- | --- | --- | --- |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 98 passed / 106 warnings / 290.51s | 36 passed / 27 warnings / 45.20s | passed |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 98 passed / 106 warnings / 143.87s | 36 passed / 27 warnings / 52.49s | passed |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 98 passed / 106 warnings / 150.81s | 36 passed / 27 warnings / 43.94s | passed |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 98 passed / 106 warnings / 138.82s | 36 passed / 27 warnings / 39.69s | passed |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 98 passed / 106 warnings / 135.13s | 36 passed / 27 warnings / 39.48s | passed |

本节的 `passed` 范围严格限定为上表两套实际运行的 E2E、精确版本 Gate、Artifact
与清理证据。第 5 节标记为 Unit-only 的检查不表示在每个 Doris 版本上单独执行，
它们由同一干净候选的 327 项 Unit 结果覆盖。

每个版本均记录 FE/BE 完整 Version 完全一致、所有节点 `Alive=true`、测试数据库
残留 0、Helper Relation 残留 0。2.1.11 暴露的调用 Session `sql_mode` 问题已由
Pre-model Ordering 修复，该版本的聚焦与完整运行均通过。Adapter 证据记录为
Git SHA `7f6d9701140188f347e9f68a25ef9013551e4e48`、dirty=`false`。每份 Functional
和聚焦日志开头的 `DORIS_E2E_VERSION_EVIDENCE` JSON 均包含
`doris_version_gate`：`expected_release` 对应矩阵版本、
`reported_build` 对应上表完整 FE/BE Version、`status=passed`。运行日志目录
模式为
`/tmp/dbt-doris-version-e2e/evidence-7f6d970/<version>/{functional.log,incremental.log,cleanup.log,version.log}`；
这是运行时审计位置，不是仓库内链接。Package Test 的最终结果见第 8.2 节。

此前从 dirty 工作树执行的五版本运行只用于预验证，现作为历史记录保留；它不是
正式发布证据，也不得覆盖本节干净提交的结果。

最终 Artifact Manifest 已将官方公布 SHA-512 与本地重新运行的 `sha512sum`
逐字节比较；下表中的 Digest 均为 `published = actual`，状态均为 passed：

| Doris | Artifact | Bytes | Published SHA-512 = actual `sha512sum` | Digest | JDK / Arch |
| --- | --- | ---: | --- | --- | --- |
| 2.1.11 | `apache-doris-2.1.11-bin-x64.tar.gz` | 2931347883 | `5464942ce430d02ebc1c1597d340b7c1129a19462ddb294d43cd74c8725824b83ae486b5d5136124e012657e3735275b6e40102c84368e3af3af1308a10e5382` | passed | Temurin 8u502-b07 / x86_64 |
| 3.0.8 | `apache-doris-3.0.8-bin-x64.tar.gz` | 4278678794 | `e4441aaf845d9e95ebefa51159316b6248f039658db2e24d0e378f56cf5b94f433a841f52a3b68c651899e214f49b75cac0e49e7ae6e53ed8a166d51dbb09b1f` | passed | Temurin 17.0.19+10 / x86_64 |
| 3.1.4 | `apache-doris-3.1.4-bin-x64.tar.gz` | 3257057437 | `0cb3d1c35372c9995c03ee69ce3717cf1bd2c9dcaff671dd45bbaefcd926fe48b7441afcae4ab8142bec19963cfe260e9ab438635c6d9f6acd094e757ddd2c59` | passed | Temurin 17.0.19+10 / x86_64 |
| 4.0.7 | `apache-doris-4.0.7-bin-x64.tar.gz` | 3681132204 | `c0e12a536a154482ad26055f459ed46fd6c705e20d5e171234c547ccdd2dd508be3d444002094b850b4adaa712489b3350a77a3fd76404681aca948f1fce3d27` | passed | Temurin 17.0.19+10 / x86_64 |
| 4.1.3 | `apache-doris-4.1.3-bin-x64.tar.gz` | 3614068121 | `265ea3324ac9db59e97bfcca452d287ae8f48f23dbdc010cafa8b32667a69adff7d4251d06d22dbf9918dc74af5b12f8655a81b6cb99152e60e45246167beab6` | passed | Temurin 17.0.19+10 / x86_64 |

### 8.5 单版本证据模板

每完成一个版本，复制并填写下面的记录；未知字段不能用推测值补齐：

```yaml
doris_version: ""
release_position: ""
status: pending  # pending | running | passed | failed
started_at: ""
finished_at: ""

artifact:
  source_url: ""
  filename: ""
  sha512_published: ""
  sha512_actual: ""
  sha512_status: ""  # passed | failed

jdk:
  java_home: ""
  vendor: ""
  version: ""
  architecture: ""

cluster:
  endpoint: ""
  frontend_build_strings: []
  backend_build_strings: []
  every_node_matches_doris_version: false

doris_version_gate:
  expected_release: ""
  reported_build: ""
  status: ""  # passed | failed

adapter:
  git_sha: ""
  dirty: null
  adapter_version: ""
  dbt_core_version: ""
  python_version: ""

tests:
  full_functional: ""       # 当前候选必须为 98 passed
  focused_incremental: ""   # 当前候选必须为 36 passed
  key_incremental_checks: ""

cleanup:
  test_schemas_remaining: null
  helper_relations_remaining: null
  fe_be_processes_remaining: null
  ports_released: null
  status: ""

evidence:
  directory: ""
  version_json_log: ""
  full_functional_log: ""
  focused_incremental_log: ""
  cleanup_log: ""
notes: ""
```
