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

{% materialization incremental, adapter='doris' %}
  {% set unique_key = config.get('unique_key', validator=validation.any[list]) %}
  {% set strategy = dbt_doris_validate_get_incremental_strategy(config) %}
  {% set full_refresh_mode = (should_full_refresh()) %}
  {% set target_relation = this.incorporate(type='table') %}
  {% set existing_relation = load_relation(this) %}
  {% set tmp_relation = make_temp_relation(this) %}
  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}
  {% set to_drop = [] %}

  {#-- Dispatch on the strategy alone.

       This used to read `if not unique_key or strategy == 'append'`, which let a
       missing unique_key silently route an 'insert_overwrite' model into the
       append branch: the model built a DUPLICATE KEY table and appended on every
       run, duplicating all rows, while dbt reported success. The strategy is
       validated up front now, so an unusable combination fails loudly instead.
  --#}
  {% if strategy == 'append' %}
        {#-- create table first --#}
        {% if existing_relation is none  %}
            {% set build_sql = doris__create_table_as(False, target_relation, sql) %}
        {% elif existing_relation.is_view or full_refresh_mode %}
            {#-- backup table is new table ,exchange table backup and old table #}
            {% set backup_identifier = existing_relation.identifier ~ "__dbt_backup" %}
            {% set backup_relation = existing_relation.incorporate(path={"identifier": backup_identifier}) %}
            {% do adapter.drop_relation(backup_relation) %} {#-- likes 'drop table if exists ... ' --#}
            {% set run_sql = doris__create_table_as(False, backup_relation, sql) %}
            {% call statement("run_sql") %}
                {{ run_sql }}
            {% endcall %}
            {% do exchange_relation(target_relation, backup_relation, True) %}
            {% set build_sql = "select 'hello doris'" %}
        {#-- append data --#}
        {% else %}
            {% do to_drop.append(tmp_relation) %}
            {% do doris__drop_relation(tmp_relation) %}
            {% do run_query(create_table_as(True, tmp_relation, sql)) %}
            {% set build_sql = tmp_insert(tmp_relation, target_relation) %}
        {% endif %}
  {#-- insert overwrite --#}
  {% elif strategy == 'insert_overwrite' %}
        {#-- create table first --#}
        {% if existing_relation is none  %}
            {% set build_sql = doris__create_unique_table_as(False, target_relation, sql) %}
        {#-- insert data refresh --#}
        {% elif existing_relation.is_view or full_refresh_mode %}
            {#-- backup table is new table ,exchange table backup and old table #}
            {% set backup_identifier = existing_relation.identifier ~ "__dbt_backup" %}
            {% set backup_relation = existing_relation.incorporate(path={"identifier": backup_identifier}) %}
            {% do adapter.drop_relation(backup_relation) %} {#-- likes 'drop table if exists ... ' --#}
            {% set run_sql = doris__create_unique_table_as(False, backup_relation, sql) %}
            {% call statement("run_sql") %}
                {{ run_sql }}
            {% endcall %}
            {% do exchange_relation(target_relation, backup_relation, True) %}
            {% set build_sql = "select 'hello doris'" %}
        {#-- append data --#}
        {% else %}
          {#-- check doris unique table  --#}
          {% if not is_unique_model(target_relation) %}
                {% set not_unique_msg -%}
Doris table {{ target_relation }} is not a UNIQUE KEY table, so incremental
strategy 'insert_overwrite' cannot upsert into it.

This happens when an existing model switches to 'insert_overwrite' from another
strategy: the table was created with DUPLICATE KEY and its key type cannot be
altered in place. Rebuild it:

    dbt run --full-refresh --select {{ model.name }}
                {%- endset %}
                {% do exceptions.raise_compiler_error(not_unique_msg) %}
          {% endif %}
          {#-- create temp duplicate table for this incremental task  --#}
          {% do to_drop.append(tmp_relation) %}
          {% do doris__drop_relation(tmp_relation) %}
          {% do run_query(create_table_as(True, tmp_relation, sql)) %}
          {% do adapter.expand_target_column_types(
                 from_relation=tmp_relation,
                 to_relation=target_relation) %}
          {% set build_sql = tmp_insert(tmp_relation, target_relation) %}
        {% endif %}
  {% else %}
          {#-- never  --#}
  {% endif %}

  {% call statement("main") %}
      {{ build_sql }}
  {% endcall %}

  {#--  {% do persist_docs(target_relation, model) %}  #}
  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {% do adapter.commit() %}
  {% for rel in to_drop %}
      {% do doris__drop_relation(rel) %}
  {% endfor %}
  {{ run_hooks(post_hooks, inside_transaction=False) }}
  {{ return({'relations': [target_relation]}) }}
{%- endmaterialization %}

{% macro dbt_doris_validate_get_incremental_strategy(config) %}
  {#-- Find and validate the incremental strategy #}
  {%- set strategy = config.get('incremental_strategy') or 'insert_overwrite' -%}
  {% set invalid_strategy_msg -%}
    Invalid incremental strategy provided: {{ strategy }}
    Expected one of: 'append', 'insert_overwrite'
  {%- endset %}
  {% if strategy not in ['append', 'insert_overwrite'] %}
    {% do exceptions.raise_compiler_error(invalid_strategy_msg) %}
  {% endif %}

  {#-- 'insert_overwrite' relies on Doris UNIQUE KEY upsert semantics, so it
       cannot work without a unique_key. Reject the combination instead of
       silently falling back to append, which duplicated rows on every run. --#}
  {%- set unique_key = config.get('unique_key', validator=validation.any[list]) -%}
  {% set missing_unique_key_msg -%}
Incremental strategy 'insert_overwrite' requires a 'unique_key' config on model
{{ model.unique_id }}.

Doris implements this strategy through UNIQUE KEY upsert semantics, which needs the
key columns to be known at table creation time.

Either add the key columns:
    {{ '{{' }} config(materialized='incremental', unique_key=['<your_key>']) {{ '}}' }}
or, if appending every row is what you want, say so explicitly:
    {{ '{{' }} config(materialized='incremental', incremental_strategy='append') {{ '}}' }}
  {%- endset %}
  {% if strategy == 'insert_overwrite' and not unique_key %}
    {% do exceptions.raise_compiler_error(missing_unique_key_msg) %}
  {% endif %}

  {% do return (strategy) %}
{% endmacro %}
