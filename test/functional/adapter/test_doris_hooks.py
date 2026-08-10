#!/usr/bin/env python
# encoding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Functional tests for pre- and post-hooks on Doris models."""

import pytest
from dbt.tests.util import relation_from_name, run_dbt


MODEL_WITH_HOOKS_SQL = """
{{ config(
    materialized='table',
    distributed_by=['id'],
    properties={'replication_num': '1'},
    pre_hook=[
        'create table if not exists `{{ this.schema }}`.`hook_audit` '
        '(id int) duplicate key(id) distributed by hash(id) buckets 1 '
        'properties ("replication_num" = "1")'
    ],
    post_hook=[
        'insert into `{{ this.schema }}`.`hook_audit` values (1)'
    ]
) }}

select 1 as id, 'hook_model' as value
"""


class TestDorisModelHooks:
    @pytest.fixture(scope="class")
    def models(self):
        return {"model_with_hooks.sql": MODEL_WITH_HOOKS_SQL}

    def test_pre_and_post_hooks_run(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "model_with_hooks")
        audit_row = project.run_sql(
            f"select count(*) from `{relation.schema}`.`hook_audit`",
            fetch="one",
        )
        assert audit_row == (1,)
