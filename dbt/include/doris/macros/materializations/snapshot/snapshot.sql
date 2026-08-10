-- Licensed to the Apache Software Foundation (ASF) under one
-- or more contributor license agreements. See the NOTICE file
-- distributed with this work for additional information
-- regarding copyright ownership. The ASF licenses this file
-- to you under the Apache License, Version 2.0 (the
-- "License"); you may not use this file except in compliance
-- with the License. You may obtain a copy of the License at
--
-- http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing,
-- software distributed under the License is distributed on an
-- "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
-- KIND, either express or implied. See the License for the
-- specific language governing permissions and limitations
-- under the License.

{% macro doris__snapshot_string_as_time(timestamp) -%}
  {%- set result = "str_to_date('" ~ timestamp ~ "', '%Y-%m-%d %T')" -%}
  {{ return(result) }}
{%- endmacro %}


{% macro doris__snapshot_get_time() -%}
  {# This expression is intentionally statement-stable. dbt renders it more
     than once when applying dbt_valid_to_current. #}
  current_timestamp()
{%- endmacro %}


{% macro doris__snapshot_upsert_relation(target) -%}
  {{ return(target.incorporate(
      path={"identifier": target.identifier ~ "__snapshot_upsert"},
      type="table"
  )) }}
{%- endmacro %}


{% macro doris__snapshot_initial_relation(target) -%}
  {{ return(target.incorporate(
      path={"identifier": target.identifier ~ "__snapshot_initial"},
      type="table"
  )) }}
{%- endmacro %}


{% macro doris__snapshot_type_can_write(source_column, target_column) -%}
  {% set source_type = source_column.expanded_data_type | lower %}
  {% set target_type = target_column.expanded_data_type | lower %}
  {% if source_type == target_type %}
    {{ return(true) }}
  {% endif %}

  {% set source_base = source_type.split('(')[0] | trim %}
  {% set target_base = target_type.split('(')[0] | trim %}
  {% set string_types = [
      'char', 'varchar', 'character varying', 'string', 'text'
  ] %}
  {% set integer_ranks = {
      'tinyint': 1, 'smallint': 2, 'int': 3, 'integer': 3,
      'bigint': 4, 'largeint': 5
  } %}
  {% set float_ranks = {'float': 1, 'double': 2} %}
  {% set datetime_types = ['datetime', 'datetimev2', 'timestamp'] %}

  {% if source_base in string_types and target_base in string_types %}
    {# get_column_schema_from_query reports a generic CHARACTER VARYING(256)
       for string expressions. It is an inference placeholder, not a declared
       source width, so only its type family is safe to compare. #}
    {% if source_base == 'character varying' %}
      {{ return(true) }}
    {% endif %}
    {% if target_base in ['string', 'text'] %}
      {{ return(true) }}
    {% endif %}
    {% if source_base in ['string', 'text'] %}
      {{ return(false) }}
    {% endif %}
    {{ return((source_column.char_size or 1) <=
              (target_column.char_size or 1)) }}
  {% endif %}

  {% if source_base in integer_ranks and target_base in integer_ranks %}
    {{ return(integer_ranks[source_base] <= integer_ranks[target_base]) }}
  {% endif %}

  {% if source_base in float_ranks and target_base in float_ranks %}
    {{ return(float_ranks[source_base] <= float_ranks[target_base]) }}
  {% endif %}

  {% if source_base == 'date' %}
    {{ return(target_base == 'date' or target_base in datetime_types) }}
  {% endif %}

  {% if source_base in datetime_types and target_base in datetime_types %}
    {% set source_precision = (
        source_type.split('(')[1].split(')')[0] | int
        if '(' in source_type else 0
    ) %}
    {% set target_precision = (
        target_type.split('(')[1].split(')')[0] | int
        if '(' in target_type else 0
    ) %}
    {{ return(source_precision <= target_precision) }}
  {% endif %}

  {% if source_base in ['decimal', 'decimalv2', 'decimalv3'] and
        target_base in ['decimal', 'decimalv2', 'decimalv3'] %}
    {% set source_precision = source_column.numeric_precision or 0 %}
    {% set source_scale = source_column.numeric_scale or 0 %}
    {% set target_precision = target_column.numeric_precision or 0 %}
    {% set target_scale = target_column.numeric_scale or 0 %}
    {{ return(source_scale <= target_scale and
              source_precision - source_scale <=
              target_precision - target_scale) }}
  {% endif %}

  {{ return(false) }}
{%- endmacro %}


{% macro doris__snapshot_current_predicate(relation_alias, columns) -%}
  {% set valid_to = relation_alias ~ "." ~ adapter.quote(columns.dbt_valid_to) %}
  {% if config.get('dbt_valid_to_current') %}
    ({{ valid_to }} = {{ config.get('dbt_valid_to_current') }} or {{ valid_to }} is null)
  {% else %}
    {{ valid_to }} is null
  {% endif %}
{%- endmacro %}


{% macro doris__snapshot_unique_key_projection(unique_key) -%}
  {% if unique_key | is_list %}
    {% for key in unique_key %}
      {{ key }} as dbt_unique_key_{{ loop.index }}{% if not loop.last %},{% endif %}
    {% endfor %}
  {% else %}
    {{ unique_key }} as dbt_unique_key
  {% endif %}
{%- endmacro %}


{% macro doris__snapshot_unique_key_columns(unique_key, relation_alias=none) -%}
  {% set prefix = (relation_alias ~ ".") if relation_alias else "" %}
  {% if unique_key | is_list %}
    {% for key in unique_key %}
      {{ prefix }}dbt_unique_key_{{ loop.index }}{% if not loop.last %},{% endif %}
    {% endfor %}
  {% else %}
    {{ prefix }}dbt_unique_key
  {% endif %}
{%- endmacro %}


{% macro doris__snapshot_unique_key_null_predicate(unique_key, relation_alias) -%}
  {% if unique_key | is_list %}
    {% for key in unique_key %}
      {{ relation_alias }}.dbt_unique_key_{{ loop.index }} is null
      {% if not loop.last %} or {% endif %}
    {% endfor %}
  {% else %}
    {{ relation_alias }}.dbt_unique_key is null
  {% endif %}
{%- endmacro %}


{% macro doris__validate_snapshot_source(strategy, source_sql, target=none) -%}
  {% if not config.get('snapshot_validate', true) %}
    {{ return(none) }}
  {% endif %}

  {% set columns = config.get('snapshot_table_column_names') or
      get_snapshot_table_column_names() %}
  {% set strategy_name = config.get('strategy') %}

  {% set validation_sql %}
    with snapshot_query as (
      {{ source_sql }}
    ),
    source_keys as (
      select
        {{ doris__snapshot_unique_key_projection(strategy.unique_key) }}
        {% if strategy_name == 'timestamp' %}
          , {{ strategy.updated_at }} as dbt_snapshot_updated_at
        {% endif %}
      from snapshot_query
    ),
    duplicate_keys as (
      select {{ doris__snapshot_unique_key_columns(strategy.unique_key) }}
      from source_keys
      group by {{ doris__snapshot_unique_key_columns(strategy.unique_key) }}
      having count(*) > 1
    )
    {% if strategy_name == 'timestamp' and target is not none %}
      , target_keys as (
        select
          {{ doris__snapshot_unique_key_projection(strategy.unique_key) }},
          {{ adapter.quote(columns.dbt_valid_from) }} as dbt_snapshot_valid_from
        from {{ target }} as snapshot_target
        where {{ doris__snapshot_current_predicate('snapshot_target', columns) }}
      )
    {% endif %}
    select
      (select count(*) from source_keys as source_key
       where {{ doris__snapshot_unique_key_null_predicate(
           strategy.unique_key, 'source_key'
       ) }}) as null_keys,
      (select count(*) from duplicate_keys) as duplicate_keys
      {% if strategy_name == 'timestamp' %}
        , (select count(*) from source_keys
           where dbt_snapshot_updated_at is null) as null_updated_at
        {% if target is not none %}
          , (select count(*)
             from source_keys as source_key
             join target_keys as target_key
               on {{ unique_key_join_on(
                   strategy.unique_key, 'target_key', 'source_key'
               ) }}
             where source_key.dbt_snapshot_updated_at
                   < target_key.dbt_snapshot_valid_from) as regressed_updated_at
        {% endif %}
      {% endif %}
  {% endset %}

  {% set result = run_query(validation_sql) %}
  {% if execute %}
    {% set null_keys = result.rows[0][0] | int %}
    {% set duplicate_keys = result.rows[0][1] | int %}
    {% set null_updated_at = (
        result.rows[0][2] | int if strategy_name == 'timestamp' else 0
    ) %}
    {% set regressed_updated_at = (
        result.rows[0][3] | int
        if strategy_name == 'timestamp' and target is not none else 0
    ) %}

    {% if null_keys > 0 %}
      {% do exceptions.raise_compiler_error(
          "Snapshot source contains " ~ null_keys ~
          " row(s) with a NULL unique_key. Snapshot unique keys must be " ~
          "non-NULL and unique before Doris history is updated."
      ) %}
    {% endif %}
    {% if duplicate_keys > 0 %}
      {% do exceptions.raise_compiler_error(
          "Snapshot source contains " ~ duplicate_keys ~
          " duplicate unique_key value(s). Deduplicate the source query " ~
          "before running dbt snapshot."
      ) %}
    {% endif %}
    {% if null_updated_at > 0 %}
      {% do exceptions.raise_compiler_error(
          "Timestamp Snapshot source contains " ~ null_updated_at ~
          " row(s) with a NULL updated_at value."
      ) %}
    {% endif %}
    {% if regressed_updated_at > 0 %}
      {% do exceptions.raise_compiler_error(
          "Timestamp Snapshot source contains " ~ regressed_updated_at ~
          " row(s) whose updated_at is earlier than the current history " ~
          "version. Timestamp Snapshot requires monotonic updated_at values."
      ) %}
    {% endif %}
  {% endif %}
{%- endmacro %}


{% macro doris__validate_snapshot_upsert(relation) -%}
  {% if not config.get('snapshot_validate', true) %}
    {{ return(none) }}
  {% endif %}

  {% set columns = config.get('snapshot_table_column_names') or
      get_snapshot_table_column_names() %}
  {% set unique_key = config.get('unique_key') %}
  {% set scd_id = adapter.quote(columns.dbt_scd_id) %}
  {% set valid_from = adapter.quote(columns.dbt_valid_from) %}
  {% set valid_to = adapter.quote(columns.dbt_valid_to) %}

  {% set validation_sql %}
    with duplicate_scd_ids as (
      select {{ scd_id }}
      from {{ relation }}
      group by {{ scd_id }}
      having count(*) > 1
    ),
    duplicate_current_versions as (
      select {{ unique_key if unique_key is string else unique_key | join(', ') }}
      from {{ relation }} as snapshot_upsert
      where {{ doris__snapshot_current_predicate('snapshot_upsert', columns) }}
      group by {{ unique_key if unique_key is string else unique_key | join(', ') }}
      having count(*) > 1
    )
    select
      (select count(*) from {{ relation }} where {{ scd_id }} is null),
      (select count(*) from {{ relation }} where {{ valid_from }} is null),
      (select count(*) from {{ relation }}
       where {{ valid_to }} is not null and {{ valid_from }} > {{ valid_to }}),
      (select count(*) from duplicate_scd_ids),
      (select count(*) from duplicate_current_versions)
  {% endset %}
  {% set validation = run_query(validation_sql) %}

  {% if execute %}
    {% set null_scd_ids = validation.rows[0][0] | int %}
    {% set null_valid_from = validation.rows[0][1] | int %}
    {% set invalid_windows = validation.rows[0][2] | int %}
    {% set duplicate_scd_ids = validation.rows[0][3] | int %}
    {% set duplicate_current_versions = validation.rows[0][4] | int %}

    {% if null_scd_ids > 0 or null_valid_from > 0 or
          invalid_windows > 0 or duplicate_scd_ids > 0 or
          duplicate_current_versions > 0 %}
      {% do exceptions.raise_compiler_error(
          "Doris Snapshot validation failed before atomic replacement: " ~
          "null dbt_scd_id=" ~ null_scd_ids ~
          ", null dbt_valid_from=" ~ null_valid_from ~
          ", invalid validity windows=" ~ invalid_windows ~
          ", duplicate dbt_scd_id=" ~ duplicate_scd_ids ~
          ", duplicate current unique_key=" ~ duplicate_current_versions ~
          ". The existing Snapshot target was not replaced."
      ) %}
    {% endif %}
  {% endif %}
{%- endmacro %}


{% macro doris__snapshot_merge_sql(target, source, insert_cols) -%}
  {% set columns = config.get('snapshot_table_column_names') or
      get_snapshot_table_column_names() %}
  {% set valid_to_col = adapter.quote(columns.dbt_valid_to) %}
  {% set scd_id_col = adapter.quote(columns.dbt_scd_id) %}
  {% set upsert = doris__snapshot_upsert_relation(target) %}

  {# A failed prior run may have left a partially populated helper table. The
     target is still the authoritative history, so always rebuild from empty. #}
  {% do doris__drop_relation(upsert) %}

  {% call statement('create_snapshot_upsert_relation') %}
    create table {{ upsert }} like {{ target }}
  {% endcall %}

  {% set target_columns = adapter.get_columns_in_relation(target) %}
  {% set source_columns = adapter.get_columns_in_relation(source) %}
  {% set source_column_names = {} %}
  {% for column in source_columns %}
    {% do source_column_names.update({column.name | lower: column.name}) %}
  {% endfor %}

  {% call statement('insert_unchanged_snapshot_rows') %}
    insert into {{ upsert }} (
      {% for column in target_columns %}
        {{ column.quoted }}{% if not loop.last %},{% endif %}
      {% endfor %}
    )
    select
      {% for column in target_columns %}
        snapshot_target.{{ column.quoted }}{% if not loop.last %},{% endif %}
      {% endfor %}
    from {{ target }} as snapshot_target
    where not exists (
      select 1
      from {{ source }} as snapshot_source
      where snapshot_source.{{ scd_id_col }} = snapshot_target.{{ scd_id_col }}
    )
  {% endcall %}

  {% call statement('close_changed_snapshot_rows') %}
    insert into {{ upsert }} (
      {% for column in target_columns %}
        {{ column.quoted }}{% if not loop.last %},{% endif %}
      {% endfor %}
    )
    with updates_and_deletes as (
      select {{ scd_id_col }}, {{ valid_to_col }}
      from {{ source }}
      where dbt_change_type in ('update', 'delete')
    )
    select
      {% for column in target_columns %}
        {% if column.name | lower == columns.dbt_valid_to | lower %}
          updates_and_deletes.{{ valid_to_col }}
        {% else %}
          snapshot_target.{{ column.quoted }}
        {% endif %}
        {% if not loop.last %},{% endif %}
      {% endfor %}
    from {{ target }} as snapshot_target
    join updates_and_deletes
      on snapshot_target.{{ scd_id_col }} = updates_and_deletes.{{ scd_id_col }}
  {% endcall %}

  {% call statement('insert_new_snapshot_rows') %}
    insert into {{ upsert }} (
      {% for column in target_columns %}
        {{ column.quoted }}{% if not loop.last %},{% endif %}
      {% endfor %}
    )
    select
      {% for column in target_columns %}
        {% if column.name | lower in source_column_names %}
          snapshot_source.{{ adapter.quote(
              source_column_names[column.name | lower]
          ) }}
        {% else %}
          cast(null as {{ column.expanded_data_type }})
        {% endif %}
        {% if not loop.last %},{% endif %}
      {% endfor %}
    from {{ source }} as snapshot_source
    where snapshot_source.dbt_change_type = 'insert'
  {% endcall %}

  {% do doris__validate_snapshot_upsert(upsert) %}

  {# swap=false atomically installs the complete helper table under the target
     name. If this statement fails, Doris leaves the old target untouched. #}
  {% do exchange_relation(target, upsert, true) %}

  {% do return('select 1') %}
{%- endmacro %}


{% macro doris__post_snapshot(staging_relation) -%}
  {% do doris__drop_relation(staging_relation) %}
{%- endmacro %}


{% macro doris__create_columns(relation, columns) -%}
  {% if columns %}
    {% for column in columns %}
      {% set previous_job_id = adapter.get_latest_schema_change_job_id(relation) %}
      {% call statement('add_snapshot_column_' ~ loop.index) %}
        alter table {{ relation }} add column
          {{ adapter.quote(column.name) }} {{ column.expanded_data_type }}
      {% endcall %}
      {% do adapter.wait_for_schema_change(relation, previous_job_id) %}
    {% endfor %}
  {% endif %}
{%- endmacro %}
