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

from datetime import datetime, timedelta, timezone

import pytest
from dbt.tests.adapter.persist_docs.test_persist_docs import (
    BasePersistDocs,
    BasePersistDocsAllColumnsMissing,
    BasePersistDocsColumnMissing,
    BasePersistDocsCommentOnQuotedColumn,
    BasePersistDocsQuotedColumnCaseSensitive,
    BasePersistDocsQuotedDescriptionNotAppliedOnMismatch,
)
from dbt.tests.util import (
    patch_microbatch_end_time,
    relation_from_name,
    run_dbt,
    write_file,
)


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


INCREMENTAL_SEQUENCE_MERGE_DOCS_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['id'],
    distributed_by=['id'],
    persist_docs={'relation': true, 'columns': true},
    properties={
        'replication_num': '1',
        'function_column.sequence_col': 'sequence_id'
    }
) }}

{% if is_incremental() %}
select cast(1 as int) as id, cast(5 as bigint) as sequence_id,
       cast('stale' as varchar(20)) as name
union all
select cast(2 as int) as id, cast(20 as bigint) as sequence_id,
       cast('second' as varchar(20)) as name
{% else %}
select cast(1 as int) as id, cast(10 as bigint) as sequence_id,
       cast('first' as varchar(20)) as name
{% endif %}
"""


INCREMENTAL_SEQUENCE_MERGE_DOCS_YML = """
version: 2
models:
  - name: incremental_sequence_merge_docs
    description: Sequence merge relation docs
    columns:
      - name: id
        description: Sequence merge identifier
      - name: sequence_id
        description: Doris sequence ordering value
      - name: name
        description: Sequence merge name
"""


INCREMENTAL_DOCS_COPY_FAILURE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    partition_by=['id'],
    partition_type='RANGE',
    partition_by_init=['PARTITION p_low VALUES LESS THAN ("10")'],
    distributed_by=['id'],
    persist_docs={'relation': true, 'columns': true},
    properties={'replication_num': '1'}
) }}

select cast(20 as int) as id, cast('outside' as varchar(20)) as name
"""


INCREMENTAL_DOCS_COPY_RECOVERY_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    partition_by=['id'],
    partition_type='RANGE',
    partition_by_init=['PARTITION p_low VALUES LESS THAN ("10")'],
    distributed_by=['id'],
    persist_docs={'relation': true, 'columns': true},
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select cast(6 as int) as id, cast('incremental' as varchar(20)) as name
{% else %}
select cast(5 as int) as id, cast('initial' as varchar(20)) as name
{% endif %}
"""


INCREMENTAL_DOCS_INITIAL_RECOVERY_YML = """
version: 2
models:
  - name: incremental_docs_initial_recovery
    description: Initial docs recovery
    columns:
      - name: id
        description: Recovery identifier
      - name: name
        description: Recovery name
"""


INCREMENTAL_DOCS_REFRESH_RECOVERY_YML = """
version: 2
models:
  - name: incremental_docs_refresh_recovery
    description: Refresh docs recovery
    columns:
      - name: id
        description: Recovery identifier
      - name: name
        description: Recovery name
