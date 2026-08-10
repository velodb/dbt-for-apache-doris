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

"""dbt-tests-adapter Store Failures compatibility coverage for Doris."""

import pytest
from dbt.tests.adapter.store_test_failures_tests.basic import (
    StoreTestFailuresAsExceptions,
    StoreTestFailuresAsGeneric,
    StoreTestFailuresAsInteractions,
    StoreTestFailuresAsProjectLevelEphemeral,
    StoreTestFailuresAsProjectLevelOff,
    StoreTestFailuresAsProjectLevelView,
)
from dbt.tests.adapter.store_test_failures_tests.test_store_test_failures import (
    BaseStoreTestFailures,
    BaseStoreTestFailuresLimit,
)


class TestDorisStoreTestFailures(BaseStoreTestFailures):
    def column_type_overrides(self):
        return {
            "expected_accepted_values": {
                "+column_types": {
                    "value_field": "varchar(255)",
                    "n_records": "bigint",
                },
            },
            "expected_not_null_problematic_model_id": {
                "+column_types": {"id": "int"},
            },
            "expected_unique_problematic_model_id": {
                "+column_types": {"n_records": "bigint"},
            },
        }


class TestDorisStoreTestFailuresLimit(BaseStoreTestFailuresLimit):
    pass


class DorisStoreFailuresAsSeedTypes:
    data_tests_config = None

    @pytest.fixture(scope="class")
    def project_config_update(self):
        config = {
            "seeds": {
                "+column_types": {
                    "name": "varchar(255)",
                    "shirt": "varchar(255)",
                }
            }
        }
        if self.data_tests_config is not None:
            config["data_tests"] = self.data_tests_config
        return config


class TestDorisStoreFailuresAsInteractions(
    DorisStoreFailuresAsSeedTypes,
    StoreTestFailuresAsInteractions,
):
    pass


class TestDorisStoreFailuresAsProjectLevelOff(
    DorisStoreFailuresAsSeedTypes,
    StoreTestFailuresAsProjectLevelOff,
):
    data_tests_config = {"store_failures": False}


class TestDorisStoreFailuresAsProjectLevelView(
    DorisStoreFailuresAsSeedTypes,
    StoreTestFailuresAsProjectLevelView,
):
    data_tests_config = {"store_failures_as": "view"}


class TestDorisStoreFailuresAsProjectLevelEphemeral(
    DorisStoreFailuresAsSeedTypes,
    StoreTestFailuresAsProjectLevelEphemeral,
):
    data_tests_config = {
        "store_failures_as": "ephemeral",
        "store_failures": True,
    }


class TestDorisStoreFailuresAsGeneric(
    DorisStoreFailuresAsSeedTypes,
    StoreTestFailuresAsGeneric,
):
    pass


class TestDorisStoreFailuresAsExceptions(
    DorisStoreFailuresAsSeedTypes,
    StoreTestFailuresAsExceptions,
):
    pass
