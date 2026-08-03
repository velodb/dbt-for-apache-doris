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
  {% set target_relation = this.incorporate(type='table') %}
  {% set existing_relation = load_cached_relation(this) %}
  {% set temp_relation = make_temp_relation(target_relation) %}
  {% set intermediate_relation = make_intermediate_relation(target_relation) %}
  {% set recovered_from_backup = false %}
  {# A failed type replacement can leave the old object at dbt's backup name
     while the canonical target is absent. Keep that durable recovery marker
     in place until a complete target build succeeds. Moving it back to the
     canonical name before the model succeeds would make the next invocation's
     is_incremental() return true after another failure. #}
  {% set recovery_backup_relation = load_cached_relation(
      make_backup_relation(target_relation, 'table')
  ) %}
  {% if existing_relation is none and recovery_backup_relation is not none %}
      {% set recovered_from_backup = true %}
  {% endif %}

  {% set backup_relation_type = (
      'table'
      if existing_relation is none or existing_relation.is_view
      else existing_relation.type
  ) %}
  {% set backup_relation = make_backup_relation(
      target_relation,
      backup_relation_type
  ) %}

  {% set unique_key = config.get('unique_key') %}
  {% set strategy = dbt_doris_validate_get_incremental_strategy(config) %}
  {% set effective_strategy = doris__effective_incremental_strategy(
      strategy,
      unique_key
  ) %}
  {# Resolve the public dbt strategy before hooks or writes, so a missing
     custom macro and an unsupported built-in fail early. #}
  {% set strategy_sql_macro_func = adapter.get_incremental_strategy_macro(
      context,
      strategy
  ) %}
  {% set full_refresh_mode = (
      should_full_refresh()
      or recovered_from_backup
      or (existing_relation is not none and existing_relation.type != 'table')
  ) %}
  {% set on_schema_change = incremental_validate_on_schema_change(
      config.get('on_schema_change'),
      default='ignore'
  ) %}
  {% set incremental_predicates = (
      config.get('predicates', none)
      or config.get('incremental_predicates', none)
  ) %}
  {% set overwrite_partitions = config.get('overwrite_partitions', none) %}
  {% set grant_config = config.get('grants') %}

  {# Reject an incompatible target before hooks, helper cleanup, staging, DDL,
     or DML so a failed preflight is completely side-effect free. #}
  {% if existing_relation is not none and not full_refresh_mode %}
      {% do doris__validate_incremental_target(
          effective_strategy,
          target_relation,
          unique_key
      ) %}
  {% endif %}

  {% set preexisting_temp_relation = load_cached_relation(temp_relation) %}
  {% set preexisting_intermediate_relation = load_cached_relation(
      intermediate_relation
  ) %}
  {% set preexisting_backup_relation = (
      none
      if recovered_from_backup
      else load_cached_relation(backup_relation)
  ) %}
  {{ drop_relation_if_exists(preexisting_temp_relation) }}
  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}
  {{ drop_relation_if_exists(preexisting_backup_relation) }}

  {# Snapshot an active View before this model's hooks or sql_header can alter
     the session used to evaluate it. Keep the View online until the physical
     replacement has been built successfully. #}
  {% if existing_relation is not none and existing_relation.is_view %}
      {% do doris__snapshot_view_data_to_table(
          existing_relation,
          backup_relation
      ) %}
  {% endif %}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% set to_drop = [] %}
  {% if recovered_from_backup %}
      {% do to_drop.append(recovery_backup_relation) %}
  {% endif %}
  {% set need_swap = false %}
  {% set sql_header = config.get('sql_header', none) %}
  {# Execute the header once, before metadata inspection and model SQL. Embedding
     it in the empty-schema query loses cursor.description when the header is a
     SET statement, while repeating it in helper/view/DML statements is not the
     dbt sql_header contract. #}
  {% if sql_header is not none %}
      {% do run_query(sql_header) %}
  {% endif %}
  {% set source_sql = doris__table_colume_type(sql) %}

  {# Validate configured keys against the query metadata before CTAS, INSERT,
     or target schema mutation. This is a zero-row metadata query. #}
  {% set source_columns = none %}
  {% if effective_strategy == 'merge' %}
      {% set source_columns = get_column_schema_from_query(source_sql) %}
      {% do doris__validate_source_unique_key_columns(
          source_columns,
          unique_key
      ) %}
  {% endif %}

  {% if existing_relation is none %}
      {% set build_sql = doris__get_incremental_create_table_as_sql(
          effective_strategy,
          target_relation,
          source_sql,
          source_columns
      ) %}
      {% set relation_for_indexes = target_relation %}

  {% elif full_refresh_mode %}
      {% set build_sql = doris__get_incremental_create_table_as_sql(
          effective_strategy,
          intermediate_relation,
          source_sql,
          source_columns
      ) %}
      {% set relation_for_indexes = intermediate_relation %}
      {% set need_swap = true %}

  {% else %}
      {% set source_relation = none %}
      {% set temp_relation_exists = false %}
      {% set dest_columns = none %}

      {# Schema-changing runs and custom strategies need a frozen batch.
         Ordinary built-ins use a logical metadata view, not a physical staging
         table, and finish with one DML statement. #}
      {% set needs_physical_staging = (
          effective_strategy not in ['append', 'merge', 'insert_overwrite']
          or on_schema_change != 'ignore'
      ) %}

      {% if needs_physical_staging %}
          {% do run_query(doris__create_incremental_staging_table(
              temp_relation,
              source_sql
          )) %}
          {% set source_relation = temp_relation %}
          {% set temp_relation_exists = true %}
          {% do to_drop.append(source_relation) %}

      {% else %}
          {# A logical view stores no batch data. It gives Doris/dbt exact
             VARCHAR lengths for Core-compatible type widening and preserves
             dbt's standard five-key strategy contract through the DML. #}
          {% set source_relation = temp_relation.incorporate(type='view') %}
          {% do run_query(doris__create_incremental_schema_view(
              source_relation,
              source_sql
          )) %}
          {% set temp_relation_exists = true %}
          {% do to_drop.append(source_relation) %}
      {% endif %}

      {% if source_relation is not none %}
          {% if (
              on_schema_change != 'ignore'
              or effective_strategy == 'merge'
          ) %}
              {% set schema_changes = check_for_schema_changes(
                  source_relation,
                  existing_relation
              ) %}
              {% if effective_strategy == 'merge' %}
                  {% do doris__validate_unique_key_schema_changes(
                      schema_changes,
                      unique_key,
                      source_relation
                  ) %}
              {% endif %}

              {% if (
                  on_schema_change == 'fail'
                  and schema_changes['schema_changed']
              ) %}
                  {% do adapter.drop_relation(source_relation) %}
                  {% do doris__raise_schema_change_failure(schema_changes) %}
              {% endif %}

          {% endif %}

          {% set contract_config = config.get('contract') %}
          {% if (
              source_relation is not none
              and (not contract_config or not contract_config.enforced)
          ) %}
              {% do adapter.expand_target_column_types(
                  from_relation=source_relation,
                  to_relation=target_relation
              ) %}
          {% endif %}

          {# Re-read after widening: dbt Core does not treat an automatically
             widened string column as an on_schema_change mismatch. #}
          {% if (
              on_schema_change != 'ignore'
              and source_relation is not none
          ) %}
              {% set schema_changes = check_for_schema_changes(
                  source_relation,
                  existing_relation
              ) %}
          {% endif %}

          {% if on_schema_change != 'ignore' %}
              {% if (
                  on_schema_change == 'fail'
                  and schema_changes['schema_changed']
              ) %}
                  {# Remove the frozen physical stage before raising. #}
                  {% if source_relation is not none %}
                      {% do adapter.drop_relation(source_relation) %}
                  {% endif %}
                  {% do doris__raise_schema_change_failure(schema_changes) %}
              {% endif %}

              {% if schema_changes['schema_changed'] %}
                  {% do sync_column_schemas(
                      on_schema_change,
                      target_relation,
                      schema_changes
                  ) %}
              {% endif %}
              {% set dest_columns = schema_changes['source_columns'] %}
          {% endif %}
      {% endif %}

      {% if not dest_columns %}
          {% set dest_columns = adapter.get_columns_in_relation(existing_relation) %}
      {% endif %}

      {% set strategy_arg_dict = {
          'target_relation': target_relation,
          'temp_relation': temp_relation,
          'unique_key': unique_key,
          'dest_columns': dest_columns,
          'incremental_predicates': incremental_predicates,
          'source_sql': source_sql,
          'temp_relation_exists': temp_relation_exists,
          'overwrite_partitions': overwrite_partitions
      } %}
      {% set build_sql = strategy_sql_macro_func(strategy_arg_dict) %}
  {% endif %}

  {% call statement('main') %}
      {{ build_sql }}
  {% endcall %}

  {% if existing_relation is none or full_refresh_mode %}
      {% do create_indexes(relation_for_indexes) %}
  {% endif %}

  {% if need_swap %}
      {% if existing_relation.type == 'table' %}
          {# swap=true leaves the old target online under intermediate_relation
             until all post-processing succeeds and cleanup runs. #}
          {% do exchange_relation(
              target_relation,
              intermediate_relation,
              false
          ) %}
          {% do to_drop.append(intermediate_relation) %}
      {% elif existing_relation.is_view %}
          {# The recovery snapshot already exists, and the old View remained
             online while the replacement was built. #}
          {% do adapter.drop_relation(existing_relation) %}
          {% do adapter.rename_relation(
              intermediate_relation,
              target_relation
          ) %}
          {% do to_drop.append(backup_relation) %}
      {% else %}
          {% do adapter.rename_relation(existing_relation, backup_relation) %}
          {% do adapter.rename_relation(intermediate_relation, target_relation) %}
          {% do to_drop.append(backup_relation) %}
      {% endif %}
  {% endif %}

  {% set should_revoke = should_revoke(
      existing_relation,
      full_refresh_mode=full_refresh_mode
  ) %}
  {% do apply_grants(
      target_relation,
      grant_config,
      should_revoke=should_revoke
  ) %}
  {% do persist_docs(target_relation, model) %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {% do adapter.commit() %}

  {% for relation in to_drop %}
      {% do adapter.drop_relation(relation) %}
  {% endfor %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}
  {{ return({'relations': [target_relation]}) }}
{%- endmaterialization %}


{% macro doris__get_incremental_create_table_as_sql(
    strategy,
    relation,
    sql,
    source_columns=none
) %}
    {% if strategy == 'merge' %}
        {% set ordered_source_columns = doris__unique_key_first_columns(
            source_columns,
            config.get('unique_key')
        ) %}
        {% set validated_sql = doris__validated_unique_ctas_source_sql(
            sql,
            config.get('unique_key'),
            ordered_source_columns
        ) %}
        {{ return(doris__create_unique_table_as(
            false,
            relation,
            validated_sql,
            false,
            true
        )) }}
    {% endif %}
    {{ return(doris__create_table_as(
        false,
        relation,
        sql,
        false,
        true
    )) }}
{% endmacro %}


{% macro dbt_doris_validate_get_incremental_strategy(config) %}
    {% set unique_key = config.get('unique_key') %}
    {% set strategy = config.get('incremental_strategy') or 'default' %}

    {% if config.get('grants', none) %}
        {% do exceptions.raise_compiler_error(
            "The dbt 'grants' config is not implemented by dbt-doris yet. "
            ~ "Remove it from incremental model " ~ model.unique_id
            ~ " and manage Doris privileges separately."
        ) %}
    {% endif %}

    {% if config.get('sequence_col', none) %}
        {% do exceptions.raise_compiler_error(
            "dbt-doris does not implement the bare 'sequence_col' config. "
            ~ "Use Doris table property 'function_column.sequence_col' on "
            ~ model.unique_id ~ " instead."
        ) %}
    {% endif %}

    {% set properties = config.get('properties', none) or {} %}
    {% set normalized_property_names = [] %}
    {% for property_name in properties.keys() %}
        {% do normalized_property_names.append(property_name | lower) %}
    {% endfor %}
    {% if 'function_column.sequence_type' in normalized_property_names %}
        {% do exceptions.raise_compiler_error(
            "dbt-doris incremental models do not support Doris property "
            ~ "'function_column.sequence_type' on " ~ model.unique_id
            ~ ": it requires writing the hidden __DORIS_SEQUENCE_COL__ "
            ~ "column. Use 'function_column.sequence_col' with a visible "
            ~ "model column instead."
        ) %}
    {% endif %}

    {# dbt normalizes '+' to '_' during macro dispatch. Reject both spellings
       explicitly so neither can resolve a dbt Core global strategy macro. #}
    {% if strategy in ['delete+insert', 'delete_insert'] %}
        {% do exceptions.raise_compiler_error(
            "Incremental strategy '" ~ strategy ~ "' is not supported by "
            ~ "dbt-doris on model " ~ model.unique_id
            ~ ". Use 'merge' for Doris Unique Key upsert semantics."
        ) %}
    {% endif %}

    {% set effective_strategy = doris__effective_incremental_strategy(
        strategy,
        unique_key
    ) %}
    {% set normalized_unique_key = doris__normalize_unique_key(unique_key) %}
    {% set sequence_column = doris__sequence_column_from_properties() %}

    {% if sequence_column is not none and (
        sequence_column is not string
        or not modules.re.fullmatch(
            '[A-Za-z_][A-Za-z0-9_]*',
            sequence_column
        )
    ) %}
        {% do exceptions.raise_compiler_error(
            "Doris property 'function_column.sequence_col' on model "
            ~ model.unique_id ~ " must name one unquoted model column."
        ) %}
    {% endif %}

    {% if sequence_column is not none and effective_strategy != 'merge' %}
        {% do exceptions.raise_compiler_error(
            "Doris property 'function_column.sequence_col' is only valid "
            ~ "with incremental strategy 'merge' on model " ~ model.unique_id
        ) %}
    {% endif %}

    {% if effective_strategy == 'insert_overwrite' and normalized_unique_key %}
        {% set message -%}
Incremental strategy 'insert_overwrite' cannot be combined with 'unique_key'
on model {{ model.unique_id }}. Older dbt-doris releases used that combination
for Unique Key upserts; accepting it as native INSERT OVERWRITE could silently
delete rows absent from the incoming batch. Use strategy='merge' to upsert, or
remove 'unique_key' to explicitly enable native INSERT OVERWRITE.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if effective_strategy == 'merge' and not normalized_unique_key %}
        {% set message -%}
Incremental strategy 'merge' requires a 'unique_key' config on model
{{ model.unique_id }}.

Add a key, for example:
    {{ '{{' }} config(
        materialized='incremental',
        incremental_strategy='merge',
        unique_key=['id']
    ) {{ '}}' }}
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if effective_strategy == 'merge' %}
        {% set normalized_key_names = [] %}
        {% for key in normalized_unique_key %}
            {% if key is not string or not modules.re.fullmatch(
                '[A-Za-z_][A-Za-z0-9_]*',
                key
            ) %}
                {% set message -%}
Invalid unique_key {{ key }} on model {{ model.unique_id }}. Doris table keys
must be unquoted column names containing only letters, digits, and underscores.
                {%- endset %}
                {% do exceptions.raise_compiler_error(message) %}
            {% endif %}
            {% if (key | lower) in normalized_key_names %}
                {% do exceptions.raise_compiler_error(
                    "Duplicate unique_key column '" ~ key ~ "' on model "
                    ~ model.unique_id
                ) %}
            {% endif %}
            {% do normalized_key_names.append(key | lower) %}
        {% endfor %}
    {% endif %}

    {% set overwrite_partitions = config.get('overwrite_partitions', none) %}
    {% if (
        overwrite_partitions is not none
        and effective_strategy != 'insert_overwrite'
    ) %}
        {% set message -%}
Config 'overwrite_partitions' is only valid with incremental strategy
'insert_overwrite' on model {{ model.unique_id }}.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if overwrite_partitions is not none %}
        {% if overwrite_partitions is string %}
            {% set partitions = [overwrite_partitions] %}
        {% else %}
            {% set partitions = overwrite_partitions | list %}
        {% endif %}

        {% if not partitions %}
            {% do exceptions.raise_compiler_error(
                "Config 'overwrite_partitions' must not be empty on model "
                ~ model.unique_id
            ) %}
        {% endif %}
        {% if '*' in partitions and partitions != ['*'] %}
            {% do exceptions.raise_compiler_error(
                "Dynamic partition '*' cannot be combined with named "
                "overwrite_partitions on model " ~ model.unique_id
            ) %}
        {% endif %}
        {% if config.get('partition_by', none) is none %}
            {% do exceptions.raise_compiler_error(
                "Config 'overwrite_partitions' requires a partitioned Doris "
                "target via 'partition_by' on model " ~ model.unique_id
            ) %}
        {% endif %}
        {% for partition in partitions %}
            {% if partition != '*' and (
                partition is not string
                or not modules.re.fullmatch(
                    '[A-Za-z_][A-Za-z0-9_]*',
                    partition
                )
            ) %}
                {% do exceptions.raise_compiler_error(
                    "Unsafe Doris partition name '" ~ partition
                    ~ "' in overwrite_partitions on model " ~ model.unique_id
                ) %}
            {% endif %}
        {% endfor %}
    {% endif %}

    {% set incremental_predicates = (
        config.get('predicates', none)
        or config.get('incremental_predicates', none)
    ) %}
    {% if incremental_predicates and effective_strategy in [
        'append',
        'merge',
        'insert_overwrite'
    ] %}
        {% set message -%}
Config 'incremental_predicates' is not supported by Doris strategy
'{{ effective_strategy }}'. Conditional target filtering requires the Doris
4.1+ native MERGE INTO path, which is not enabled yet.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {% if effective_strategy == 'merge' and (
        config.get('merge_update_columns', none)
        or config.get('merge_exclude_columns', none)
    ) %}
        {% set message -%}
Doris strategy 'merge' currently performs a full-row Unique Key upsert.
'merge_update_columns' and 'merge_exclude_columns' require the Doris 4.1+
native MERGE INTO path, which is not enabled yet.
        {%- endset %}
        {% do exceptions.raise_compiler_error(message) %}
    {% endif %}

    {{ return(strategy) }}
{% endmacro %}
