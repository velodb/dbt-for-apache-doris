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

{% macro doris__create_view_as(relation, sql) -%}
  {%- set sql_header = config.get('sql_header', none) -%}
  {%- set contract_config = config.get('contract') -%}

  {#-- dbt's default macro validates a view contract before executing CREATE.
       Keep that preflight check when overriding the macro for Doris so a bad
       contract cannot replace an existing, valid view. --#}
  {% if contract_config and contract_config.enforced %}
    {{ get_assert_columns_equivalent(sql) }}
  {% endif %}

  {{ sql_header if sql_header is not none }}
  create or replace view {{ relation }} as {{ sql }};
{%- endmacro %}
