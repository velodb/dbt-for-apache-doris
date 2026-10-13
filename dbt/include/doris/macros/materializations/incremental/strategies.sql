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

{#
    Doris strategy macros keep dbt Core's standard arg_dict keys and accept four
    adapter-specific keys:

      source_sql               compiled model SQL for a direct, single DML
      temp_relation_exists     whether temp_relation is a named source
      overwrite_partitions     optional whole/static/dynamic overwrite scope
      microbatch_partition     exact physical partition for the current batch

    append, merge, insert_overwrite and microbatch may inline source_sql. The
    materialization can instead provide a logical schema view or a frozen
    physical stage through the same contract.
#}

{% macro doris__get_incremental_default_sql(arg_dict) %}
    {% set effective_strategy = doris__effective_incremental_strategy(
        'default',
        arg_dict.get('unique_key')
    ) %}
    {% if effective_strategy == 'merge' %}
        {{ return(doris__get_incremental_merge_sql(arg_dict)) }}
    {% endif %}
    {{ return(doris__get_incremental_append_sql(arg_dict)) }}
{% endmacro %}


{% macro doris__get_incremental_append_sql(arg_dict) %}
    {% set target_relation = arg_dict['target_relation'] %}
    {% set dest_columns = arg_dict['dest_columns'] %}
    insert into {{ target_relation }}
        ({{ doris__incremental_dest_columns_csv(dest_columns) }})
    {{ doris__incremental_source_select(arg_dict) }}
{% endmacro %}


{% macro doris__get_incremental_merge_sql(arg_dict) %}
    {% if doris__is_iceberg_catalog(arg_dict['target_relation']) %}
        {{ return(doris__get_iceberg_merge_sql(arg_dict)) }}
    {% endif %}
    {# A full-row Unique Key INSERT is Doris's portable 2.1+ upsert for both
       Merge-on-Write and Merge-on-Read targets. Native MERGE INTO is reserved
       for conditional/partial 4.1+ operations. #}
    {% set target_relation = arg_dict['target_relation'] %}
    {% set dest_columns = arg_dict['dest_columns'] %}
    insert into {{ target_relation }}
        ({{ doris__incremental_dest_columns_csv(dest_columns) }})
    {{ doris__validated_unique_source_select(arg_dict) }}
{% endmacro %}


{% macro doris__get_iceberg_merge_sql(arg_dict) %}
    {% set columns = arg_dict['dest_columns'] %}
    {% set keys = doris__normalize_unique_key(arg_dict['unique_key']) %}
    merge into {{ arg_dict['target_relation'] }} DBT_INTERNAL_DEST
    using (
        {% if arg_dict.get('source_keys_validated', false) %}
            {{ doris__incremental_source_select(arg_dict) }}
        {% else %}
            {{ doris__validated_unique_source_select(arg_dict) }}
        {% endif %}
    ) DBT_INTERNAL_SOURCE
    on {% for key in keys %}
        DBT_INTERNAL_DEST.{{ adapter.quote(key) }} <=> DBT_INTERNAL_SOURCE.{{ adapter.quote(key) }}
        {% if not loop.last %} and {% endif %}
    {% endfor %}
    when matched then update set
        {% for column in columns %}
            {{ adapter.quote(column.name) }} = DBT_INTERNAL_SOURCE.{{ adapter.quote(column.name) }}
            {% if not loop.last %}, {% endif %}
        {% endfor %}
    when not matched then insert ({{ doris__incremental_dest_columns_csv(columns) }})
    values (
        {% for column in columns %}
            DBT_INTERNAL_SOURCE.{{ adapter.quote(column.name) }}{% if not loop.last %}, {% endif %}
        {% endfor %}
    )
{% endmacro %}


{% macro doris__get_incremental_insert_overwrite_sql(arg_dict) %}
    {% set target_relation = arg_dict['target_relation'] %}
    {% set dest_columns = arg_dict['dest_columns'] %}
    {% if arg_dict.get('overwrite_partitions') is not none and doris__is_iceberg_catalog(target_relation) %}
        {% set values = doris__iceberg_partition_values(arg_dict['overwrite_partitions']) %}
        {% if 'null' in values.values() %}
            {# Doris's native static NULL predicate leaves old rows behind.
               Replace this complete logical scope in one V2 MERGE instead. #}
            {% set predicates = [] %}
            {% for column, value in values.items() %}
                {% do predicates.append('DBT_INTERNAL_DEST.' ~ adapter.quote(column) ~ ' <=> ' ~ value) %}
            {% endfor %}
            {{ return(doris__get_iceberg_replace_sql(arg_dict, predicates | join(' and '))) }}
        {% endif %}
        {% set names = values.keys() | map('lower') | list %}
        {% set dest_columns = [] %}
        {% for column in arg_dict['dest_columns'] %}
            {% if column.name | lower not in names %}{% do dest_columns.append(column) %}{% endif %}
        {% endfor %}
        {% if not dest_columns %}
            {% do exceptions.raise_compiler_error("Iceberg static overwrite needs a non-partition column") %}
        {% endif %}
        {% set source_args = {} %}
        {% do source_args.update(arg_dict) %}
        {% do source_args.update({'dest_columns': dest_columns}) %}
        insert overwrite table {{ target_relation }}
        partition ({% for column, value in values.items() %}
            {{ adapter.quote(column) }}={{ value }}{% if not loop.last %}, {% endif %}
        {% endfor %})
        ({{ doris__incremental_dest_columns_csv(dest_columns) }})
        {{ doris__incremental_source_select(source_args) }}
    {% else %}
    {% set partition_clause = doris__overwrite_partition_clause(
        arg_dict.get('overwrite_partitions')
    ) %}
    insert overwrite table {{ target_relation }}
        {{ partition_clause }}
        ({{ doris__incremental_dest_columns_csv(dest_columns) }})
    {{ doris__incremental_source_select(arg_dict) }}
    {% endif %}
{% endmacro %}


{% macro doris__get_incremental_microbatch_sql(arg_dict) %}
    {% if doris__is_iceberg_catalog(arg_dict['target_relation']) %}
        {{ return(doris__get_iceberg_microbatch_sql(arg_dict)) }}
    {% endif %}
    {# Resolve one exact physical RANGE partition before overwriting it. Unlike
       PARTITION(*), a named partition is replaced even when this batch emits
       zero rows, which gives dbt Microbatch its full-batch replacement
       semantics. #}
    {% set partition = arg_dict.get('microbatch_partition', none) %}
    {% if partition is none %}
        {% set partition = doris__resolve_microbatch_partition(
            arg_dict['target_relation']
        ) %}
    {% endif %}
    {% do arg_dict.update({'overwrite_partitions': [partition]}) %}
    {{ return(doris__get_incremental_insert_overwrite_sql(arg_dict)) }}
{% endmacro %}


{% macro doris__get_iceberg_microbatch_sql(arg_dict) %}
    {% set batch = doris__microbatch_context() %}
    {% set start = batch['event_time_start'].strftime('%Y-%m-%d %H:%M:%S.%f') %}
    {% set end = batch['event_time_end'].strftime('%Y-%m-%d %H:%M:%S.%f') %}
    {% set predicate = 'DBT_INTERNAL_DEST.' ~ adapter.quote(config.get('event_time'))
        ~ " >= '" ~ start ~ "' and DBT_INTERNAL_DEST." ~ adapter.quote(config.get('event_time'))
        ~ " < '" ~ end ~ "'" %}
    {{ return(doris__get_iceberg_replace_sql(arg_dict, predicate)) }}
{% endmacro %}


{% macro doris__get_iceberg_replace_sql(arg_dict, predicate=none) %}
    {% set columns = arg_dict['dest_columns'] %}
    {% set names = columns | map(attribute='name') | map('lower') | list %}
    {% set marker = namespace(name=none) %}
    {% for index in range((columns | length) + 1) %}
        {% set name = 'DBT_INTERNAL_BATCH_OPERATION_' ~ index %}
        {% if marker.name is none and name | lower not in names %}{% set marker.name = name %}{% endif %}
    {% endfor %}
    {# Each old row matches exactly one deletion sentinel; I rows never match.
       Whole-table replacement uses NULL/non-NULL buckets so the join keeps
       a real target expression instead of degenerating into a Cartesian join. #}
    {% set delete_modes = ['D', 'N'] if predicate is none else ['D'] %}
    merge into {{ arg_dict['target_relation'] }} DBT_INTERNAL_DEST
    using (
        {% for mode in delete_modes %}
            {% if not loop.first %} union all {% endif %}
            select {% for column in columns %}
                cast(null as {{ column.data_type }}) as {{ adapter.quote(column.name) }},
            {% endfor %} '{{ mode }}' as {{ adapter.quote(marker.name) }}
        {% endfor %}
        union all
        select DBT_INTERNAL_BATCH.*, 'I' as {{ adapter.quote(marker.name) }}
        from ({{ doris__incremental_source_select(arg_dict) }}) DBT_INTERNAL_BATCH
    ) DBT_INTERNAL_SOURCE
    on DBT_INTERNAL_SOURCE.{{ adapter.quote(marker.name) }} = case when
        {% if predicate is none %}
            DBT_INTERNAL_DEST.{{ adapter.quote(columns[0].name) }} is not null
        {% else %}
            {{ predicate }}
        {% endif %}
        then 'D' else '{{ 'N' if predicate is none else 'O' }}' end
    when matched then delete
    when not matched and DBT_INTERNAL_SOURCE.{{ adapter.quote(marker.name) }}='I' then
        insert ({{ doris__incremental_dest_columns_csv(columns) }}) values (
            {% for column in columns %}
                DBT_INTERNAL_SOURCE.{{ adapter.quote(column.name) }}{% if not loop.last %}, {% endif %}
            {% endfor %}
        )
{% endmacro %}
