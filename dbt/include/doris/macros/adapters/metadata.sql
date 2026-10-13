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

{% macro doris__catalog_name(database) -%}
  {% set catalog = database or 'internal' %}
  {# Core list_schemas passes quote_as_configured(database), whereas Relation
     fields contain the raw name. Decode one identifier wrapper, then quote once. #}
  {% if catalog[0:1] == '`' and catalog[-1:] == '`' %}
    {% set catalog = catalog[1:-1] | replace('``', '`') %}
  {% endif %}
  {{ return(catalog) }}
{%- endmacro %}


{% macro doris__information_schema_name(database) -%}
  {{ return(adapter.quote(doris__catalog_name(database)) ~ '.information_schema') }}
{%- endmacro %}


{% macro doris__list_relations_without_caching(schema_relation) -%}
  {% set catalog = schema_relation.database or 'internal' %}
  {% set information_schema_name = (
      adapter.quote(catalog) ~ '.information_schema'
      if schema_relation.database else 'information_schema'
  ) %}
  {% call statement('list_relations_without_caching', fetch_result=True) %}
    select
      {% if schema_relation.database %}
      tables.table_catalog as "database",
      {% else %}
      case when lower(tables.table_catalog) = 'internal' then null
           else tables.table_catalog end as "database",
      {% endif %}
      tables.table_name as name,
      tables.table_schema as "schema",
      case when materialized_views.Name is not null then 'materialized_view'
           when tables.table_type = 'BASE TABLE' then 'table'
           when tables.table_type = 'VIEW' then 'view'
           else tables.table_type end as table_type
    from {{ information_schema_name }}.tables as tables
    {% if not schema_relation.database or schema_relation.database | lower == 'internal' %}
    left join mv_infos(
      "database" = "{{ schema_relation.schema | replace('\\', '\\\\') | replace('"', '\\"') }}"
    ) as materialized_views
      on tables.table_name = materialized_views.Name
    {% else %}
    left join (select null as Name) as materialized_views
      on false
    {% endif %}
    where upper(tables.table_catalog) = upper('{{ catalog | replace("'", "''") }}')
      and tables.table_schema = '{{ schema_relation.schema | replace("\\", "\\\\") | replace("'", "\\'") }}'
  {% endcall %}
  {{ return(load_result('list_relations_without_caching').table) }}
{%- endmacro %}

{% macro doris__get_catalog(information_schema, schemas) -%}
    {% set catalog = information_schema.database or 'internal' %}
    {% set information_schema_name = (
        adapter.quote(catalog) ~ '.information_schema'
        if information_schema.database else 'information_schema'
    ) %}
    {%- call statement('catalog', fetch_result=True) -%}
    with materialized_views as (
        {% if catalog | lower == 'internal' %}
        {%- for schema in schemas %}
        select
            '{{ schema | replace("\\", "\\\\") | replace("'", "\\'") }}' as table_schema,
            Name as table_name
        from mv_infos(
            "database" = "{{ schema | replace('\\', '\\\\') | replace('"', '\\"') }}"
        )
        {%- if not loop.last %} union all {% endif -%}
        {%- endfor %}
        {% else %}
        select null as table_schema, null as table_name where false
        {% endif %}
    ),
    tables as (
        select
            {% if information_schema.database %}
            information_schema_tables.table_catalog as "table_database",
            {% else %}
            case when lower(information_schema_tables.table_catalog) = 'internal' then null
                 else information_schema_tables.table_catalog end as "table_database",
            {% endif %}
            information_schema_tables.table_schema,
            information_schema_tables.table_name,
            case when materialized_views.table_name is not null then 'materialized_view'
                 when information_schema_tables.table_type = 'BASE TABLE' then 'table'
                 when information_schema_tables.table_type = 'VIEW' then 'view'
                 else information_schema_tables.table_type
            end as table_type,
            null as table_owner,
            nullif(information_schema_tables.table_comment, '') as table_comment
        from {{ information_schema_name }}.tables as information_schema_tables
        left join materialized_views
          on information_schema_tables.table_schema = materialized_views.table_schema
         and information_schema_tables.table_name = materialized_views.table_name
    ),
    columns as (
        select
            {% if information_schema.database %}
            information_schema_columns.table_catalog as "table_database",
            {% else %}
            case when lower(information_schema_columns.table_catalog) = 'internal' then null
                 else information_schema_columns.table_catalog end as "table_database",
            {% endif %}
            table_schema as "table_schema",
            table_name as "table_name",
            column_name as "column_name",
            ordinal_position as "column_index",
            data_type as "column_type",
            nullif(column_comment, '') as "column_comment"
        from {{ information_schema_name }}.columns as information_schema_columns
        where upper(information_schema_columns.table_catalog) = upper('{{ catalog | replace("'", "''") }}')
    )
    select
        columns.table_database,
        columns.table_schema,
        columns.table_name,
        tables.table_type,
        tables.table_comment as "table_comment",
        tables.table_owner,
        columns.column_name,
        columns.column_index,
        columns.column_type,
        columns.column_comment
    from tables
    join columns using (table_schema, table_name)
    where tables.table_schema not in ('information_schema')
    and (
      {% if information_schema.database %}
      upper(tables.table_database) = upper('{{ catalog | replace("'", "''") }}')
      {% else %}
      ('{{ catalog | lower | replace("'", "''") }}' = 'internal'
       and tables.table_database is null)
      or upper(tables.table_database) = upper('{{ catalog | replace("'", "''") }}')
      {% endif %}
    )
    and (
    {%- for schema in schemas -%}
      upper(tables.table_schema) = upper('{{ schema }}'){%- if not loop.last %} or {% endif -%}
    {%- endfor -%}
    )
    order by column_index
    {%- endcall -%}

    {{ return(load_result('catalog').table) }}

{%- endmacro %}

{% macro doris__check_schema_exists(database, schema) -%}
{%- endmacro %}

{% macro doris__list_schemas(database) -%}
    {% set catalog = doris__catalog_name(database) %}
    {% set information_schema_name = doris__information_schema_name(database) %}
    {% call statement('list_schemas', fetch_result=True, auto_begin=False) -%}
    select distinct schema_name
    from {{ information_schema_name }}.schemata
    where upper(catalog_name) = upper('{{ catalog | replace("'", "''") }}')
    {%- endcall %}
    {{ return(load_result('list_schemas').table) }}
{%- endmacro %}
