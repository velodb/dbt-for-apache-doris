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

{% macro doris__copy_grants() -%}
    {{ return(true) }}
{%- endmacro %}

{% macro doris__support_multiple_grantees_per_dcl_statement() -%}
    {{ return(false) }}
{%- endmacro %}

{% macro doris__grant_privilege(privilege) -%}
    {%- set normalized = privilege | lower -%}
    {%- set privilege_map = {
        'select': 'SELECT_PRIV',
        'insert': 'LOAD_PRIV',
        'alter': 'ALTER_PRIV',
        'create': 'CREATE_PRIV',
        'drop': 'DROP_PRIV',
        'show_view': 'SHOW_VIEW_PRIV'
    } -%}
    {%- if normalized not in privilege_map -%}
        {% do exceptions.raise_compiler_error(
            "Unsupported Doris grant privilege '" ~ privilege ~ "'. "
            ~ "Expected one of: select, insert, alter, create, drop, show_view."
        ) %}
    {%- endif -%}
    {{ return(privilege_map[normalized]) }}
{%- endmacro %}

{% macro doris__grant_user_identity(grantee) -%}
    {%- set identity = grantee | string | trim -%}
    {%- if identity[0:5] | lower == 'role:' -%}
        {% do exceptions.raise_compiler_error(
            "Doris role grants cannot be reconciled through "
            ~ "information_schema.table_privileges. Configure Doris usernames "
            ~ "in dbt grants instead of role:" ~ identity[5:] ~ "."
        ) %}
    {%- endif -%}
    {%- set identity_parts = identity.rsplit('@', 1) -%}
    {%- set user_name = identity_parts[0] -%}
    {%- set host_name = identity_parts[1] if identity_parts | length == 2 else '%' -%}
    {%- if not user_name or not host_name -%}
        {% do exceptions.raise_compiler_error(
            "Invalid Doris grant grantee '" ~ identity ~ "'. Expected "
            ~ "a username or username@host."
        ) %}
    {%- endif -%}
    {%- set quoted_user = user_name | replace("\\", "\\\\") | replace("'", "\\'") -%}
    {%- set quoted_host = host_name | replace("\\", "\\\\") | replace("'", "\\'") -%}
    {{ return("'" ~ quoted_user ~ "'@'" ~ quoted_host ~ "'") }}
{%- endmacro %}

{% macro doris__get_show_grant_sql(relation) -%}
    {%- set schema_name = relation.schema | replace("\\", "\\\\") | replace("'", "\\'") -%}
    {%- set relation_name = relation.identifier | replace("\\", "\\\\") | replace("'", "\\'") -%}
    select
        case
            when trim(both "'" from substring_index(grantee, '@', -1)) = '%'
                then trim(both "'" from substring_index(grantee, '@', 1))
            else concat(
                trim(both "'" from substring_index(grantee, '@', 1)),
                '@',
                trim(both "'" from substring_index(grantee, '@', -1))
            )
        end as grantee,
        lower(replace(privilege_type, ' ', '_')) as privilege_type
    from information_schema.table_privileges
    where table_schema = '{{ schema_name }}'
      and table_name = '{{ relation_name }}'
{%- endmacro %}

{% macro doris__get_grant_sql(relation, privilege, grantees) -%}
    {%- if grantees | length != 1 -%}
        {% do exceptions.raise_compiler_error(
            "Doris requires one grantee per GRANT statement."
        ) %}
    {%- endif -%}
    grant {{ doris__grant_privilege(privilege) }}
    on {{ relation.include(database=false) }}
    to {{ doris__grant_user_identity(grantees[0]) }}
{%- endmacro %}

{% macro doris__get_revoke_sql(relation, privilege, grantees) -%}
    {%- if grantees | length != 1 -%}
        {% do exceptions.raise_compiler_error(
            "Doris requires one grantee per REVOKE statement."
        ) %}
    {%- endif -%}
    revoke {{ doris__grant_privilege(privilege) }}
    on {{ relation.include(database=false) }}
    from {{ doris__grant_user_identity(grantees[0]) }}
{%- endmacro %}

{% macro doris__call_dcl_statements(dcl_statement_list) -%}
    {#-- The Doris connector and FE should receive one DCL statement at a time. --#}
    {%- for dcl_statement in dcl_statement_list -%}
        {% call statement('grant_' ~ loop.index, auto_begin=false) %}
            {{ dcl_statement }}
        {% endcall %}
    {%- endfor -%}
{%- endmacro %}
