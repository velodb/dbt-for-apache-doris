# dbt-doris Incremental 自动化测试清单

本文逐项记录当前 PR 中直接验证 Incremental 的自动化测试。测试目标、五版本矩阵、
失败注入要求和历史执行证据仍以
[Incremental 测试方案](incremental-test-plan.zh-CN.md) 为准；本文解决的是“当前
代码里到底有哪些测试、每项测什么、pytest 实际收集多少 case”。

清单于 2026-08-06 从分支 `agent/complete-incremental-strategies` 自动收集并人工
核对。测试发生增删、重命名或参数变化时，必须同时更新本文和测试方案中的计数。

## 1. 收集口径

直接 Incremental 清单使用以下命令：

```bash
python -m pytest --collect-only -q \
  test/functional/adapter/test_doris_incremental.py

python -m pytest --collect-only -q \
  test/unit/test_incremental.py \
  test/unit/test_macro_behavior.py::TestIncrementalStrategyValidation \
  test/unit/test_macro_behavior.py::TestIncrementalStrategySql \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_preserves_replication_allocation \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_prefers_top_level_replication_num \
  test/unit/test_macro_behavior.py::TestSingleStatementDDL::test_incremental_staging_is_keyless_for_non_keyable_first_column \
  test/unit/test_adapter_api.py::test_incremental_strategy_allowlist_excludes_delete_insert \
  test/unit/test_adapter_api.py::test_microbatch_batches_remain_sequential \
  test/unit/test_relation.py::test_event_time_filter_renders_utc_as_naive_doris_datetime

python -m pytest --collect-only -q \
  test/unit/test_macro_syntax.py -k incremental
```

当前精确收集数：

| 层级 | pytest case | 唯一测试方法 | 是否需要 Doris |
| --- | ---: | ---: | --- |
| Incremental Functional | 42 | 36 | 是 |
| Incremental 行为 Unit / Macro | 106 | 65 | 否 |
| 三个 Incremental Macro 的解析与 License 门禁 | 6 | 2 | 否 |
| 合计 | 154 | 103 | - |

“pytest case”按参数化展开后计数；“唯一测试方法”按 Python 方法名计数。一个
Functional 方法可能在内部执行首次运行、普通增量、失败、重试、Backfill 和 Full
Refresh 多轮 dbt 命令，因此 case 数不等于执行的 dbt invocation 数。

## 2. Functional：42 个 case

文件：`test/functional/adapter/test_doris_incremental.py`。

除明确标记的参数化项外，每行收集 1 个 pytest case。

