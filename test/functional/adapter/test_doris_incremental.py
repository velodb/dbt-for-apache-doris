#!/usr/bin/env python
# encoding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""
Tests for Doris incremental materialization:
- append writes through one INSERT without physical staging
- merge upserts MOW or MOR Unique Key tables without physical staging
- Sequence columns keep Doris's native conflict-ordering semantics
- insert_overwrite performs real whole-table and partition overwrites
- microbatch replaces one exact time partition, including an empty batch
- full refresh replaces the target and preserves its Doris table configuration
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from dbt.tests.adapter.incremental.test_incremental_on_schema_change import (
    BaseIncrementalOnSchemaChange,
)
from dbt.tests.util import (
    patch_microbatch_end_time,
    relation_from_name,
    run_dbt,
    set_model_file,
)


def _run_and_capture_sql(model_name, args=None, expect_pass=True):
    """Run dbt and return relevant SQLQuery events.

    Inspecting the catalog after a run only proves that a staging relation was
    cleaned up. SQLQuery events prove whether dbt physically created and read one
    during the run. Run-operation events have no model node id, so retain every
    statement from that invocation.
    """
    statements = []
    run_args = args or ["run"]
    is_run_operation = run_args[0] == "run-operation"

    def capture_sql(event):
        if event.info.name == "SQLQuery" and (
            is_run_operation
            or event.data.node_info.node_name == model_name
        ):
            statements.append(" ".join(event.data.sql.lower().split()))

    results = run_dbt(
        run_args,
        expect_pass=expect_pass,
        callbacks=[capture_sql],
    )
    return results, list(statements)


def _assert_no_physical_dbt_staging(statements):
    physical_staging_statements = [
        statement
        for statement in statements
        if (
            "__dbt_tmp" in statement
            and "create table" in statement
        )
        or re.search(
            r"\binsert\s+(?:into|overwrite\s+table)\s+"
            r"(?:`[^`]+`\.)*`[^`]*__dbt_tmp`",
            statement,
        )
    ]
    assert physical_staging_statements == []


def _target_dml_statements(statements):
    return [
        statement
        for statement in statements
        if re.search(
            r"\binsert\s+(?:into|overwrite\s+table)\s+",
            statement,
        )
    ]


def _assert_logical_view_staging(statements):
    logical_views = [
        statement
        for statement in statements
        if "create or replace view" in statement and "__dbt_tmp" in statement
    ]
    assert len(logical_views) == 1
    _assert_no_physical_dbt_staging(statements)

    target_dml = _target_dml_statements(statements)
    assert len(target_dml) == 1
    assert "__dbt_tmp" in target_dml[0]


def _assert_direct_initial_ctas(statements, model_name):
    target_ctas = [
        statement
        for statement in statements
        if "create table" in statement
        and model_name in statement
        and "__dbt_" not in statement
        and " as " in statement
    ]
    assert len(target_ctas) == 1
    assert not any("create or replace view" in statement for statement in statements)
    _assert_no_physical_dbt_staging(statements)
    assert _target_dml_statements(statements) == []


def _assert_physical_staging(statements):
    staging_ctas = [
        statement
        for statement in statements
        if "create table" in statement
        and "__dbt_tmp" in statement
        and " as " in statement
    ]
    assert len(staging_ctas) == 1

    target_dml = _target_dml_statements(statements)
    assert len(target_dml) == 1
    assert "__dbt_tmp" in target_dml[0]


def _dbt_helper_relations(project, relation):
    return project.run_sql(
        "select table_name from information_schema.tables "
        f"where table_schema = '{relation.schema}' "
        f"and table_name like '{relation.identifier}__dbt_%' "
        "order by table_name",
        fetch="all",
    )


# -- Append strategy: works with duplicate key tables --

INCREMENTAL_DEFAULT_APPEND_SQL = """
{{ config(
    materialized='incremental',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 1 as id, 'second' as value
union all
select 3 as id, 'new' as value
{% else %}
select 1 as id, 'initial' as value
union all
select 2 as id, 'keep' as value
{% endif %}
"""


INCREMENTAL_DEFAULT_MERGE_SQL = """
{{ config(
    materialized='incremental',
    unique_key=['id'],
    distributed_by=['id'],
    properties={
        'replication_num': '1',
        'enable_unique_key_merge_on_write': 'true'
    }
) }}

{% if is_incremental() %}
    {% if var('emit_duplicate_keys', false) %}
select 1 as id, 'conflict_a' as value
union all
select 1 as id, 'conflict_b' as value
    {% else %}
select 1 as id, 'updated' as value
union all
select 3 as id, 'new' as value
    {% endif %}
{% else %}
select 1 as id, 'initial' as value
union all
select 2 as id, 'keep' as value
{% endif %}
"""


INCREMENTAL_APPEND_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 4 as id, 'dave' as name
union all
select 5 as id, 'eve' as name
{% else %}
select 1 as id, 'alice' as name
union all
select 2 as id, 'bob' as name
union all
select 3 as id, 'charlie' as name
{% endif %}
"""


# -- Merge strategy: Doris Unique Key upsert --

INCREMENTAL_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    sql_header='set enable_nereids_planner = true',
    properties={
        'replication_num': '1',
        'enable_unique_key_merge_on_write': 'true'
    }
) }}

{% if is_incremental() %}
select 1 as id, 'alice_updated' as name, 150 as score,
       'updated_0' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_0`,
       'updated_1' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_1`
union all
select 4 as id, 'dave' as name, 400 as score,
       'new_0' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_0`,
       'new_1' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_1`
{% else %}
select 1 as id, 'alice' as name, 100 as score,
       'initial_0' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_0`,
       'initial_1' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_1`
union all
select 2 as id, 'bob' as name, 200 as score,
       'initial_0' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_0`,
       'initial_1' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_1`
union all
select 3 as id, 'charlie' as name, 300 as score,
       'initial_0' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_0`,
       'initial_1' as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_1`
{% endif %}
"""


INCREMENTAL_DUPLICATE_KEY_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    properties={
        'replication_num': '1',
        'enable_unique_key_merge_on_write': 'true'
    }
) }}

{% if is_incremental() %}
select 1 as id, 'conflicting_first' as name
union all
select 1 as id, 'conflicting_second' as name
{% else %}
select 1 as id, 'alice' as name
union all
select 2 as id, 'bob' as name
{% endif %}
"""


INCREMENTAL_COMPOSITE_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['tenant_id', 'id'],
    distributed_by=['tenant_id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 1 as tenant_id, 1 as id, 'updated' as value
union all
select 2 as tenant_id, 2 as id, 'new' as value
{% else %}
select 1 as tenant_id, 1 as id, 'old' as value
union all
select 1 as tenant_id, 2 as id, 'keep' as value
union all
select 2 as tenant_id, 1 as id, 'other_tenant' as value
{% endif %}
"""


# -- Key columns need not be first in model SQL; CTAS reorders the projection --

INCREMENTAL_REORDERED_KEY_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 'updated' as value, 1 as id
{% else %}
select 'original' as value, 1 as id
{% endif %}
"""


INCREMENTAL_RESERVED_KEY_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['order'],
    distributed_by=['order'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 1 as `order`, 'updated' as value
{% else %}
select 1 as `order`, 'original' as value
{% endif %}
"""


# -- Merge also supports Merge-on-Read Unique Key targets --

INCREMENTAL_MOR_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    properties={
        'replication_num': '1',
        'enable_unique_key_merge_on_write': 'false'
    }
) }}

{% if is_incremental() %}
select 1 as id, 'alice_updated' as name, 150 as score
union all
select 4 as id, 'dave' as name, 400 as score
{% else %}
select 1 as id, 'alice' as name, 100 as score
union all
select 2 as id, 'bob' as name, 200 as score
union all
select 3 as id, 'charlie' as name, 300 as score
{% endif %}
"""


# -- Sequence ordering is delegated to the Doris Unique Key storage model --

INCREMENTAL_SEQUENCE_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    properties={
        'replication_num': '1',
        'enable_unique_key_merge_on_write': 'true',
        'function_column.sequence_col': 'sequence_id'
    }
) }}

{% if is_incremental() %}
select 1 as id, 50 as sequence_id, 'lower_sequence' as value
{% else %}
select 1 as id, 100 as sequence_id, 'original' as value
{% endif %}
"""


# -- Removed strategy: fail before hooks, staging, or target writes --

INCREMENTAL_UNSUPPORTED_DELETE_INSERT_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, 'never_written' as value
"""


# -- Insert overwrite: replace the complete target with the current batch --

INCREMENTAL_OVERWRITE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 1 as id, 'alice_replaced' as name
union all
select 4 as id, 'dave' as name
{% else %}
select 1 as id, 'alice' as name
union all
select 2 as id, 'bob' as name
union all
select 3 as id, 'charlie' as name
{% endif %}
"""


# -- Static partition overwrite: replace p1 while retaining p2 --

INCREMENTAL_STATIC_PARTITION_OVERWRITE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    overwrite_partitions=['p1'],
    duplicate_key=['part_id'],
    partition_by=['part_id'],
    partition_type='RANGE',
    partition_by_init=[
        'PARTITION p1 VALUES LESS THAN ("2")',
        'PARTITION p2 VALUES LESS THAN ("3")'
    ],
    distributed_by=['part_id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 1 as part_id, 'static_new_p1' as value
{% else %}
select 1 as part_id, 'static_old_p1' as value
union all
select 2 as part_id, 'static_unchanged_p2' as value
{% endif %}
"""


# -- Dynamic partition overwrite: replace only partitions present in this batch --

INCREMENTAL_DYNAMIC_PARTITION_OVERWRITE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    overwrite_partitions='*',
    duplicate_key=['part_id'],
    partition_by=['part_id'],
    partition_type='RANGE',
    partition_by_init=[
        'PARTITION p1 VALUES LESS THAN ("2")',
        'PARTITION p2 VALUES LESS THAN ("3")'
    ],
    distributed_by=['part_id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 1 as part_id, 'dynamic_new_p1' as value
{% else %}
select 1 as part_id, 'dynamic_old_p1' as value
union all
select 2 as part_id, 'dynamic_unchanged_p2' as value
{% endif %}
"""


# -- Microbatch: Core filters refs to [start, end), and Doris replaces that
# -- exact named RANGE partition. Unit coverage separately proves that existing
# -- physical partition names do not have to follow the generated convention.

MICROBATCH_TODAY = datetime.now(timezone.utc).date()
MICROBATCH_DATE_1 = MICROBATCH_TODAY - timedelta(days=3)
MICROBATCH_DATE_2 = MICROBATCH_TODAY - timedelta(days=2)
MICROBATCH_DATE_3 = MICROBATCH_TODAY - timedelta(days=1)


MICROBATCH_INPUT_SQL = """
{{ config(
    materialized='table',
    event_time='event_time',
    duplicate_key=['id', 'event_time'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, cast('__DATE_1__ 00:00:00' as datetime) as event_time,
       'first' as value
union all
select 2 as id, cast('__DATE_2__ 00:00:00' as datetime) as event_time,
       'second' as value
union all
select 3 as id, cast('__DATE_3__ 00:00:00' as datetime) as event_time,
       'third' as value
"""
MICROBATCH_INPUT_SQL = (
    MICROBATCH_INPUT_SQL.replace("__DATE_1__", MICROBATCH_DATE_1.isoformat())
    .replace("__DATE_2__", MICROBATCH_DATE_2.isoformat())
    .replace("__DATE_3__", MICROBATCH_DATE_3.isoformat())
)


INCREMENTAL_MICROBATCH_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='microbatch',
    event_time='event_time',
    batch_size='day',
    begin=modules.datetime.datetime(__YEAR__, __MONTH__, __DAY__, 0, 0, 0),
    duplicate_key=['id', 'event_time'],
    partition_by=['event_time'],
    partition_type='RANGE',
    distributed_by=['id'],
    properties={
        'replication_num': '1'
    }
) }}

