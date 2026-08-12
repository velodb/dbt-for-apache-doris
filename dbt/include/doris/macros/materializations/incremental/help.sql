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
                  and model.config.materialized == 'incremental'
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


{% macro doris__incremental_config_property(property_name) %}
    {% set values = [] %}
    {% set properties = config.get('properties', none) or {} %}
    {% for configured_name, configured_value in properties.items() %}
        {% if configured_name | string | lower == property_name | lower %}
            {% do values.append(configured_value) %}
        {% endif %}
    {% endfor %}
    {% if values %}
        {{ return(values[0]) }}
    {% endif %}
    {{ return(none) }}
{% endmacro %}


{% macro doris__microbatch_uses_dynamic_partitions() %}
    {% set enabled = doris__incremental_config_property(
        'dynamic_partition.enable'
    ) %}
    {{ return(enabled | string | lower == 'true') }}
{% endmacro %}


{% macro doris__show_create_property_value(create_table, property_name) %}
    {% set property = modules.re.search(
        '(?im)^[ \\t]*"' ~ modules.re.escape(property_name)
        ~ '"[ \\t]*=[ \\t]*"([^"\\r\\n]*)"[ \\t]*,?[ \\t]*\\r?$',
        create_table
    ) %}
    {% if property is not none %}
        {{ return(property.group(1)) }}
    {% endif %}
    {{ return(none) }}
{% endmacro %}


{% macro doris__validate_microbatch_target_properties(
    create_table,
    target_relation
) %}
    {% set configured_dynamic = doris__microbatch_uses_dynamic_partitions() %}
    {% set physical_enable = doris__show_create_property_value(
        create_table,
        'dynamic_partition.enable'
    ) %}
    {% set physical_dynamic = physical_enable | string | lower == 'true' %}
    {% if configured_dynamic != physical_dynamic %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch model " ~ model.unique_id ~ " configures "
            ~ ("Dynamic Partition" if configured_dynamic else "static partitions")
            ~ ", but existing target " ~ target_relation ~ " uses "
            ~ ("Dynamic Partition" if physical_dynamic else "static partitions")
            ~ ". Align the physical table or run --full-refresh."
        ) %}
    {% endif %}
    {% if not configured_dynamic %}
        {{ return(none) }}
    {% endif %}

    {% set property_names = [
        'dynamic_partition.time_unit',
        'dynamic_partition.prefix',
        'dynamic_partition.end',
        'dynamic_partition.create_history_partition'
    ] %}
    {% set mismatches = [] %}
    {% for property_name in property_names %}
        {% set configured_value = doris__incremental_config_property(
            property_name
        ) %}
        {% set physical_value = doris__show_create_property_value(
            create_table,
            property_name
        ) %}
        {% if configured_value | string | lower != physical_value | string | lower %}
            {% do mismatches.append(property_name) %}
        {% endif %}
    {% endfor %}

    {% set configured_timezone = doris__incremental_config_property(
        'dynamic_partition.time_zone'
    ) | string | lower %}
    {% set physical_timezone = doris__show_create_property_value(
        create_table,
        'dynamic_partition.time_zone'
    ) | string | lower %}
    {% set utc_timezones = ['utc', 'etc/utc', '+00:00'] %}
    {% if (
        configured_timezone not in utc_timezones
        or physical_timezone not in utc_timezones
    ) %}
        {% do mismatches.append('dynamic_partition.time_zone') %}
    {% endif %}

    {% for property_name in [
        'dynamic_partition.start',
        'dynamic_partition.history_partition_num',
        'dynamic_partition.start_day_of_month'
    ] %}
        {% set configured_value = doris__incremental_config_property(
            property_name
        ) %}
        {% if configured_value is not none %}
            {% set physical_value = doris__show_create_property_value(
                create_table,
                property_name
            ) %}
            {% if configured_value | string != physical_value | string %}
                {% do mismatches.append(property_name) %}
            {% endif %}
        {% endif %}
    {% endfor %}

    {% if mismatches %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch Dynamic Partition properties on existing target "
            ~ target_relation ~ " do not match model " ~ model.unique_id
            ~ ": " ~ (mismatches | unique | list | string)
            ~ ". Align the physical properties or run --full-refresh."
        ) %}
    {% endif %}
{% endmacro %}