| 类别 | pytest 节点（省略文件前缀） | case | 核心断言 |
| --- | --- | ---: | --- |
| 默认路由 | `TestDorisIncrementalDefaultStrategy::test_default_without_unique_key_routes_to_append` | 1 | 无 `unique_key` 时使用 append，普通增量无 physical staging |
| 默认路由 | `TestDorisIncrementalDefaultStrategy::test_default_with_unique_key_routes_to_merge` | 1 | 有 `unique_key` 时使用 merge，并完成 Upsert |
| Append | `TestDorisIncrementalAppend::test_incremental_append` | 1 | 首次建表、二次追加、单目标 DML、无 physical staging |
| Append | `TestDorisIncrementalAppend::test_append_accepts_keyless_duplicate_target` | 1 | 已有 keyless Duplicate 表可以继续 append |
| Merge | `TestDorisIncrementalMerge::test_merge_upserts_without_staging` | 1 | MOW Unique Key 更新、新增、保留未触达 Key，只有一条目标 INSERT |
| Merge | `TestDorisIncrementalMergeRejectsDuplicateKeys::test_duplicate_source_keys_fail_without_changing_target` | 1 | 批内重复 Key 原子失败，目标不变 |
| Merge | `TestDorisIncrementalCompositeMerge::test_merge_uses_all_unique_key_columns` | 1 | 复合 Key 的所有列共同参与 Upsert |
| Merge | `TestDorisIncrementalReorderedKeyMerge::test_initial_ctas_places_key_first_then_incremental_upserts` | 1 | 首次 CTAS 把 Key 投影为物理 Schema 前缀，后续 Upsert 正确 |
| Merge | `TestDorisIncrementalReservedKeyMerge::test_reserved_word_key_is_quoted_in_table_ddl` | 1 | 保留字 Key 在 Key/Distribution DDL 中正确引用 |
| Merge | `TestDorisIncrementalMorMerge::test_merge_upserts_merge_on_read_target` | 1 | MOR Unique Key 与 MOW 保持相同结果语义 |
| Sequence | `TestDorisIncrementalSequenceMerge::test_lower_sequence_arriving_later_does_not_replace_row` | 1 | 后到低 Sequence 不覆盖已有高 Sequence 版本 |
| 配置拒绝 | `TestDorisIncrementalRejectsDeleteInsert::test_removed_strategy_fails_before_any_sql` | 1 | `delete+insert` 在 Hook、DDL、DML 前拒绝 |
| 配置拒绝 | `TestDorisIncrementalRejectsLegacyOverwriteUniqueKey::test_legacy_combination_fails_before_any_sql` | 1 | `insert_overwrite + unique_key` 在任何写入前拒绝 |
| 目标前置校验 | `TestDorisIncrementalTargetPreflight::test_mismatch_fails_before_hooks_staging_and_dml[...]` | 7 | 七种物理目标不一致均在 Hook、staging、ALTER、DML 前失败；分支见下表 |
| Hook/Retry | `TestDorisIncrementalHookFailures::test_pre_and_post_hook_failure_states_and_retry` | 1 | Pre-hook 零写入；Post-hook 后数据可见；Helper 与重试状态收敛 |
| Overwrite | `TestDorisIncrementalInsertOverwrite::test_whole_table_insert_overwrite_removes_old_rows` | 1 | 整表覆盖删除本批缺失旧行，无 physical staging |
| Overwrite | `TestDorisIncrementalStaticPartitionOverwrite::test_static_partition_overwrite_replaces_only_named_partition` | 1 | 只覆盖显式命名静态分区，其他分区保留 |
| Overwrite | `TestDorisIncrementalDynamicPartitionOverwrite::test_dynamic_partition_overwrite_preserves_unseen_partitions` | 1 | `PARTITION(*)` 只替换本批触达分区 |
| Microbatch | `TestDorisIncrementalMicrobatch::test_microbatch_replaces_exact_and_empty_batches` | 1 | 静态精确分区、缺分区 ADD、默认 lookback、空批清空和显式 event-time Backfill；普通增量批无 physical staging；Full Refresh 首批仅一次 intermediate CTAS，后续直接分区覆盖 |
| Microbatch | `TestDorisIncrementalMicrobatchDynamicPartitions::test_dynamic_partitions_are_resolved_without_manual_add` | 1 | Dynamic Partition 解析真实分区名、逐批命名覆盖、不手动 ADD |
| Schema | `TestDorisIncrementalVarcharWidening::test_ignore_widens_string_without_physical_staging` | 1 | `ignore` 下 VARCHAR 安全扩宽并等待 Alter，无 physical staging |
| Schema | `TestDorisIncrementalKeyWidening::test_default_ignore_rejects_unique_key_type_change_before_alter` | 1 | Unique Key 类型变化在 ALTER 前拒绝并提示 Full Refresh |
| Schema | `TestDorisIncrementalCaseOnlySchemaChange::test_case_only_alias_change_is_not_add_drop` | 1 | 仅大小写变化不会误发 Add/Drop |
| Schema/Retry | `TestDorisIncrementalSchemaChangeRetry::test_alter_precedes_dml_and_retry_replaces_frozen_batch` | 1 | physical staging 冻结批次；ALTER 先于 DML；失败保留；Retry 替换并清理 |
| Custom | `TestDorisIncrementalCustomStrategy::test_custom_strategy_uses_frozen_physical_staging` | 1 | 自定义策略使用 keyless RANDOM/AUTO physical staging 和标准参数契约 |
| Full Refresh | `TestDorisIncrementalFullRefresh::test_full_refresh` | 1 | intermediate CTAS 一次、零 copy INSERT、元数据交换、配置保留 |
| View → Table | `TestDorisIncrementalViewToTable::test_view_with_as_identifier_is_replaced_by_table` | 1 | 含 `AS` 标识符/注释的旧 View 安全替换为 Table；数据和类型正确；陈旧 Helper 清理 |
| Durable Marker | `TestDorisIncrementalBackupRecovery::test_keeps_backup_marker_until_a_full_retry_succeeds` | 1 | Canonical 缺失时保留 Legacy View Backup 的名称、类型和数据；失败后仍缺 Canonical；成功完整重建后才清理 Backup |
| View Failure | `TestDorisIncrementalViewReplacementFailures::test_snapshot_survives_replacement_build_failure_and_retry` | 1 | Snapshot 后 Replacement Build 失败仍保留旧 View/Backup，Retry 收敛 |
| View Failure | `TestDorisIncrementalViewReplacementFailures::test_snapshot_precedes_pre_hook_failure_and_retry` | 1 | Snapshot 先于 Pre-hook；Hook 失败时旧 View 在线、零目标 DML |
| Snapshot Guard | `TestDorisViewSnapshotPreconditions::test_invalid_snapshot_relations_execute_no_mutating_sql_or_drop` | 1 | 同名源/目标或已存在目标只读失败，零修改 SQL、零 Drop |
| SQL Mode | `TestDorisIncrementalViewNoBackslashMode::test_view_replacement_snapshots_under_no_backslash_mode` | 1 | 双向切换 NO_BACKSLASH/default；Snapshot 先于新 Header；CTAS 失败保源、Rename 失败保数据；Durable Marker Retry 收敛并清理 |
| Core Schema | `TestDorisIncrementalOnSchemaChange::test_run_incremental_ignore` | 1 | dbt Core `ignore` 契约 |
| Core Schema | `TestDorisIncrementalOnSchemaChange::test_run_incremental_append_new_columns` | 1 | dbt Core `append_new_columns` 契约 |
| Core Schema | `TestDorisIncrementalOnSchemaChange::test_run_incremental_sync_all_columns` | 1 | dbt Core `sync_all_columns` 契约 |
| Core Schema | `TestDorisIncrementalOnSchemaChange::test_run_incremental_fail_on_schema_change` | 1 | dbt Core `fail` 契约 |

