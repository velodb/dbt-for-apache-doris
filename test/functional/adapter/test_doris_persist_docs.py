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

"""dbt-tests-adapter compatibility coverage for Doris Persist Docs."""

import pytest
from dbt.tests.adapter.persist_docs.test_persist_docs import (
    BasePersistDocs,
    BasePersistDocsAllColumnsMissing,
    BasePersistDocsColumnMissing,
    BasePersistDocsCommentOnQuotedColumn,
    BasePersistDocsQuotedColumnCaseSensitive,
    BasePersistDocsQuotedDescriptionNotAppliedOnMismatch,
)
from dbt.tests.util import relation_from_name, run_dbt, write_file


PERSIST_DOCS_DISABLED_SQL = """
{{ config(materialized='table') }}
select 1 as id
"""

PERSIST_DOCS_DISABLED_YML = """
version: 2
models:
  - name: persist_docs_disabled
    description: Must not be persisted
    columns:
      - name: id
        description: Must not be persisted either
"""

INCREMENTAL_DOCS_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    persist_docs={'relation': true, 'columns': true}
) }}

select cast(1 as int) as id, cast('first' as varchar(20)) as name
"""

INCREMENTAL_DOCS_INITIAL_YML = """
version: 2
models:
  - name: incremental_docs
    description: |
      Initial "orders" owner's history
    columns:
      - name: id
        description: |
          Identifier "id" owner's value
      - name: name
        description: Initial name
"""

INCREMENTAL_DOCS_UPDATED_YML = """
version: 2
models:
  - name: incremental_docs
    description: Updated incremental relation docs
    columns:
      - name: id
        description: Updated identifier docs
      - name: name
        description: Updated name docs
"""

INCREMENTAL_MERGE_DOCS_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    persist_docs={'relation': true, 'columns': true},
    properties={'replication_num': '1'}
) }}

select cast(1 as int) as id, cast('first' as varchar(20)) as name
"""

INCREMENTAL_MERGE_DOCS_YML = """
version: 2
models:
  - name: incremental_merge_docs
    description: Merge relation docs
    columns:
      - name: id
        description: |
          Merge "id" owner's value
      - name: name
        description: Merge name docs
"""


def relation_comment(project, relation):
    row = project.run_sql(
        "select table_comment from information_schema.tables "
        f"where table_schema = '{relation.schema}' "
        f"and table_name = '{relation.identifier}'",
        fetch="one",
    )
    return row[0]


def column_comments(project, relation):
    rows = project.run_sql(f"show full columns from {relation}", fetch="all")
    return {row[0]: row[8] for row in rows}


class TestDorisPersistDocs(BasePersistDocs):
    pass


class TestDorisPersistDocsColumnMissing(BasePersistDocsColumnMissing):
    pass


class TestDorisPersistDocsAllColumnsMissing(BasePersistDocsAllColumnsMissing):
    pass


class TestDorisPersistDocsQuotedColumnCaseSensitive(
    BasePersistDocsQuotedColumnCaseSensitive
):
    pass


class TestDorisPersistDocsQuotedDescriptionNotAppliedOnMismatch(
    BasePersistDocsQuotedDescriptionNotAppliedOnMismatch
):
    pass


class TestDorisPersistDocsCommentOnQuotedColumn(
    BasePersistDocsCommentOnQuotedColumn
):
    pass


class TestDorisPersistDocsDisabled:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "persist_docs_disabled.sql": PERSIST_DOCS_DISABLED_SQL,
            "schema.yml": PERSIST_DOCS_DISABLED_YML,
        }

    def test_descriptions_are_not_persisted_without_config(self, project):
        assert len(run_dbt(["run"])) == 1
        relation = relation_from_name(project.adapter, "persist_docs_disabled")

        assert relation_comment(project, relation) == ""
        assert column_comments(project, relation) == {"id": ""}


class TestDorisIncrementalPersistDocs:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_docs.sql": INCREMENTAL_DOCS_SQL,
            "schema.yml": INCREMENTAL_DOCS_INITIAL_YML,
        }

    def test_incremental_docs_create_and_update(self, project):
        assert len(run_dbt(["run"])) == 1
        relation = relation_from_name(project.adapter, "incremental_docs")

        assert relation_comment(project, relation).strip() == (
            'Initial "orders" owner\'s history'
        )
        assert column_comments(project, relation)["id"].strip() == (
            'Identifier "id" owner\'s value'
        )

        write_file(INCREMENTAL_DOCS_UPDATED_YML, "models", "schema.yml")
        assert len(run_dbt(["run"])) == 1

        assert relation_comment(project, relation) == "Updated incremental relation docs"
        assert column_comments(project, relation) == {
            "id": "Updated identifier docs",
            "name": "Updated name docs",
        }

        write_file(INCREMENTAL_DOCS_INITIAL_YML, "models", "schema.yml")
        assert len(run_dbt(["run", "--full-refresh"])) == 1

        assert relation_comment(project, relation).strip() == (
            'Initial "orders" owner\'s history'
        )
        assert column_comments(project, relation)["id"].strip() == (
            'Identifier "id" owner\'s value'
        )


class TestDorisIncrementalMergePersistDocs:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_merge_docs.sql": INCREMENTAL_MERGE_DOCS_SQL,
            "schema.yml": INCREMENTAL_MERGE_DOCS_YML,
        }

    def test_merge_create_keeps_unique_table_and_inline_docs(self, project):
        assert len(run_dbt(["run"])) == 1
        relation = relation_from_name(project.adapter, "incremental_merge_docs")

        assert relation_comment(project, relation) == "Merge relation docs"
        assert column_comments(project, relation)["id"].strip() == (
            'Merge "id" owner\'s value'
        )
        ddl = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert "unique key" in ddl
        assert '"enable_unique_key_merge_on_write" = "true"' in ddl