"""


MICROBATCH_DOCS_TODAY = datetime.now(timezone.utc).date()
MICROBATCH_DOCS_DATE = MICROBATCH_DOCS_TODAY - timedelta(days=1)


MICROBATCH_DOCS_INPUT_SQL = """
{{ config(
    materialized='table',
    event_time='event_time',
    duplicate_key=['id', 'event_time'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, cast('__EVENT_DATE__ 00:00:00' as datetime) as event_time,
       cast('documented' as varchar(20)) as name
""".replace("__EVENT_DATE__", MICROBATCH_DOCS_DATE.isoformat())


MICROBATCH_DOCS_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='microbatch',
    event_time='event_time',
    batch_size='day',
    begin=modules.datetime.datetime(__YEAR__, __MONTH__, __DAY__, 0, 0, 0),
    duplicate_key=['id', 'event_time'],
    partition_by=['event_time'],
    partition_type='RANGE',
    distributed_by=['id'],
    persist_docs={'relation': true, 'columns': true},
    properties={'replication_num': '1'}
) }}

select id, event_time, name
from {{ ref('microbatch_docs_input') }}
"""
MICROBATCH_DOCS_SQL = (
    MICROBATCH_DOCS_SQL.replace("__YEAR__", str(MICROBATCH_DOCS_DATE.year))
    .replace("__MONTH__", str(MICROBATCH_DOCS_DATE.month))
    .replace("__DAY__", str(MICROBATCH_DOCS_DATE.day))
)


MICROBATCH_DOCS_YML = """
version: 2
models:
  - name: microbatch_docs
    description: Microbatch relation docs
    columns:
      - name: id
        description: Microbatch identifier
      - name: event_time
        description: Microbatch event time
      - name: name
        description: Microbatch name
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


def dbt_helper_relations(project, relation):
    return project.run_sql(
        "select table_name from information_schema.tables "
        f"where table_schema = '{relation.schema}' "
        f"and table_name like '{relation.identifier}__dbt_%' "
        "order by table_name",
        fetch="all",
    )


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


class TestDorisIncrementalSequenceMergePersistDocs:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_sequence_merge_docs.sql": (
                INCREMENTAL_SEQUENCE_MERGE_DOCS_SQL
            ),
            "schema.yml": INCREMENTAL_SEQUENCE_MERGE_DOCS_YML,
        }

    def test_sequence_merge_uses_safe_docs_source_and_keeps_ordering(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["run"])) == 1
        relation = relation_from_name(
            project.adapter,
            "incremental_sequence_merge_docs",
        )

        assert project.run_sql(
            f"select id, sequence_id, name from {relation} order by id",
            fetch="all",
        ) == [(1, 10, "first"), (2, 20, "second")]
        assert relation_comment(project, relation) == "Sequence merge relation docs"
        assert column_comments(project, relation)["sequence_id"] == (
            "Doris sequence ordering value"
        )
        ddl = project.run_sql(
            f"show create table {relation}",
            fetch="one",
        )[1].lower()
        assert "unique key" in ddl
        assert '"function_column.sequence_col" = "sequence_id"' in ddl


class TestDorisIncrementalPersistDocsInitialRecovery:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_docs_initial_recovery.sql": (
                INCREMENTAL_DOCS_COPY_FAILURE_SQL
            ),
            "schema.yml": INCREMENTAL_DOCS_INITIAL_RECOVERY_YML,
        }

    def test_failed_initial_copy_is_not_published_and_retry_is_full(self, project):
        failure = run_dbt(["run"], expect_pass=False)
        assert len(failure.results) == 1
        relation = relation_from_name(
            project.adapter,
            "incremental_docs_initial_recovery",
        )
        assert project.run_sql(
            "select count(*) from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}'",
            fetch="one",
        )[0] == 0

        write_file(
            INCREMENTAL_DOCS_COPY_RECOVERY_SQL,
            "models",
            "incremental_docs_initial_recovery.sql",
        )
        assert len(run_dbt(["run"])) == 1
        assert project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        ) == [(5, "initial")]
        assert dbt_helper_relations(project, relation) == []


class TestDorisIncrementalPersistDocsFullRefreshRecovery:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incremental_docs_refresh_recovery.sql": (
                INCREMENTAL_DOCS_COPY_RECOVERY_SQL
            ),
            "schema.yml": INCREMENTAL_DOCS_REFRESH_RECOVERY_YML,
        }

    def test_failed_refresh_copy_preserves_target_and_retry_cleans_helpers(
        self,
        project,
    ):
        assert len(run_dbt(["run"])) == 1
        relation = relation_from_name(
            project.adapter,
            "incremental_docs_refresh_recovery",
        )

        write_file(
            INCREMENTAL_DOCS_COPY_FAILURE_SQL,
            "models",
            "incremental_docs_refresh_recovery.sql",
        )
        failure = run_dbt(["run", "--full-refresh"], expect_pass=False)
        assert len(failure.results) == 1
        assert project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        ) == [(5, "initial")]

        write_file(
            INCREMENTAL_DOCS_COPY_RECOVERY_SQL,
            "models",
            "incremental_docs_refresh_recovery.sql",
        )
        assert len(run_dbt(["run"])) == 1
        assert project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        ) == [(5, "initial"), (6, "incremental")]
        assert dbt_helper_relations(project, relation) == []


class TestDorisMicrobatchPersistDocs:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "microbatch_docs_input.sql": MICROBATCH_DOCS_INPUT_SQL,
            "microbatch_docs.sql": MICROBATCH_DOCS_SQL,
            "schema.yml": MICROBATCH_DOCS_YML,
        }

    def test_initial_docs_publish_and_incremental_partition_overwrite(self, project):
        invocation_time = datetime.combine(
            MICROBATCH_DOCS_TODAY,
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        with patch_microbatch_end_time(
            invocation_time.strftime("%Y-%m-%d %H:%M:%S")
        ):
            assert len(run_dbt(["run"])) == 2

        relation = relation_from_name(project.adapter, "microbatch_docs")
        assert project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        ) == [(1, "documented")]
        assert relation_comment(project, relation) == "Microbatch relation docs"
        assert column_comments(project, relation)["event_time"] == (
            "Microbatch event time"
        )
        assert dbt_helper_relations(project, relation) == []

        with patch_microbatch_end_time(
            invocation_time.strftime("%Y-%m-%d %H:%M:%S")
        ):
            assert len(run_dbt(["run", "--select", "microbatch_docs"])) == 1
        assert project.run_sql(
            f"select id, name from {relation} order by id",
            fetch="all",
        ) == [(1, "documented")]
        assert dbt_helper_relations(project, relation) == []