select id, event_time, value
from {{ ref('microbatch_input') }}
"""
INCREMENTAL_MICROBATCH_SQL = (
    INCREMENTAL_MICROBATCH_SQL.replace("__YEAR__", str(MICROBATCH_DATE_1.year))
    .replace("__MONTH__", str(MICROBATCH_DATE_1.month))
    .replace("__DAY__", str(MICROBATCH_DATE_1.day))
)

INCREMENTAL_MICROBATCH_DYNAMIC_SQL = INCREMENTAL_MICROBATCH_SQL.replace(
    """    properties={
        'replication_num': '1'
    }""",
    """    properties={
        'replication_num': '1',
        'dynamic_partition.enable': 'true',
        'dynamic_partition.time_unit': 'DAY',
        'dynamic_partition.time_zone': 'UTC',
        'dynamic_partition.prefix': 'dyn',
        'dynamic_partition.start': '-5',
        'dynamic_partition.end': '1',
        'dynamic_partition.buckets': '1',
        'dynamic_partition.create_history_partition': 'true'
    }""",
)


# -- Full refresh --

INCREMENTAL_FULL_REFRESH_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={
        'replication_num': '1',
        'disable_auto_compaction': 'true'
    }
) }}

select 1 as id, 'only_row' as name
"""


INCREMENTAL_VIEW_TO_TABLE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['ASSET_ID'],
    distributed_by=['ASSET_ID'],
    properties={'replication_num': '1'}
) }}

select 7 as `ASSET_ID`, 'new_table' as value
"""


INCREMENTAL_VIEW_NO_BACKSLASH_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id'],
    sql_header="set sql_mode='NO_BACKSLASH_ESCAPES'",
    properties={'replication_num': '1'}
) }}

select 7 as id, 'new_table' as value
"""

INCREMENTAL_VIEW_DEFAULT_SQL = INCREMENTAL_VIEW_NO_BACKSLASH_SQL.replace(
    '    sql_header="set sql_mode=\'NO_BACKSLASH_ESCAPES\'",\n',
    "",
)


INCREMENTAL_VARCHAR_WIDEN_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    on_schema_change='ignore',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select cast(2 as int) as `ID`, cast('expanded' as varchar(40)) as `NAME`
{% else %}
select 1 as id, cast('a' as varchar(5)) as name
{% endif %}
"""


INCREMENTAL_FAIL_TARGET_STABILITY_SQL = """
{{ config(
    materialized='incremental',
    unique_key=['id'],
    on_schema_change='fail',
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

with source_data as (select * from {{ ref('model_a') }})

{% if is_incremental() %}
select id, cast(field1 as varchar(40)) as field1, field2
from source_data
{% else %}
select id, cast(field1 as varchar(5)) as field1, field3
from source_data
{% endif %}
"""


INCREMENTAL_KEY_WIDEN_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select cast('expanded-key' as varchar(40)) as id, 'new' as value
"""


INCREMENTAL_CASE_ONLY_SCHEMA_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    on_schema_change='sync_all_columns',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select cast(2 as int) as `ID`, cast('new' as varchar(20)) as `VALUE`
"""


INCREMENTAL_SCHEMA_CHANGE_RETRY_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    on_schema_change='append_new_columns',
    distributed_by=['id'],
    properties={
        'replication_num': '1',
        'enable_unique_key_merge_on_write': 'true'
    }
) }}

{% if not is_incremental() %}
select cast(1 as int) as id, cast('original' as varchar(40)) as value
{% elif var('emit_duplicate_keys', false) %}
select cast(1 as int) as id,
       cast('conflict_a' as varchar(40)) as value,
       cast(10 as int) as added_value
union all
select cast(1 as int) as id,
       cast('conflict_b' as varchar(40)) as value,
       cast(20 as int) as added_value
{% else %}
select cast(1 as int) as id,
       cast('updated' as varchar(40)) as value,
       cast(30 as int) as added_value
union all
select cast(2 as int) as id,
       cast('new' as varchar(40)) as value,
       cast(40 as int) as added_value
{% endif %}
"""


INCREMENTAL_CUSTOM_STRATEGY_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='frozen_append',
    incremental_predicates=['DBT_CUSTOM_SOURCE.`id` >= 0'],
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select cast(2.5 as double) as measure, 2 as id, 'incremental' as value
{% else %}
select 1 as id, cast(1.0 as double) as measure, 'initial' as value
{% endif %}
"""


INCREMENTAL_CUSTOM_STRATEGY_MACRO = """
{% macro get_incremental_frozen_append_sql(arg_dict) %}
    {% set target_relation = arg_dict['target_relation'] %}
    {% set temp_relation = arg_dict['temp_relation'] %}
    {% set unique_key = arg_dict['unique_key'] %}
    {% set dest_columns = arg_dict['dest_columns'] %}
    {% set incremental_predicates = arg_dict['incremental_predicates'] %}
    insert into {{ target_relation }}
        ({% for column in dest_columns -%}
            {{ adapter.quote(column.name) }}{% if not loop.last %}, {% endif %}
        {%- endfor %})
    select
        {% for column in dest_columns -%}
            DBT_CUSTOM_SOURCE.{{ adapter.quote(column.name) }}{% if not loop.last %}, {% endif %}
        {%- endfor %}
    from {{ temp_relation }} DBT_CUSTOM_SOURCE
    {% if incremental_predicates %}
    where {{ incremental_predicates | join(' and ') }}
    {% endif %}
{% endmacro %}
"""


INCREMENTAL_RECOVERY_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select *
from dbt_incremental_intentional_missing_relation
"""


INCREMENTAL_VIEW_PRE_HOOK_FAILURE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'},
    pre_hook='select * from __dbt_incremental_view_pre_hook__'
) }}

select 7 as id, 'new_table' as value
"""


CREATE_NO_BACKSLASH_RECOVERY_VIEW_MACRO = r"""
{% macro create_no_backslash_recovery_view(schema_name, view_name) %}
    {% do run_query("set sql_mode='NO_BACKSLASH_ESCAPES'") %}
    {% set view_ddl %}
        create view `{{ schema_name }}`.`{{ view_name }}` as
        select cast(1.5 as double) as floating_value,
               99 as id, 'C:\new\path' as value, 7 as `odd"name`
    {% endset %}
    {% do run_query(view_ddl) %}
    {% do run_query("set sql_mode='ONLY_FULL_GROUP_BY'") %}
{% endmacro %}
"""


VIEW_SNAPSHOT_FAILURE_MACROS = """
{% macro snapshot_view_for_test(schema_name, view_name, snapshot_name) %}
    {% set source_relation = api.Relation.create(
        schema=schema_name,
        identifier=view_name,
        type='view'
    ) %}
    {% set snapshot_relation = api.Relation.create(
        schema=schema_name,
        identifier=snapshot_name,
        type='table'
    ) %}
    {% do doris__snapshot_view_to_table(
        source_relation,
        snapshot_relation
    ) %}
{% endmacro %}

{% macro doris__rename_relation(from_relation, to_relation) %}
    {% if (
        var('fail_intermediate_rename', false)
        and '__dbt_tmp' in from_relation.identifier
    ) %}
        {% do exceptions.raise_compiler_error(
            'intentional intermediate rename failure'
        ) %}
    {% endif %}
    {% call statement('drop_relation') %}
        drop {{
            'materialized view'
            if to_relation.type == 'materialized_view'
            else to_relation.type
        }} if exists {{ to_relation }}
    {% endcall %}
    {% call statement('rename_relation') %}
        {% if to_relation.type == 'materialized_view' %}
        alter materialized view {{ from_relation }}
            rename `{{ to_relation.table | replace("`", "``") }}`
        {% else %}
        alter table {{ from_relation }} rename {{ to_relation.table }}
        {% endif %}
    {% endcall %}
{% endmacro %}
"""


INCREMENTAL_UNSAFE_OVERWRITE_UNIQUE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, 'unsafe' as value
"""


INCREMENTAL_INVALID_GRANTS_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id'],
    grants={'select': ['dbt_incremental_definitely_missing_user']},
    properties={'replication_num': '1'}
) }}

select 2 as id, 'must_not_be_written' as value
"""


INCREMENTAL_TARGET_GUARD_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy=var('guard_strategy'),
    unique_key=var('guard_unique_key', none),
    distributed_by=['id'],
    properties=var('guard_properties', {'replication_num': '1'}),
    pre_hook='select * from __dbt_incremental_target_guard_hook__'
) }}

select 2 as id, 2 as tenant_id, 2 as sequence_id,
       'must_not_be_written' as value
"""


INCREMENTAL_HOOK_FAILURE_SQL = """
{{ config(
    materialized='incremental',
    unique_key=['id'],
    distributed_by=['id'],
    properties={
        'replication_num': '1',
        'enable_unique_key_merge_on_write': 'true'
    },
    pre_hook=(
        'select * from __dbt_missing_incremental_pre_hook__'
        if var('fail_pre_hook', false)
        else []
    ),
    post_hook=(
        'select * from __dbt_missing_incremental_post_hook__'
        if var('fail_post_hook', false)
        else []
    )
) }}

{% if is_incremental() %}
select 1 as id, 'updated' as value
union all
select 2 as id, 'new' as value
{% else %}
select 1 as id, 'original' as value
{% endif %}
"""