{% macro doris__microbatch_context() %}
    {% set batch = model.get('batch', none) %}
    {% if batch is none %}
        {% do exceptions.raise_compiler_error(
            "Doris incremental strategy 'microbatch' requires dbt Core's "
            ~ "batched execution context model.batch on " ~ model.unique_id
            ~ ". Use dbt Core 1.12.x and run the model through dbt run."
        ) %}
    {% endif %}
    {% if (
        batch.get('id', none) is none
        or batch.get('event_time_start', none) is none
        or batch.get('event_time_end', none) is none
    ) %}
        {% do exceptions.raise_compiler_error(
            "Incomplete model.batch context for Doris microbatch model "
            ~ model.unique_id ~ "."
        ) %}
    {% endif %}
    {{ return(batch) }}
{% endmacro %}


{% macro doris__microbatch_generated_partition_name() %}
    {% set batch = doris__microbatch_context() %}
    {% set partition_name = 'dbt_mb_' ~ batch['id'] | replace('T', '') %}
    {% if not modules.re.fullmatch(
        '[A-Za-z_][A-Za-z0-9_]*',
        partition_name
    ) %}
        {% do exceptions.raise_compiler_error(
            "Could not generate a safe Doris partition name from model.batch.id "
            ~ "'" ~ batch['id'] ~ "' on " ~ model.unique_id ~ "."
        ) %}
    {% endif %}
    {{ return(partition_name) }}
{% endmacro %}


{% macro doris__microbatch_partition_by_clause() %}
    {% set batch = doris__microbatch_context() %}
    {% set partition_name = doris__microbatch_generated_partition_name() %}
    {% set event_time = config.get('event_time') %}
    {% set start = batch['event_time_start'].strftime('%Y-%m-%d %H:%M:%S') %}
    {% set end = batch['event_time_end'].strftime('%Y-%m-%d %H:%M:%S') %}
    {% set clause -%}
PARTITION BY RANGE ({{ adapter.quote(event_time) }}) (
    PARTITION {{ adapter.quote(partition_name) }}
    VALUES [("{{ start }}"), ("{{ end }}"))
)
    {%- endset %}
    {{ return(clause) }}
{% endmacro %}


{% macro doris__add_microbatch_partition_sql(target_relation) %}
    {% set batch = doris__microbatch_context() %}
    {% set partition_name = doris__microbatch_generated_partition_name() %}
    {% set start = batch['event_time_start'].strftime('%Y-%m-%d %H:%M:%S') %}
    {% set end = batch['event_time_end'].strftime('%Y-%m-%d %H:%M:%S') %}
    {% set add_sql -%}
alter table {{ target_relation }}
add partition {{ adapter.quote(partition_name) }}
values [("{{ start }}"), ("{{ end }}"))
    {%- endset %}
    {{ return(add_sql) }}
{% endmacro %}


