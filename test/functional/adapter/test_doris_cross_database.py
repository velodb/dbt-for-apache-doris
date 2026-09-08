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
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Tests for Doris two-part and three-part relation handling."""

import pytest
from dbt.tests.util import run_dbt
from dbt.adapters.doris.relation import DorisRelation


# ---------------------------------------------------------------------------
# Unit tests — no database connection needed
# ---------------------------------------------------------------------------

class TestDorisRelationNamespace:
    """Verify internal and external Doris relation rendering."""

    def test_database_equals_schema(self):
        """Equal catalog and database names still render as three parts."""
        rel = DorisRelation.create(
            database="my_db",
            schema="my_db",
            identifier="my_table",
        )
        assert rel.schema == "my_db"
        assert rel.identifier == "my_table"
        assert rel.render() == "`my_db`.`my_db`.`my_table`"

    def test_catalog_differs_from_database(self):
        """A catalog value is preserved separately from the Doris database."""
        rel = DorisRelation.create(
            database="other_catalog",
            schema="default_database",
            identifier="my_table",
        )
        assert rel.database == "other_catalog"
        assert rel.schema == "default_database"
        assert rel.identifier == "my_table"
        assert rel.render() == "`other_catalog`.`default_database`.`my_table`"

    def test_database_none(self):
        """database=None should leave schema unchanged."""
        rel = DorisRelation.create(
            database=None,
            schema="my_schema",
            identifier="my_table",
        )
        assert rel.schema == "my_schema"

    def test_database_empty_string(self):
        """database='' (falsy) should leave schema unchanged."""
        rel = DorisRelation.create(
            database="",
            schema="my_schema",
            identifier="my_table",
        )
        assert rel.schema == "my_schema"

    def test_render_includes_catalog(self):
        """External catalog relations render all three qualified parts."""
        rel = DorisRelation.create(
            database="cross_db",
            schema="original_schema",
            identifier="orders",
        )
        rendered = rel.render()
        assert "cross_db" in rendered
        assert "orders" in rendered
        assert rendered.count(".") == 2

    def test_render_without_database(self):
        """Normal case: database=None renders schema.identifier."""
        rel = DorisRelation.create(
            database=None,
            schema="analytics",
            identifier="users",
        )
        rendered = rel.render()
        assert "analytics" in rendered
        assert "users" in rendered


# ---------------------------------------------------------------------------
# Integration tests — require a running Doris instance
# ---------------------------------------------------------------------------

# A source table created in a DIFFERENT database to verify cross-db access.
# The test will: 1) create database cross_db_test, 2) create a table in it,
# 3) define a dbt source pointing to that database, 4) build a model that
# reads from it.

CROSS_DB_SOURCE_YML = """
version: 2

sources:
  - name: cross_db_src
    database: internal
    schema: cross_db_test
    tables:
      - name: remote_table
"""

CROSS_DB_MODEL_SQL = """
{{ config(
    materialized='table',
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select id, val from {{ source('cross_db_src', 'remote_table') }}
"""

# A source defined with a Doris database and no external catalog.
DB_ONLY_SOURCE_YML = """
version: 2

sources:
  - name: db_only_src
    schema: cross_db_test
    tables:
      - name: remote_table
"""

DB_ONLY_MODEL_SQL = """
{{ config(
    materialized='view'
) }}

select id, val from {{ source('db_only_src', 'remote_table') }}
"""


class TestDorisCrossDatabaseSource:
    """End-to-end: model reads from a source in a different Doris database."""

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "cross_db_model.sql": CROSS_DB_MODEL_SQL,
            "sources.yml": CROSS_DB_SOURCE_YML,
        }

    @pytest.fixture(scope="class", autouse=True)
    def setup_cross_db(self, project):
        """Create the remote database and table before tests, clean up after."""
        project.run_sql("CREATE DATABASE IF NOT EXISTS cross_db_test")
        project.run_sql(
            "CREATE TABLE IF NOT EXISTS cross_db_test.remote_table "
            "(id INT, val VARCHAR(50)) "
            "DISTRIBUTED BY HASH(id) BUCKETS 1 "
            "PROPERTIES('replication_num' = '1')"
        )
        project.run_sql(
            "INSERT INTO cross_db_test.remote_table VALUES (1, 'hello'), (2, 'world')"
        )
        yield
        project.run_sql("DROP TABLE IF EXISTS cross_db_test.remote_table")
        project.run_sql("DROP DATABASE IF EXISTS cross_db_test")

    def test_cross_database_source(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        result = project.run_sql(
            "select count(*) from cross_db_model", fetch="one"
        )
        assert result[0] == 2

        result = project.run_sql(
            "select val from cross_db_model where id = 1", fetch="one"
        )
        assert result[0] == "hello"


class TestDorisDatabaseOnlySource:
    """Source defined with a Doris database and no catalog."""

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "db_only_model.sql": DB_ONLY_MODEL_SQL,
            "sources.yml": DB_ONLY_SOURCE_YML,
        }

    @pytest.fixture(scope="class", autouse=True)
    def setup_cross_db(self, project):
        project.run_sql("CREATE DATABASE IF NOT EXISTS cross_db_test")
        project.run_sql(
            "CREATE TABLE IF NOT EXISTS cross_db_test.remote_table "
            "(id INT, val VARCHAR(50)) "
            "DISTRIBUTED BY HASH(id) BUCKETS 1 "
            "PROPERTIES('replication_num' = '1')"
        )
        project.run_sql(
            "INSERT INTO cross_db_test.remote_table VALUES (1, 'aaa'), (2, 'bbb')"
        )
        yield
        project.run_sql("DROP TABLE IF EXISTS cross_db_test.remote_table")
        project.run_sql("DROP DATABASE IF EXISTS cross_db_test")

    def test_database_only_source(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        result = project.run_sql(
            "select count(*) from db_only_model", fetch="one"
        )
        assert result[0] == 2