class TestDorisIncrementalDefaultStrategy:
    @classmethod
    @pytest.fixture(scope="class")
    def models(cls):
        return {
            "incremental_default_append.sql": INCREMENTAL_DEFAULT_APPEND_SQL,
            "incremental_default_merge.sql": INCREMENTAL_DEFAULT_MERGE_SQL,
        }

    def test_default_without_unique_key_routes_to_append(self, project):
        model_name = "incremental_default_append"
        run_args = ["run", "--select", model_name]

        results, initial_statements = _run_and_capture_sql(
            model_name,
            run_args,
        )
        assert len(results) == 1
        _assert_direct_initial_ctas(initial_statements, model_name)
        assert not any(
            "dbt_internal_duplicate_keys" in statement
            for statement in initial_statements
        )

        relation = relation_from_name(project.adapter, model_name)
        ddl = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert "duplicate key" in ddl
        assert "unique key" not in ddl

        results, statements = _run_and_capture_sql(model_name, run_args)
        assert len(results) == 1
        _assert_logical_view_staging(statements)

        target_dml = _target_dml_statements(statements)
        assert len(target_dml) == 1
        assert "insert into" in target_dml[0]
        assert model_name in target_dml[0]
        assert "dbt_internal_duplicate_keys" not in target_dml[0]
        assert "count(*) over" not in target_dml[0]
        assert not any("delete from" in statement for statement in statements)
        assert not any(statement == "begin" for statement in statements)

        assert project.run_sql(
            f"select id, value from {relation} order by id, value",
            fetch="all",
        ) == [
            (1, "initial"),
            (1, "second"),
            (2, "keep"),
            (3, "new"),
        ]
        assert _dbt_helper_relations(project, relation) == []

    def test_default_with_unique_key_routes_to_merge(self, project):
        model_name = "incremental_default_merge"
        run_args = ["run", "--select", model_name]

        results, initial_statements = _run_and_capture_sql(
            model_name,
            run_args,
        )
        assert len(results) == 1
        _assert_direct_initial_ctas(initial_statements, model_name)

        initial_ctas = [
            statement
            for statement in initial_statements
            if "create table" in statement
            and model_name in statement
            and "__dbt_" not in statement
            and " as " in statement
        ]
        assert len(initial_ctas) == 1
        assert "dbt_internal_duplicate_keys" in initial_ctas[0]

        relation = relation_from_name(project.adapter, model_name)
        ddl = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert "unique key" in ddl
        assert '"enable_unique_key_merge_on_write" = "true"' in ddl

        rows_before = project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        )
        assert rows_before == [(1, "initial"), (2, "keep")]

        failure, failed_statements = _run_and_capture_sql(
            model_name,
            [
                "run",
                "--select",
                model_name,
                "--vars",
                "{emit_duplicate_keys: true}",
            ],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        _assert_logical_view_staging(failed_statements)

        failed_dml = _target_dml_statements(failed_statements)
        assert len(failed_dml) == 1
        assert "dbt_internal_duplicate_keys" in failed_dml[0]
        assert "count(*) over" in failed_dml[0]
        assert "json_parse(if(" in failed_dml[0]
        assert not any(
            "delete from" in statement for statement in failed_statements
        )
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == rows_before

        results, statements = _run_and_capture_sql(model_name, run_args)
        assert len(results) == 1
        _assert_logical_view_staging(statements)

        target_dml = _target_dml_statements(statements)
        assert len(target_dml) == 1
        assert "insert into" in target_dml[0]
        assert "dbt_internal_duplicate_keys" in target_dml[0]
        assert "count(*) over" in target_dml[0]
        assert "json_parse(if(" in target_dml[0]
        assert not any("delete from" in statement for statement in statements)
        assert not any(statement == "begin" for statement in statements)

        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == [
            (1, "updated"),
            (2, "keep"),
            (3, "new"),
        ]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalAppend:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_append.sql": INCREMENTAL_APPEND_SQL}

    def test_incremental_append(self, project):
        results, initial_statements = _run_and_capture_sql(
            "incremental_append"
        )
        assert len(results) == 1
        _assert_direct_initial_ctas(
            initial_statements,
            "incremental_append",
        )

        relation = relation_from_name(project.adapter, "incremental_append")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 3

        temp_name = f"{relation.identifier}__dbt_tmp"
        backup_name = f"{relation.identifier}__dbt_backup"
        project.run_sql(
            f"create table `{relation.schema}`.`{temp_name}` "
            "(`sentinel` int) duplicate key(`sentinel`) "
            "distributed by hash(`sentinel`) buckets 1 "
            'properties ("replication_num" = "1")'
        )
        project.run_sql(
            f"insert into `{relation.schema}`.`{temp_name}` values (-1)"
        )
        project.run_sql(
            f"create view `{relation.schema}`.`{backup_name}` as "
            "select -2 as sentinel"
        )
        assert _dbt_helper_relations(project, relation) == [
            (backup_name,),
            (temp_name,),
        ]

        results, statements = _run_and_capture_sql("incremental_append")
        assert len(results) == 1

        rows = project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        )
        assert rows == [
            (1, "alice"),
            (2, "bob"),
            (3, "charlie"),
            (4, "dave"),
            (5, "eve"),
        ]

        direct_inserts = [
            statement
            for statement in statements
            if "insert into" in statement and "incremental_append" in statement
        ]
        assert len(direct_inserts) == 1
        _assert_logical_view_staging(statements)
        logical_view_index = next(
            index
            for index, statement in enumerate(statements)
            if "create or replace view" in statement and temp_name in statement
        )
        assert any(
            index < logical_view_index
            for index, statement in enumerate(statements)
            if "drop table if exists" in statement and temp_name in statement
        )
        assert any(
            index < logical_view_index
            for index, statement in enumerate(statements)
            if "drop view if exists" in statement and backup_name in statement
        )
        assert _dbt_helper_relations(project, relation) == []

    def test_append_accepts_keyless_duplicate_target(self, project):
        relation = relation_from_name(project.adapter, "incremental_append")
        project.run_sql(f"drop table if exists {relation}")
        project.run_sql(
            f"create table {relation} (`id` int, `name` varchar(40)) "
            "distributed by random buckets auto "
            'properties ("enable_duplicate_without_keys_by_default" = '
            '"true", "replication_num" = "1")'
        )
        project.run_sql(f"insert into {relation} values (1, 'existing')")

        results, statements = _run_and_capture_sql("incremental_append")
        assert len(results) == 1
        _assert_logical_view_staging(statements)

        ddl = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert "duplicate key(" not in ddl
        assert '"enable_duplicate_without_keys_by_default" = "true"' in ddl
        assert project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        ) == [
            (1, "existing"),
            (4, "dave"),
            (5, "eve"),
        ]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalMerge:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_merge.sql": INCREMENTAL_MERGE_SQL}

    def test_merge_upserts_without_staging(self, project):
        results, initial_statements = _run_and_capture_sql(
            "incremental_merge"
        )
        assert len(results) == 1
        _assert_direct_initial_ctas(
            initial_statements,
            "incremental_merge",
        )

        relation = relation_from_name(project.adapter, "incremental_merge")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 3

        results, statements = _run_and_capture_sql("incremental_merge")
        assert len(results) == 1

        rows = project.run_sql(
            "select id, name, score, "
            "DBT_INTERNAL_UNIQUE_KEY_VALIDATION_0, "
            "DBT_INTERNAL_UNIQUE_KEY_VALIDATION_1 "
            f"from {relation} order by id",
            fetch="all",
        )
        assert rows == [
            (1, "alice_updated", 150, "updated_0", "updated_1"),
            (2, "bob", 200, "initial_0", "initial_1"),
            (3, "charlie", 300, "initial_0", "initial_1"),
            (4, "dave", 400, "new_0", "new_1"),
        ]

        create_table = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert "unique key" in create_table
        assert '"enable_unique_key_merge_on_write" = "true"' in create_table

        direct_inserts = [
            statement
            for statement in statements
            if "insert into" in statement and "incremental_merge" in statement
        ]
        assert len(direct_inserts) == 1
        assert "dbt_internal_unique_key_validation_2" in direct_inserts[0]
        _assert_logical_view_staging(statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalMergeRejectsDuplicateKeys:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_duplicate_key_merge.sql": (INCREMENTAL_DUPLICATE_KEY_MERGE_SQL),
        }

    def test_duplicate_source_keys_fail_without_changing_target(self, project):
        assert len(run_dbt(["run"])) == 1

        relation = relation_from_name(
            project.adapter,
            "incremental_duplicate_key_merge",
        )
        rows_before = project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        )

        failure, statements = _run_and_capture_sql(
            "incremental_duplicate_key_merge",
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert any("dbt_internal_duplicate_keys" in statement for statement in statements)
        _assert_no_physical_dbt_staging(statements)

        rows_after = project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        )
        assert (
            rows_after
            == rows_before
            == [
                (1, "alice"),
                (2, "bob"),
            ]
        )


class TestDorisIncrementalCompositeMerge:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_composite_merge.sql": INCREMENTAL_COMPOSITE_MERGE_SQL}

    def test_merge_uses_all_unique_key_columns(self, project):
        assert len(run_dbt(["run"])) == 1
        results, statements = _run_and_capture_sql("incremental_composite_merge")
        assert len(results) == 1

        relation = relation_from_name(
            project.adapter,
            "incremental_composite_merge",
        )
        rows = project.run_sql(
            f"select tenant_id, id, value from {relation} " "order by tenant_id, id",
            fetch="all",
        )
        assert rows == [
            (1, 1, "updated"),
            (1, 2, "keep"),
            (2, 1, "other_tenant"),
            (2, 2, "new"),
        ]
        _assert_no_physical_dbt_staging(statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalReorderedKeyMerge:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_reordered_key_merge.sql": (
                INCREMENTAL_REORDERED_KEY_MERGE_SQL
            ),
        }

    def test_initial_ctas_places_key_first_then_incremental_upserts(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["run"])) == 1

        relation = relation_from_name(
            project.adapter,
            "incremental_reordered_key_merge",
        )
        columns = project.run_sql(
            "select column_name from information_schema.columns "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}' "
            "order by ordinal_position",
            fetch="all",
        )
        assert columns[:2] == [("id",), ("value",)]
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [(1, "updated")]


