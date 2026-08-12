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

{% macro doris__engine() -%}
    {% set label = 'ENGINE' %}
    {% set engine = config.get('engine', 'OLAP') %}
    {{ label }} = {{ engine }}
{%- endmacro %}

{% macro doris__partition_by() -%}
  {% if config.get('incremental_strategy', none) == 'microbatch' %}
    {{ return(doris__microbatch_partition_by_clause()) }}
  {% endif %}
  {% set cols = config.get('partition_by', validator=validation.any[list, basestring]) %}
  {% set partition_type = config.get('partition_type', 'RANGE') %}
  {% if cols is not none %}
      {%- if cols is string -%}
        {%- set cols = [cols] -%}
      {%- endif -%}
    PARTITION BY {{ partition_type }} (
      {% for col in cols %}
        {{ col }}{% if not loop.last %},{% endif %}
      {% endfor %}
    )(
        {% set init = config.get('partition_by_init', validator=validation.any[list]) %}
        {% if init is not none %}
          {% for row in init %}
            {{ row }}{% if not loop.last %},{% endif %}
          {% endfor %}
        {% endif %}
    )
  {% endif %}
{%- endmacro %}

{% macro doris__duplicate_key() -%}
  {% set cols = config.get('duplicate_key', validator=validation.any[list, basestring]) %}
  {% if cols is not none %}
      {%- if cols is string -%}
        {%- set cols = [cols] -%}
      {%- endif -%}
    DUPLICATE KEY (
      {% for item in cols %}
        {{ adapter.quote(item) }}
      {% if not loop.last %},{% endif %}
      {% endfor %}
    )
  {% endif %}
{%- endmacro %}

{% macro doris__table_comment() -%}
  {% set description = model.get('description', "") %}
  {% if config.persist_relation_docs() and description %}
    COMMENT '{{ description | replace("\\", "\\\\") | replace("'", "\\'") }}'
  {% endif %}
{%- endmacro %}

{% macro doris__unique_key() -%}
  {% set cols = config.get('unique_key', validator=validation.any[list, basestring]) %}

  {% if cols is not none %}
    {%- if cols is string -%}
      {%- set cols = [cols] -%}
    {%- endif -%}

    UNIQUE KEY (
      {% for item in cols %}
        {{ adapter.quote(item) }}
      {% if not loop.last %},{% endif %}
      {% endfor %}
    )
  {% endif %}
{%- endmacro %}

{% macro doris__distributed_by(column_names=none) -%}
  {% set engine = config.get('engine', validator=validation.any[basestring]) %}
  {% set cols = config.get('distributed_by', validator=validation.any[list, basestring]) %}
  {% if cols is none and engine in [none,'OLAP'] %}
    {% set cols = column_names %}
  {% endif %}

  {% if cols %}
      {%- if cols is string -%}
        {%- set cols = [cols] -%}
      {%- endif -%}
    DISTRIBUTED BY HASH (
      {% for item in cols %}
        {{ adapter.quote(item) }}{% if not loop.last %},{% endif %}
      {% endfor %}
    ) BUCKETS {{ config.get('buckets', validator=validation.any[int]) or 10 }}
  {% endif %}
{%- endmacro %}

