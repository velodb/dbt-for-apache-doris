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

{% materialization table, adapter='doris' %}

  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='table') %}
  {%- set intermediate_relation =  make_intermediate_relation(target_relation) -%}
  {%- set backup_relation = make_backup_relation(target_relation, 'table') -%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation) -%}
  {%- set preexisting_backup_relation = load_cached_relation(
      backup_relation
  ) -%}

  {% if existing_relation is none and preexisting_backup_relation is not none %}
    {%- set restored_relation = target_relation.incorporate(type=(
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
    {%- set existing_relation = load_cached_relation(restored_relation) -%}
    {%- set preexisting_backup_relation = none -%}
  {% endif %}

  {# Preserve an async Materialized View as the same relation type so a failed
     type switch can restore it. View backups are always physical Tables. #}
  {%- set backup_relation = make_backup_relation(
      target_relation,
      (
        'table'
        if existing_relation is none or existing_relation.is_view
        else existing_relation.type
      )
  ) -%}

  -- grab current tables grants config for comparision later on
  {% set grant_config = config.get('grants') %}

  -- drop the temp relations if they exist already in the database
  {% if preexisting_intermediate_relation is not none %}
    {% do adapter.drop_relation(preexisting_intermediate_relation) %}
  {% endif %}
  {% if preexisting_backup_relation is not none %}
    {% do adapter.drop_relation(preexisting_backup_relation) %}
  {% endif %}

  {# Snapshot an active View before this model's pre-hooks or sql_header can
     alter the session used to evaluate it. Keep the source View online until
     the replacement Table has been built successfully. #}
  {% if existing_relation is not none and existing_relation.is_view %}
    {% do doris__snapshot_view_data_to_table(
        existing_relation,
        backup_relation
    ) %}
  {% endif %}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}

  -- `BEGIN` happens here:
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  -- build model
  {% if config.persist_column_docs() %}
    {% do doris__create_documented_table_as(False, intermediate_relation, sql) %}
  {% else %}
    {% call statement('main') -%}
      {{ doris__create_table_as(False, intermediate_relation, sql) }}
    {%- endcall %}
  {% endif %}

  {% set to_drop = [intermediate_relation] %}
  {% if existing_relation is not none and existing_relation.type == 'table' -%}
    {% do exchange_relation(target_relation, intermediate_relation, True) %}
  {% elif existing_relation is not none and existing_relation.is_view -%}
    {% do adapter.drop_relation(existing_relation) %}
    {{ adapter.rename_relation(intermediate_relation, target_relation) }}
    {% do to_drop.append(backup_relation) %}
  {% else %}
    {% if existing_relation is not none %}
      {% do adapter.rename_relation(existing_relation, backup_relation) %}
      {% do to_drop.append(backup_relation) %}
    {% endif %}
    {{ adapter.rename_relation(intermediate_relation, target_relation) }}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode=True) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}

  -- alter relation comment
  {% do persist_docs(target_relation, model) %}

  -- `COMMIT` happens here
  {% do adapter.commit() %}

  -- finally, drop the existing/backup relations after the commit
  {% for relation in to_drop %}
    {% do adapter.drop_relation(relation) %}
  {% endfor %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}