class TestDorisIncrementalReservedKeyMerge:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_reserved_key_merge.sql": (
                INCREMENTAL_RESERVED_KEY_MERGE_SQL
            ),
        }

    def test_reserved_word_key_is_quoted_in_table_ddl(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["run"])) == 1

        relation = relation_from_name(
            project.adapter,
            "incremental_reserved_key_merge",
        )
        assert project.run_sql(
            f"select `order`, value from {relation}",
            fetch="all",
        ) == [(1, "updated")]
        ddl = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1]
        assert "UNIQUE KEY(`order`)" in ddl
        assert "DISTRIBUTED BY HASH(`order`)" in ddl


class TestDorisIncrementalMorMerge:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_mor_merge.sql": INCREMENTAL_MOR_MERGE_SQL}

    def test_merge_upserts_merge_on_read_target(self, project):
        assert len(run_dbt(["run"])) == 1
        results, statements = _run_and_capture_sql("incremental_mor_merge")
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "incremental_mor_merge")
        rows = project.run_sql(
            f"select id, name, score from {relation} order by id",
            fetch="all",
        )
        assert rows == [
            (1, "alice_updated", 150),
            (2, "bob", 200),
            (3, "charlie", 300),
            (4, "dave", 400),
        ]

        create_table = (
            project.run_sql(
                f"show create table {relation}",
                fetch="one",
            )[1]
            .lower()
            .replace(" ", "")
        )
        assert '"enable_unique_key_merge_on_write"="false"' in create_table
        assert (
            len(
                [
                    statement
                    for statement in statements
                    if "insert into" in statement and "incremental_mor_merge" in statement
                ]
            )
            == 1
        )
        assert not any("delete from" in statement for statement in statements)
        assert not any(statement == "begin" for statement in statements)
        _assert_no_physical_dbt_staging(statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalSequenceMerge:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_sequence_merge.sql": INCREMENTAL_SEQUENCE_MERGE_SQL,
        }

    def test_lower_sequence_arriving_later_does_not_replace_row(self, project):
        assert len(run_dbt(["run"])) == 1
        results, statements = _run_and_capture_sql("incremental_sequence_merge")
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "incremental_sequence_merge")
        rows = project.run_sql(
            f"select id, sequence_id, value from {relation}",
            fetch="all",
        )
        assert rows == [(1, 100, "original")]
        _assert_no_physical_dbt_staging(statements)
        assert not any("delete from" in statement for statement in statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalRejectsDeleteInsert:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_unsupported_delete_insert.sql": (
                INCREMENTAL_UNSUPPORTED_DELETE_INSERT_SQL
            ),
        }

    def test_removed_strategy_fails_before_any_sql(self, project):
        failure, statements = _run_and_capture_sql(
            "incremental_unsupported_delete_insert",
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert "not supported" in failure.results[0].message.lower()
        assert "use 'merge'" in failure.results[0].message.lower()
        assert statements == []

        relation = relation_from_name(
            project.adapter,
            "incremental_unsupported_delete_insert",
        )
        assert project.adapter.get_relation(
            database=relation.database,
            schema=relation.schema,
            identifier=relation.identifier,
        ) is None


class TestDorisIncrementalRejectsLegacyOverwriteUniqueKey:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_unsafe_overwrite_unique.sql": (
                INCREMENTAL_UNSAFE_OVERWRITE_UNIQUE_SQL
            ),
        }

    def test_legacy_combination_fails_before_any_sql(self, project):
        failure, statements = _run_and_capture_sql(
            "incremental_unsafe_overwrite_unique",
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert "could silently" in failure.results[0].message.lower()
        assert "strategy='merge'" in failure.results[0].message.lower()
        assert statements == []

        relation = relation_from_name(
            project.adapter,
            "incremental_unsafe_overwrite_unique",
        )
        assert project.adapter.get_relation(
            database=relation.database,
            schema=relation.schema,
            identifier=relation.identifier,
        ) is None


class TestDorisIncrementalTargetPreflight:
    @classmethod
    @pytest.fixture(scope="class")
    def models(cls):
        return {
            "incremental_target_guard.sql": INCREMENTAL_TARGET_GUARD_SQL,
        }

    @pytest.mark.parametrize(
        (
            "target_table_tail",
            "vars_yaml",
            "expected_fragments",
        ),
        [
            pytest.param(
                (
                    "unique key(`id`) "
                    "distributed by hash(`id`) buckets 1 "
                    'properties("replication_num" = "1", '
                    '"enable_unique_key_merge_on_write" = "true")'
                ),
                "{guard_strategy: append}",
                ("requires a duplicate key target", "is unique"),
                id="append-rejects-unique-target",
            ),
            pytest.param(
                (
                    "duplicate key(`id`) "
                    "distributed by hash(`id`) buckets 1 "
                    'properties("replication_num" = "1")'
                ),
                "{guard_strategy: merge, guard_unique_key: [id]}",
                ("requires a unique key target", "is duplicate"),
                id="merge-rejects-duplicate-target",
            ),
            pytest.param(
                (
                    "unique key(`id`) "
                    "distributed by hash(`id`) buckets 1 "
                    'properties("replication_num" = "1", '
                    '"enable_unique_key_merge_on_write" = "true")'
                ),
                (
                    "{guard_strategy: merge, "
                    "guard_unique_key: [tenant_id, id]}"
                ),
                ("configured unique_key", "physical unique key"),
                id="merge-rejects-physical-key-mismatch",
            ),
            pytest.param(
                (
                    "unique key(`id`) "
                    "distributed by hash(`id`) buckets 1 "
                    'properties("replication_num" = "1", '
                    '"function_column.sequence_col" = "sequence_id")'
                ),
                "{guard_strategy: merge, guard_unique_key: [id]}",
                (
                    "uses physical",
                    "sequence mapping column 'sequence_id'",
                    "does not configure 'function_column.sequence_col'",
                ),
                id="merge-rejects-unconfigured-physical-sequence",
            ),
            pytest.param(
                (
                    "unique key(`id`) "
                    "distributed by hash(`id`) buckets 1 "
                    'properties("replication_num" = "1", '
                    '"function_column.sequence_type" = "bigint")'
                ),
                "{guard_strategy: merge, guard_unique_key: [id]}",
                (
                    "cannot safely write",
                    "__doris_sequence_col__",
                ),
                id="merge-rejects-hidden-physical-sequence",
            ),
            pytest.param(
                (
                    "unique key(`id`) "
                    "distributed by hash(`id`) buckets 1 "
                    'properties("replication_num" = "1", '
                    '"function_column.sequence_type" = "bigint")'
                ),
                "{guard_strategy: insert_overwrite}",
                (
                    "incremental strategy 'insert_overwrite'",
                    "cannot safely write",
                    "__doris_sequence_col__",
                ),
                id="overwrite-rejects-hidden-physical-sequence",
            ),
            pytest.param(
                (
                    "unique key(`id`) "
                    "distributed by hash(`id`) buckets 1 "
                    'properties("replication_num" = "1")'
                ),
                (
                    "{guard_strategy: merge, guard_unique_key: [id], "
                    "guard_properties: {replication_num: '1', "
                    "function_column.sequence_col: sequence_id}}"
                ),
                (
                    "configured sequence mapping column",
                    "'sequence_id'",
                    "no visible sequence mapping",
                ),
                id="merge-rejects-configured-sequence-missing-physically",
            ),
        ],
    )
    def test_mismatch_fails_before_hooks_staging_and_dml(
        self,
        project,
        target_table_tail,
        vars_yaml,
        expected_fragments,
    ):
        model_name = "incremental_target_guard"
        relation = relation_from_name(project.adapter, model_name)

        project.run_sql(f"drop table if exists {relation}")
        project.run_sql(
            f"create table {relation} ("
            "`id` int, `tenant_id` int, `sequence_id` bigint, "
            "`value` varchar(40)"
            f") {target_table_tail}"
        )
        if "function_column.sequence_type" in target_table_tail:
            project.run_sql(
                f"insert into {relation} "
                "(`id`, `tenant_id`, `sequence_id`, `value`, "
                "`__DORIS_SEQUENCE_COL__`) "
                "values (1, 1, 1, 'original', 1)"
            )
        else:
            project.run_sql(
                f"insert into {relation} values (1, 1, 1, 'original')"
            )

        temp_name = f"{relation.identifier}__dbt_tmp"
        backup_name = f"{relation.identifier}__dbt_backup"
        project.run_sql(
            f"drop view if exists `{relation.schema}`.`{temp_name}`"
        )
        project.run_sql(
            f"drop table if exists `{relation.schema}`.`{backup_name}`"
        )
        project.run_sql(
            f"create view `{relation.schema}`.`{temp_name}` as "
            "select -1 as sentinel"
        )
        project.run_sql(
            f"create table `{relation.schema}`.`{backup_name}` "
            "(`sentinel` int) duplicate key(`sentinel`) "
            "distributed by hash(`sentinel`) buckets 1 "
            'properties ("replication_num" = "1")'
        )
        project.run_sql(
            f"insert into `{relation.schema}`.`{backup_name}` values (-2)"
        )

        ddl_before = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1]
        rows_before = project.run_sql(
            f"select id, tenant_id, sequence_id, value "
            f"from {relation} order by id",
            fetch="all",
        )
        assert rows_before == [(1, 1, 1, "original")]
        helper_relations_before = _dbt_helper_relations(project, relation)
        assert helper_relations_before == [
            (backup_name,),
            (temp_name,),
        ]

        failure, statements = _run_and_capture_sql(
            model_name,
            [
                "run",
                "--select",
                model_name,
                "--vars",
                vars_yaml,
            ],
            expect_pass=False,
        )

        assert len(failure.results) == 1
        message = failure.results[0].message.lower()
        for fragment in expected_fragments:
            assert fragment in message

        assert any("show create table" in sql for sql in statements)
        assert not any(
            "__dbt_incremental_target_guard_hook__" in sql
            for sql in statements
        )
        assert not any(
            "create or replace view" in sql and "__dbt_tmp" in sql
            for sql in statements
        )
        _assert_no_physical_dbt_staging(statements)
        assert _target_dml_statements(statements) == []
        assert not any(
            "alter table" in sql or "delete from" in sql
            for sql in statements
        )
        assert not any(
            "drop " in sql and "__dbt_" in sql
            for sql in statements
        )

        assert project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1] == ddl_before
        assert project.run_sql(
            f"select id, tenant_id, sequence_id, value "
            f"from {relation} order by id",
            fetch="all",
        ) == rows_before
        assert _dbt_helper_relations(project, relation) == helper_relations_before
        assert project.run_sql(
            f"select sentinel from `{relation.schema}`.`{temp_name}`",
            fetch="all",
        ) == [(-1,)]
        assert project.run_sql(
            f"select sentinel from `{relation.schema}`.`{backup_name}`",
            fetch="all",
        ) == [(-2,)]


