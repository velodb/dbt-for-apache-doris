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

"""dbt-tests-adapter Grants compatibility coverage for Doris."""

import os
import uuid

import pytest
from dbt.tests.adapter.grants.base_grants import BaseGrants, TEST_USER_ENV_VARS
from dbt.tests.adapter.grants.test_incremental_grants import (
    BaseIncrementalGrants,
)
from dbt.tests.adapter.grants.test_invalid_grants import BaseInvalidGrants
from dbt.tests.adapter.grants.test_model_grants import BaseModelGrants
from dbt.tests.adapter.grants.test_seed_grants import BaseSeedGrants
from dbt.tests.adapter.grants.test_snapshot_grants import BaseSnapshotGrants
from dbt.tests.util import (
    relation_from_name,
    run_dbt,
    run_dbt_and_capture,
    set_model_file,
    write_file,
)


MISSING_USER = f"dbt_gnt_missing_{uuid.uuid4().hex[:8]}"

MV_BASE_SQL = """
{{ config(
    materialized='table',
    duplicate_key=['id'],
    distributed_by=['id'],
    buckets=1,
    properties={'replication_num': '1'}
) }}
select 1 as id
"""

MV_SQL = """
{{ config(
    materialized='materialized_view',
    build_mode='deferred',
    refresh_method='complete',
    refresh_trigger='manual',
    distributed_by=['id'],
    distribution_type='hash',
    buckets=1,
    properties={'replication_num': '1'}
) }}
select id from {{ ref('grant_base') }}
"""

MV_CHANGED_SQL = MV_SQL.replace(
    "select id from",
    "select id + 0 as id from",
)

MV_USER_1_YML = """
version: 2
models:
  - name: grant_mv
    config:
      grants:
        select: ["{{ env_var('DBT_TEST_USER_1') }}"]
"""

MV_USER_2_YML = MV_USER_1_YML.replace("DBT_TEST_USER_1", "DBT_TEST_USER_2")


class DorisGrantUsers:
    @pytest.fixture(scope="class", autouse=True)
    def get_test_users(self, project):
        suffix = uuid.uuid4().hex[:8]
        users = [f"dbt_gnt_{suffix}_{index}" for index in range(1, 4)]
        previous_values = {
            env_var: os.environ.get(env_var) for env_var in TEST_USER_ENV_VARS
        }

        for env_var, user in zip(TEST_USER_ENV_VARS, users):
            os.environ[env_var] = user
            project.run_sql(f"create user '{user}'@'%'")

        try:
            yield users
        finally:
            for user in users:
                project.run_sql(f"drop user if exists '{user}'@'%'")
            for env_var, previous_value in previous_values.items():
                if previous_value is None:
                    os.environ.pop(env_var, None)
                else:
                    os.environ[env_var] = previous_value


class TestDorisModelGrants(DorisGrantUsers, BaseModelGrants):
    pass


class TestDorisIncrementalGrants(DorisGrantUsers, BaseIncrementalGrants):
    def interpolate_name_overrides(self, yaml_text):
        rendered = super().interpolate_name_overrides(yaml_text)
        return rendered.replace(
            "materialized: incremental",
            "materialized: incremental\n      incremental_strategy: append",
        )


class TestDorisSeedGrants(DorisGrantUsers, BaseSeedGrants):
    pass


class TestDorisSnapshotGrants(DorisGrantUsers, BaseSnapshotGrants):
    pass


class TestDorisMaterializedViewGrants(DorisGrantUsers, BaseGrants):
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "grant_base.sql": MV_BASE_SQL,
            "grant_mv.sql": MV_SQL,
            "schema.yml": MV_USER_1_YML,
        }

    def test_grants_update_without_rebuilding_the_materialized_view(
        self,
        project,
        get_test_users,
    ):
        assert len(run_dbt(["run"])) == 2
        expected = {"select": [get_test_users[0]]}
        self.assert_expected_grants_match_actual(project, "grant_mv", expected)

        write_file(MV_USER_2_YML, project.project_root, "models", "schema.yml")
        results, _ = run_dbt_and_capture(["--debug", "run", "--select", "grant_mv"])
        assert len(results) == 1
        expected = {"select": [get_test_users[1]]}
        self.assert_expected_grants_match_actual(project, "grant_mv", expected)

        relation = relation_from_name(project.adapter, "grant_mv")
        set_model_file(project, relation, MV_CHANGED_SQL)
        assert len(run_dbt(["run", "--select", "grant_mv"])) == 1
        self.assert_expected_grants_match_actual(project, "grant_mv", expected)

        results, log_output = run_dbt_and_capture(
            ["--debug", "run", "--select", "grant_mv"]
        )
        assert len(results) == 1
        assert "grant " not in log_output
        assert "revoke " not in log_output
        self.assert_expected_grants_match_actual(project, "grant_mv", expected)


class TestDorisInvalidGrants(DorisGrantUsers, BaseInvalidGrants):
    def privilege_grantee_name_overrides(self):
        overrides = super().privilege_grantee_name_overrides()
        overrides["invalid_user"] = MISSING_USER
        return overrides

    def grantee_does_not_exist_error(self):
        return "does not exist"

    def privilege_does_not_exist_error(self):
        return "Unsupported Doris grant privilege"
