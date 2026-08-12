{#
  Licensed to the Apache Software Foundation (ASF) under one or more
  contributor license agreements. See the NOTICE file distributed with
  this work for additional information regarding copyright ownership.
  The ASF licenses this file to you under the Apache License, Version 2.0.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
#}

{#
  Based on dbt Core 1.12's global format_row macro. Core widens character
  varying(n) fixture values (dbt-core#11974), but Doris reports the equivalent
  type as varchar(n). Shadow the global macro so inline string fixtures do not
  silently truncate to the source relation's declared width.
#}
{% macro format_row(row, column_name_to_data_types) -%}
    {% set formatted_row = {} %}
    {%- for column_name, column_value in row.items() -%}
        {% set column_name = column_name|lower %}

        {%- if column_name not in column_name_to_data_types %}
            {% set fixture_name = "expected output" if model.resource_type == 'unit_test' else ("'" ~ model.name ~ "'") %}
            {{ exceptions.raise_compiler_error(
                "Invalid column name: '" ~ column_name ~ "' in unit test fixture for " ~ fixture_name ~ "."
                "\nAccepted columns for " ~ fixture_name ~ " are: " ~ (column_name_to_data_types.keys()|list)
            ) }}
        {%- endif -%}

        {%- set column_type = column_name_to_data_types[column_name] %}
        {%- set normalized_column_type = column_type|lower|trim %}
        {%- if column_value is string and (
            'varying' in normalized_column_type
            or normalized_column_type.startswith('varchar(')
        ) -%}
            {%- set column_type = column_type.split('(')[0] -%}
        {%- endif -%}

        {%- set column_value_clean = column_value -%}
        {%- if column_value is string -%}
            {%- set column_value_clean = dbt.string_literal(dbt.escape_single_quotes(column_value)) -%}
        {%- elif column_value is none -%}
            {%- set column_value_clean = 'null' -%}
        {%- endif -%}

        {%- set row_update = {column_name: safe_cast(column_value_clean, column_type)} -%}
        {%- do formatted_row.update(row_update) -%}
    {%- endfor -%}
    {{ return(formatted_row) }}
{%- endmacro %}
