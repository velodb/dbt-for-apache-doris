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
    Replace each target partition with its staged temporary partition.

    Each ALTER runs as its own statement. Emitting them as one semicolon-separated
    blob put several statements into a single `cursor.execute()`; the connector
    left the extra result sets unconsumed and every later statement on that
    connection failed with `2014 Commands out of sync`. The replaces themselves
    had already succeeded, so the visible failure landed on the temp-table cleanup
    and left a `__dbt_tmp` table behind.

    Returns the number of partitions replaced, for the caller to report.
--#}
{% macro doris__replace_partitions(relation, partitions) %}
    {% for partition in partitions %}
        {% set items = get_partition_items(partition) %}
        {% set p = ''.join(items) %}
        {% call statement('replace_partition_' ~ p) %}
            alter table {{ relation }} replace partition (p{{ p }}) with temporary partition (tp{{ p }}) properties (
                "strict_range" = "false"
            )
        {% endcall %}
    {% endfor %}
    {{ return(partitions | length) }}
{% endmacro %}
