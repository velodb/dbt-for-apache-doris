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

{% macro is_incremental() %}
    {% if not execute %}
        {{ return(False) }}
    {% else %}
        {% set relation = adapter.get_relation(this.database, this.schema, this.table) %}
        {{ return(relation is not none
                  and relation.type == 'table'
                  and model.config.materialized in ['incremental','partition']
                  and not should_full_refresh()) }}
    {% endif %}
{% endmacro %}


{% macro doris__normalize_unique_key(unique_key) %}
    {% if unique_key is none %}
        {{ return([]) }}
    {% elif unique_key is string %}
        {{ return([unique_key]) }}
    {% else %}
        {{ return(unique_key | list) }}
    {% endif %}
{% endmacro %}


{% macro doris__sequence_column_from_properties() %}
    {% set sequence_columns = [] %}
    {% set properties = config.get('properties', none) or {} %}
    {% for property_name, property_value in properties.items() %}
        {% if (property_name | string | lower) == 'function_column.sequence_col' %}
            {% do sequence_columns.append(property_value) %}
        {% endif %}
    {% endfor %}
    {% if sequence_columns %}
        {{ return(sequence_columns[0]) }}
    {% endif %}
    {{ return(none) }}
{% endmacro %}


{% macro doris__effective_incremental_strategy(strategy, unique_key) %}
    {% if strategy == 'default' %}
        {{ return('merge' if doris__normalize_unique_key(unique_key) else 'append') }}
    {% endif %}
    {{ return(strategy) }}
{% endmacro %}


{% macro doris__normalized_column_names(columns) %}
    {% set names = [] %}
    {% for column in columns %}
        {% do names.append(column.name | lower) %}
    {% endfor %}
    {{ return(names) }}
{% endmacro %}


{% macro doris__validate_source_unique_key_columns(source_columns, unique_key) %}
    {% set source_names = doris__normalized_column_names(source_columns) %}
    {% set missing_keys = [] %}
    {% for key in doris__normalize_unique_key(unique_key) %}
        {% if key | lower not in source_names %}
            {% do missing_keys.append(key) %}
        {% endif %}
    {% endfor %}

    {% if missing_keys %}
        {% set message -%}
Incremental model {{ model.unique_id }} does not return configured unique_key
column(s) {{ missing_keys }}. No target data has been changed.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% set sequence_column = doris__sequence_column_from_properties() %}
    {% if (
        sequence_column is not none
        and sequence_column | lower not in source_names
    ) %}
        {% set message -%}
Incremental model {{ model.unique_id }} does not return configured Doris
Sequence mapping column '{{ sequence_column }}'. No target data has been changed.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}
{% endmacro %}


{% macro doris__unique_key_first_columns(source_columns, unique_key) %}
    {# Doris requires all key columns to be the ordered prefix of the physical
       schema. A model query is free to return them in any position, so project
       the initial/full-refresh CTAS in configured key order first. #}
    {% set unique_keys = doris__normalize_unique_key(unique_key) %}
    {% set key_names = [] %}
    {% set ordered_columns = [] %}

    {% for key in unique_keys %}
        {% do key_names.append(key | lower) %}
        {% for column in source_columns %}
            {% if (column.name | lower) == (key | lower) %}
                {% do ordered_columns.append(column) %}
            {% endif %}
        {% endfor %}
    {% endfor %}

    {% for column in source_columns %}
        {% if column.name | lower not in key_names %}
            {% do ordered_columns.append(column) %}
        {% endif %}
    {% endfor %}

    {{ return(ordered_columns) }}
{% endmacro %}


{% macro doris__validate_unique_key_schema_changes(
    schema_changes,
    unique_key,
    source_relation=none
) %}
    {% set protected_columns = [] %}
    {% for key in doris__normalize_unique_key(unique_key) %}
        {% do protected_columns.append(key | lower) %}
    {% endfor %}
    {% set sequence_column = doris__sequence_column_from_properties() %}
    {% if sequence_column is not none %}
        {% do protected_columns.append(sequence_column | lower) %}
    {% endif %}

    {% set changed_protected_types = [] %}
    {% for type_change in schema_changes['new_target_types'] %}
        {% if type_change['column_name'] | lower in protected_columns %}
            {% do changed_protected_types.append(type_change['column_name']) %}
        {% endif %}
    {% endfor %}

    {% if changed_protected_types %}
        {% if source_relation is not none %}
            {% do adapter.drop_relation(source_relation) %}
        {% endif %}
        {% set message -%}
Incremental model {{ model.unique_id }} changes the data type of immutable Doris
UNIQUE KEY or Sequence mapping column(s) {{ changed_protected_types }}. Doris
cannot mutate these physical columns during an incremental run; use
--full-refresh.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}
{% endmacro %}


{% macro doris__incremental_dest_columns_csv(dest_columns) %}
    {% set quoted_columns = [] %}
    {% for column in dest_columns %}
        {% do quoted_columns.append(adapter.quote(column.name)) %}
    {% endfor %}
    {{ return(quoted_columns | join(', ')) }}
{% endmacro %}