{% macro doris__validate_microbatch_config(unique_key) %}
    {% set batch = doris__microbatch_context() %}
    {% set batch_size = config.get('batch_size', none) | string | lower %}
    {% set valid_batch_ids = {
        'hour': '[0-9]{8}T[0-9]{2}',
        'day': '[0-9]{8}',
        'month': '[0-9]{6}',
        'year': '[0-9]{4}'
    } %}
    {% if (
        batch_size not in valid_batch_ids
        or not modules.re.fullmatch(valid_batch_ids[batch_size], batch['id'])
    ) %}
        {% do exceptions.raise_compiler_error(
            "Invalid dbt Core model.batch.id '" ~ batch['id']
            ~ "' for batch_size='" ~ batch_size ~ "' on " ~ model.unique_id
            ~ ". dbt-doris supports Core 1.12.x batch identifiers."
        ) %}
    {% endif %}

    {% if doris__normalize_unique_key(unique_key) %}
        {% do exceptions.raise_compiler_error(
            "Doris incremental strategy 'microbatch' cannot be combined "
            ~ "with 'unique_key' on model " ~ model.unique_id
            ~ ". Microbatch replaces one complete time partition; use a "
            ~ "DUPLICATE KEY target instead."
        ) %}
    {% endif %}
    {% if config.get('overwrite_partitions', none) is not none %}
        {% do exceptions.raise_compiler_error(
            "Do not configure 'overwrite_partitions' for Doris microbatch "
            ~ "model " ~ model.unique_id ~ ". The adapter resolves the one "
            ~ "exact physical partition for each dbt batch."
        ) %}
    {% endif %}
    {% if config.get('partition_by_init', none) is not none %}
        {% do exceptions.raise_compiler_error(
            "Do not configure 'partition_by_init' for Doris microbatch model "
            ~ model.unique_id ~ ". The adapter creates the initial exact "
            ~ "[event_time_start, event_time_end) partition from model.batch."
        ) %}
    {% endif %}

    {% set event_time = config.get('event_time', none) %}
    {% if (
        event_time is not string
        or not modules.re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', event_time)
    ) %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch model " ~ model.unique_id
            ~ " requires 'event_time' to name one unquoted model column."
        ) %}
    {% endif %}

    {% set partition_by = config.get('partition_by', none) %}
    {% if partition_by is string %}
        {% set partition_columns = [partition_by] %}
    {% elif partition_by is none %}
        {% set partition_columns = [] %}
    {% else %}
        {% set partition_columns = partition_by | list %}
    {% endif %}
    {% if not partition_columns %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch model " ~ model.unique_id
            ~ " requires 'partition_by' on its event_time column."
        ) %}
    {% endif %}
    {% if (
        partition_columns | length != 1
        or partition_columns[0] | lower != event_time | lower
    ) %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch 'partition_by' and 'event_time' must name the "
            ~ "same column on model " ~ model.unique_id ~ "."
        ) %}
    {% endif %}
    {% if config.get('partition_type', 'RANGE') | upper != 'RANGE' %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch requires a single-column RANGE partition on "
            ~ model.unique_id ~ "."
        ) %}
    {% endif %}

    {% if doris__microbatch_uses_dynamic_partitions() %}
        {% set dynamic_unit = doris__incremental_config_property(
            'dynamic_partition.time_unit'
        ) %}
        {% if dynamic_unit | string | lower != batch_size %}
            {% do exceptions.raise_compiler_error(
                "Doris dynamic_partition.time_unit must equal dbt batch_size "
                ~ "on microbatch model " ~ model.unique_id ~ "."
            ) %}
        {% endif %}
        {% set dynamic_timezone = doris__incremental_config_property(
            'dynamic_partition.time_zone'
        ) %}
        {% set normalized_timezone = dynamic_timezone | string | lower %}
        {% if normalized_timezone not in ['utc', 'etc/utc', '+00:00'] %}
            {% do exceptions.raise_compiler_error(
                "Doris microbatch dynamic_partition.time_zone must be UTC on "
                ~ model.unique_id ~ " because dbt Core builds batch boundaries "
                ~ "in UTC."
            ) %}
        {% endif %}
        {% set create_history = doris__incremental_config_property(
            'dynamic_partition.create_history_partition'
        ) %}
        {% if create_history | string | lower != 'true' %}
            {% do exceptions.raise_compiler_error(
                "Doris microbatch model " ~ model.unique_id ~ " requires "
                ~ "dynamic_partition.create_history_partition='true'. Set "
                ~ "dynamic_partition.start far enough back to cover begin and "
                ~ "every requested backfill batch."
            ) %}
        {% endif %}
        {% set dynamic_prefix = doris__incremental_config_property(
            'dynamic_partition.prefix'
        ) %}
        {% if (
            dynamic_prefix is not string
            or not modules.re.fullmatch(
                '[A-Za-z_][A-Za-z0-9_]*',
                dynamic_prefix
            )
        ) %}
            {% do exceptions.raise_compiler_error(
                "Doris microbatch model " ~ model.unique_id
                ~ " requires a safe dynamic_partition.prefix."
            ) %}
        {% endif %}
        {% set dynamic_end = doris__incremental_config_property(
            'dynamic_partition.end'
        ) %}
        {% if not modules.re.fullmatch('[1-9][0-9]*', dynamic_end | string) %}
            {% do exceptions.raise_compiler_error(
                "Doris microbatch model " ~ model.unique_id ~ " requires a "
                ~ "positive integer dynamic_partition.end."
            ) %}
        {% endif %}
        {% set dynamic_start = doris__incremental_config_property(
            'dynamic_partition.start'
        ) %}
        {% set history_count = doris__incremental_config_property(
            'dynamic_partition.history_partition_num'
        ) %}
        {% if dynamic_start is none and history_count is none %}
            {% do exceptions.raise_compiler_error(
                "Doris microbatch model " ~ model.unique_id
                ~ " must configure dynamic_partition.start or "
                ~ "history_partition_num far enough back to cover begin and "
                ~ "requested backfills."
            ) %}
        {% endif %}
        {% if batch_size == 'month' %}
            {% set month_start = doris__incremental_config_property(
                'dynamic_partition.start_day_of_month'
            ) %}
            {% if month_start is not none and month_start | string != '1' %}
                {% do exceptions.raise_compiler_error(
                    "Doris monthly microbatch partitions must start on day 1 "
                    ~ "on model " ~ model.unique_id ~ "."
                ) %}
            {% endif %}
        {% endif %}
    {% endif %}
{% endmacro %}


