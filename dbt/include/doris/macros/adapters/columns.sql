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

{% macro doris__get_columns_in_relation(relation) -%}
    {% call statement('get_columns_in_relation', fetch_result=True) %}
        select column_name  as `column`,
               column_type  as `dtype`,
               character_maximum_length as char_size,
               numeric_precision,
               numeric_scale
        from information_schema.columns
        where table_schema = '{{ relation.schema }}'
          and table_name = '{{ relation.identifier }}'
        order by ordinal_position
    {% endcall %}
    {% set table = load_result('get_columns_in_relation').table %}
    {{ return(sql_convert_columns_in_relation(table)) }}
{%- endmacro %}

{% macro sql_convert_columns_in_relation(table) -%}
    {% set columns = [] %}
    {% for row in table %}
        {% set col_name = row['column'] %}
        {% set col_type = row['dtype'] %}
        {% do columns.append(api.Column.create(col_name, col_type)) %}
    {% endfor %}
    {{ return(columns) }}
{%- endmacro %}

{% macro doris__alter_column_type(relation, column_name, new_column_type) -%}
    {% call statement('alter_column_type') %}
        alter table {{ relation }} modify column {{ column_name }} {{ new_column_type }}
    {% endcall %}
{% endmacro %}

{% macro columns_and_constraints(table_type="table") %}
  {# loop through user_provided_columns to create DDL with data types and constraints #}
    {%- set raw_column_constraints = adapter.render_raw_columns_constraints(raw_columns=model['columns']) -%}
    {% for c in raw_column_constraints -%}
      {% if table_type == "table" %}
        {{ c.get_table_column_constraint() }}{{ "," if not loop.last or raw_model_constraints }}
      {% else %}
        {{ c.get_view_column_constraint() }}{{ "," if not loop.last or raw_model_constraints }}
      {% endif %}
    {% endfor %}
{% endmacro %}

{% macro doris__get_table_columns_and_constraints() -%}
  {{ return(columns_and_constraints("table")) }}
{%- endmacro %}


{% macro doris__get_view_columns_comment() -%}
  {{ return(columns_and_constraints("view")) }}
{%- endmacro %}

{% macro doris__alter_comment_literal(comment) -%}
    {#-- Doris ALTER COMMENT strips the outer quotes but does not consistently
         decode escaped delimiters across supported releases. Pick a delimiter
         absent from the value so the stored text remains byte-for-byte equal. --#}
    {% if '"' not in comment %}
        {{ return('"' ~ comment ~ '"') }}
    {% elif "'" not in comment %}
        {{ return("'" ~ comment ~ "'") }}
    {% else %}
        {% do exceptions.raise_compiler_error(
            "Doris cannot losslessly ALTER a comment containing both single and "
            ~ "double quotes. Rebuild the relation with --full-refresh so dbt "
            ~ "can persist the comment in CREATE TABLE/VIEW."
        ) %}
    {% endif %}
{%- endmacro %}

{% macro doris__alter_relation_comment(relation, relation_comment) -%}
    {#-- Views do not support MODIFY COMMENT, only tables do --#}
    {% if relation.type != 'view' %}
        {% call statement('alter_relation_comment') %}
            alter table {{ relation }} modify comment {{ doris__alter_comment_literal(relation_comment) }}
        {% endcall %}
    {% endif %}
{% endmacro %}

{% macro doris__alter_column_comment(relation, column_dict) -%}
    {#-- Views do not support MODIFY COLUMN COMMENT; column comments for views
         are set at CREATE VIEW time via column definitions --#}
    {% if relation.type != 'view' %}
        {#-- dbt hands us {column_name: column_info_dict}, not {name: description}.
             Interpolating the value directly wrote the whole dict repr into the
             comment, and its embedded quotes broke the statement outright.

             The column type is deliberately omitted: Doris accepts
             `MODIFY COLUMN <col> COMMENT '<c>'`, and naming a type there is
             rejected for distribution and key columns. --#}
        {% for column_name, column_info in column_dict.items() %}
            {% set comment = (column_info.get('description') or '') if column_info is mapping else (column_info or '') %}
            {% if comment %}
                {% call statement('alter_column_comment') %}
                    alter table {{ relation }} modify column `{{ column_name | replace("`", "``") }}` comment {{ doris__alter_comment_literal(comment) }}
                {% endcall %}
            {% endif %}
        {% endfor %}
    {% endif %}
{% endmacro %}

{% macro doris__persist_docs(relation, model, for_relation, for_columns) -%}
    {#-- Inline CREATE comments are already correct. Only issue ALTER when the
         desired metadata differs, which avoids rewriting lossless inline text
         through Doris releases whose ALTER COMMENT parser preserves escapes. --#}
    {% if for_relation and config.persist_relation_docs() and model.description %}
        {% set relation_rows = run_query(
            "select table_comment from information_schema.tables where table_schema = '"
            ~ (relation.schema | replace("\\", "\\\\") | replace("'", "\\'"))
            ~ "' and table_name = '"
            ~ (relation.identifier | replace("\\", "\\\\") | replace("'", "\\'"))
            ~ "'"
        ) %}
        {% set current_relation_comment = relation_rows[0][0] if relation_rows and relation_rows | length else '' %}
        {% if (current_relation_comment or '') != model.description %}
            {% do doris__alter_relation_comment(relation, model.description) %}
        {% endif %}
    {% endif %}

    {% if for_columns and config.persist_column_docs() and model.columns %}
        {% set column_rows = run_query(
            "select column_name, column_comment from information_schema.columns where table_schema = '"
            ~ (relation.schema | replace("\\", "\\\\") | replace("'", "\\'"))
            ~ "' and table_name = '"
            ~ (relation.identifier | replace("\\", "\\\\") | replace("'", "\\'"))
            ~ "' order by ordinal_position"
        ) %}
        {% set existing_names = [] %}
        {% set existing_comments = {} %}
        {% for row in column_rows %}
            {% do existing_names.append(row[0]) %}
            {% do existing_comments.update({row[0]: row[1] or ''}) %}
        {% endfor %}
        {% set filtered_columns = validate_doc_columns(relation, model.columns, existing_names) %}

        {% for documented_name, column_info in filtered_columns.items() %}
            {% set physical = namespace(name=none) %}
            {% for existing_name in existing_names %}
                {% if (column_info.get('quote') and documented_name == existing_name)
                    or (not column_info.get('quote') and documented_name | lower == existing_name | lower) %}
                    {% set physical.name = existing_name %}
                {% endif %}
            {% endfor %}
            {% set desired_comment = column_info.get('description') or '' %}
            {% if physical.name and desired_comment
                and existing_comments.get(physical.name, '') != desired_comment %}
                {% do doris__alter_column_comment(
                    relation,
                    {physical.name: column_info}
                ) %}
            {% endif %}
        {% endfor %}
    {% endif %}
{%- endmacro %}