{% macro doris__incremental_source_select(arg_dict) %}
    {% set dest_columns = arg_dict['dest_columns'] %}
    {% set source_sql = arg_dict.get('source_sql', none) %}
    {# dbt's public strategy contract contains only target_relation,
       temp_relation, unique_key, dest_columns and incremental_predicates.
       Doris's direct path adds source_sql, but packages calling this macro with
       the standard five keys must continue to read temp_relation. #}
    {% set temp_relation_exists = arg_dict.get(
        'temp_relation_exists',
        source_sql is none
    ) %}
    select
        {% for column in dest_columns -%}
        DBT_INTERNAL_SOURCE.{{ adapter.quote(column.name) }}{% if not loop.last %}, {% endif %}
        {%- endfor %}
    from
        {% if temp_relation_exists %}
        {{ arg_dict['temp_relation'] }} DBT_INTERNAL_SOURCE
        {% else %}
        (
            {{ source_sql }}
        ) DBT_INTERNAL_SOURCE
        {% endif %}
{% endmacro %}


{#
    Make duplicate source keys fail inside the same statement as a direct Unique
    Key upsert. The source is consumed once by a windowed derived table. For a
    duplicate row, json_parse receives a deliberately invalid sentinel and
    cancels the DML before Doris publishes it. Valid rows parse an empty JSON
    object, so the predicate remains true.

    Doris 2.1 restricts correlated scalar subqueries in binary predicates, so
    this must not use a multi-row scalar-subquery guard. Do not rewrite it as two
    consumers of a CTE either: Doris may inline both consumers, evaluating
    volatile model SQL twice and validating a different batch from the one
    inserted. The window-result alias is selected from n + 1 reserved candidates
    so it cannot collide with any of the n model columns.
#}
{% macro doris__validated_unique_source_select(arg_dict) %}
    {% set unique_key = doris__normalize_unique_key(arg_dict['unique_key']) %}
    {% set source_sql = arg_dict.get('source_sql', none) %}
    {% set temp_relation_exists = arg_dict.get(
        'temp_relation_exists',
        source_sql is none
    ) %}
    {% set dest_columns = arg_dict['dest_columns'] %}
    {% set dest_column_names = [] %}
    {% for column in dest_columns %}
        {% do dest_column_names.append(column.name | lower) %}
    {% endfor %}
    {# There are n destination names and n + 1 candidates, so one is free. #}
    {% set validation = namespace(column=none) %}
    {% for candidate_index in range((dest_columns | length) + 1) %}
        {% set candidate = 'DBT_INTERNAL_UNIQUE_KEY_VALIDATION_' ~ candidate_index %}
        {% if validation.column is none and candidate | lower not in dest_column_names %}
            {% set validation.column = candidate %}
        {% endif %}
    {% endfor %}

    select
        {% for column in dest_columns -%}
        DBT_INTERNAL_SOURCE.{{ adapter.quote(column.name) }}{% if not loop.last %}, {% endif %}
        {%- endfor %}
    from (
        select
            {% for column in dest_columns -%}
            DBT_INTERNAL_RAW_SOURCE.{{ adapter.quote(column.name) }},
            {%- endfor %}
            if(
                count(*) over (
                    partition by
                        {% for key in unique_key -%}
                        DBT_INTERNAL_RAW_SOURCE.{{ adapter.quote(key) }}{% if not loop.last %}, {% endif %}
                        {%- endfor %}
                ) > 1,
                2,
                1
            ) as {{ adapter.quote(validation.column) }}
        from (
            {% if temp_relation_exists %}
            select * from {{ arg_dict['temp_relation'] }}
            {% else %}
            {{ source_sql }}
            {% endif %}
        ) DBT_INTERNAL_RAW_SOURCE
    ) DBT_INTERNAL_SOURCE
    where json_parse(if(
        DBT_INTERNAL_SOURCE.{{ adapter.quote(validation.column) }} > 1,
        'DBT_INTERNAL_DUPLICATE_KEYS',
        '{}'
    )) is not null
{% endmacro %}


{% macro doris__validated_unique_ctas_source_sql(
    source_sql,
    unique_key,
    source_columns
) %}
    {% set arg_dict = {
        'source_sql': source_sql,
        'temp_relation_exists': false,
        'unique_key': unique_key,
        'dest_columns': source_columns
    } %}
    {{ return(doris__validated_unique_source_select(arg_dict)) }}
{% endmacro %}

{% macro doris__create_incremental_schema_view(relation, source_sql) %}
    create or replace view {{ relation }} as {{ source_sql }}
{% endmacro %}


{% macro doris__raise_schema_change_failure(schema_changes) %}
    {% set message -%}
The source and target schemas on incremental model {{ model.unique_id }} are
out of sync.

Source columns not in target: {{ schema_changes['source_not_in_target'] }}
Target columns not in source: {{ schema_changes['target_not_in_source'] }}
New column types: {{ schema_changes['new_target_types'] }}

Set on_schema_change to append_new_columns or sync_all_columns, update the
schema manually, or run:

    dbt run --full-refresh --select {{ model.name }}
    {%- endset %}
    {% do exceptions.raise_compiler_error(message) %}
{% endmacro %}


{% macro doris__overwrite_partition_clause(overwrite_partitions) %}
    {% if overwrite_partitions is none %}
        {{ return('') }}
    {% endif %}

    {% if overwrite_partitions is string %}
        {% set partitions = [overwrite_partitions] %}
    {% else %}
        {% set partitions = overwrite_partitions | list %}
    {% endif %}

    {% if partitions == ['*'] %}
        {{ return('partition(*)') }}
    {% endif %}

    {% set quoted_partitions = [] %}
    {% for partition in partitions %}
        {% do quoted_partitions.append(adapter.quote(partition)) %}
    {% endfor %}
    {{ return('partition(' ~ (quoted_partitions | join(', ')) ~ ')') }}
{% endmacro %}


{% macro doris__show_create_table(target_relation, statement_name='doris_incremental_show_create') %}
    {% call statement(statement_name, fetch_result=True) %}
        show create table {{ target_relation }}
    {% endcall %}
    {% set result = load_result(statement_name) %}
    {% if result is none or result['data'] | length == 0 %}
        {{ return('') }}
    {% endif %}
    {{ return(result['data'][0][1]) }}
{% endmacro %}


{% macro doris__get_table_model(target_relation) %}
    {% set create_table = doris__show_create_table(target_relation) | upper %}
    {% if 'UNIQUE KEY(' in create_table %}
        {{ return('unique') }}
    {% elif 'AGGREGATE KEY(' in create_table %}
        {{ return('aggregate') }}
    {% elif 'DUPLICATE KEY(' in create_table %}
        {{ return('duplicate') }}
    {% endif %}
    {{ return('unknown') }}
{% endmacro %}

{% macro doris__get_unique_key_columns(target_relation) %}
    {% call statement('doris_incremental_unique_key_columns', fetch_result=True) %}
        select column_name
        from information_schema.columns
        where table_schema = '{{ target_relation.schema | replace("'", "''") }}'
          and table_name = '{{ target_relation.identifier | replace("'", "''") }}'
          and column_key = 'UNI'
        order by ordinal_position
    {% endcall %}
    {% set result = load_result('doris_incremental_unique_key_columns') %}
    {% set columns = [] %}
    {% for row in result['data'] %}
        {% do columns.append(row[0]) %}
    {% endfor %}
    {{ return(columns) }}
{% endmacro %}


{% macro doris__validate_incremental_target(strategy, target_relation, unique_key) %}
    {% set table_model = doris__get_table_model(target_relation) %}

    {% if strategy == 'append' and table_model != 'duplicate' %}
        {% set message -%}
Doris incremental strategy 'append' requires a DUPLICATE KEY target, but
{{ target_relation }} is {{ table_model | upper }}. Rebuild the model with:

    dbt run --full-refresh --select {{ model.name }}
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if strategy == 'merge' %}
        {% if table_model != 'unique' %}
            {% set message -%}
Doris incremental strategy '{{ strategy }}' requires a UNIQUE KEY target, but
{{ target_relation }} is {{ table_model | upper }}. Rebuild the model with:

    dbt run --full-refresh --select {{ model.name }}
            {%- endset %}
            {% do exceptions.raise_compiler_error(message) %}
        {% endif %}

        {% set configured_keys = [] %}
        {% for key in doris__normalize_unique_key(unique_key) %}
            {% do configured_keys.append(key | lower) %}
        {% endfor %}
        {% set physical_keys = [] %}
        {% for key in doris__get_unique_key_columns(target_relation) %}
            {% do physical_keys.append(key | lower) %}
        {% endfor %}

        {% if configured_keys != physical_keys %}
            {% set message -%}
Doris incremental strategy '{{ strategy }}' configured unique_key
{{ configured_keys }}, but {{ target_relation }} uses physical UNIQUE KEY
{{ physical_keys }}. Rebuild it with:

    dbt run --full-refresh --select {{ model.name }}
            {%- endset %}
            {% do exceptions.raise_compiler_error(message) %}
        {% endif %}
    {% endif %}
{% endmacro %}


{# Backwards-compatible insert helper retained for packages that called it directly. #}
{% macro tmp_insert(tmp_relation, target_relation, unique_key=none, statement_name='main') %}
    {% set dest_columns = adapter.get_columns_in_relation(target_relation) %}
    {% set arg_dict = {
        'temp_relation': tmp_relation,
        'temp_relation_exists': true,
        'dest_columns': dest_columns
    } %}
    insert into {{ target_relation }}
        ({{ doris__incremental_dest_columns_csv(dest_columns) }})
    {{ doris__incremental_source_select(arg_dict) }}
{% endmacro %}


{% macro show_create(target_relation, statement_name='table_model') %}
    show create table {{ target_relation }}
{% endmacro %}


{% macro is_unique_model(target_relation) %}
    {{ return(doris__get_table_model(target_relation) == 'unique') }}
{% endmacro %}