目标前置校验的 7 个参数分支：

| 参数 ID | 物理不一致 |
| --- | --- |
| `append-rejects-unique-target` | append 指向 Unique Key 表 |
| `merge-rejects-duplicate-target` | merge 指向 Duplicate Key 表 |
| `merge-rejects-physical-key-mismatch` | 模型 `unique_key` 与物理 Unique Key 顺序/列不一致 |
| `merge-rejects-unconfigured-physical-sequence` | 物理表存在可见 Sequence mapping，模型未配置 |
| `merge-rejects-hidden-physical-sequence` | merge 目标使用隐藏 `function_column.sequence_type` |
| `overwrite-rejects-hidden-physical-sequence` | insert_overwrite 目标使用隐藏 Sequence Type |
| `merge-rejects-configured-sequence-missing-physically` | 模型配置可见 Sequence mapping，物理表不存在 |

Functional 精确合计：35 个非参数化方法 + 1 个七分支方法 = 42 case。

## 3. Unit：112 个 Incremental case

其中 106 个直接验证行为，另有 6 个验证三个 Incremental Macro 文件可以解析且
包含 ASF License。

### 3.1 Incremental Python Helper：21 case

文件：`test/unit/test_incremental.py`。

| 测试方法 | case | 覆盖 |
| --- | ---: | --- |
| `test_doris_column_preserves_parameterized_types` | 5 | VARCHAR、DECIMAL、CHAR、DATETIMEV2、ARRAY 参数类型保真 |
| `test_doris_column_widens_with_valid_varchar_syntax` | 1 | VARCHAR 扩宽 SQL 语法 |
| `test_view_snapshot_ctas_does_not_inherit_new_model_layout` | 1 | Snapshot 不继承新模型 Key、Distribution 或 Partition；使用 keyless Duplicate + RANDOM/AUTO，并保留 replication_num |
| `test_view_snapshot_drops_source_only_after_ctas_succeeds` | 1 | CTAS 成功后才允许删除源 |
| `test_view_data_snapshot_keeps_source_online` | 1 | Snapshot 正常路径保持源在线 |
| `test_view_snapshot_ctas_failure_keeps_source_view` | 1 | CTAS 失败保持源 View |
| `test_view_snapshot_rejects_same_source_and_destination_without_side_effects` | 1 | 同名关系零副作用拒绝 |
| `test_view_snapshot_rejects_existing_destination_without_dropping_it` | 1 | 已存在目标零 Drop 拒绝 |
| `test_schema_change_comparison_is_case_insensitive_for_doris_columns` | 1 | Schema 列名大小写不敏感 |
| `test_string_widening_matches_doris_columns_case_insensitively` | 1 | 字符串扩宽大小写不敏感 |
| `test_schema_change_waits_for_finished_job` | 1 | 等待当前 Alter Job 完成 |
| `test_schema_change_waits_for_new_job_to_appear` | 1 | 等待新 Job 出现在元数据中 |
| `test_latest_schema_change_job_orders_by_job_id` | 1 | 按 Job ID 选择最新 Job |
| `test_cancelled_schema_change_is_reported` | 1 | CANCELLED Job 明确失败 |
| `test_schema_change_timeout_is_reported` | 3 | running job、旧完成 Job 仍可见、新 Job 暂不可见三种确定性超时 |