class TestDorisIncrementalGrantPreflight:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_invalid_grants.sql": INCREMENTAL_INVALID_GRANTS_SQL}

    def test_invalid_grants_fail_before_incremental_dml(self, project):
        relation = relation_from_name(
            project.adapter,
            "incremental_invalid_grants",
        )
        project.run_sql(
            f"create table {relation} ("
            "`id` int, `value` varchar(40)"
            ") duplicate key(`id`) "
            "distributed by hash(`id`) buckets auto "
            'properties("replication_num" = "1")'
        )
        project.run_sql(f"insert into {relation} values (1, 'original')")

        failure, statements = _run_and_capture_sql(
            "incremental_invalid_grants",
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert "does not exist" in failure.results[0].message.lower()
        assert not any("insert into" in statement for statement in statements)
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [(1, "original")]


class TestDorisIncrementalHookFailures:
    @classmethod
    @pytest.fixture(scope="class")
    def models(cls):
        return {
            "incremental_hook_failure.sql": INCREMENTAL_HOOK_FAILURE_SQL,
        }

    def test_pre_and_post_hook_failure_states_and_retry(self, project):
        model_name = "incremental_hook_failure"
        relation = relation_from_name(project.adapter, model_name)
        run_args = ["run", "--select", model_name]

        assert len(run_dbt(run_args)) == 1
        initial_rows = project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        )
        assert initial_rows == [(1, "original")]

        pre_failure, pre_statements = _run_and_capture_sql(
            model_name,
            run_args + ["--vars", "{fail_pre_hook: true}"],
            expect_pass=False,
        )
        assert len(pre_failure.results) == 1
        assert "__dbt_missing_incremental_pre_hook__" in (
            pre_failure.results[0].message
        )
        assert any(
            "__dbt_missing_incremental_pre_hook__" in statement
            for statement in pre_statements
        )
        assert not any(
            "create or replace view" in statement
            and "__dbt_tmp" in statement
            for statement in pre_statements
        )
        assert _target_dml_statements(pre_statements) == []
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == initial_rows
        assert _dbt_helper_relations(project, relation) == []

        post_failure, post_statements = _run_and_capture_sql(
            model_name,
            run_args + ["--vars", "{fail_post_hook: true}"],
            expect_pass=False,
        )
        assert len(post_failure.results) == 1
        assert "__dbt_missing_incremental_post_hook__" in (
            post_failure.results[0].message
        )
        _assert_logical_view_staging(post_statements)
        post_hook_index = next(
            index
            for index, statement in enumerate(post_statements)
            if "__dbt_missing_incremental_post_hook__" in statement
        )
        target_dml_index = next(
            index
            for index, statement in enumerate(post_statements)
            if statement in _target_dml_statements(post_statements)
        )
        assert target_dml_index < post_hook_index
        assert not any(
            "drop view if exists" in statement
            and "__dbt_tmp" in statement
            for statement in post_statements[target_dml_index + 1 :]
        )
        committed_rows = project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        )
        assert committed_rows == [(1, "updated"), (2, "new")]
        assert _dbt_helper_relations(project, relation) == [
            (f"{relation.identifier}__dbt_tmp",),
        ]

        retry, retry_statements = _run_and_capture_sql(model_name, run_args)
        assert len(retry) == 1
        _assert_logical_view_staging(retry_statements)
        retry_view_index = next(
            index
            for index, statement in enumerate(retry_statements)
            if "create or replace view" in statement
            and "__dbt_tmp" in statement
        )
        assert any(
            index < retry_view_index
            for index, statement in enumerate(retry_statements)
            if "drop view if exists" in statement
            and "__dbt_tmp" in statement
        )
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == committed_rows
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalInsertOverwrite:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_overwrite.sql": INCREMENTAL_OVERWRITE_SQL}

    def test_whole_table_insert_overwrite_removes_old_rows(self, project):
        results, initial_statements = _run_and_capture_sql(
            "incremental_overwrite"
        )
        assert len(results) == 1
        _assert_direct_initial_ctas(
            initial_statements,
            "incremental_overwrite",
        )

        relation = relation_from_name(project.adapter, "incremental_overwrite")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 3

        results, statements = _run_and_capture_sql("incremental_overwrite")
        assert len(results) == 1

        rows = project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        )
        assert rows == [
            (1, "alice_replaced"),
            (4, "dave"),
        ]

        overwrite_statements = [
            statement
            for statement in statements
            if "insert overwrite" in statement and "incremental_overwrite" in statement
        ]
        assert len(overwrite_statements) == 1
        _assert_logical_view_staging(statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalStaticPartitionOverwrite:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_static_partition_overwrite.sql": (
                INCREMENTAL_STATIC_PARTITION_OVERWRITE_SQL
            ),
        }

    def test_static_partition_overwrite_replaces_only_named_partition(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        relation = relation_from_name(
            project.adapter,
            "incremental_static_partition_overwrite",
        )
        results, statements = _run_and_capture_sql("incremental_static_partition_overwrite")
        assert len(results) == 1

        rows = project.run_sql(
            f"select part_id, value from {relation} order by part_id",
            fetch="all",
        )
        assert rows == [
            (1, "static_new_p1"),
            (2, "static_unchanged_p2"),
        ]

        overwrite_statements = [
            statement
            for statement in statements
            if "insert overwrite" in statement
            and "incremental_static_partition_overwrite" in statement
        ]
        assert len(overwrite_statements) == 1
        assert "partition(`p1`)" in overwrite_statements[0].replace(" ", "")
        _assert_no_physical_dbt_staging(statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalDynamicPartitionOverwrite:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_dynamic_partition_overwrite.sql": (
                INCREMENTAL_DYNAMIC_PARTITION_OVERWRITE_SQL
            ),
        }

    def test_dynamic_partition_overwrite_preserves_unseen_partitions(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        relation = relation_from_name(
            project.adapter,
            "incremental_dynamic_partition_overwrite",
        )
        results, statements = _run_and_capture_sql("incremental_dynamic_partition_overwrite")
        assert len(results) == 1

        rows = project.run_sql(
            f"select part_id, value from {relation} order by part_id",
            fetch="all",
        )
        assert rows == [
            (1, "dynamic_new_p1"),
            (2, "dynamic_unchanged_p2"),
        ]

        overwrite_statements = [
            statement
            for statement in statements
            if "insert overwrite" in statement
            and "incremental_dynamic_partition_overwrite" in statement
        ]
        assert len(overwrite_statements) == 1
        assert "partition(*)" in overwrite_statements[0].replace(" ", "")
        _assert_no_physical_dbt_staging(statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalMicrobatch:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "microbatch_input.sql": MICROBATCH_INPUT_SQL,
            "incremental_microbatch.sql": INCREMENTAL_MICROBATCH_SQL,
        }

    def test_microbatch_replaces_exact_and_empty_batches(self, project):
        model_name = "incremental_microbatch"
        run_args = ["run"]
        invocation_time = datetime.combine(
            MICROBATCH_TODAY,
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        second_partition = f"dbt_mb_{MICROBATCH_DATE_2:%Y%m%d}"
        third_partition = f"dbt_mb_{MICROBATCH_DATE_3:%Y%m%d}"

        with patch_microbatch_end_time(
            invocation_time.strftime("%Y-%m-%d %H:%M:%S")
        ):
            results, initial_statements = _run_and_capture_sql(
                model_name,
                run_args,
            )
        assert len(results) == 2

        initial_overwrites = [
            statement
            for statement in initial_statements
            if "insert overwrite" in statement
            and model_name in statement
        ]
        assert len(initial_overwrites) == 2
        assert any(
            f"partition(`{second_partition}`)" in statement.replace(" ", "")
            for statement in initial_overwrites
        )
        assert any(
            f"partition(`{third_partition}`)" in statement.replace(" ", "")
            for statement in initial_overwrites
        )
        assert not any(
            "partition(*)" in statement.replace(" ", "")
            for statement in initial_overwrites
        )
        added_partitions = [
            statement
            for statement in initial_statements
            if "alter table" in statement and "add partition" in statement
        ]
        assert len(added_partitions) == 2
        _assert_no_physical_dbt_staging(initial_statements)

        relation = relation_from_name(project.adapter, model_name)
        input_relation = relation_from_name(project.adapter, "microbatch_input")
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == [
            (1, "first"),
            (2, "second"),
            (3, "third"),
        ]

        # Re-running the lookback after deleting all source rows for one batch
        # must empty that target partition. PARTITION(*) would leave id=3.
        project.run_sql(f"delete from {input_relation} where id = 3")
        with patch_microbatch_end_time(
            invocation_time.strftime("%Y-%m-%d %H:%M:%S")
        ):
            results, empty_batch_statements = _run_and_capture_sql(
                model_name,
                ["run", "--select", model_name],
            )
        assert len(results) == 1
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == [
            (1, "first"),
            (2, "second"),
        ]

        empty_batch_overwrites = [
            statement
            for statement in empty_batch_statements
            if "insert overwrite" in statement
            and model_name in statement
        ]
        assert len(empty_batch_overwrites) == 2
        assert any(
            f"partition(`{third_partition}`)" in statement.replace(" ", "")
            for statement in empty_batch_overwrites
        )
        assert not any(
            "add partition" in statement
            for statement in empty_batch_statements
        )
        assert len(
            [
                statement
                for statement in empty_batch_statements
                if "create or replace view" in statement
                and "__dbt_tmp_" in statement
            ]
        ) == 2
        _assert_no_physical_dbt_staging(empty_batch_statements)
        assert _dbt_helper_relations(project, relation) == []

        # Explicit Core event-time bounds backfill only the requested batch.
        project.run_sql(
            f"insert into {input_relation} values "
            f"(3, '{MICROBATCH_DATE_3.isoformat()} 00:00:00', 'third')"
        )
        results, backfill_statements = _run_and_capture_sql(
            model_name,
            [
                "run",
                "--select",
                model_name,
                "--event-time-start",
                MICROBATCH_DATE_3.isoformat(),
                "--event-time-end",
                MICROBATCH_TODAY.isoformat(),
            ],
        )
        assert len(results) == 1
        backfill_overwrites = [
            statement
            for statement in backfill_statements
            if "insert overwrite" in statement and model_name in statement
        ]
        assert len(backfill_overwrites) == 1
        assert (
            f"partition(`{third_partition}`)"
            in backfill_overwrites[0].replace(" ", "")
        )
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == [
            (1, "first"),
            (2, "second"),
            (3, "third"),
        ]
        _assert_no_physical_dbt_staging(backfill_statements)

        project.run_sql(f"delete from {input_relation} where id = 3")

        # A full refresh uses CTAS for the first batch, then the same exact
        # partition-overwrite path for every remaining batch. It must not copy
        # the complete model through an additional physical staging table.
        with patch_microbatch_end_time(
            invocation_time.strftime("%Y-%m-%d %H:%M:%S")
        ):
            results, full_refresh_statements = _run_and_capture_sql(
                model_name,
                ["run", "--full-refresh", "--select", model_name],
            )
        assert len(results) == 1
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == [
            (1, "first"),
            (2, "second"),
        ]
        full_refresh_overwrites = [
            statement
            for statement in full_refresh_statements
            if "insert overwrite" in statement and model_name in statement
        ]
        assert len(full_refresh_overwrites) == 2
        assert len(
            [
                statement
                for statement in full_refresh_statements
                if "create table" in statement
                and f"{model_name}__dbt_tmp" in statement
                and " as " in statement
            ]
        ) == 1
        _assert_no_physical_dbt_staging(full_refresh_overwrites)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalMicrobatchDynamicPartitions:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "microbatch_input.sql": MICROBATCH_INPUT_SQL,
            "incremental_microbatch_dynamic.sql": (
                INCREMENTAL_MICROBATCH_DYNAMIC_SQL
            ),
        }

    def test_dynamic_partitions_are_resolved_without_manual_add(self, project):
        model_name = "incremental_microbatch_dynamic"
        invocation_time = datetime.combine(
            MICROBATCH_TODAY,
            datetime.min.time(),
            tzinfo=timezone.utc,
        )

        with patch_microbatch_end_time(
            invocation_time.strftime("%Y-%m-%d %H:%M:%S")
        ):
            results, statements = _run_and_capture_sql(model_name, ["run"])
        assert len(results) == 2

        overwrite_statements = [
            statement
            for statement in statements
            if "insert overwrite" in statement and model_name in statement
        ]
        assert len(overwrite_statements) == 2
        assert any(
            f"partition(`dyn{MICROBATCH_DATE_2:%Y%m%d}`)"
            in statement.replace(" ", "")
            for statement in overwrite_statements
        )
        assert any(
            f"partition(`dyn{MICROBATCH_DATE_3:%Y%m%d}`)"
            in statement.replace(" ", "")
            for statement in overwrite_statements
        )
        assert not any("add partition" in statement for statement in statements)
        _assert_no_physical_dbt_staging(statements)

        relation = relation_from_name(project.adapter, model_name)
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == [
            (1, "first"),
            (2, "second"),
            (3, "third"),
        ]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalVarcharWidening:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_varchar_widen.sql": INCREMENTAL_VARCHAR_WIDEN_SQL,
        }

    def test_ignore_widens_string_without_physical_staging(self, project):
        relation = relation_from_name(
            project.adapter,
            "incremental_varchar_widen",
        )
        project.run_sql(
            f"create table {relation} ("
            "`id` int, `name` varchar(5)"
            ") duplicate key(`id`) "
            "distributed by hash(`id`) buckets auto "
            'properties("replication_num" = "1")'
        )
        project.run_sql(f"insert into {relation} values (1, 'a')")

        results, statements = _run_and_capture_sql("incremental_varchar_widen")
        assert len(results) == 1

        rows = project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        )
        assert rows == [(1, "a"), (2, "expanded")]

        column_type = project.run_sql(
            "select column_type from information_schema.columns "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}' "
            "and column_name = 'name'",
            fetch="one",
        )[0]
        widened_size = int(column_type.lower().removeprefix("varchar(").removesuffix(")"))
        assert widened_size >= 40
        assert any(
            "create or replace view" in statement and "__dbt_tmp" in statement
            for statement in statements
        )
        _assert_no_physical_dbt_staging(statements)
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalKeyWidening:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_key_widen.sql": INCREMENTAL_KEY_WIDEN_SQL}

    def test_default_ignore_rejects_unique_key_type_change_before_alter(self, project):
        relation = relation_from_name(project.adapter, "incremental_key_widen")
        project.run_sql(
            f"create table {relation} ("
            "`id` varchar(5), `value` varchar(20)"
            ") unique key(`id`) "
            "distributed by hash(`id`) buckets auto "
            'properties("replication_num" = "1", '
            '"enable_unique_key_merge_on_write" = "true")'
        )
        project.run_sql(f"insert into {relation} values ('old', 'original')")

        failure = run_dbt(["run"], expect_pass=False)
        assert len(failure.results) == 1
        assert "--full-refresh" in failure.results[0].message

        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [("old", "original")]
        column_type = project.run_sql(
            "select column_type from information_schema.columns "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}' "
            "and column_name = 'id'",
            fetch="one",
        )[0]
        assert column_type.lower() == "varchar(5)"
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalCaseOnlySchemaChange:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_case_only_schema.sql": (
                INCREMENTAL_CASE_ONLY_SCHEMA_SQL
            ),
        }

    def test_case_only_alias_change_is_not_add_drop(self, project):
        relation = relation_from_name(
            project.adapter,
            "incremental_case_only_schema",
        )
        project.run_sql(
            f"create table {relation} ("
            "`id` int, `value` varchar(20)"
            ") duplicate key(`id`) "
            "distributed by hash(`id`) buckets auto "
            'properties("replication_num" = "1")'
        )
        project.run_sql(f"insert into {relation} values (1, 'original')")

        results, statements = _run_and_capture_sql(
            "incremental_case_only_schema"
        )
        assert len(results) == 1
        _assert_physical_staging(statements)
        assert project.run_sql(
            f"select id, value from {relation} order by id",
            fetch="all",
        ) == [(1, "original"), (2, "new")]
        columns = project.run_sql(
            "select column_name from information_schema.columns "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}' "
            "order by ordinal_position",
            fetch="all",
        )
        assert columns == [("id",), ("value",)]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalSchemaChangeRetry:
    @classmethod
    @pytest.fixture(scope="class")
    def models(cls):
        return {
            "incremental_schema_change_retry.sql": (
                INCREMENTAL_SCHEMA_CHANGE_RETRY_SQL
            ),
        }

    def test_alter_precedes_dml_and_retry_replaces_frozen_batch(self, project):
        model_name = "incremental_schema_change_retry"
        run_args = ["run", "--select", model_name]
        relation = relation_from_name(project.adapter, model_name)
        temp_name = f"{relation.identifier}__dbt_tmp"

        initial, initial_statements = _run_and_capture_sql(
            model_name,
            run_args,
        )
        assert len(initial) == 1
        _assert_direct_initial_ctas(initial_statements, model_name)
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [(1, "original")]
        assert "`added_value`" not in project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert _dbt_helper_relations(project, relation) == []

        failure, statements = _run_and_capture_sql(
            model_name,
            run_args + ["--vars", "{emit_duplicate_keys: true}"],
            expect_pass=False,
        )

        assert len(failure.results) == 1
        failure_message = failure.results[0].message.lower()
        assert "json" in failure_message
        assert "parse" in failure_message
        _assert_physical_staging(statements)
        staging_index = next(
            index
            for index, statement in enumerate(statements)
            if "create table" in statement
            and temp_name in statement
            and " as " in statement
        )
        alter_index = next(
            index
            for index, statement in enumerate(statements)
            if "alter table" in statement
            and relation.identifier in statement
            and "add column" in statement
        )
        target_dml_index = next(
            index
            for index, statement in enumerate(statements)
            if statement in _target_dml_statements(statements)
        )
        assert staging_index < alter_index < target_dml_index
        assert any(
            alter_index < index < target_dml_index
            and "show alter table column" in statement
            for index, statement in enumerate(statements)
        )
        target_dml = _target_dml_statements(statements)[0]
        assert "dbt_internal_duplicate_keys" in target_dml
        assert "count(*) over" in target_dml
        assert project.run_sql(
            f"select id, value, added_value from {relation} order by id",
            fetch="all",
        ) == [(1, "original", None)]
        assert "`added_value`" in project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert _dbt_helper_relations(project, relation) == [(temp_name,)]
        assert project.run_sql(
            "select id, value, added_value "
            f"from `{relation.schema}`.`{temp_name}` order by value",
            fetch="all",
        ) == [
            (1, "conflict_a", 10),
            (1, "conflict_b", 20),
        ]

        results, retry_statements = _run_and_capture_sql(
            model_name,
            run_args,
        )
        assert len(results) == 1
        _assert_physical_staging(retry_statements)
        stale_drop_index = next(
            index
            for index, statement in enumerate(retry_statements)
            if "drop table if exists" in statement and temp_name in statement
        )
        new_staging_index = next(
            index
            for index, statement in enumerate(retry_statements)
            if "create table" in statement
            and temp_name in statement
            and " as " in statement
        )
        assert stale_drop_index < new_staging_index
        retry_dml_index = next(
            index
            for index, statement in enumerate(retry_statements)
            if statement in _target_dml_statements(retry_statements)
        )
        assert any(
            retry_dml_index < index
            and "drop table if exists" in statement
            and temp_name in statement
            for index, statement in enumerate(retry_statements)
        )
        assert not any(
            "alter table" in statement and "add column" in statement
            for statement in retry_statements
        )
        assert project.run_sql(
            f"select id, value, added_value from {relation} order by id",
            fetch="all",
        ) == [
            (1, "updated", 30),
            (2, "new", 40),
        ]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalCustomStrategy:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_custom_strategy.sql": (
                INCREMENTAL_CUSTOM_STRATEGY_SQL
            ),
        }

    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "incremental_custom_strategy.sql": (
                INCREMENTAL_CUSTOM_STRATEGY_MACRO
            ),
        }

    def test_custom_strategy_uses_frozen_physical_staging(self, project):
        assert len(run_dbt(["run"])) == 1

        results, statements = _run_and_capture_sql(
            "incremental_custom_strategy"
        )
        assert len(results) == 1
        _assert_physical_staging(statements)
        assert not any(
            "create or replace view" in statement
            for statement in statements
        )
        staging_ctas = next(
            statement
            for statement in statements
            if "create table" in statement
            and "__dbt_tmp" in statement
            and " as " in statement
        )
        compact_staging_ctas = staging_ctas.replace(" ", "")
        assert "distributedbyrandombucketsauto" in compact_staging_ctas
        assert "distributedbyhash" not in compact_staging_ctas
        assert "duplicatekey" not in compact_staging_ctas
        assert (
            '"enable_duplicate_without_keys_by_default"="true"'
            in compact_staging_ctas
        )
        assert '"replication_num"="1"' in compact_staging_ctas

        target_dml = _target_dml_statements(statements)[0]
        assert "dbt_custom_source.`id` >= 0" in target_dml

        relation = relation_from_name(
            project.adapter,
            "incremental_custom_strategy",
        )
        assert project.run_sql(
            f"select id, measure, value from {relation} order by id",
            fetch="all",
        ) == [
            (1, 1.0, "initial"),
            (2, 2.5, "incremental"),
        ]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalFullRefresh:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_fr.sql": INCREMENTAL_FULL_REFRESH_SQL}

    def test_full_refresh(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "incremental_fr")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 1

        results = run_dbt(["run"])
        assert len(results) == 1
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 2

        results, statements = _run_and_capture_sql(
            "incremental_fr",
            ["run", "--full-refresh"],
        )
        assert len(results) == 1

        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 1

        create_table = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert "duplicate key" in create_table
        assert "distributed by hash(`id`)" in create_table
        assert '"disable_auto_compaction" = "true"' in create_table

        # Full refresh intentionally builds an intermediate table before the
        # atomic REPLACE WITH TABLE. The no-staging rule applies to ordinary
        # incremental DML, not to safe full-refresh replacement.
        intermediate_ctas = [
            statement
            for statement in statements
            if "create table" in statement
            and "incremental_fr__dbt_tmp" in statement
            and " as " in statement
        ]
        assert len(intermediate_ctas) == 1
        assert _target_dml_statements(statements) == []
        exchanges = [
            statement
            for statement in statements
            if "replace with table" in statement
            and "incremental_fr__dbt_tmp" in statement
        ]
        assert len(exchanges) == 1
        assert not any(
            "create or replace view" in statement
            for statement in statements
        )
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalViewToTable:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_view_to_table.sql": INCREMENTAL_VIEW_TO_TABLE_SQL,
        }

    def test_view_with_as_identifier_is_replaced_by_table(self, project):
        relation = relation_from_name(
            project.adapter,
            "incremental_view_to_table",
        )
        backup_name = f"{relation.identifier}__dbt_backup"
        project.run_sql(
            f"create view `{relation.schema}`.`{backup_name}` as "
            "select -1 as `ASSET_ID`, 'stale_backup' as `value`"
        )
        project.run_sql(
            f"create view {relation} "
            "(`ASSET_ID` comment 'identifier AS label', `value`) "
            "comment 'view AS metadata' as "
            "select 99 as `ASSET_ID`, 'old_view' as `value`"
        )

        results = run_dbt(["run"])
        assert len(results) == 1

        rows = project.run_sql(
            f"select `ASSET_ID`, `value` from {relation}",
            fetch="all",
        )
        assert rows == [(7, "new_table")]
        table_type = project.run_sql(
            "select table_type from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}'",
            fetch="one",
        )[0]
        assert table_type == "BASE TABLE"
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalBackupRecovery:
    @pytest.fixture(scope="class")
    def models(self):
        return {"incremental_recovery.sql": INCREMENTAL_RECOVERY_SQL}

    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "create_no_backslash_recovery_view.sql": (
                CREATE_NO_BACKSLASH_RECOVERY_VIEW_MACRO
            ),
        }

    def test_keeps_backup_marker_until_a_full_retry_succeeds(self, project):
        relation = relation_from_name(project.adapter, "incremental_recovery")
        backup_name = f"{relation.identifier}__dbt_backup"
        operation = run_dbt(
            [
                "run-operation",
                "create_no_backslash_recovery_view",
                "--args",
                (
                    "{schema_name: "
                    f"{relation.schema}, view_name: {backup_name}"
                    "}"
                ),
            ]
        )
        assert len(operation) == 1
        original_backup_rows = project.run_sql(
            f"select floating_value, id, value "
            f"from `{relation.schema}`.`{backup_name}`",
            fetch="all",
        )
        assert len(original_backup_rows) == 1
        assert original_backup_rows[0][:2] == (1.5, 99)

        temp_name = f"{relation.identifier}__dbt_tmp"
        project.run_sql(
            f"create table `{relation.schema}`.`{temp_name}` "
            "(`sentinel` int) duplicate key(`sentinel`) "
            "distributed by hash(`sentinel`) buckets 1 "
            'properties ("replication_num" = "1")'
        )
        project.run_sql(
            f"insert into `{relation.schema}`.`{temp_name}` values (-1)"
        )
        assert _dbt_helper_relations(project, relation) == [
            (backup_name,),
            (temp_name,),
        ]

        failure, failure_statements = _run_and_capture_sql(
            "incremental_recovery",
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert any(
            "drop table if exists" in statement and temp_name in statement
            for statement in failure_statements
        )

        # The failed recovery run must not publish the old snapshot at the
        # canonical Table name. Keeping only the backup name makes the next
        # dbt compilation render the model's full (non-incremental) branch.
        assert project.run_sql(
            "select count(*) from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}'",
            fetch="one",
        )[0] == 0
        rows = project.run_sql(
            f"select floating_value, id, value "
            f"from `{relation.schema}`.`{backup_name}`",
            fetch="all",
        )
        assert rows == original_backup_rows
        backup_type = project.run_sql(
            "select table_type from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{backup_name}'",
            fetch="one",
        )[0]
        assert backup_type == "VIEW"
        assert _dbt_helper_relations(project, relation) == [(backup_name,)]

        project.run_sql(
            "create table "
            f"`{relation.schema}`.dbt_incremental_intentional_missing_relation "
            "(id int, value varchar(20)) "
            "duplicate key(id) distributed by hash(id) buckets 1 "
            'properties ("replication_num" = "1")'
        )
        project.run_sql(
            "insert into "
            f"`{relation.schema}`.dbt_incremental_intentional_missing_relation "
            "values (7, 'rebuilt')"
        )

        retry = run_dbt(["run"])
        assert len(retry) == 1
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [(7, "rebuilt")]
        create_sql = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower().replace(" ", "")
        assert "duplicatekey(`id`)" in create_sql
        assert "distributedbyhash(`id`)" in create_sql
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalViewReplacementFailures:
    @classmethod
    @pytest.fixture(scope="class")
    def models(cls):
        return {
            "incremental_view_build_failure.sql": INCREMENTAL_RECOVERY_SQL,
            "incremental_view_pre_hook_failure.sql": (
                INCREMENTAL_VIEW_PRE_HOOK_FAILURE_SQL
            ),
        }

    def test_snapshot_survives_replacement_build_failure_and_retry(
        self,
        project,
    ):
        model_name = "incremental_view_build_failure"
        relation = relation_from_name(project.adapter, model_name)
        backup_name = f"{relation.identifier}__dbt_backup"
        project.run_sql(
            f"create view {relation} as "
            "select 99 as id, 'old_view' as value"
        )
        source_rows = project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        )

        failure, statements = _run_and_capture_sql(
            model_name,
            ["run", "--select", model_name],
            expect_pass=False,
        )

        assert len(failure.results) == 1
        assert "dbt_incremental_intentional_missing_relation" in (
            failure.results[0].message
        )
        snapshot_ctas = [
            statement
            for statement in statements
            if "create table" in statement
            and backup_name in statement
            and f"select * from {relation}" in statement
        ]
        replacement_ctas = [
            statement
            for statement in statements
            if "create table" in statement
            and f"{relation.identifier}__dbt_tmp" in statement
            and "dbt_incremental_intentional_missing_relation" in statement
        ]
        assert len(snapshot_ctas) == 1
        assert len(replacement_ctas) == 1
        assert statements.index(snapshot_ctas[0]) < statements.index(
            replacement_ctas[0]
        )
        assert _target_dml_statements(statements) == []
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == source_rows
        assert project.run_sql(
            f"select id, value from `{relation.schema}`.`{backup_name}`",
            fetch="all",
        ) == source_rows
        relation_types = project.run_sql(
            "select table_name, table_type from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name in ('{relation.identifier}', '{backup_name}') "
            "order by table_name",
            fetch="all",
        )
        assert relation_types == [
            (relation.identifier, "VIEW"),
            (backup_name, "BASE TABLE"),
        ]
        assert _dbt_helper_relations(project, relation) == [(backup_name,)]

        set_model_file(project, relation, INCREMENTAL_VIEW_DEFAULT_SQL)
        retry = run_dbt(["run", "--select", model_name])
        assert len(retry) == 1
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [(7, "new_table")]
        assert _dbt_helper_relations(project, relation) == []

    def test_snapshot_precedes_pre_hook_failure_and_retry(self, project):
        model_name = "incremental_view_pre_hook_failure"
        relation = relation_from_name(project.adapter, model_name)
        backup_name = f"{relation.identifier}__dbt_backup"
        project.run_sql(
            f"create view {relation} as "
            "select 99 as id, 'old_view' as value"
        )
        source_rows = project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        )

        failure, statements = _run_and_capture_sql(
            model_name,
            ["run", "--select", model_name],
            expect_pass=False,
        )

        assert len(failure.results) == 1
        assert "__dbt_incremental_view_pre_hook__" in (
            failure.results[0].message
        )
        snapshot_ctas = [
            statement
            for statement in statements
            if "create table" in statement
            and backup_name in statement
            and f"select * from {relation}" in statement
        ]
        pre_hook_indexes = [
            index
            for index, statement in enumerate(statements)
            if "__dbt_incremental_view_pre_hook__" in statement
        ]
        assert len(snapshot_ctas) == 1
        assert len(pre_hook_indexes) == 1
        assert statements.index(snapshot_ctas[0]) < pre_hook_indexes[0]
        assert not any(
            "create table" in statement
            and f"{relation.identifier}__dbt_tmp" in statement
            for statement in statements
        )
        assert _target_dml_statements(statements) == []
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == source_rows
        assert project.run_sql(
            f"select id, value from `{relation.schema}`.`{backup_name}`",
            fetch="all",
        ) == source_rows
        assert _dbt_helper_relations(project, relation) == [(backup_name,)]

        set_model_file(project, relation, INCREMENTAL_VIEW_DEFAULT_SQL)
        retry = run_dbt(["run", "--select", model_name])
        assert len(retry) == 1
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [(7, "new_table")]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisViewSnapshotPreconditions:
    @classmethod
    @pytest.fixture(scope="class")
    def macros(cls):
        return {
            "view_snapshot_failure_macros.sql": (
                VIEW_SNAPSHOT_FAILURE_MACROS
            ),
        }

    def test_invalid_snapshot_relations_execute_no_mutating_sql_or_drop(
        self,
        project,
    ):
        source = relation_from_name(project.adapter, "snapshot_guard_source")
        destination = relation_from_name(
            project.adapter,
            "snapshot_guard_destination",
        )
        project.run_sql(
            f"create view {source} as select 7 as id, 'source' as value"
        )
        source_rows = project.run_sql(
            f"select id, value from {source}",
            fetch="all",
        )

        same_name_failure, same_name_statements = _run_and_capture_sql(
            "snapshot_view_for_test",
            [
                "run-operation",
                "snapshot_view_for_test",
                "--args",
                (
                    "{schema_name: "
                    f"{source.schema}, view_name: {source.identifier}, "
                    f"snapshot_name: {source.identifier}"
                    "}"
                ),
            ],
            expect_pass=False,
        )
        assert len(same_name_failure.results) == 1
        assert "must be different" in same_name_failure.results[0].message
        assert same_name_statements == []
        assert project.run_sql(
            f"select id, value from {source}",
            fetch="all",
        ) == source_rows

        project.run_sql(
            f"create table {destination} ("
            "`id` int, `value` varchar(20)"
            ") duplicate key(`id`) "
            "distributed by hash(`id`) buckets 1 "
            'properties ("replication_num" = "1")'
        )
        project.run_sql(
            f"insert into {destination} values (9, 'destination')"
        )
        destination_rows = project.run_sql(
            f"select id, value from {destination}",
            fetch="all",
        )

        existing_failure, existing_statements = _run_and_capture_sql(
            "snapshot_view_for_test",
            [
                "run-operation",
                "snapshot_view_for_test",
                "--args",
                (
                    "{schema_name: "
                    f"{source.schema}, view_name: {source.identifier}, "
                    f"snapshot_name: {destination.identifier}"
                    "}"
                ),
            ],
            expect_pass=False,
        )
        assert len(existing_failure.results) == 1
        assert "must not already exist" in existing_failure.results[0].message
        assert existing_statements
        assert any(
            "information_schema" in statement
            or "mv_infos" in statement
            for statement in existing_statements
        )
        assert not any(
            re.search(
                r"\b(create|drop|alter|insert|delete|replace|truncate)\b",
                statement,
            )
            for statement in existing_statements
        )
        assert project.run_sql(
            f"select id, value from {source}",
            fetch="all",
        ) == source_rows
        assert project.run_sql(
            f"select id, value from {destination}",
            fetch="all",
        ) == destination_rows
        relation_types = project.run_sql(
            "select table_name, table_type from information_schema.tables "
            f"where table_schema = '{source.schema}' "
            f"and table_name in ('{source.identifier}', "
            f"'{destination.identifier}') order by table_name",
            fetch="all",
        )
        assert relation_types == [
            (destination.identifier, "BASE TABLE"),
            (source.identifier, "VIEW"),
        ]


