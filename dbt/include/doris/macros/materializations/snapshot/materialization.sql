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

{#--
  dbt Core's default Snapshot materialization assumes that temporary relations
  are session-scoped and that snapshot_merge_sql is a single DML statement.
  Doris uses physical CTAS staging and rebuilds the complete history before an
  atomic REPLACE WITH TABLE, so it needs an explicit failure-safe lifecycle.
--#}
{% materialization snapshot, adapter='doris' %}
  {% set target_table = model.get('alias', model.get('name')) %}
  {% set strategy_name = config.get('strategy') %}
  {% set unique_key = config.get('unique_key') %}
  {% set grant_config = config.get('grants') %}

  {% set target_relation_exists, target_relation = get_or_create_relation(
      database=model.database,
      schema=model.schema,
      identifier=target_table,
      type='table'
  ) %}
  {% set staging_relation = make_temp_relation(target_relation) %}
  {% set upsert_relation = doris__snapshot_upsert_relation(target_relation) %}
  {% set initial_relation = doris__snapshot_initial_relation(target_relation) %}

  {% if target_relation_exists and not target_relation.is_table %}
    {% do exceptions.relation_wrong_type(target_relation, 'table') %}
  {% endif %}

  {# Fixed helper names are reserved by this materialization. Every execution
     starts clean; the target remains authoritative while helpers are rebuilt. #}
  {% do doris__drop_relation(staging_relation) %}
  {% do doris__drop_relation(initial_relation) %}
  {% do doris__drop_relation(upsert_relation) %}

  {{ run_hooks(pre_hooks, inside_transaction=false) }}
  {{ run_hooks(pre_hooks, inside_transaction=true) }}

  {% set strategy_macro = strategy_dispatch(strategy_name) %}
  {% set strategy = strategy_macro(
      model,
      'snapshotted_data',
      'source_data',
      model['config'],
      target_relation_exists
  ) %}

  {% do doris__validate_snapshot_source(
      strategy,
      model['compiled_code'],
      target_relation if target_relation_exists else none
  ) %}

  {% if not target_relation_exists %}
    {% set build_sql = build_snapshot_table(strategy, model['compiled_code']) %}
    {{ check_time_data_types(build_sql) }}

    {% call statement('main') %}
      {{ create_table_as(false, initial_relation, build_sql) }}
    {% endcall %}

    {% do doris__validate_snapshot_upsert(initial_relation) %}
    {% do adapter.rename_relation(initial_relation, target_relation) %}
  {% else %}
    {% set columns = config.get('snapshot_table_column_names') or
        get_snapshot_table_column_names() %}

    {{ adapter.assert_valid_snapshot_target_given_strategy(
        target_relation, columns, strategy
    ) }}

    {% set build_or_select_sql = snapshot_staging_table(
        strategy, model['compiled_code'], target_relation
    ) %}
    {% set staging_relation = build_snapshot_staging_table(
        strategy, model['compiled_code'], target_relation
    ) %}

    {% set remove_columns = [
        'dbt_change_type', 'DBT_CHANGE_TYPE',
        'dbt_unique_key', 'DBT_UNIQUE_KEY'
    ] %}
    {% if unique_key | is_list %}
      {% for key in strategy.unique_key %}
        {% do remove_columns.append('dbt_unique_key_' ~ loop.index) %}
        {% do remove_columns.append('DBT_UNIQUE_KEY_' ~ loop.index) %}
      {% endfor %}
    {% endif %}

    {# Adding a nullable source field preserves history. Removing or changing a
       historical business field is destructive and requires an explicit Doris
       migration outside dbt snapshot. dbt_is_deleted is metadata and may be
       absent when moving away from hard_deletes='new_record'. #}
    {% set target_only_columns = adapter.get_missing_columns(
        target_relation, staging_relation
    ) %}
    {% set removed_business_columns = [] %}
    {% for column in target_only_columns %}
      {% if column.name | lower != columns.dbt_is_deleted | lower %}
        {% do removed_business_columns.append(column.name) %}
      {% endif %}
    {% endfor %}
    {% if removed_business_columns %}
      {% do exceptions.raise_compiler_error(
          "Snapshot source removed historical column(s): " ~
          (removed_business_columns | join(', ')) ~
          ". dbt-doris will not erase historical fields automatically; " ~
          "migrate the Snapshot table explicitly."
      ) %}
    {% endif %}

    {% set target_columns = adapter.get_columns_in_relation(target_relation) %}
    {# Doris widens VARCHAR columns while materializing the UNION-based staging
       query. Validate user columns against the original Snapshot query schema
       so that generated staging types do not look like destructive changes. #}
    {% set source_query_columns = get_column_schema_from_query(
        model['compiled_code']
    ) %}
    {% set target_columns_by_name = {} %}
    {% for column in target_columns %}
      {% do target_columns_by_name.update({column.name | lower: column}) %}
    {% endfor %}
    {% set metadata_column_names = [
        columns.dbt_scd_id | lower,
        columns.dbt_updated_at | lower,
        columns.dbt_valid_from | lower,
        columns.dbt_valid_to | lower,
        columns.dbt_is_deleted | lower
    ] %}
    {% set changed_types = [] %}
    {% for column in source_query_columns %}
      {% set column_name = column.name | lower %}
      {% if column_name in target_columns_by_name and
            column_name not in metadata_column_names %}
        {% set target_column = target_columns_by_name[column_name] %}
        {% if not doris__snapshot_type_can_write(column, target_column) %}
          {% do changed_types.append(
              column.name ~ ': ' ~ target_column.expanded_data_type ~
              ' -> ' ~ column.expanded_data_type
          ) %}
        {% endif %}
      {% endif %}
    {% endfor %}
    {% if changed_types %}
      {% do exceptions.raise_compiler_error(
          "Snapshot source changed historical column type(s): " ~
          (changed_types | join(', ')) ~
          ". dbt-doris requires an explicit type migration for Snapshot history."
      ) %}
    {% endif %}

    {% set missing_columns = adapter.get_missing_columns(
        staging_relation, target_relation
    ) | rejectattr('name', 'in', remove_columns) | list %}
    {% do create_columns(target_relation, missing_columns) %}

    {% set source_columns = adapter.get_columns_in_relation(staging_relation)
        | rejectattr('name', 'in', remove_columns) | list %}
    {% set quoted_source_columns = [] %}
    {% for column in source_columns %}
      {% do quoted_source_columns.append(adapter.quote(column.name)) %}
    {% endfor %}

    {{ check_time_data_types(build_or_select_sql) }}
    {% set final_sql = snapshot_merge_sql(
        target=target_relation,
        source=staging_relation,
        insert_cols=quoted_source_columns
    ) %}

    {% call statement('main') %}
      {{ final_sql }}
    {% endcall %}
  {% endif %}

  {% set should_revoke = should_revoke(
      target_relation_exists, full_refresh_mode=false
  ) %}
  {% do apply_grants(
      target_relation, grant_config, should_revoke=should_revoke
  ) %}
  {% do persist_docs(target_relation, model) %}

  {% if not target_relation_exists %}
    {% do create_indexes(target_relation) %}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=true) }}
  {{ adapter.commit() }}

  {% if staging_relation is defined %}
    {% do post_snapshot(staging_relation) %}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=false) }}

  {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}