### 3.2 Incremental 配置验证：52 case

文件/类：
`test/unit/test_macro_behavior.py::TestIncrementalStrategyValidation`。

| 测试方法 | case | 参数分支或覆盖 |
| --- | ---: | --- |
| `test_default_strategy_keeps_public_default_name` | 2 | 无 Key / 有 Key 的公开策略名仍为 default |
| `test_default_routes_by_unique_key` | 2 | None→append；Key→merge |
| `test_append_needs_no_unique_key` | 1 | append 合法配置 |
| `test_grants_are_rejected_before_execution` | 1 | 未实现 Grants 早失败 |
| `test_merge_requires_unique_key` | 1 | merge 缺 Key 早失败和提示 |
| `test_merge_accepts_single_and_composite_keys` | 2 | 单 Key / 复合 Key |
| `test_insert_overwrite_needs_no_unique_key` | 1 | 原生覆盖合法配置 |
| `test_microbatch_accepts_aligned_dynamic_range_partition` | 1 | 合法 Dynamic Partition Microbatch |
| `test_microbatch_accepts_adapter_managed_static_range_partitions` | 1 | 合法静态 Microbatch |
| `test_microbatch_rejects_unsafe_partition_configs` | 11 | 缺 event_time、缺 partition_by、列不一致、非 RANGE、粒度不一致、非 UTC、未启用历史分区、缺 dynamic_partition.prefix、unique_key、overwrite_partitions、partition_by_init |
| `test_microbatch_requires_core_batch_context` | 1 | 缺 `model.batch` 早失败 |
| `test_insert_overwrite_rejects_legacy_unique_key_combination` | 1 | 危险旧组合早失败 |
| `test_delete_insert_is_explicitly_rejected` | 2 | `delete+insert` / `delete_insert` |
| `test_merge_accepts_mor_and_sequence_properties` | 2 | MOR / 可见 Sequence 属性 |
| `test_merge_rejects_sequence_type_hidden_column_mode` | 1 | 隐藏 Sequence Type 配置拒绝 |
| `test_existing_merge_target_accepts_matching_visible_sequence_mapping` | 1 | 模型与物理 mapping 一致 |
| `test_sequence_property_text_in_comment_is_not_physical_mapping` | 1 | Comment 文本不误判为属性 |
| `test_keyless_duplicate_property_identifies_append_target` | 1 | keyless Duplicate 物理模型识别 |
| `test_existing_merge_target_rejects_physical_sequence_mismatch` | 4 | 模型缺配置、列名不一致、物理缺 mapping、隐藏 Sequence Type |
| `test_insert_overwrite_rejects_hidden_physical_sequence` | 1 | Overwrite 隐藏 Sequence 目标拒绝 |
| `test_sequence_column_requires_merge_and_a_plain_column_name` | 2 | 非 merge 使用 / 非普通列名 |
| `test_merge_rejects_duplicate_configured_key_columns` | 1 | 重复配置 Key 拒绝 |
| `test_predicates_are_rejected_for_builtins` | 4 | append、merge、insert_overwrite、microbatch |
| `test_partial_merge_configs_are_rejected` | 2 | update columns / exclude columns |
| `test_overwrite_partition_config_is_validated` | 1 | append 配置 overwrite_partitions 时拒绝；insert_overwrite 配置 partition_by + `overwrite_partitions='*'` 时通过 |
| `test_invalid_overwrite_partitions_are_rejected` | 3 | 空集合、星号混用、不安全分区名 |
| `test_empty_strategy_falls_back_to_the_default` | 1 | 空字符串回退 default |