{% macro doris__normalized_microbatch_boundary(boundary) %}
    {% set normalized = boundary | string | trim | replace('T', ' ') %}
    {% set normalized = modules.re.sub('[.]0+$', '', normalized) %}
    {% if modules.re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}', normalized) %}
        {% set normalized = normalized ~ ' 00:00:00' %}
    {% endif %}
    {{ return(normalized) }}
{% endmacro %}


{% macro doris__microbatch_partition_from_rows(
    rows,
    target_relation,
    allow_missing=false
) %}
    {% set batch = doris__microbatch_context() %}
    {% set event_time = config.get('event_time') | lower %}
    {% set expected_start = batch['event_time_start'].strftime(
        '%Y-%m-%d %H:%M:%S'
    ) %}
    {% set expected_end = batch['event_time_end'].strftime(
        '%Y-%m-%d %H:%M:%S'
    ) %}
    {% set exact_partitions = [] %}
    {% set overlapping_partitions = [] %}
    {% set unparsed_partitions = [] %}
    {% set physical_expressions = [] %}

    {% for row in rows %}
        {% set partition_method = row[1] | string | upper %}
        {% set partition_expression = (
            row[2] | string | trim | replace('`', '') | lower
        ) %}
        {% set description = row[3] | string | trim %}
        {% set boundaries = modules.re.fullmatch(
            "\\[\\('([^']+)'\\),\\s*\\('([^']+)'\\)\\)",
            description
        ) %}
        {% if partition_method == 'RANGE' %}
            {% if partition_expression not in physical_expressions %}
                {% do physical_expressions.append(partition_expression) %}
            {% endif %}
            {% if partition_expression == event_time %}
                {% if boundaries is none %}
                    {% do unparsed_partitions.append(row[0]) %}
                {% else %}
                    {% set physical_start = (
                        doris__normalized_microbatch_boundary(
                            boundaries.group(1)
                        )
                    ) %}
                    {% set physical_end = (
                        doris__normalized_microbatch_boundary(
                            boundaries.group(2)
                        )
                    ) %}
                    {% if (
                        physical_start == expected_start
                        and physical_end == expected_end
                    ) %}
                        {% do exact_partitions.append(row[0]) %}
                    {% elif (
                        physical_start < expected_end
                        and physical_end > expected_start
                    ) %}
                        {% do overlapping_partitions.append(row[0]) %}
                    {% endif %}
                {% endif %}
            {% endif %}
        {% endif %}
    {% endfor %}

    {% if physical_expressions != [event_time] %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch target " ~ target_relation
            ~ " must use one plain RANGE partition expression on event_time "
            ~ "column '" ~ config.get('event_time') ~ "', but found "
            ~ (physical_expressions | string) ~ "."
        ) %}
    {% endif %}
    {% if exact_partitions | length > 1 %}
        {% set message -%}
