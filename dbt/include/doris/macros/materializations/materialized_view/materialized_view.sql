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

{% macro doris__materialized_view_identifier_list(values) -%}
    {%- if values is string -%}
        {%- set values = [values] -%}
    {%- endif -%}
    {%- for value in values -%}
        `{{ value | replace("`", "``") }}`{% if not loop.last %}, {% endif %}
    {%- endfor -%}
{%- endmacro %}

{% macro doris__validate_materialized_view_refresh_config() -%}
    {%- set refresh_method = (config.get('refresh_method', 'auto') or 'auto') | lower -%}
    {%- set refresh_trigger = (config.get('refresh_trigger', 'manual') or 'manual') | lower -%}
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
        {%- set unit = (schedule.get('unit') or '') | lower -%}
        {%- if interval is not integer or interval <= 0 -%}
            {{ exceptions.raise_compiler_error(
                "materialized view refresh_schedule.interval must be a positive integer."
            ) }}
        {%- endif -%}
        {%- if unit not in ['second', 'minute', 'hour', 'day', 'week'] -%}
            {{ exceptions.raise_compiler_error(
                "Invalid materialized view refresh_schedule.unit '" ~ unit
                ~ "'. Expected one of: second, minute, hour, day, week."
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
    {%- set refresh_method = (config.get('refresh_method', 'auto') or 'auto') | lower -%}
    {%- set refresh_trigger = (config.get('refresh_trigger', 'manual') or 'manual') | lower -%}
    refresh {{ refresh_method }}
    {% if refresh_trigger == 'schedule' -%}
        {%- set schedule = config.get('refresh_schedule') -%}
        on schedule every {{ schedule.get('interval') }} {{ schedule.get('unit') | lower }}
        {%- if schedule.get('start_time') %}
            starts '{{ schedule.get('start_time') | replace("\\", "\\\\") | replace("'", "\\'") }}'
        {%- endif -%}
    {%- else %}
        on {{ refresh_trigger }}
    {%- endif -%}
{%- endmacro %}

{% macro doris__validate_materialized_view_identifier_config(name, values) -%}
    {%- if values is none -%}
        {{ return(none) }}
    {%- endif -%}
    {%- if values is string -%}
        {%- set values = [values] -%}
    {%- elif values is not sequence or values is mapping -%}
        {{ exceptions.raise_compiler_error(
            "materialized view " ~ name ~ " must be a string or a list of strings."
        ) }}
    {%- endif -%}
    {%- for value in values -%}
        {%- if value is not string or not value | trim -%}
            {{ exceptions.raise_compiler_error(
                "Every materialized view " ~ name ~ " value must be a non-empty string."
            ) }}
        {%- endif -%}
    {%- endfor -%}
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
    {%- set partition_by = config.get('partition_by') -%}
    {%- if partition_by is not none and partition_by is not string -%}
        {{ exceptions.raise_compiler_error(
            "materialized view partition_by must be a string."
        ) }}
    {%- endif -%}
    {%- if (
        partition_by
        and (
            ';' in partition_by
            or '--' in partition_by
            or '/*' in partition_by
            or '*/' in partition_by
        )
    ) -%}
        {{ exceptions.raise_compiler_error(
            "materialized view partition_by contains unsafe SQL tokens."
        ) }}
    {%- endif -%}
    {%- set properties = config.get('properties') -%}
    {%- if properties is not none and properties is not mapping -%}
        {{ exceptions.raise_compiler_error(
            "materialized view properties must be a dictionary."
        ) }}
    {%- endif -%}
    {%- set refresh_on_run = config.get('refresh_on_run', false) -%}
    {%- if refresh_on_run is not boolean -%}
        {{ exceptions.raise_compiler_error(
            "materialized view refresh_on_run must be true or false."
        ) }}
    {%- endif -%}
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
    {%- set distributed_by = config.get('distributed_by') -%}
    {%- set distribution_type = (config.get(
        'distribution_type',
        'hash' if distributed_by else 'random'
    ) or 'random') | lower -%}
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
    {%- set distributed_by = config.get('distributed_by') -%}
    {%- set distribution_type = (config.get(
        'distribution_type',
        'hash' if distributed_by else 'random'
    ) or 'random') | lower -%}
    {%- set buckets = config.get('buckets', 'auto') -%}
    distributed by {{ distribution_type }}
    {%- if distribution_type == 'hash' %}
        ({{ doris__materialized_view_identifier_list(distributed_by) }})
    {%- endif %}
    buckets {{ buckets | lower if buckets is string else buckets }}
{%- endmacro %}

{% macro doris__materialized_view_properties_clause() -%}
    {%- set properties = config.get('properties') or {} -%}
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
    {%- set distributed_by = config.get('distributed_by') -%}
    {%- set distribution_type = (config.get(
        'distribution_type',
        'hash' if distributed_by else 'random'
    ) or 'random') | lower -%}
    {%- set schedule = config.get('refresh_schedule') or {} -%}
    {%- set properties = config.get('properties') or {} -%}
    {%- set property_values = [] -%}
    {%- for property in properties | dictsort -%}
        {%- do property_values.append(property[0] ~ '=' ~ property[1]) -%}
    {%- endfor -%}
    {%- set definition = [
        sql | trim,
        (config.get('build_mode', 'immediate') or 'immediate') | lower,
        (config.get('refresh_method', 'auto') or 'auto') | lower,
        (config.get('refresh_trigger', 'manual') or 'manual') | lower,
        schedule.get('interval', ''),
        (schedule.get('unit', '') or '') | lower,
        schedule.get('start_time', ''),
        config.get('duplicate_key') or '',
        config.get('partition_by') or '',
        distribution_type,
        distributed_by or '',
        config.get('buckets', 'auto'),
        property_values | join(','),
        model.get('description', '')
    ] -%}
    {{ return(local_md5(definition | join('\u001f'))) }}
{%- endmacro %}

{% macro doris__materialized_view_comment(sql, deployment_complete=false) -%}
    {%- set description = model.get('description', '') -%}
    {%- set marker_name = (
        'definition-hash' if deployment_complete else 'deployment-pending'
    ) -%}
    {%- set comment = (
        description ~ ' ' if description else ''
    ) ~ 'dbt-doris:' ~ marker_name ~ '='
      ~ doris__materialized_view_definition_hash(sql) -%}
    {{ return(comment) }}
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
        {%- set refresh_on_run = config.get('refresh_on_run', false) -%}
        {%- if refresh_on_run is not boolean -%}
            {{ exceptions.raise_compiler_error(
                "materialized view refresh_on_run must be true or false."
            ) }}
        {%- endif -%}
        {{ return('refresh' if refresh_on_run else 'skip') }}
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
    {%- set refresh_method = (config.get('refresh_method', 'auto') or 'auto') | lower -%}
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

{% macro doris__get_create_materialized_view_as_sql(
    relation,
    sql
) -%}
    {%- set build_mode = config.get('build_mode', 'immediate') or 'immediate' -%}
    {%- set build_mode = build_mode | lower -%}
    {%- if build_mode not in ['immediate', 'deferred'] -%}
        {{ exceptions.raise_compiler_error(
            "Invalid materialized view build_mode '" ~ build_mode
            ~ "'. Expected one of: immediate, deferred."
        ) }}
    {%- endif -%}
    {% do doris__validate_materialized_view_refresh_config() %}
    {% do doris__validate_materialized_view_distribution_config() %}
    {% do doris__validate_materialized_view_ddl_config() %}
    {%- set duplicate_key = config.get('duplicate_key') -%}
    {%- set pending_comment = doris__materialized_view_comment(sql) -%}
    {%- set partition_by = config.get('partition_by') -%}
    create materialized view {{ relation }}
    build {{ build_mode }}
    {{ doris__materialized_view_refresh_clause() }}
    {%- if duplicate_key %}
        duplicate key ({{ doris__materialized_view_identifier_list(duplicate_key) }})
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
    {%- set backup_relation = target_relation.incorporate(
        path={'identifier': target_relation.identifier ~ '__dbt_backup'},
        type=existing_relation.type if existing_relation is not none else 'table'
    ) -%}
    {%- set preexisting_backup_relation = load_cached_relation(
        backup_relation
    ) -%}
    {%- if (
        existing_relation is none
        and preexisting_backup_relation is not none
    ) -%}
        {%- set restored_relation = this.incorporate(
            type=preexisting_backup_relation.type
        ) -%}
        {% do adapter.rename_relation(
            preexisting_backup_relation,
            restored_relation
        ) %}
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
    ) | lower -%}
    {%- set definition_state = 'changed' -%}
    {%- if (
        existing_relation is not none
        and existing_relation.type == 'materialized_view'
    ) -%}
        {%- set definition_state =
            doris__materialized_view_definition_state(existing_relation, sql)
        -%}
    {%- endif -%}
    {%- set grant_config = config.get('grants') -%}
    {%- set backup_relation_to_drop = none -%}

    {%- if preexisting_intermediate_relation is not none -%}
        {% do adapter.drop_relation(preexisting_intermediate_relation) %}
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
    {{ run_hooks(pre_hooks, inside_transaction=false) }}
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
        {% do store_raw_result(
            name='main',
            message='skip ' ~ target_relation,
            code='skip',
            rows_affected=-1
        ) %}
        {% set revoke_existing_grants = should_revoke(
            existing_relation,
            full_refresh_mode=true
        ) %}
        {% do apply_grants(
            target_relation,
            grant_config,
            should_revoke=revoke_existing_grants
        ) %}
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
                {% do doris__wait_for_materialized_view_refresh(
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
                    type=existing_relation.type
                ) -%}
                {% do adapter.rename_relation(
                    existing_relation,
                    current_backup_relation
                ) %}
                {% do adapter.rename_relation(
                    intermediate_relation,
                    target_relation
                ) %}
                {%- set backup_relation_to_drop = current_backup_relation -%}
                {% do store_raw_result(
                    name='main',
                    message='CREATE MATERIALIZED VIEW ' ~ target_relation,
                    code='CREATE MATERIALIZED VIEW',
                    rows_affected=-1
                ) %}
            {%- else -%}
                {% do adapter.rename_relation(
                    intermediate_relation,
                    target_relation
                ) %}
                {% do store_raw_result(
                    name='main',
                    message='CREATE MATERIALIZED VIEW ' ~ target_relation,
                    code='CREATE MATERIALIZED VIEW',
                    rows_affected=-1
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
            {% do doris__wait_for_materialized_view_refresh(
                target_relation,
                previous_task_ids
            ) %}
        {%- endif -%}

        {% set revoke_existing_grants = should_revoke(
            existing_relation,
            full_refresh_mode=true
        ) %}
        {% do apply_grants(
            target_relation,
            grant_config,
            should_revoke=revoke_existing_grants
        ) %}

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
    {%- endif -%}

    {{ doris__drop_relation(intermediate_relation) }}
    {{ run_hooks(post_hooks, inside_transaction=false) }}
    {%- if backup_relation_to_drop is not none -%}
        {% do adapter.drop_relation(backup_relation_to_drop) %}
    {%- endif -%}

    {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}
