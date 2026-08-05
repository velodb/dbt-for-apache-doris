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

{% macro doris__materialized_view_identifier_values(name, values) -%}
    {%- if values is none -%}
        {{ return([]) }}
    {%- elif values is string -%}
        {%- set values = [values] -%}
    {%- elif values is not sequence or values is mapping -%}
        {{ exceptions.raise_compiler_error(
            "materialized view " ~ name ~ " must be a string or a list of strings."
        ) }}
    {%- endif -%}
    {%- if values | length == 0 -%}
        {{ exceptions.raise_compiler_error(
            "materialized view " ~ name ~ " must not be empty."
        ) }}
    {%- endif -%}
    {%- set normalized_values = [] -%}
    {%- for value in values -%}
        {%- if value is not string or not value | trim -%}
            {{ exceptions.raise_compiler_error(
                "Every materialized view " ~ name
                ~ " value must be a non-empty string."
            ) }}
        {%- endif -%}
        {%- do normalized_values.append(value | trim) -%}
    {%- endfor -%}
    {{ return(normalized_values) }}
{%- endmacro %}

{% macro doris__materialized_view_identifier_list(values, name='identifier') -%}
    {%- set values = doris__materialized_view_identifier_values(name, values) -%}
    {%- for value in values -%}
        `{{ value | replace("`", "``") }}`{% if not loop.last %}, {% endif %}
    {%- endfor -%}
{%- endmacro %}

{% macro doris__materialized_view_hash_values(values) -%}
    {%- set encoded = [] -%}
    {%- for value in values -%}
        {%- set text = value | string -%}
        {%- do encoded.append(text | length ~ ':' ~ text) -%}
    {%- endfor -%}
    {{ return(encoded | join) }}
{%- endmacro %}

{% macro doris__validate_materialized_view_refresh_config() -%}
    {%- set refresh_method = (
        config.get('refresh_method', 'auto') or 'auto'
    ) | trim | lower -%}
    {%- set refresh_trigger = (
        config.get('refresh_trigger', 'manual') or 'manual'
    ) | trim | lower -%}
    {%- set schedule = config.get('refresh_schedule') -%}
    {%- if refresh_method not in ['auto', 'complete'] -%}
        {{ exceptions.raise_compiler_error(
            "Invalid materialized view refresh_method '" ~ refresh_method
            ~ "'. Expected one of: auto, complete."
        ) }}
    {%- endif -%}
    {%- if refresh_trigger not in ['manual', 'schedule', 'commit'] -%}
        {{ exceptions.raise_compiler_error(
            "Invalid materialized view refresh_trigger '" ~ refresh_trigger
            ~ "'. Expected one of: manual, schedule, commit."
        ) }}
    {%- endif -%}
    {%- if refresh_trigger == 'schedule' -%}
        {%- if schedule is not mapping -%}
            {{ exceptions.raise_compiler_error(
                "materialized view refresh_schedule is required when refresh_trigger is schedule."
            ) }}
        {%- endif -%}
        {%- set interval = schedule.get('interval') -%}
        {%- set unit = (schedule.get('unit') or '') | trim | lower -%}
        {%- if interval is not integer or interval <= 0 -%}
            {{ exceptions.raise_compiler_error(
                "materialized view refresh_schedule.interval must be a positive integer."
            ) }}
        {%- endif -%}
        {%- if unit not in ['minute', 'hour', 'day', 'week'] -%}
            {{ exceptions.raise_compiler_error(
                "Invalid materialized view refresh_schedule.unit '" ~ unit
                ~ "'. Expected one of: minute, hour, day, week. Doris only "
                ~ "enables second-level schedules through a test-only setting."
            ) }}
        {%- endif -%}
        {%- set start_time = schedule.get('start_time') -%}
        {%- if start_time is not none and start_time is not string -%}
            {{ exceptions.raise_compiler_error(
                "materialized view refresh_schedule.start_time must be a string."
            ) }}
        {%- endif -%}
    {%- elif schedule is not none -%}
        {{ exceptions.raise_compiler_error(
            "materialized view refresh_schedule is only valid when refresh_trigger is schedule."
        ) }}
    {%- endif -%}
{%- endmacro %}

{% macro doris__materialized_view_refresh_clause() -%}
    {%- set refresh_method = (
        config.get('refresh_method', 'auto') or 'auto'
    ) | trim | lower -%}
    {%- set refresh_trigger = (
        config.get('refresh_trigger', 'manual') or 'manual'
    ) | trim | lower -%}
    refresh {{ refresh_method }}
    {% if refresh_trigger == 'schedule' -%}
        {%- set schedule = config.get('refresh_schedule') -%}
        on schedule every {{ schedule.get('interval') }} {{ schedule.get('unit') | trim | lower }}
        {%- if schedule.get('start_time') %}
            starts '{{ schedule.get('start_time') | replace("\\", "\\\\") | replace("'", "\\'") }}'
        {%- endif -%}
    {%- else %}
        on {{ refresh_trigger }}
    {%- endif -%}
{%- endmacro %}

{% macro doris__validate_materialized_view_identifier_config(name, values) -%}
    {% do doris__materialized_view_identifier_values(name, values) %}
{%- endmacro %}