Doris microbatch target {{ target_relation }} must have exactly one existing
single-column RANGE partition on '{{ config.get('event_time') }}' for
[{{ expected_start }}, {{ expected_end }}). Found {{ exact_partitions | length }}
exact range partitions.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}
    {% if exact_partitions | length == 1 %}
        {{ return(exact_partitions[0]) }}
    {% endif %}
    {% if overlapping_partitions or unparsed_partitions %}
        {% do exceptions.raise_compiler_error(
            "Doris microbatch target " ~ target_relation ~ " has no exact "
            ~ "range partition for [" ~ expected_start ~ ", " ~ expected_end
            ~ ") and cannot safely create one because overlapping or "
            ~ "unparseable partitions exist: "
            ~ ((overlapping_partitions + unparsed_partitions) | string) ~ "."
        ) %}
    {% endif %}
    {% if allow_missing %}
        {{ return(none) }}
    {% endif %}
    {% set message -%}
Doris microbatch target {{ target_relation }} has no exact range partition on
'{{ config.get('event_time') }}' for [{{ expected_start }}, {{ expected_end }}).
Dynamic Partition is enabled, so dbt-doris cannot add it manually. Extend the
dynamic partition history window before retrying this batch.
    {%- endset %}
    {% do exceptions.raise_compiler_error(message) %}
{% endmacro %}


{% macro doris__resolve_microbatch_partition(
    target_relation,
    allow_missing=false
) %}
    {% call statement('doris_incremental_microbatch_partition', fetch_result=True) %}
        select partition_name,
               partition_method,
               partition_expression,
               partition_description
        from information_schema.partitions
        where table_schema = '{{ target_relation.schema | replace("'", "''") }}'
          and table_name = '{{ target_relation.identifier | replace("'", "''") }}'
          and partition_name is not null
    {% endcall %}
    {% set rows = load_result(
        'doris_incremental_microbatch_partition'
    )['data'] %}
    {{ return(doris__microbatch_partition_from_rows(
        rows,
        target_relation,
        allow_missing
    )) }}
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


