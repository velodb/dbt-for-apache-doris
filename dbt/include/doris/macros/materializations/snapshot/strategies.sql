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

{% macro doris__snapshot_hash_arguments(args) -%}
    {# Doris current_timestamp() has second precision. Check-strategy runs in
       the same second therefore need a nonce to keep version ids distinct.
       Timestamp strategy expressions come from the source and stay stable. #}
    {% set hash_state = namespace(needs_nonce=false) %}
    {% for arg in args %}
        {% if 'current_timestamp()' in (arg | lower | replace(' ', '')) %}
            {% set hash_state.needs_nonce = true %}
        {% endif %}
    {% endfor %}
    md5(concat_ws('|', {%- for arg in args -%}
        coalesce(cast({{ arg }} as char), '')
        {% if not loop.last %}, {% endif %}
    {%- endfor -%}
        {% if hash_state.needs_nonce %}, uuid(){% endif %}
    ))
{%- endmacro %}