### 3.3 Incremental SQL 宏：27 case

文件/类：`test/unit/test_macro_behavior.py::TestIncrementalStrategySql`。

| 测试方法 | case | 参数分支或覆盖 |
| --- | ---: | --- |
| `test_direct_insert_is_one_statement_and_reads_source_once` | 2 | append / merge 单语句、Source 只读一次 |
| `test_merge_validates_duplicate_source_keys_in_the_insert` | 1 | 重复 Key Guard 与目标 INSERT 同语句 |
| `test_merge_validation_column_cannot_collide_with_model_columns` | 1 | 内部校验 Alias 避让用户列 |
| `test_initial_merge_projects_unique_keys_before_value_columns` | 1 | 首次 CTAS Key 前缀投影 |
| `test_native_insert_overwrite` | 3 | 整表、`PARTITION(*)`、多命名分区 SQL |
| `test_microbatch_overwrites_the_resolved_static_partition` | 1 | 单命名分区、非 `PARTITION(*)`、无 physical staging |
| `test_microbatch_initial_partition_uses_the_core_batch_range` | 4 | hour、day、month、year 的 Batch ID、分区名和边界 |
| `test_microbatch_resolves_any_exact_range_partition_name` | 2 | 日/小时粒度可复用任意物理分区名 |
| `test_microbatch_rejects_a_coarser_physical_partition` | 1 | 粗粒度重叠分区拒绝 |
| `test_static_microbatch_can_create_a_missing_non_overlapping_partition` | 1 | 静态目标缺少精确分区且与已有分区不重叠时返回缺分区结果，供 materialization 进入 ADD 路径 |
| `test_dynamic_microbatch_rejects_a_missing_exact_partition` | 1 | Dynamic 窗口缺精确分区拒绝 |
| `test_microbatch_rejects_static_dynamic_target_drift` | 1 | 模型配置静态分区、物理目标启用 Dynamic Partition 时拒绝 |
| `test_microbatch_accepts_matching_physical_dynamic_properties` | 1 | Dynamic 物理属性一致时通过 |
| `test_standard_five_key_contract_reads_temp_relation` | 3 | append、merge、insert_overwrite 的 dbt 标准五参数兼容 |
| `test_default_sql_routes_by_unique_key` | 2 | 默认 SQL append / merge 路由 |
| `test_unique_key_type_change_requires_full_refresh` | 1 | Key 类型变化拒绝 |
| `test_sequence_mapping_type_change_requires_full_refresh` | 1 | Sequence 类型变化拒绝 |

### 3.4 Adapter、UTC 与 physical staging：6 case