class TestDorisIncrementalViewNoBackslashMode:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_view_no_backslash.sql": (
                INCREMENTAL_VIEW_NO_BACKSLASH_SQL
            ),
        }

    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "create_no_backslash_recovery_view.sql": (
                CREATE_NO_BACKSLASH_RECOVERY_VIEW_MACRO
            ),
            "view_snapshot_failure_macros.sql": (
                VIEW_SNAPSHOT_FAILURE_MACROS
            ),
        }

    def test_view_replacement_snapshots_under_no_backslash_mode(
            self,
            project,
    ):
        relation = relation_from_name(
            project.adapter,
            "incremental_view_no_backslash",
        )
        backup_name = f"{relation.identifier}__dbt_backup"

        # Reverse SQL-mode direction: create the canonical View under
        # NO_BACKSLASH_ESCAPES, then replace it from a default-mode model. The
        # injected rename failure leaves the physical snapshot available for
        # an exact data assertion.
        created_mode_view = run_dbt(
            [
                "run-operation",
                "create_no_backslash_recovery_view",
                "--args",
                (
                    "{schema_name: "
                    f"{relation.schema}, view_name: {relation.identifier}"
                    "}"
                ),
            ]
        )
        assert len(created_mode_view) == 1
        reverse_source_rows = project.run_sql(
            f"select floating_value, id, value, `odd\"name` from {relation}",
            fetch="all",
        )
        assert len(reverse_source_rows) == 1
        assert reverse_source_rows[0][0:2] == (1.5, 99)
        assert reverse_source_rows[0][3] == 7
        set_model_file(project, relation, INCREMENTAL_VIEW_DEFAULT_SQL)
        reverse_failure, reverse_statements = _run_and_capture_sql(
            "incremental_view_no_backslash",
            [
                "run",
                "--vars",
                "{fail_intermediate_rename: true}",
            ],
            expect_pass=False,
        )
        assert len(reverse_failure.results) == 1
        assert project.run_sql(
            f"select floating_value, id, value, `odd\"name` "
            f"from `{relation.schema}`.`{backup_name}`",
            fetch="all",
        ) == reverse_source_rows
        assert len([
            statement
            for statement in reverse_statements
            if "create table" in statement
            and backup_name in statement
            and f"select * from {relation}" in statement
        ]) == 1
        assert len(run_dbt(["run"])) == 1
        assert _dbt_helper_relations(project, relation) == []

        project.run_sql(f"drop table {relation}")
        set_model_file(project, relation, INCREMENTAL_VIEW_NO_BACKSLASH_SQL)

        # Forward SQL-mode direction: source View uses the default mode while
        # the replacement model runs with NO_BACKSLASH_ESCAPES.
        project.run_sql(
            f"create view {relation} as "
            "select cast(1.5 as double) as floating_value, "
            "99 as id, 'C:\\new\\path' as value"
        )
        source_rows = project.run_sql(
            f"select floating_value, id, value from {relation}",
            fetch="all",
        )

        # Exercise a real Doris CTAS failure. The helper must leave its source
        # View intact and queryable when the snapshot table cannot be created.
        invalid_snapshot_name = "x" * 65
        failed_snapshot, failed_snapshot_statements = _run_and_capture_sql(
            "snapshot_view_for_test",
            [
                "run-operation",
                "snapshot_view_for_test",
                "--args",
                (
                    "{schema_name: "
                    f"{relation.schema}, view_name: {relation.identifier}, "
                    f"snapshot_name: {invalid_snapshot_name}"
                    "}"
                ),
            ],
            expect_pass=False,
        )
        assert len(failed_snapshot.results) == 1
        assert len(
            [
                statement
                for statement in failed_snapshot_statements
                if "create table" in statement
                and invalid_snapshot_name in statement
            ]
        ) == 1
        assert project.run_sql(
            f"select floating_value, id, value from {relation}",
            fetch="all",
        ) == source_rows

        project.run_sql(
            f"create view `{relation.schema}`.`{backup_name}` as "
            "select -1 as id, 'stale_backup' as value"
        )

        failure, statements = _run_and_capture_sql(
            "incremental_view_no_backslash",
            [
                "run",
                "--vars",
                "{fail_intermediate_rename: true}",
            ],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert project.run_sql(
            "select table_type from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{backup_name}'",
            fetch="one",
        )[0] == "BASE TABLE"
        assert project.run_sql(
            f"select floating_value, id, value "
            f"from `{relation.schema}`.`{backup_name}`",
            fetch="all",
        ) == source_rows
        snapshot_ctas = [
            statement
            for statement in statements
            if "create table" in statement
            and backup_name in statement
            and f"select * from {relation}" in statement
        ]
        assert len(snapshot_ctas) == 1
        sql_header_indexes = [
            index
            for index, statement in enumerate(statements)
            if "set sql_mode='no_backslash_escapes'" in statement
        ]
        assert len(sql_header_indexes) == 1
        assert statements.index(snapshot_ctas[0]) < sql_header_indexes[0]
        assert _target_dml_statements(statements) == []

        # A retry keeps the physical backup marker in place while it builds the
        # canonical target from scratch; it must not treat the old snapshot as
        # an incremental target.
        results = run_dbt(["run"])
        assert len(results) == 1
        assert project.run_sql(
            f"select id, value from {relation}",
            fetch="all",
        ) == [(7, "new_table")]
        assert _dbt_helper_relations(project, relation) == []


class TestDorisIncrementalOnSchemaChange(BaseIncrementalOnSchemaChange):
    """Run dbt's 1.12 schema-change contract against logical source views."""

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "models": {
                "+properties": {
                    "replication_num": "1",
                }
            }
        }

    def test_run_incremental_fail_on_schema_change(self, project):
        relation = relation_from_name(project.adapter, "incremental_fail")
        set_model_file(
            project,
            relation,
            INCREMENTAL_FAIL_TARGET_STABILITY_SQL,
        )
        initial = run_dbt(
            [
                "run",
                "--select",
                "model_a incremental_fail",
                "--full-refresh",
            ]
        )
        assert len(initial) == 2

        ddl_before = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1]
        columns_before = project.run_sql(
            "select column_name, column_type, is_nullable, "
            "column_default, extra from information_schema.columns "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}' "
            "order by ordinal_position",
            fetch="all",
        )
        rows_before = project.run_sql(
            f"select id, field1, field3 from {relation} order by id",
            fetch="all",
        )
        assert _dbt_helper_relations(project, relation) == []

        failure, statements = _run_and_capture_sql(
            "incremental_fail",
            ["run", "--select", "incremental_fail"],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert "Compilation Error" in failure.results[0].message
        assert "out of sync" in failure.results[0].message

        staging_ctas = [
            statement
            for statement in statements
            if "create table" in statement
            and "incremental_fail__dbt_tmp" in statement
            and " as " in statement
        ]
        assert len(staging_ctas) == 1
        assert _target_dml_statements(statements) == []
        assert not any("alter table" in statement for statement in statements)
        assert not any("replace with table" in statement for statement in statements)

        assert project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1] == ddl_before
        assert project.run_sql(
            "select column_name, column_type, is_nullable, "
            "column_default, extra from information_schema.columns "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}' "
            "order by ordinal_position",
            fetch="all",
        ) == columns_before
        assert project.run_sql(
            f"select id, field1, field3 from {relation} order by id",
            fetch="all",
        ) == rows_before
        assert _dbt_helper_relations(project, relation) == []
