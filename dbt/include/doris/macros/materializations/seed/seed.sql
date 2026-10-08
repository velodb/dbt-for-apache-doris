-- Licensed to the Apache Software Foundation (ASF) under one
-- or more contributor license agreements. See the NOTICE file
-- distributed with this work for additional information
-- regarding copyright ownership. The ASF licenses this file
-- to you under the Apache License, Version 2.0 (the
-- "License"); you may not use this file except in compliance
-- with the License. You may obtain a copy of the License at
--
--   http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing,
-- software distributed under the License is distributed on an
-- "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
-- KIND, either express or implied. See the License for the
-- specific language governing permissions and limitations
-- under the License.

{% materialization seed, adapter='doris' %}
    {% set target_relation = this.incorporate(type='table') %}
    {% set target_catalog = target_relation.database or 'internal' %}
    {% if target_catalog | lower == 'internal' and doris__is_olap_table(target_relation) %}
        {# Keep Core's existing OLAP TRUNCATE/INSERT and full-refresh behavior. #}
        {{ return(dbt.materialization_seed_default()) }}
    {% endif %}

    {% set agate_table = load_agate_table() %}
    {% do store_result('agate_table', response='OK', agate_table=agate_table) %}
    {% set full_refresh_mode = should_full_refresh() %}
    {% set grant_config = config.get('grants') %}
    {% set stage_relation = make_intermediate_relation(target_relation) %}
    {% set backup_relation = make_backup_relation(target_relation, 'table') %}
    {% set old_relation = load_cached_relation(target_relation) %}
    {% set preexisting_backup = load_cached_relation(backup_relation) %}

    {% if old_relation is none and preexisting_backup is not none %}
        {% do adapter.rename_relation(preexisting_backup, target_relation) %}
        {% set old_relation = load_cached_relation(target_relation) %}
        {% set preexisting_backup = none %}
    {% endif %}
    {% if old_relation is not none and not old_relation.is_table %}
        {% do exceptions.raise_compiler_error(
            "Cannot seed to " ~ old_relation ~ ": it is not a table."
        ) %}
    {% endif %}
    {% if preexisting_backup is not none %}
        {% do exceptions.raise_compiler_error(
            "Seed recovery backup " ~ backup_relation ~ " already exists. "
            ~ "Inspect the target and backup before retrying."
        ) %}
    {% endif %}

    {{ run_hooks(pre_hooks, inside_transaction=False) }}
    {{ run_hooks(pre_hooks, inside_transaction=True) }}
    {% do doris__preflight_grants(target_relation, grant_config) %}

    {% do drop_relation_if_exists(load_cached_relation(stage_relation)) %}
    {# CSV samples can change their inferred types without a schema change,
       for example when a string column becomes all NULL or numeric text.
       Ordinary reload must retain target types while checking headers;
       explicit column_types continue to override these defaults. #}
    {% set inferred_column_types = {} %}
    {% if (
        old_relation is not none
        and not full_refresh_mode
    ) %}
        {% for column in adapter.get_columns_in_relation(target_relation) %}
            {% do inferred_column_types.update({column.name | lower: column.data_type}) %}
        {% endfor %}
    {% endif %}
    {% set create_table_sql = doris__create_csv_table(
        model, agate_table, relation=stage_relation,
        inferred_column_types=inferred_column_types
    ) %}
    {% set insert_sql = doris__load_csv_rows_into_relation(
        model, agate_table, stage_relation
    ) %}
    {% set publish_sql = '' %}

    {% if old_relation is none %}
        {% do adapter.rename_relation(stage_relation, target_relation) %}
    {% elif full_refresh_mode %}
        {# Keep the old data under the intermediate name until post-processing
           succeeds, matching the external full-refresh exchange contract. #}
        {% do exchange_relation(target_relation, stage_relation, false) %}
    {% else %}
        {% set schema_changes = doris__check_for_schema_changes(
            stage_relation, target_relation
        ) %}
        {% if schema_changes['schema_changed'] %}
            {% do adapter.drop_relation(stage_relation) %}
            {% do exceptions.raise_compiler_error(
                "Seed schema changed for " ~ target_relation ~ ". "
                ~ "Run dbt seed --full-refresh --select " ~ model.name ~ "."
            ) %}
        {% endif %}
        {% set publish_sql = doris__get_incremental_insert_overwrite_sql({
            'target_relation': target_relation,
            'temp_relation': stage_relation,
            'dest_columns': schema_changes['source_columns'],
            'temp_relation_exists': true,
            'overwrite_partitions': none
        }) %}
        {# Execute even for zero rows: an empty CSV must replace old data. #}
        {% call statement('seed_overwrite') %}
            {{ publish_sql }}
        {% endcall %}
    {% endif %}

    {% set code = 'CREATE' if full_refresh_mode else 'INSERT' %}
    {% set rows_affected = agate_table.rows | length %}
    {% call noop_statement('main', code ~ ' ' ~ rows_affected, code, rows_affected) %}
        {{ get_csv_sql(create_table_sql, insert_sql) }};
        {{ publish_sql }};
    {% endcall %}
    {% set should_revoke = should_revoke(old_relation, full_refresh_mode) %}
    {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}
    {% do persist_docs(target_relation, model) %}
    {% if full_refresh_mode or old_relation is none %}
        {% do create_indexes(target_relation) %}
    {% endif %}
    {{ run_hooks(post_hooks, inside_transaction=True) }}
    {% do adapter.commit() %}
    {% do adapter.drop_relation(stage_relation) %}
    {{ run_hooks(post_hooks, inside_transaction=False) }}
    {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}
