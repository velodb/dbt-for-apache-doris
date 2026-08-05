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
    NOTE: this macro must emit exactly one statement.

    It used to prepend `drop table if exists ...` when temporary=True, which put
    two semicolon-separated statements into a single dbt statement. The connector
    executes those in one `execute()` call and leaves unconsumed result sets
    behind, so the next statement on that connection fails with
    `2014 Commands out of sync`. Callers now drop the relation themselves.
--#}
{% macro doris__create_table_as(temporary, relation, sql) -%}
    {% set sql_header = config.get('sql_header', none) %}
    {% set table = relation.include(database=False) %}
    {{ sql_header if sql_header is not none }}
    create table {{ table }}
    {{ doris__duplicate_key() }}
    {{ doris__table_comment()}}
    {{ doris__partition_by() }}
    {{ doris__distributed_by() }}
    {{ doris__properties() }} as {{ doris__table_colume_type(sql) }};

{%- endmacro %}

{% macro doris__create_unique_table_as(temporary, relation, sql) -%}
    {% set sql_header = config.get('sql_header', none) %}
    {% set table = relation.include(database=False) %}
    {{ sql_header if sql_header is not none }}
    create table {{ table }}
    {{ doris__unique_key() }}
    {{ doris__table_comment()}}
    {{ doris__partition_by() }}
    {{ doris__distributed_by() }}
    {{ doris__properties() }} as {{ doris__table_colume_type(sql) }};

{%- endmacro %}


{% macro doris__documented_column_description(column_name) -%}
    {%- set documented = namespace(value=none) -%}
    {%- for documented_name, column_info in model.get('columns', {}).items() -%}
        {%- set quoted = column_info.get('quote', false) -%}
        {%- if (quoted and documented_name == column_name)
            or (not quoted and documented_name | lower == column_name | lower) -%}
            {%- set documented.value = column_info.get('description') or none -%}
        {%- endif -%}
    {%- endfor -%}
    {{ return(documented.value) }}
{%- endmacro %}


{% macro doris__create_documented_table_as(temporary, relation, sql, unique=false) -%}
    {#-- Doris CTAS cannot declare column comments. Build the query once in a
         private source table, read Doris' exact inferred types, then create the
         final staging table with inline comments and copy the rows. Inline
         comments preserve arbitrary quotes; ALTER COMMENT does not on all
         supported Doris versions. --#}
    {%- set source_relation = relation.incorporate(
        path={'identifier': relation.identifier ~ '__dbt_docs_source'},
        type='table'
    ) -%}
    {% do doris__drop_relation(source_relation) %}

    {% call statement('create_documented_table_source') %}
        {{ doris__create_table_as(temporary, source_relation, sql) }}
    {% endcall %}

    {%- set source_columns = adapter.get_columns_in_relation(source_relation) -%}
    {% call statement('create_documented_table') %}
        create table {{ relation.include(database=False) }} (
        {%- for column in source_columns %}
            `{{ column.name | replace("`", "``") }}` {{ column.data_type }}
            {%- set description = doris__documented_column_description(column.name) -%}
            {%- if description %}
                COMMENT '{{ description | replace("\\", "\\\\") | replace("'", "\\'") }}'
            {%- endif -%}
            {{- "," if not loop.last }}
        {%- endfor %}
        )
        {% if unique %}
            {{ doris__unique_key() }}
        {% else %}
            {{ doris__duplicate_key() }}
        {% endif %}
        {{ doris__table_comment() }}
        {{ doris__partition_by() }}
        {{ doris__distributed_by() }}
        {{ doris__properties() }}
    {% endcall %}

    {% call statement('main') %}
        insert into {{ relation }}
        select * from {{ source_relation }}
    {% endcall %}

    {% do doris__drop_relation(source_relation) %}
{%- endmacro %}


{#--
    Wrap the model SQL so that declared column types are applied via CAST.

    This projection lists columns explicitly, so it may only be used when dbt
    guarantees that the declared column set matches the SQL column set exactly
    -- that is, when the model contract is enforced. `assert_columns_equivalent`
    raises a contract error on any mismatch.

    Without an enforced contract, `columns:` in schema.yml is documentation and
    must not change the model result. Projecting a partial column list there
    silently dropped every undeclared column from the target table. Column
    comments are applied separately by `persist_docs`.
--#}
{% macro doris__table_colume_type(sql) -%}
    {% set contract_config = config.get('contract') %}
    {% if contract_config and contract_config.enforced %}
        {{ get_assert_columns_equivalent(sql) }}
        select {{get_table_columns_and_constraints()}} from (
            {{sql}}
        ) `_table_colume_type_name`
    {% else %}
        {{sql}}
    {%- endif -%}
{%- endmacro %}