{% macro doris__properties(default_properties=none) -%}
  {# Work on a new dictionary. Mutating config.get('properties') leaks adapter
     defaults into the parsed model config used by later macros. #}
  {% set properties = {} %}
  {% if default_properties %}
    {% do properties.update(default_properties) %}
  {% endif %}

  {% set configured_properties = config.get('properties', validator=validation.any[dict]) %}
  {% if configured_properties %}
    {% do properties.update(configured_properties) %}
  {% endif %}

  {% set replice_num = config.get('replication_num') %}

  {% if replice_num is not none %}
    {% do properties.update({'replication_num': replice_num}) %}
  {% endif %}

  {% if properties %}
    PROPERTIES (
        {% for key, value in properties.items() %}
          "{{ key }}" = "{{ value }}"{% if not loop.last %},{% endif %}
        {% endfor %}
    )
  {% endif %}
{%- endmacro%}

{% macro doris__drop_relation(relation) -%}
  {% if relation is not none %}
    {% set relation_type = relation.type %}
    {% if not relation_type or relation_type is none %}
        {% set relation_type = 'table' %}
    {% endif %}
    {% if relation_type == 'materialized_view' %}
        {% set relation_type = 'materialized view' %}
    {% endif %}
    {% call statement('drop_relation', auto_begin=False) %}
      drop {{ relation_type }} if exists {{ relation }}
    {% endcall %}
  {% endif %}

{%- endmacro %}

{% macro doris__truncate_relation(relation) -%}
    {% call statement('truncate_relation') %}
      truncate table {{ relation }}
    {% endcall %}
{%- endmacro %}

{% macro doris__snapshot_view_data_to_table(
    from_relation,
    to_relation
) -%}
  {% if not from_relation.is_view or to_relation.type != 'table' %}
    {% do exceptions.raise_compiler_error(
        "Doris View recovery requires a View source and Table destination."
    ) %}
  {% endif %}
  {% if (
      from_relation.schema == to_relation.schema
      and from_relation.identifier == to_relation.identifier
  ) %}
    {% do exceptions.raise_compiler_error(
        "Doris View recovery source and destination must be different relations."
    ) %}
  {% endif %}

  {# Evaluate the existing View before the replacement model can change the
     session. Doris 2.1 can apply the current session's SQL mode while reading
     a View, and SHOW CREATE VIEW does not expose enough state to replay the
     View definition portably. #}
  {% set existing_destination = load_cached_relation(to_relation) %}
  {% if existing_destination is not none %}
    {% do exceptions.raise_compiler_error(
        "Doris View recovery destination must not already exist: "
        ~ to_relation
    ) %}
  {% endif %}
  {% do run_query(doris__create_view_snapshot_table(
      to_relation,
      from_relation
  )) %}
  {% do adapter.cache_added(to_relation) %}
{%- endmacro %}

{% macro doris__snapshot_view_to_table(
    from_relation,
    to_relation
) -%}
  {% do doris__snapshot_view_data_to_table(from_relation, to_relation) %}
  {# Callers that need an immediate conversion drop the source only after the
     physical recovery snapshot succeeds. Active type replacements use the
     data-only helper and keep the source online until the replacement is
     ready to publish. #}
  {% do adapter.drop_relation(from_relation) %}
{%- endmacro %}

{% macro doris__rename_relation(from_relation, to_relation) -%}
  {% if from_relation.is_view or to_relation.is_view %}
    {% do exceptions.raise_compiler_error(
        "Doris cannot safely rename a View. Materializations must snapshot "
        ~ "the View to a Table."
    ) %}
  {% endif %}

  {% call statement('drop_relation') %}
    drop {{ 'materialized view' if to_relation.type == 'materialized_view' else to_relation.type }} if exists {{ to_relation }}
  {% endcall %}
  {% call statement('rename_relation') %}
    {% if to_relation.type == 'materialized_view' %}
    alter materialized view {{ from_relation }} rename `{{ to_relation.table | replace("`", "``") }}`
    {% else %}
    alter table {{ from_relation }} rename {{ to_relation.table }}
    {% endif %}
  {% endcall %}

{%- endmacro %}


{% macro exchange_relation(relation1, relation2, is_drop_r1=false) -%}
  {% if relation1.is_view or relation2.is_view %}
    {% do exceptions.raise_compiler_error(
        "Doris cannot safely exchange Views."
    ) %}
  {% endif %}
  {% call statement('exchange_relation') %}
    ALTER TABLE {{ relation1 }} REPLACE WITH TABLE `{{ relation2.table }}` PROPERTIES('swap' = '{{not is_drop_r1}}');
  {% endcall %}

{%- endmacro %}

{% macro doris__get_or_create_relation(database, schema, identifier, type) %}
  {%- set target_relation = adapter.get_relation(database=database, schema=schema, identifier=identifier) %}

  {% if target_relation %}
    {% do return([true, target_relation]) %}
  {% endif %}

  {%- set new_relation = api.Relation.create(
      database=none,
      schema=schema,
      identifier=identifier,
      type=type
  ) -%}
  {% do return([false, new_relation]) %}
{% endmacro %}

{% macro drop_relation_if_exists(relation) %}
  {% if relation is not none %}
    {% do adapter.drop_relation(relation) %}
  {% endif %}
{% endmacro %}

{% macro create_indexes(relation) -%}
  {#-- No-op: this adapter has no index config yet.

       Doris does support secondary indexes -- inverted, bloom filter, bitmap and
       ngram bloom filter -- so this is a missing feature, not a platform limit.
       Adding them means exposing an `indexes` config and building the clauses at
       CREATE TABLE time, since Doris declares indexes in the table definition
       rather than through a separate CREATE INDEX statement. --#}
{%- endmacro %}