{% macro doris__table_model_from_create_table(create_table) %}
    {# SHOW CREATE emits the physical key clause on its own line. Anchor the
       match there so arbitrary table/column comments containing text such as
       "UNIQUE KEY(" cannot spoof the target model. #}
    {% set key_clause = modules.re.search(
        '(?im)^[ \t]*(UNIQUE|AGGREGATE|DUPLICATE)[ \t]+KEY[ \t]*[(]',
        create_table
    ) %}
    {% set keyless_duplicate = modules.re.search(
        '(?im)^[ \t]*"enable_duplicate_without_keys_by_default"[ \t]*=[ \t]*"true"[ \t]*,?[ \t]*\r?$',
        create_table
    ) %}
    {% if key_clause is not none %}
        {{ return(key_clause.group(1) | lower) }}
    {% elif keyless_duplicate is not none %}
        {{ return('duplicate') }}
    {% endif %}
    {{ return('unknown') }}
{% endmacro %}


{% macro doris__get_table_model(target_relation) %}
    {% set create_table = doris__show_create_table(target_relation) %}
    {{ return(doris__table_model_from_create_table(create_table)) }}
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


{% macro doris__validate_incremental_sequence_mapping(
    create_table,
    target_relation,
    strategy='merge'
) %}
    {# Match only canonical SHOW CREATE property lines. Searching a compacted
       full DDL can mistake a table or column comment for a real property. This
       line format is stable across the supported Doris release families. #}
    {% set visible_property = modules.re.search(
        '(?im)^[ \t]*"function_column[.]sequence_col"[ \t]*=[ \t]*"([^"\r\n]+)"[ \t]*,?[ \t]*\r?$',
        create_table
    ) %}
    {% set hidden_property = modules.re.search(
        '(?im)^[ \t]*"function_column[.]sequence_type"[ \t]*=[ \t]*"[^"\r\n]+"[ \t]*,?[ \t]*\r?$',
        create_table
    ) %}
    {% set configured_sequence = doris__sequence_column_from_properties() %}
    {% set physical_sequence = (
        visible_property.group(1)
        if visible_property is not none
        else none
    ) %}

    {% if hidden_property is not none %}
        {% if strategy == 'merge' %}
            {% set message -%}
Doris incremental strategy '{{ strategy }}' cannot safely write {{ target_relation }}
because the existing table uses physical property
'function_column.sequence_type' and the hidden __DORIS_SEQUENCE_COL__. Rebuild
the model with a visible 'function_column.sequence_col' mapping:

    dbt run --full-refresh --select {{ model.name }}
            {%- endset %}
        {% else %}
            {% set message -%}
Doris incremental strategy '{{ strategy }}' cannot safely write {{ target_relation }}
because the existing table uses physical property
'function_column.sequence_type' and the hidden __DORIS_SEQUENCE_COL__. Rebuild
this target without hidden Sequence state:

    dbt run --full-refresh --select {{ model.name }}

To retain Sequence ordering, switch the model to strategy 'merge' with a visible
'function_column.sequence_col' mapping before rebuilding it.
            {%- endset %}
        {% endif %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if (
        strategy == 'merge'
        and configured_sequence is none
        and physical_sequence is not none
    ) %}
        {% set message -%}
Doris incremental strategy 'merge' target {{ target_relation }} uses physical
Sequence mapping column '{{ physical_sequence }}', but model {{ model.unique_id }}
does not configure 'function_column.sequence_col'. Restore the matching model
property, or rebuild the table without Sequence mapping using:

    dbt run --full-refresh --select {{ model.name }}
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if strategy == 'merge' and configured_sequence is not none and (
        physical_sequence is none
        or configured_sequence | lower != physical_sequence | lower
    ) %}
        {% set physical_description = (
            "no visible Sequence mapping"
            if physical_sequence is none
            else "Sequence mapping column '" ~ physical_sequence ~ "'"
        ) %}
        {% set message -%}
Doris incremental strategy 'merge' configured Sequence mapping column
'{{ configured_sequence }}', but {{ target_relation }} uses
{{ physical_description }}. An incremental run cannot change this physical table
property. Rebuild it with:

    dbt run --full-refresh --select {{ model.name }}
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}
{% endmacro %}


{% macro doris__validate_incremental_target(strategy, target_relation, unique_key) %}
    {% set create_table = doris__show_create_table(
        target_relation,
        statement_name='doris_incremental_validate_target'
    ) %}
    {% set table_model = doris__table_model_from_create_table(create_table) %}

    {% if strategy == 'append' and table_model != 'duplicate' %}
        {% set message -%}
Doris incremental strategy 'append' requires a DUPLICATE KEY target, but
{{ target_relation }} is {{ table_model | upper }}. Rebuild the model with:

    dbt run --full-refresh --select {{ model.name }}
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if strategy == 'microbatch' and table_model != 'duplicate' %}
        {% set message -%}
Doris incremental strategy 'microbatch' requires a DUPLICATE KEY target, but
{{ target_relation }} is {{ table_model | upper }}. Rebuild the model with:

    dbt run --full-refresh --select {{ model.name }}
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if strategy == 'microbatch' %}
        {% do doris__validate_microbatch_target_properties(
            create_table,
            target_relation
        ) %}
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

    {% if strategy in ['merge', 'insert_overwrite'] %}
        {% do doris__validate_incremental_sequence_mapping(
            create_table,
            target_relation,
            strategy
        ) %}
    {% endif %}

    {% if strategy == 'microbatch' %}
        {{ return(doris__resolve_microbatch_partition(
            target_relation,
            allow_missing=not doris__microbatch_uses_dynamic_partitions()
        )) }}
    {% endif %}
    {{ return(none) }}
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
