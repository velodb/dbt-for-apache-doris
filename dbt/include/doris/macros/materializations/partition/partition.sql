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

{#-- Registered for the doris adapter specifically. This was declared as the
     global `default` materialization, but its body is Doris-only: temporary
     partitions, REPLACE PARTITION, and Doris DDL throughout. --#}
{% materialization partition, adapter='doris' -%}

  {% set partition_by = config.get('partition_by') %}

  {% set target_relation = this.incorporate(type='table') %}
  {% set existing_relation = load_relation(this) %}
  {% set tmp_relation = make_temp_relation(target_relation) %}
  {% set recovered_from_backup = false %}

  {% set on_schema_change = incremental_validate_on_schema_change(config.get('on_schema_change'), default='ignore') %}

  {% set tmp_identifier = model['name'] + '__dbt_tmp' %}
  {% set backup_identifier = model['name'] + "__dbt_backup" %}

  -- the intermediate_ and backup_ relations should not already exist in the database; get_relation
  -- will return None in that case. Otherwise, we get a relation that we can drop
  -- later, before we try to use this name for the current operation. This has to happen before
  -- BEGIN, in a separate transaction
  {% set preexisting_intermediate_relation = adapter.get_relation(identifier=tmp_identifier,
                                                                  schema=schema,
                                                                  database=database) %}
  {% set preexisting_backup_relation = adapter.get_relation(identifier=backup_identifier,
                                                            schema=schema,
                                                            database=database) %}
  {% if existing_relation is none and preexisting_backup_relation is not none %}
      {# Keep the backup name as a durable recovery marker. If the model fails
         again, the next compilation must still see no canonical Table so
         is_incremental() remains false. #}
      {% set recovery_backup_relation = preexisting_backup_relation %}
      {% set recovered_from_backup = true %}
      {% set preexisting_backup_relation = none %}
  {% endif %}
  {%- set full_refresh_mode = (
      should_full_refresh() or recovered_from_backup
  ) -%}
  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}
  {{ drop_relation_if_exists(preexisting_backup_relation) }}

  {% set active_view_backup_relation = none %}
  {% if existing_relation is not none and existing_relation.is_view %}
      {# Snapshot before this model's pre-hooks or sql_header can alter the
         session used to evaluate the View. Keep the source online until the
         replacement Table has been built successfully. #}
      {% set active_view_backup_relation = existing_relation.incorporate(
          path={"identifier": backup_identifier},
          type='table'
      ) %}
      {% do doris__snapshot_view_data_to_table(
          existing_relation,
          active_view_backup_relation
      ) %}
  {% endif %}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}

  -- `BEGIN` happens here:
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% set to_drop = [] %}
  {% if recovered_from_backup %}
      {% do to_drop.append(recovery_backup_relation) %}
  {% endif %}

  {# -- first check whether we want to full refresh for source view or config reasons #}
  {% set trigger_full_refresh = (
      full_refresh_mode
      or (
          existing_relation is not none
          and existing_relation.type != 'table'
      )
  ) %}

  {% if existing_relation is none %}
      {% set build_sql = create_table_as(False, target_relation, sql) %}
    {% elif trigger_full_refresh %}
      {#-- Make sure the backup doesn't exist so we don't encounter issues with the rename below #}
      {% set tmp_identifier = model['name'] + '__dbt_tmp' %}
      {% set backup_identifier = model['name'] + '__dbt_backup' %}
      {% set intermediate_relation = target_relation.incorporate(
          path={"identifier": tmp_identifier}
      ) %}
      {% if existing_relation.is_view %}
          {% set backup_relation = active_view_backup_relation %}
      {% else %}
          {% set backup_relation = existing_relation.incorporate(
              path={"identifier": backup_identifier},
              type=existing_relation.type
          ) %}
      {% endif %}

      {% set build_sql = create_table_as(False, intermediate_relation, sql) %}
      {% set need_swap = true %}
      {% do to_drop.append(backup_relation) %}
  {% else %}
    {% do to_drop.append(tmp_relation) %}
    {% do doris__drop_relation(tmp_relation) %}
    {% do run_query(create_table_as(True, tmp_relation, sql)) %}
    {% do adapter.expand_target_column_types(
             from_relation=tmp_relation,
             to_relation=target_relation) %}

    {% set distinct_partitions = get_distinct_partitions(tmp_relation, partition_by) %}
    {% do create_partitions(target_relation,distinct_partitions) %}
    {% do insert_data_to_tmp_partitions(tmp_relation,target_relation, distinct_partitions) %}

    {#-- Each replace runs as its own statement; see doris__replace_partitions. --#}
    {% set replaced = doris__replace_partitions(target_relation, distinct_partitions) %}
    {% set build_sql = none %}
  {% endif %}

  {#-- On the incremental path the work is already done by the statements above.
       dbt still needs a 'main' statement to exist for the run to be recorded, so
       emit a trivial one rather than a multi-statement blob. --#}
  {% call statement("main") %}
      {% if build_sql is none %}
          select {{ replaced }} as `partitions_replaced`
      {% else %}
          {{ build_sql }}
      {% endif %}
  {% endcall %}

  {% if need_swap %}
      {% if existing_relation.is_view %}
          {% do adapter.drop_relation(existing_relation) %}
      {% else %}
          {% do adapter.rename_relation(
              existing_relation,
              backup_relation
          ) %}
      {% endif %}
      {% do adapter.rename_relation(intermediate_relation, target_relation) %}
  {% endif %}

  {% do persist_docs(target_relation, model) %}

  {% if existing_relation is none or trigger_full_refresh %}
    {% do create_indexes(target_relation) %}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  -- `COMMIT` happens here
  {% do adapter.commit() %}

  {% for rel in to_drop %}
      {% do adapter.drop_relation(rel) %}
  {% endfor %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target_relation]}) }}

{%- endmaterialization %}