| 文件与测试方法 | case | 覆盖 |
| --- | ---: | --- |
| `test/unit/test_adapter_api.py::test_incremental_strategy_allowlist_excludes_delete_insert` | 1 | 合法策略精确为 append、merge、insert_overwrite、microbatch |
| `test/unit/test_adapter_api.py::test_microbatch_batches_remain_sequential` | 1 | 不声明 MicrobatchConcurrency，Core 顺序执行 |
| `test/unit/test_relation.py::test_event_time_filter_renders_utc_as_naive_doris_datetime` | 1 | aware 时间统一转 UTC 并去除 Offset，保持 `[start,end)` |
| `TestSingleStatementDDL::test_incremental_staging_preserves_replication_allocation` | 1 | physical staging 保留 replication allocation |
| `TestSingleStatementDDL::test_incremental_staging_prefers_top_level_replication_num` | 1 | 顶层 replication num 优先级 |
| `TestSingleStatementDDL::test_incremental_staging_is_keyless_for_non_keyable_first_column` | 1 | staging 固定 keyless RANDOM/AUTO，不继承不可 Key 首列 |

行为 Unit 精确合计：21 + 52 + 27 + 6 = 106 case。

### 3.5 Incremental Macro 解析与 License：6 case

`test/unit/test_macro_syntax.py` 的两个参数化方法分别覆盖以下三个文件：

- `materializations/incremental/incremental.sql`；
- `materializations/incremental/help.sql`；
- `materializations/incremental/strategies.sql`。

对应 `test_macro_file_parses` 3 case 和 `test_macro_file_has_license_header` 3 case，
合计 6。它们已包含在完整 281 项 Unit 中，但不与前述 106 项行为测试重复。

## 4. 共享回归与发布门禁

下列共享套件和发布门禁不作为额外 case 加到前述 154；完整 Unit/Functional 套件
本身包含前述子集：

- `TestSingleStatementDDL` 共 13 case：第 3.4 节已计入 3 个 staging case；另有
  9 个 Incremental 复用的 CTAS/Table DDL case（CTAS 单语句 4、调用方负责 Drop
  2、默认 MOW 1、MOR 覆盖 1、Key/Distribution 引用 1）；余下 1 个
  `REPLACE PARTITION` case 属于独立 partition materialization；
- `TestContractProjection` 4 case：普通/Unique CTAS 的 enforced 与 unenforced
  Contract；
- `test_macro_syntax.py` 共 41 case：第 3.5 节已计入 6 个 Incremental Macro
  case，其余 35 个是共享宏解析、License、宏名、adapter scope 和版本一致性门禁；
- `test_packaging.py` 4 case：dbt Core 1.12、Python 3.10、Connector 版本和源码发行
  边界；
- 完整 Unit：`python -m pytest -q test/unit`，当前记录为 281 passed；
- 共享 Doris Functional：Table/View/Partition 12 case；其中 Partition 使用
  `is_incremental()`，但验证的是 `materialized='partition'`，不计入第 2 节；
- 完整 Adapter Functional：99 case，其中第 2 节的 42 个 Incremental case
  是其子集；
- Package：wheel/sdist build、Twine、全新 venv 安装、四策略检查、三个 Incremental
  Macro 文件存在性和 `pip check`；
- CI：Python 3.10、Python 3.14、Build distributions。

## 5. 维护规则

1. 新增或删除 Incremental 测试时，先运行第 1 节的 `--collect-only` 命令；
2. 更新对应方法、参数分支、case 数和覆盖说明；
3. 同步更新测试方案第 5 节的 INC 场景矩阵与第 8 节实际执行结果；
4. 参数化方法必须记录全部分支，不能只写方法名；
5. 只有实际运行通过的结果才能写为 passed；`--collect-only` 只证明收集，不证明
   执行成功；
6. 五个 Doris 精确版本的历史结果不包含后来新增的测试时，必须明确标记待复验，
   不能用旧总数覆盖新 PR Head。