{% macro doris__materialized_view_effective_properties() -%}
    {%- set configured_properties = config.get('properties') -%}
    {%- if configured_properties is none -%}
        {%- set configured_properties = {} -%}
    {%- elif configured_properties is not mapping -%}
        {{ exceptions.raise_compiler_error(
            "materialized view properties must be a dictionary."
        ) }}
    {%- endif -%}

    {#-- Match the table materialization's replication_num convenience config
         without mutating the dictionary held by dbt's model config. --#}
    {%- set properties = {} -%}
    {%- do properties.update(configured_properties) -%}
    {%- set replication_num = config.get('replication_num') -%}
    {%- if replication_num is not none -%}
        {%- do properties.update({'replication_num': replication_num}) -%}
    {%- endif -%}
    {%- for property, value in properties.items() -%}
        {%- if property is not string or not property | trim -%}
            {{ exceptions.raise_compiler_error(
                "Every materialized view property name must be a non-empty string."
            ) }}
        {%- endif -%}
        {%- if (
            value is not string
            and value is not number
            and value is not boolean
        ) -%}
            {{ exceptions.raise_compiler_error(
                "materialized view property '" ~ property
                ~ "' must have a string, number, or boolean value."
            ) }}
        {%- endif -%}
    {%- endfor -%}
    {%- set effective_replication_num = properties.get('replication_num') -%}
    {%- if effective_replication_num is not none -%}
        {%- set replication_text = effective_replication_num | string | trim -%}
        {%- if (
            not replication_text.isdigit()
            or replication_text | int <= 0
        ) -%}
            {{ exceptions.raise_compiler_error(
                "materialized view replication_num must be a positive integer."
            ) }}
        {%- endif -%}
        {%- do properties.update({
            'replication_num': replication_text | int | string
        }) -%}
    {%- endif -%}
    {{ return(properties) }}
{%- endmacro %}

{% macro doris__materialized_view_is_identifier(value, allow_qualified=false) -%}
    {#-- Doris identifiers contain letters, digits, dollar signs, underscores,
         or a complete backtick-quoted segment. Function identifiers may have
         one database qualifier. --#}
    {%- set identifier = value | trim -%}
    {%- set scan = namespace(
        in_backticks=false,
        escaped_backtick=false,
        segment_started=false,
        segment_closed=false,
        segment_has_non_digit=false,
        pending_whitespace=false,
        dots=0,
        valid=true
    ) -%}
    {%- for character in identifier -%}
        {%- if scan.in_backticks -%}
            {%- if scan.escaped_backtick -%}
                {%- set scan.escaped_backtick = false -%}
            {%- elif character == '`' -%}
                {%- if not loop.last and identifier[loop.index] == '`' -%}
                    {%- set scan.escaped_backtick = true -%}
                {%- else -%}
                    {%- set scan.in_backticks = false -%}
                    {%- set scan.segment_closed = true -%}
                {%- endif -%}
            {%- endif -%}
        {%- elif (
            character in [' ', '\t', '\r', '\n']
            and allow_qualified
        ) -%}
            {%- set scan.pending_whitespace = true -%}
        {%- elif character == '`' -%}
            {%- if scan.pending_whitespace and scan.segment_started -%}
                {%- set scan.valid = false -%}
            {%- endif -%}
            {%- set scan.pending_whitespace = false -%}
            {%- if scan.segment_started or scan.segment_closed -%}
                {%- set scan.valid = false -%}
            {%- else -%}
                {%- set scan.in_backticks = true -%}
                {%- set scan.segment_started = true -%}
                {%- set scan.segment_has_non_digit = true -%}
            {%- endif -%}
        {%- elif character == '.' and allow_qualified -%}
            {%- if (
                not scan.segment_started
                or not scan.segment_has_non_digit
                or scan.dots >= 1
            ) -%}
                {%- set scan.valid = false -%}
            {%- endif -%}
            {%- set scan.pending_whitespace = false -%}
            {%- set scan.dots = scan.dots + 1 -%}
            {%- set scan.segment_started = false -%}
            {%- set scan.segment_closed = false -%}
            {%- set scan.segment_has_non_digit = false -%}
        {%- elif (
            character.isalnum()
            or character in ['$', '_']
            or not character.isascii()
        ) -%}
            {%- if scan.pending_whitespace and scan.segment_started -%}
                {%- set scan.valid = false -%}
            {%- endif -%}
            {%- set scan.pending_whitespace = false -%}
            {%- if scan.segment_closed -%}
                {%- set scan.valid = false -%}
            {%- else -%}
                {%- set scan.segment_started = true -%}
                {%- if character not in '0123456789' -%}
                    {%- set scan.segment_has_non_digit = true -%}
                {%- endif -%}
            {%- endif -%}
        {%- else -%}
            {%- set scan.valid = false -%}
        {%- endif -%}
    {%- endfor -%}
    {{ return(
        scan.valid
        and not scan.in_backticks
        and scan.segment_started
        and scan.segment_has_non_digit
    ) }}
{%- endmacro %}

{% macro doris__materialized_view_is_partition_expression(expression) -%}
    {#-- Match the top-level shape of Doris mvPartition:
         identifier | functionCallExpression. Function arguments remain SQL and
         are validated by Doris itself. --#}
    {%- set scan = namespace(
        depth=0,
        quote=none,
        escaped=false,
        open_index=none,
        close_index=none,
        valid=true
    ) -%}
    {%- for character in expression -%}
        {%- if scan.quote is not none -%}
            {%- if scan.escaped -%}
                {%- set scan.escaped = false -%}
            {%- elif character == '\\' and scan.quote != '`' -%}
                {%- set scan.escaped = true -%}
            {%- elif character == scan.quote -%}
                {%- if not loop.last and expression[loop.index] == scan.quote -%}
                    {%- set scan.escaped = true -%}
                {%- else -%}
                    {%- set scan.quote = none -%}
                {%- endif -%}
            {%- endif -%}
        {%- elif character in ["'", '"', '`'] -%}
            {%- set scan.quote = character -%}
        {%- elif character == '(' -%}
            {%- if scan.depth == 0 -%}
                {%- if scan.open_index is not none -%}
                    {%- set scan.valid = false -%}
                {%- else -%}
                    {%- set scan.open_index = loop.index0 -%}
                {%- endif -%}
            {%- endif -%}
            {%- set scan.depth = scan.depth + 1 -%}
        {%- elif character == ')' -%}
            {%- if scan.depth <= 0 -%}
                {%- set scan.valid = false -%}
            {%- else -%}
                {%- set scan.depth = scan.depth - 1 -%}
                {%- if scan.depth == 0 -%}
                    {%- set scan.close_index = loop.index0 -%}
                {%- endif -%}
            {%- endif -%}
        {%- endif -%}
    {%- endfor -%}

    {%- if not scan.valid or scan.quote is not none or scan.depth != 0 -%}
        {{ return(false) }}
    {%- elif scan.open_index is none -%}
        {{ return(doris__materialized_view_is_identifier(expression)) }}
    {%- elif scan.close_index != expression | length - 1 -%}
        {{ return(false) }}
    {%- endif -%}

    {%- set function_identifier = expression[:scan.open_index] | trim -%}
    {{ return(
        doris__materialized_view_is_identifier(
            function_identifier,
            allow_qualified=true
        )
    ) }}
{%- endmacro %}

{% macro doris__materialized_view_partition_expression() -%}
    {%- set configured_partition = config.get('partition_by') -%}
    {%- if configured_partition is none -%}
        {{ return(none) }}
    {%- elif configured_partition is string -%}
        {%- set partitions = [configured_partition] -%}
    {%- elif configured_partition is sequence and configured_partition is not mapping -%}
        {%- set partitions = configured_partition -%}
    {%- else -%}
        {%- set partitions = [] -%}
    {%- endif -%}

    {%- if (
        partitions | length != 1
        or partitions[0] is not string
        or not partitions[0] | trim
    ) -%}
        {{ exceptions.raise_compiler_error(
            "materialized view partition_by must be a string or a list "
            ~ "containing exactly one non-empty string."
        ) }}
    {%- endif -%}

    {%- set partition = partitions[0] | trim -%}
    {%- if (
        ';' in partition
        or '--' in partition
        or '/*' in partition
        or '*/' in partition
    ) -%}
        {{ exceptions.raise_compiler_error(
            "materialized view partition_by contains unsafe SQL tokens."
        ) }}
    {%- endif -%}
    {%- if not doris__materialized_view_is_partition_expression(partition) -%}
        {{ exceptions.raise_compiler_error(
            "materialized view partition_by must contain one Doris identifier "
            ~ "or function call."
        ) }}
    {%- endif -%}
    {{ return(partition) }}
{%- endmacro %}

{% macro doris__validate_materialized_view_ddl_config() -%}
    {% do doris__validate_materialized_view_identifier_config(
        'duplicate_key',
        config.get('duplicate_key')
    ) %}
    {% do doris__validate_materialized_view_identifier_config(
        'distributed_by',
        config.get('distributed_by')
    ) %}
    {% do doris__materialized_view_partition_expression() %}
    {% do doris__materialized_view_effective_properties() %}
    {%- set wait_for_refresh = config.get('wait_for_refresh', true) -%}
    {%- if wait_for_refresh is not boolean -%}
        {{ exceptions.raise_compiler_error(
            "materialized view wait_for_refresh must be true or false."
        ) }}
    {%- endif -%}
    {%- set refresh_wait_timeout = config.get('refresh_wait_timeout', 300) -%}
    {%- if refresh_wait_timeout is not integer or refresh_wait_timeout <= 0 -%}
        {{ exceptions.raise_compiler_error(
            "materialized view refresh_wait_timeout must be a positive integer."
        ) }}
    {%- endif -%}
    {%- set refresh_poll_interval = config.get('refresh_poll_interval', 1) -%}
    {%- if refresh_poll_interval is not integer or refresh_poll_interval <= 0 -%}
        {{ exceptions.raise_compiler_error(
            "materialized view refresh_poll_interval must be a positive integer."
        ) }}
    {%- endif -%}
    {%- if refresh_poll_interval > refresh_wait_timeout -%}
        {{ exceptions.raise_compiler_error(
            "materialized view refresh_poll_interval cannot be greater than "
            ~ "refresh_wait_timeout."
        ) }}
    {%- endif -%}
{%- endmacro %}

{% macro doris__validate_materialized_view_distribution_config() -%}
    {%- set distributed_by = doris__materialized_view_identifier_values(
        'distributed_by',
        config.get('distributed_by')
    ) -%}
    {%- set distribution_type = (config.get(
        'distribution_type',
        'hash' if distributed_by else 'random'
    ) or 'random') | trim | lower -%}
    {%- set buckets = config.get('buckets', 'auto') -%}
    {%- if distribution_type not in ['hash', 'random'] -%}
        {{ exceptions.raise_compiler_error(
            "Invalid materialized view distribution_type '" ~ distribution_type
            ~ "'. Expected one of: hash, random."
        ) }}
    {%- endif -%}
    {%- if distribution_type == 'hash' and not distributed_by -%}
        {{ exceptions.raise_compiler_error(
            "materialized view distributed_by is required when distribution_type is hash."
        ) }}
    {%- elif distribution_type == 'random' and distributed_by -%}
        {{ exceptions.raise_compiler_error(
            "materialized view distributed_by is only valid when distribution_type is hash."
        ) }}
    {%- endif -%}
    {%- if buckets is string -%}
        {%- if buckets | lower != 'auto' -%}
            {{ exceptions.raise_compiler_error(
                "materialized view buckets must be a positive integer or 'auto'."
            ) }}
        {%- endif -%}
    {%- elif buckets is not integer or buckets <= 0 -%}
        {{ exceptions.raise_compiler_error(
            "materialized view buckets must be a positive integer or 'auto'."
        ) }}
    {%- endif -%}
{%- endmacro %}

{% macro doris__materialized_view_distribution_clause() -%}
    {%- set distributed_by = doris__materialized_view_identifier_values(
        'distributed_by',
        config.get('distributed_by')
    ) -%}
    {%- set distribution_type = (config.get(
        'distribution_type',
        'hash' if distributed_by else 'random'
    ) or 'random') | trim | lower -%}
    {%- set buckets = config.get('buckets', 'auto') -%}
    distributed by {{ distribution_type }}
    {%- if distribution_type == 'hash' %}
        ({{ doris__materialized_view_identifier_list(
            distributed_by,
            'distributed_by'
        ) }})
    {%- endif %}
    buckets {{ buckets | lower if buckets is string else buckets }}
{%- endmacro %}

{% macro doris__materialized_view_properties_clause() -%}
    {%- set properties = doris__materialized_view_effective_properties() -%}
    {%- if properties %}
        properties (
        {%- for property in properties | dictsort -%}
            "{{ property[0] | replace("\\", "\\\\") | replace('"', '\\"') }}" =
            "{{ property[1] | string | replace("\\", "\\\\") | replace('"', '\\"') }}"
            {%- if not loop.last %}, {% endif -%}
        {%- endfor -%}
        )
    {%- endif -%}
{%- endmacro %}

{% macro doris__materialized_view_definition_hash(sql) -%}
    {%- set duplicate_key = doris__materialized_view_identifier_values(
        'duplicate_key',
        config.get('duplicate_key')
    ) -%}
    {%- set distributed_by = doris__materialized_view_identifier_values(
        'distributed_by',
        config.get('distributed_by')
    ) -%}
    {%- set distribution_type = (config.get(
        'distribution_type',
        'hash' if distributed_by else 'random'
    ) or 'random') | trim | lower -%}
    {%- set schedule = config.get('refresh_schedule') or {} -%}
    {%- set partition_by =
        doris__materialized_view_partition_expression()
    -%}
    {%- set properties = doris__materialized_view_effective_properties() -%}
    {%- set property_values = [] -%}
    {%- for property in properties | dictsort -%}
        {%- do property_values.append(property[0]) -%}
        {%- do property_values.append(property[1] | string) -%}
    {%- endfor -%}
    {%- set persisted_column_docs = [] -%}
    {%- if config.persist_column_docs() -%}
        {%- for column in (model.get('columns', {}) or {}) | dictsort -%}
            {%- set description = column[1].get('description', '') or '' -%}
            {%- if description -%}
                {%- set quoted = column[1].get('quote', false) -%}
                {%- do persisted_column_docs.append(
                    column[0] if quoted else column[0] | lower
                ) -%}
                {%- do persisted_column_docs.append(
                    'quoted' if quoted else 'unquoted'
                ) -%}
                {%- do persisted_column_docs.append(description) -%}
            {%- endif -%}
        {%- endfor -%}
    {%- endif -%}
    {%- set persisted_relation_description = (
        model.get('description', '') if config.persist_relation_docs() else ''
    ) -%}
    {%- set buckets = config.get('buckets', 'auto') -%}
    {%- set normalized_buckets = (
        buckets | trim | lower if buckets is string else buckets | string
    ) -%}
    {%- set definition = [
        sql | trim,
        (config.get('build_mode', 'immediate') or 'immediate') | trim | lower,
        (config.get('refresh_method', 'auto') or 'auto') | trim | lower,
        (config.get('refresh_trigger', 'manual') or 'manual') | trim | lower,
        schedule.get('interval', ''),
        (schedule.get('unit', '') or '') | trim | lower,
        schedule.get('start_time', ''),
        doris__materialized_view_hash_values(duplicate_key),
        partition_by or '',
        distribution_type,
        doris__materialized_view_hash_values(distributed_by),
        normalized_buckets,
        doris__materialized_view_hash_values(property_values),
        persisted_relation_description,
        doris__materialized_view_hash_values(persisted_column_docs)
    ] -%}
    {{ return(local_md5(doris__materialized_view_hash_values(definition))) }}
{%- endmacro %}

{% macro doris__materialized_view_comment(sql, deployment_complete=false) -%}
    {%- set description = (
        model.get('description', '') if config.persist_relation_docs() else ''
    ) -%}
    {%- set marker_name = (
        'definition-hash' if deployment_complete else 'deployment-pending'
    ) -%}
    {%- set comment = (
        description ~ ' ' if description else ''
    ) ~ 'dbt-doris:' ~ marker_name ~ '='
      ~ doris__materialized_view_definition_hash(sql) -%}
    {{ return(comment) }}
{%- endmacro %}

{% macro doris__materialized_view_column_definitions(sql) -%}
    {%- set documented_columns = model.get('columns', {}) or {} -%}
    {%- if (
        not execute
        or not config.persist_column_docs()
        or not documented_columns
    ) -%}
        {{ return('') }}
    {%- endif -%}

    {%- set column_probe_sql -%}
        select *
        from (
            {{ sql }}
        ) as `__dbt_materialized_view_columns`
        where false
        limit 0
    {%- endset -%}
    {%- set query_columns = adapter.get_column_schema_from_query(
        column_probe_sql
    ) -%}
    {%- set definitions = [] -%}
    {%- set matched_documented_columns = [] -%}
    {%- for query_column in query_columns -%}
        {%- set query_column_name = query_column.name | string -%}
        {%- set match = namespace(name=none, info=none) -%}
        {%- for documented_name, documented_info in documented_columns.items() -%}
            {%- set documented_is_quoted = documented_info.get('quote', false) -%}
            {%- if (
                match.info is none
                and (
                    documented_name == query_column_name
                    if documented_is_quoted
                    else documented_name | lower == query_column_name | lower
                )
            ) -%}
                {%- set match.name = documented_name -%}
                {%- set match.info = documented_info -%}
            {%- endif -%}
        {%- endfor -%}
        {%- set definition = (
            '`' ~ query_column_name | replace("`", "``") ~ '`'
        ) -%}
        {%- if match.info is not none -%}
            {%- do matched_documented_columns.append(match.name) -%}
            {%- set description = match.info.get('description', '') or '' -%}
            {%- if description -%}
                {%- set definition = definition ~ " comment '"
                    ~ description | replace("\\", "\\\\") | replace("'", "\\'")
                    ~ "'" -%}
            {%- endif -%}
        {%- endif -%}
        {%- do definitions.append(definition) -%}
    {%- endfor -%}

    {%- set missing_columns = [] -%}
    {%- for documented_name, documented_info in documented_columns.items() -%}
        {%- if (
            documented_info.get('description')
            and documented_name not in matched_documented_columns
        ) -%}
            {%- do missing_columns.append(documented_name) -%}
        {%- endif -%}
    {%- endfor -%}
    {%- if missing_columns -%}
        {{ exceptions.warn(
            "In materialized view " ~ model.get('name', '<model>')
            ~ ": The following documented columns are not present in the model "
            ~ "query: " ~ missing_columns | join(', ')
        ) }}
    {%- endif -%}
    {{ return(definitions | join(', ')) }}
{%- endmacro %}

{% macro doris__materialized_view_definition_state(relation, sql) -%}
    {%- if not execute -%}
        {{ return('changed') }}
    {%- endif -%}
    {%- set results = run_query('show create materialized view ' ~ relation.render()) -%}
    {%- if results is none or results.rows | length == 0 -%}
        {{ return('changed') }}
    {%- endif -%}
    {%- set create_sql = results.rows[0][1] -%}
    {%- set expected_marker = (
        'dbt-doris:definition-hash='
        ~ doris__materialized_view_definition_hash(sql)
    ) -%}
    {%- if expected_marker in create_sql -%}
        {{ return('complete') }}
    {%- elif 'dbt-doris:deployment-pending=' in create_sql -%}
        {{ return('pending') }}
    {%- endif -%}
    {{ return('changed') }}
{%- endmacro %}

{% macro doris__materialized_view_definition_matches(relation, sql) -%}
    {{ return(
        doris__materialized_view_definition_state(relation, sql) == 'complete'
    ) }}
{%- endmacro %}

{% macro doris__materialized_view_action(
    existing_relation,
    definition_state,
    full_refresh_mode
) -%}
    {%- if existing_relation is none -%}
        {{ return('create') }}
    {%- elif existing_relation.type != 'materialized_view' -%}
        {{ return('replace_type') }}
    {%- elif full_refresh_mode -%}
        {{ return('replace') }}
    {%- elif definition_state == 'pending' -%}
        {{ return('replace') }}
    {%- elif definition_state == 'complete' or definition_state is sameas true -%}
        {%- set refresh_trigger = (
            config.get('refresh_trigger', 'manual') or 'manual'
        ) | trim | lower -%}
        {{ return('refresh' if refresh_trigger == 'manual' else 'skip') }}
    {%- endif -%}

    {%- set on_configuration_change = (
        config.get('on_configuration_change', 'apply') or 'apply'
    ) | lower -%}
    {%- if on_configuration_change == 'apply' -%}
        {{ return('replace') }}
    {%- elif on_configuration_change == 'continue' -%}
        {{ return('continue') }}
    {%- elif on_configuration_change == 'fail' -%}
        {{ exceptions.raise_fail_fast_error(
            "Configuration changes were identified and "
            ~ "`on_configuration_change` was set to `fail` for `"
            ~ existing_relation.render() ~ "`"
        ) }}
    {%- else -%}
        {{ exceptions.raise_compiler_error(
            "Invalid materialized view on_configuration_change '"
            ~ on_configuration_change ~ "'. Expected one of: apply, continue, fail."
        ) }}
    {%- endif -%}
{%- endmacro %}

{% macro doris__get_refresh_materialized_view_sql(relation) -%}
    {% do doris__validate_materialized_view_refresh_config() %}
    {%- set refresh_method = (
        config.get('refresh_method', 'auto') or 'auto'
    ) | trim | lower -%}
    refresh materialized view {{ relation }} {{ refresh_method }}
{%- endmacro %}

{% macro doris__refresh_materialized_view(relation) -%}
    {{ doris__get_refresh_materialized_view_sql(relation) }}
{%- endmacro %}

{% macro doris__drop_materialized_view(relation) -%}
    drop materialized view if exists {{ relation.render() }}
{%- endmacro %}

{% macro doris__get_rename_materialized_view_sql(relation, new_name) -%}
    alter materialized view {{ relation.render() }}
    rename `{{ new_name | replace("`", "``") }}`
{%- endmacro %}

{% macro doris__get_swap_materialized_view_sql(
    target_relation,
    intermediate_relation
) -%}
    alter materialized view {{ target_relation }}
    replace with materialized view
      `{{ intermediate_relation.identifier | replace("`", "``") }}`
    properties("swap" = "true")
{%- endmacro %}

{% macro doris__get_mark_materialized_view_deployment_complete_sql(
    relation,
    sql
) -%}
    {%- set comment = doris__materialized_view_comment(
        sql,
        deployment_complete=true
    ) -%}
    alter table {{ relation }}
    modify comment '{{ comment | replace("\\", "\\\\") | replace("'", "\\'") }}'
{%- endmacro %}

{% macro doris__materialized_view_task_ids(relation) -%}
    {%- if not execute -%}
        {{ return([]) }}
    {%- endif -%}
    {%- set database_name = (
        relation.schema | string | replace("\\", "\\\\") | replace("'", "\\'")
    ) -%}
    {%- set materialized_view_name = (
        relation.identifier | string | replace("\\", "\\\\") | replace("'", "\\'")
    ) -%}
    {%- set results = run_query(
        "select TaskId from tasks('type'='mv') "
        ~ "where MvDatabaseName = '" ~ database_name ~ "' "
        ~ "and MvName = '" ~ materialized_view_name ~ "' "
        ~ "order by CreateTime desc, TaskId desc"
    ) -%}
    {%- set task_ids = [] -%}
    {%- if results is not none -%}
        {%- for row in results.rows -%}
            {%- do task_ids.append(row[0] | string) -%}
        {%- endfor -%}
    {%- endif -%}
    {{ return(task_ids) }}
{%- endmacro %}

{% macro doris__wait_for_materialized_view_refresh(
    relation,
    previous_task_ids
) -%}
    {%- if not execute or not config.get('wait_for_refresh', true) -%}
        {{ return(none) }}
    {%- endif -%}
    {% do doris__validate_materialized_view_ddl_config() %}
    {%- set timeout = config.get('refresh_wait_timeout', 300) -%}
    {%- set poll_interval = config.get('refresh_poll_interval', 1) -%}
    {%- set attempts = ((timeout + poll_interval - 1) // poll_interval) + 1 -%}
    {%- set database_name = (
        relation.schema | string | replace("\\", "\\\\") | replace("'", "\\'")
    ) -%}
    {%- set materialized_view_name = (
        relation.identifier | string | replace("\\", "\\\\") | replace("'", "\\'")
    ) -%}
    {%- set task_query -%}
        select TaskId, Status, ErrorMsg, LastQueryId
        from tasks('type'='mv')
        where MvDatabaseName = '{{ database_name }}'
          and MvName = '{{ materialized_view_name }}'
        order by CreateTime desc, TaskId desc
    {%- endset -%}
    {%- set observed_tasks = [] -%}
    {%- set previous_task_ids = previous_task_ids or [] -%}
    {%- for attempt in range(attempts) -%}
        {%- set results = run_query(task_query) -%}
        {%- set new_tasks = [] -%}
        {%- if results is not none and results.rows | length > 0 -%}
            {%- for row in results.rows -%}
                {%- set task_id = row[0] | string -%}
                {%- if task_id not in previous_task_ids and not new_tasks -%}
                    {%- do new_tasks.append({
                        'task_id': task_id,
                        'status': (row[1] or 'NULL') | string | upper,
                        'error_message': (row[2] or '') | string,
                        'last_query_id': (row[3] or '') | string
                    }) -%}
                {%- endif -%}
            {%- endfor -%}
        {%- endif -%}
        {%- if new_tasks -%}
            {%- set new_task = new_tasks[0] -%}
            {%- set task_id = new_task['task_id'] -%}
            {%- set status = new_task['status'] -%}
            {%- set error_message = new_task['error_message'] -%}
            {%- set last_query_id = new_task['last_query_id'] -%}
            {%- do observed_tasks.append(new_task) -%}
            {%- if status == 'SUCCESS' -%}
                {{ return(observed_tasks[-1]) }}
            {%- elif status in ['FAILED', 'CANCELED'] -%}
                {{ exceptions.raise_compiler_error(
                    "Doris materialized view refresh failed for " ~ relation
                    ~ " (task " ~ task_id ~ ", status " ~ status
                    ~ (", query " ~ last_query_id if last_query_id else "")
                    ~ "): " ~ (error_message or "no error message was returned")
                ) }}
            {%- elif status not in ['PENDING', 'RUNNING', 'NULL'] -%}
                {{ exceptions.raise_compiler_error(
                    "Doris materialized view refresh returned unexpected status "
                    ~ status ~ " for " ~ relation ~ " (task " ~ task_id ~ ")."
                ) }}
            {%- endif -%}
        {%- endif -%}
        {%- if not loop.last -%}
            {%- do run_query('select sleep(' ~ poll_interval ~ ')') -%}
        {%- endif -%}
    {%- endfor -%}
    {%- if observed_tasks -%}
        {%- set last_task = observed_tasks[-1] -%}
        {{ exceptions.raise_compiler_error(
            "Timed out after " ~ timeout ~ " seconds waiting for Doris "
            ~ "materialized view refresh for " ~ relation ~ " (task "
            ~ last_task['task_id'] ~ " is " ~ last_task['status'] ~ ")."
        ) }}
    {%- else -%}
        {{ exceptions.raise_compiler_error(
            "Timed out after " ~ timeout ~ " seconds waiting for a new Doris "
            ~ "materialized view refresh task for " ~ relation
            ~ ". Ensure materialized view task history is enabled."
        ) }}
    {%- endif -%}
{%- endmacro %}

{% macro doris__store_materialized_view_result(
    action,
    relation,
    refresh_task=none
) -%}
    {%- set response = adapter.materialized_view_adapter_response(
        action,
        relation,
        refresh_task
    ) -%}
    {% do store_result(
        name='main',
        response=response
    ) %}
{%- endmacro %}

{% macro doris__get_create_materialized_view_as_sql(
    relation,
    sql
) -%}
    {%- set build_mode = config.get('build_mode', 'immediate') or 'immediate' -%}
    {%- set build_mode = build_mode | trim | lower -%}
    {%- if build_mode not in ['immediate', 'deferred'] -%}
        {{ exceptions.raise_compiler_error(
            "Invalid materialized view build_mode '" ~ build_mode
            ~ "'. Expected one of: immediate, deferred."
        ) }}
    {%- endif -%}
    {% do doris__validate_materialized_view_refresh_config() %}
    {% do doris__validate_materialized_view_distribution_config() %}
    {% do doris__validate_materialized_view_ddl_config() %}
    {%- set duplicate_key = doris__materialized_view_identifier_values(
        'duplicate_key',
        config.get('duplicate_key')
    ) -%}
    {%- set pending_comment = doris__materialized_view_comment(sql) -%}
    {%- set column_definitions =
        doris__materialized_view_column_definitions(sql)
    -%}
    {%- set partition_by =
        doris__materialized_view_partition_expression()
    -%}
    create materialized view {{ relation }}
    {%- if column_definitions %}
    ({{ column_definitions }})
    {%- endif %}
    build {{ build_mode }}
    {{ doris__materialized_view_refresh_clause() }}
    {%- if duplicate_key %}
        duplicate key ({{ doris__materialized_view_identifier_list(
            duplicate_key,
            'duplicate_key'
        ) }})
    {%- endif %}
    comment '{{ pending_comment | replace("\\", "\\\\") | replace("'", "\\'") }}'
    {%- if partition_by %}
        partition by ({{ partition_by }})
    {%- endif %}
    {{ doris__materialized_view_distribution_clause() }}
    {{ doris__materialized_view_properties_clause() }}
    as {{ sql }}
{%- endmacro %}

{% materialization materialized_view, adapter='doris' %}
    {%- set existing_relation = load_cached_relation(this) -%}
    {%- set target_relation = this.incorporate(type='materialized_view') -%}
    {%- set backup_relation_type = 'table' -%}
    {%- if existing_relation is not none and not existing_relation.is_view -%}
        {%- set backup_relation_type = existing_relation.type -%}
    {%- endif -%}
    {%- set backup_relation = target_relation.incorporate(
        path={'identifier': target_relation.identifier ~ '__dbt_backup'},
        type=backup_relation_type
    ) -%}
    {%- set preexisting_backup_relation = load_cached_relation(
        backup_relation
    ) -%}
    {%- if (
        existing_relation is none
        and preexisting_backup_relation is not none
    ) -%}
        {%- set restored_relation = this.incorporate(type=(
            'table'
            if preexisting_backup_relation.is_view
            else preexisting_backup_relation.type
        )) -%}
        {% if preexisting_backup_relation.is_view %}
            {% do doris__snapshot_view_to_table(
                preexisting_backup_relation,
                restored_relation
            ) %}
        {% else %}
            {% do adapter.rename_relation(
                preexisting_backup_relation,
                restored_relation
            ) %}
        {% endif %}
        {%- set existing_relation = restored_relation -%}
        {%- set preexisting_backup_relation = none -%}
    {%- endif -%}
    {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
    {%- set preexisting_intermediate_relation = load_cached_relation(
        intermediate_relation
    ) -%}
    {%- set full_refresh_mode = should_full_refresh() -%}
    {% do doris__validate_materialized_view_refresh_config() %}
    {% do doris__validate_materialized_view_distribution_config() %}
    {% do doris__validate_materialized_view_ddl_config() %}
    {%- set build_mode = (
        config.get('build_mode', 'immediate') or 'immediate'
    ) | trim | lower -%}
    {%- set definition_state = 'changed' -%}
    {%- set grant_config = config.get('grants') -%}
    {%- set backup_relation_to_drop = none -%}
    {%- set refresh_task = none -%}

    {# Snapshot an active View before this model's pre-hooks or sql_header can
         alter the session used to evaluate it. Keep the source View online
         until the replacement Materialized View is ready to publish. #}
    {%- if existing_relation is not none and existing_relation.is_view -%}
        {%- if preexisting_backup_relation is not none -%}
            {% do adapter.drop_relation(preexisting_backup_relation) %}
            {%- set preexisting_backup_relation = none -%}
        {%- endif -%}
        {% do doris__snapshot_view_data_to_table(
            existing_relation,
            backup_relation
        ) %}
    {%- endif -%}

    {{ run_hooks(pre_hooks, inside_transaction=false) }}
    {%- if execute -%}
        {%- set frontends = run_query('show frontends') -%}
        {% do adapter.validate_materialized_view_version(frontends) %}
    {%- endif -%}
    {#-- Doris DCL is non-transactional. Validate every requested principal
         before CREATE, REPLACE, or type-switch DDL can expose a new
         target definition. --#}
    {% do doris__preflight_grants(target_relation, grant_config) %}
    {#-- Outside-transaction pre-hooks may set session state used by metadata
         queries, so inspect the deployed definition only after those hooks. --#}
    {%- if (
        existing_relation is not none
        and existing_relation.type == 'materialized_view'
    ) -%}
        {%- set definition_state =
            doris__materialized_view_definition_state(existing_relation, sql)
        -%}
    {%- endif -%}
    {%- if preexisting_intermediate_relation is not none -%}
        {%- if (
            existing_relation is not none
            and existing_relation.type == 'materialized_view'
            and preexisting_intermediate_relation.type == 'materialized_view'
            and definition_state == 'pending'
        ) -%}
            {{ log(
                'Rolling back incomplete materialized view deployment for: '
                ~ target_relation,
                info=true
            ) }}
            {% call statement('rollback_materialized_view') %}
                {{ doris__get_swap_materialized_view_sql(
                    target_relation,
                    intermediate_relation
                ) }}
            {% endcall %}
            {#-- The swap restored the previous complete MV at the target and
                 moved the pending replacement back to the temporary name. --#}
            {% do adapter.drop_relation(preexisting_intermediate_relation) %}
            {%- set preexisting_intermediate_relation = none -%}
            {#-- Retry interrupted deployments even when the configured change
                 policy would otherwise continue or fail. --#}
            {%- set definition_state = 'pending' -%}
        {%- else -%}
            {% do adapter.drop_relation(preexisting_intermediate_relation) %}
            {%- set preexisting_intermediate_relation = none -%}
        {%- endif -%}
    {%- endif -%}
    {%- if preexisting_backup_relation is not none -%}
        {%- if (
            existing_relation is not none
            and existing_relation.type == 'materialized_view'
            and definition_state == 'pending'
        ) -%}
            {%- set backup_relation_to_drop =
                preexisting_backup_relation
            -%}
        {%- else -%}
            {% do adapter.drop_relation(preexisting_backup_relation) %}
        {%- endif -%}
    {%- endif -%}
    {%- set action = doris__materialized_view_action(
        existing_relation,
        definition_state,
        full_refresh_mode
    ) -%}

    {%- if action in ['skip', 'continue'] -%}
        {%- if action == 'continue' -%}
            {{ exceptions.warn(
                "Configuration changes were identified and "
                ~ "`on_configuration_change` was set to `continue` for `"
                ~ target_relation.render() ~ "`"
            ) }}
        {%- endif -%}
    {%- else -%}
        {{ run_hooks(pre_hooks, inside_transaction=true) }}

        {%- if action == 'create' and build_mode == 'deferred' -%}
                {% call statement('main') %}
                    {{ doris__get_create_materialized_view_as_sql(
                        target_relation,
                        sql
                    ) }}
                {% endcall %}

        {%- elif action in ['create', 'replace', 'replace_type'] -%}
            {%- set previous_task_ids = [] -%}
            {%- if (
                build_mode == 'immediate'
                and config.get('wait_for_refresh', true)
            ) -%}
                {%- set previous_task_ids =
                    doris__materialized_view_task_ids(intermediate_relation)
                -%}
            {%- endif -%}
            {% call statement('create_materialized_view_intermediate') %}
                {{ doris__get_create_materialized_view_as_sql(
                    intermediate_relation,
                    sql
                ) }}
            {% endcall %}

            {%- if build_mode == 'immediate' -%}
                {% set refresh_task = doris__wait_for_materialized_view_refresh(
                    intermediate_relation,
                    previous_task_ids
                ) %}
            {%- endif -%}

            {%- if action == 'replace' -%}
                {{ log('Applying REPLACE to: ' ~ existing_relation, info=true) }}
                {% call statement('main') %}
                    {{ doris__get_swap_materialized_view_sql(
                        target_relation,
                        intermediate_relation
                    ) }}
                {% endcall %}
            {%- elif action == 'replace_type' -%}
                {%- set current_backup_relation = backup_relation.incorporate(
                    type=(
                        'table'
                        if existing_relation.is_view
                        else existing_relation.type
                    )
                ) -%}
                {% if existing_relation.is_view %}
                    {% do adapter.drop_relation(existing_relation) %}
                {% else %}
                    {% do adapter.rename_relation(
                        existing_relation,
                        current_backup_relation
                    ) %}
                {% endif %}
                {% do adapter.rename_relation(
                    intermediate_relation,
                    target_relation
                ) %}
                {%- set backup_relation_to_drop = current_backup_relation -%}
            {%- else -%}
                {% do adapter.rename_relation(
                    intermediate_relation,
                    target_relation
                ) %}
            {%- endif -%}

        {%- elif action == 'refresh' -%}
            {%- set previous_task_ids = [] -%}
            {%- if config.get('wait_for_refresh', true) -%}
                {%- set previous_task_ids =
                    doris__materialized_view_task_ids(target_relation)
                -%}
            {%- endif -%}
            {% call statement('main') %}
                {{ doris__get_refresh_materialized_view_sql(target_relation) }}
            {% endcall %}
            {% set refresh_task = doris__wait_for_materialized_view_refresh(
                target_relation,
                previous_task_ids
            ) %}
        {%- endif -%}

    {%- endif -%}

    {% set grants_should_revoke = should_revoke(
        existing_relation,
        full_refresh_mode=action in ['replace', 'replace_type']
    ) %}
    {% do apply_grants(
        target_relation,
        grant_config,
        should_revoke=grants_should_revoke
    ) %}

    {%- if action not in ['skip', 'continue'] -%}
        {{ run_hooks(post_hooks, inside_transaction=true) }}
        {%- if action in ['create', 'replace', 'replace_type'] -%}
            {% call statement('mark_materialized_view_deployment_complete') %}
                {{ doris__get_mark_materialized_view_deployment_complete_sql(
                    target_relation,
                    sql
                ) }}
            {% endcall %}
        {%- endif -%}
        {% do adapter.commit() %}
    {%- elif grant_config -%}
        {% do adapter.commit() %}
    {%- endif -%}

    {% do doris__store_materialized_view_result(
        action,
        target_relation,
        refresh_task
    ) %}
    {{ doris__drop_relation(intermediate_relation) }}
    {{ run_hooks(post_hooks, inside_transaction=false) }}
    {%- if backup_relation_to_drop is not none -%}
        {% do adapter.drop_relation(backup_relation_to_drop) %}
    {%- endif -%}

    {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}
