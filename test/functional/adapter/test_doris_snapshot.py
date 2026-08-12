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

"""
Tests for Doris snapshot (SCD Type 2) using check strategy.
Doris snapshots require a unique key table for the upsert-based merge.
"""

import pytest
from dbt.tests.adapter.simple_snapshot.test_snapshot import (
    BaseSimpleSnapshot,
    BaseSnapshotCheck,
)
from dbt.tests.util import run_dbt, relation_from_name, write_file


SOURCE_TABLE_SQL = """
{{ config(
    materialized='table',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, 'alice' as name, 100 as score
union all
select 2 as id, 'bob' as name, 200 as score
"""

SOURCE_TABLE_UPDATED_SQL = """
{{ config(
    materialized='table',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, 'alice' as name, 150 as score
union all
select 3 as id, 'charlie' as name, 300 as score
"""

SNAPSHOT_SQL = """
{% snapshot snap_users %}

{{
    config(
        target_database=target.schema,
        target_schema=target.schema,
        unique_key='id',
        strategy='check',
        check_cols=['name', 'score'],
        invalidate_hard_deletes=True,
    )
}}

select * from {{ ref('snap_source') }}

{% endsnapshot %}
"""


def helper_relations(project, relation):
    rows = project.run_sql(
        "select table_name from information_schema.tables "
        f"where table_schema = '{relation.schema}' "
        f"and table_name like '{relation.identifier}\\_\\_%' "
        "order by table_name",
        fetch="all",
    )
    return [row[0] for row in rows]


class TestDorisSnapshot:
    @pytest.fixture(scope="class")
    def models(self):
        return {"snap_source.sql": SOURCE_TABLE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_users.sql": SNAPSHOT_SQL}

    def test_snapshot(self, project):
        # Create the source table
        results = run_dbt(["run"])
        assert len(results) == 1

        # First snapshot
        results = run_dbt(["snapshot"])
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "snap_users")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 2

        # All rows should have dbt_valid_to = NULL (current)
        result = project.run_sql(
            f"select count(*) from {relation} where dbt_valid_to is null",
            fetch="one",
        )
        assert result[0] == 2

        write_file(
            SOURCE_TABLE_UPDATED_SQL,
            "models",
            "snap_source.sql",
        )
        results = run_dbt(["run"])
        assert len(results) == 1

        results = run_dbt(["snapshot"])
        assert len(results) == 1

        result = project.run_sql(
            f"select count(*) from {relation}",
            fetch="one",
        )
        assert result[0] == 4

        current_rows = project.run_sql(
            f"select id, score from {relation} "
            "where dbt_valid_to is null order by id",
            fetch="all",
        )
        assert current_rows == [(1, 150), (3, 300)]

        id_one_history = project.run_sql(
            f"select score, dbt_valid_to is null from {relation} "
            "where id = 1 order by score",
            fetch="all",
        )
        assert id_one_history == [(100, 0), (150, 1)]

        id_two_history = project.run_sql(
            f"select score, dbt_valid_to is null from {relation} where id = 2",
            fetch="all",
        )
        assert id_two_history == [(200, 0)]

        # A no-change run is idempotent and successful runs leave no physical
        # staging or upsert relations behind.
        results = run_dbt(["snapshot"])
        assert len(results) == 1
        result = project.run_sql(
            f"select count(*) from {relation}",
            fetch="one",
        )
        assert result[0] == 4
        assert helper_relations(project, relation) == []


ATOMIC_SNAPSHOT_SQL = SNAPSHOT_SQL.replace("snap_users", "snap_atomic")

FAIL_SNAPSHOT_EXCHANGE_MACRO = """
{% macro exchange_relation(relation1, relation2, is_drop_r1=false) %}
    {% if var('fail_snapshot_exchange', false)
          and relation1.identifier == 'snap_atomic' %}
        {% do exceptions.raise_compiler_error(
            'Injected Snapshot exchange failure'
        ) %}
    {% endif %}
    {% call statement('test_exchange_relation') %}
        alter table {{ relation1 }}
        replace with table `{{ relation2.table }}`
        properties('swap' = '{{ not is_drop_r1 }}')
    {% endcall %}
{% endmacro %}
"""


class TestDorisSnapshotAtomicRecovery:
    @pytest.fixture(scope="class")
    def models(self):
        return {"snap_source.sql": SOURCE_TABLE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_atomic.sql": ATOMIC_SNAPSHOT_SQL}

    @pytest.fixture(scope="class")
    def macros(self):
        return {"inject_snapshot_exchange_failure.sql": FAIL_SNAPSHOT_EXCHANGE_MACRO}

    def test_exchange_failure_preserves_history_and_rerun_cleans_helpers(
        self, project
    ):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1

        relation = relation_from_name(project.adapter, "snap_atomic")
        original_rows = project.run_sql(
            f"select id, score from {relation} "
            "where dbt_valid_to is null order by id",
            fetch="all",
        )
        assert original_rows == [(1, 100), (2, 200)]

        write_file(SOURCE_TABLE_UPDATED_SQL, "models", "snap_source.sql")
        assert len(run_dbt(["run"])) == 1

        failed = run_dbt(
            [
                "snapshot",
                "--vars",
                "{fail_snapshot_exchange: true}",
            ],
            expect_pass=False,
        )
        assert len(failed) == 1

        # The atomic exchange was never committed: the target still exposes
        # the complete old history, while failed helpers may remain for retry.
        rows_after_failure = project.run_sql(
            f"select id, score from {relation} "
            "where dbt_valid_to is null order by id",
            fetch="all",
        )
        assert rows_after_failure == original_rows
        assert set(helper_relations(project, relation)) == {
            "snap_atomic__dbt_tmp",
            "snap_atomic__snapshot_upsert",
        }

        # The next run drops both unknown-state helpers, rebuilds from the old
        # authoritative target, and installs the complete new history.
        assert len(run_dbt(["snapshot"])) == 1
        current_rows = project.run_sql(
            f"select id, score from {relation} "
            "where dbt_valid_to is null order by id",
            fetch="all",
        )
        assert current_rows == [(1, 150), (3, 300)]
        assert helper_relations(project, relation) == []


TIMESTAMP_SOURCE_SQL = """
{{ config(
    materialized='table',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, 'alice' as name, 100 as score,
       cast('2026-01-01 00:00:00.123456' as datetime(6)) as updated_at
union all
select 2 as id, 'bob' as name, 200 as score,
       cast('2026-01-01 00:00:00.123456' as datetime(6)) as updated_at
"""

TIMESTAMP_SOURCE_UPDATED_SQL = TIMESTAMP_SOURCE_SQL.replace(
    "100 as score,\n       cast('2026-01-01 00:00:00.123456'",
    "150 as score,\n       cast('2026-01-02 00:00:00.654321'",
) + """
union all
select 3 as id, 'charlie' as name, 300 as score,
       cast('2026-01-03 00:00:00.111111' as datetime(6)) as updated_at
"""

TIMESTAMP_SOURCE_REGRESSED_SQL = TIMESTAMP_SOURCE_UPDATED_SQL.replace(
    "150 as score,\n       cast('2026-01-02 00:00:00.654321'",
    "175 as score,\n       cast('2025-12-31 23:59:59.999999'",
)

TIMESTAMP_SNAPSHOT_SQL = """
{% snapshot snap_timestamp %}
{{
    config(
        target_schema=target.schema,
        unique_key='id',
        strategy='timestamp',
        updated_at='updated_at',
        hard_deletes='invalidate'
    )
}}
select * from {{ ref('timestamp_source') }}
{% endsnapshot %}
"""


class TestDorisTimestampSnapshot:
    @pytest.fixture(scope="class")
    def models(self):
        return {"timestamp_source.sql": TIMESTAMP_SOURCE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_timestamp.sql": TIMESTAMP_SNAPSHOT_SQL}

    def test_timestamp_precision_idempotence_and_regression_guard(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1

        relation = relation_from_name(project.adapter, "snap_timestamp")
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 2

        write_file(
            TIMESTAMP_SOURCE_UPDATED_SQL,
            "models",
            "timestamp_source.sql",
        )
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1

        id_one_history = project.run_sql(
            f"select score, dbt_valid_from, dbt_valid_to is null "
            f"from {relation} where id = 1 order by score",
            fetch="all",
        )
        assert [row[0] for row in id_one_history] == [100, 150]
        assert id_one_history[1][1].microsecond == 654321
        assert [row[2] for row in id_one_history] == [0, 1]

        assert len(run_dbt(["snapshot"])) == 1
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 4

        null_updated_at_sql = TIMESTAMP_SOURCE_UPDATED_SQL + """
union all
select 4 as id, 'null-time' as name, 400 as score,
       cast(null as datetime(6)) as updated_at
"""
        write_file(
            null_updated_at_sql,
            "models",
            "timestamp_source.sql",
        )
        assert len(run_dbt(["run"])) == 1
        failed = run_dbt(["snapshot"], expect_pass=False)
        assert len(failed) == 1
        assert "NULL updated_at" in failed[0].message
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 4

        write_file(
            TIMESTAMP_SOURCE_REGRESSED_SQL,
            "models",
            "timestamp_source.sql",
        )
        assert len(run_dbt(["run"])) == 1
        failed = run_dbt(["snapshot"], expect_pass=False)
        assert len(failed) == 1
        assert "monotonic updated_at" in failed[0].message
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 4


HARD_DELETE_SOURCE_SQL = SOURCE_TABLE_SQL.replace(
    "snap_source", "hard_delete_source"
)
HARD_DELETE_SOURCE_UPDATED_SQL = """
{{ config(
    materialized='table',
    unique_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}
select 1 as id, 'alice' as name, 100 as score
"""


def hard_delete_snapshot(name, behavior):
    return f"""
{{% snapshot {name} %}}
{{{{
    config(
        target_schema=target.schema,
        unique_key='id',
        strategy='check',
        check_cols=['name', 'score'],
        hard_deletes='{behavior}'
    )
}}}}
select * from {{{{ ref('hard_delete_source') }}}}
{{% endsnapshot %}}
"""


class TestDorisSnapshotHardDeletes:
    @pytest.fixture(scope="class")
    def models(self):
        return {"hard_delete_source.sql": HARD_DELETE_SOURCE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {
            "snap_ignore.sql": hard_delete_snapshot("snap_ignore", "ignore"),
            "snap_invalidate.sql": hard_delete_snapshot(
                "snap_invalidate", "invalidate"
            ),
            "snap_new_record.sql": hard_delete_snapshot(
                "snap_new_record", "new_record"
            ),
        }

    def test_hard_delete_behavior_matrix(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 3

        write_file(
            HARD_DELETE_SOURCE_UPDATED_SQL,
            "models",
            "hard_delete_source.sql",
        )
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 3

        ignored = relation_from_name(project.adapter, "snap_ignore")
        invalidated = relation_from_name(project.adapter, "snap_invalidate")
        new_record = relation_from_name(project.adapter, "snap_new_record")

        assert project.run_sql(
            f"select dbt_valid_to is null from {ignored} where id = 2",
            fetch="all",
        ) == [(1,)]
        assert project.run_sql(
            f"select dbt_valid_to is null from {invalidated} where id = 2",
            fetch="all",
        ) == [(0,)]
        assert project.run_sql(
            f"select dbt_is_deleted, dbt_valid_to is null "
            f"from {new_record} where id = 2 order by dbt_valid_from",
            fetch="all",
        ) == [("False", 0), ("True", 1)]

        assert len(run_dbt(["snapshot"])) == 3
        assert project.run_sql(
            f"select count(*) from {new_record} where id = 2",
            fetch="one",
        )[0] == 2


META_SOURCE_SQL = """
{{ config(
    materialized='table',
    properties={'replication_num': '1'}
) }}
select 1 as tenant_id, 1 as id, 'alice' as name, 100 as score
union all
select 2 as tenant_id, 1 as id, 'bob' as name, 200 as score
"""

META_SOURCE_UPDATED_SQL = META_SOURCE_SQL.replace(
    "'alice' as name, 100 as score",
    "'alice' as name, 150 as score",
)

META_SNAPSHOT_SQL = """
{% snapshot snap_custom_meta %}
{{
    config(
        target_schema=target.schema,
        unique_key=['tenant_id', 'id'],
        strategy='check',
        check_cols=['name', 'score'],
        dbt_valid_to_current="cast('2099-12-31 00:00:00' as datetime)",
        snapshot_meta_column_names={
            'dbt_valid_from': 'valid_from',
            'dbt_valid_to': 'valid_until',
            'dbt_scd_id': 'version_id',
            'dbt_updated_at': 'recorded_at',
            'dbt_is_deleted': 'is_deleted'
        }
    )
}}
select * from {{ ref('meta_source') }}
{% endsnapshot %}
"""


class TestDorisSnapshotMetadataConfig:
    @pytest.fixture(scope="class")
    def models(self):
        return {"meta_source.sql": META_SOURCE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_custom_meta.sql": META_SNAPSHOT_SQL}

    def test_composite_key_custom_meta_and_valid_to_current(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1

        relation = relation_from_name(project.adapter, "snap_custom_meta")
        columns = project.run_sql(
            f"show full columns from {relation}", fetch="all"
        )
        column_names = {column[0] for column in columns}
        assert {
            "valid_from",
            "valid_until",
            "version_id",
            "recorded_at",
        }.issubset(column_names)
        assert project.run_sql(
            f"select count(*) from {relation} "
            "where valid_until = cast('2099-12-31 00:00:00' as datetime)",
            fetch="one",
        )[0] == 2

        write_file(META_SOURCE_UPDATED_SQL, "models", "meta_source.sql")
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1

        tenant_one = project.run_sql(
            f"select score, valid_until = "
            "cast('2099-12-31 00:00:00' as datetime) "
            f"from {relation} where tenant_id = 1 and id = 1 order by score",
            fetch="all",
        )
        assert tenant_one == [(100, 0), (150, 1)]
        assert project.run_sql(
            f"select count(*) from {relation} "
            "where tenant_id = 2 and id = 1",
            fetch="one",
        )[0] == 1

        # A normal rerun must not erase historical Snapshot versions.
        assert len(run_dbt(["snapshot"])) == 1
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 3


SCHEMA_SOURCE_SQL = """
{{ config(materialized='table', properties={'replication_num': '1'}) }}
select cast(1 as int) as id,
       cast('alice' as varchar(20)) as name,
       cast(100 as int) as score
"""

SCHEMA_SOURCE_WITH_COLUMN_SQL = """
{{ config(materialized='table', properties={'replication_num': '1'}) }}
select cast(1 as int) as id,
       cast('alice' as varchar(20)) as name,
       cast(100 as int) as score,
       cast('alice@example.com' as varchar(40)) as email,
       cast('east' as varchar(20)) as region
"""

SCHEMA_SOURCE_CHANGED_TYPE_SQL = SCHEMA_SOURCE_WITH_COLUMN_SQL.replace(
    "cast(100 as int) as score",
    "cast('100' as varchar(20)) as score",
)

SCHEMA_SNAPSHOT_SQL = """
{% snapshot snap_schema %}
{{
    config(
        target_schema=target.schema,
        unique_key='id',
        strategy='check',
        check_cols='all'
    )
}}
select * from {{ ref('schema_source') }}
{% endsnapshot %}
"""


class TestDorisSnapshotSchemaChange:
    @pytest.fixture(scope="class")
    def models(self):
        return {"schema_source.sql": SCHEMA_SOURCE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_schema.sql": SCHEMA_SNAPSHOT_SQL}

    def test_add_preserves_history_and_destructive_changes_fail(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1
        relation = relation_from_name(project.adapter, "snap_schema")

        write_file(
            SCHEMA_SOURCE_WITH_COLUMN_SQL,
            "models",
            "schema_source.sql",
        )
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1

        history = project.run_sql(
            f"select email, region, dbt_valid_to is null from {relation} "
            "where id = 1 order by dbt_valid_from",
            fetch="all",
        )
        assert history == [
            (None, None, 0),
            ("alice@example.com", "east", 1),
        ]

        write_file(SCHEMA_SOURCE_SQL, "models", "schema_source.sql")
        assert len(run_dbt(["run"])) == 1
        removed = run_dbt(["snapshot"], expect_pass=False)
        assert "removed historical column" in removed[0].message
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 2

        # Restore the source to prove the failed staging table is discarded.
        write_file(
            SCHEMA_SOURCE_WITH_COLUMN_SQL,
            "models",
            "schema_source.sql",
        )
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1
        assert helper_relations(project, relation) == []

        write_file(
            SCHEMA_SOURCE_CHANGED_TYPE_SQL,
            "models",
            "schema_source.sql",
        )
        assert len(run_dbt(["run"])) == 1
        changed = run_dbt(["snapshot"], expect_pass=False)
        assert "changed historical column type" in changed[0].message
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 2


INVALID_SOURCE_SQL = """
{{ config(materialized='table', properties={'replication_num': '1'}) }}
select 1 as id, 'first' as value
union all
select 1 as id, 'duplicate' as value
"""

VALID_SOURCE_SQL = """
{{ config(materialized='table', properties={'replication_num': '1'}) }}
select 1 as id, 'first' as value
"""

NULL_KEY_SOURCE_SQL = VALID_SOURCE_SQL + """
union all
select cast(null as int) as id, 'null-key' as value
"""

VALIDATION_SNAPSHOT_SQL = """
{% snapshot snap_validated %}
{{
    config(
        target_schema=target.schema,
        unique_key='id',
        strategy='check',
        check_cols=['value']
    )
}}
select * from {{ ref('validated_source') }}
{% endsnapshot %}
"""


class TestDorisSnapshotSourceValidation:
    @pytest.fixture(scope="class")
    def models(self):
        return {"validated_source.sql": INVALID_SOURCE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_validated.sql": VALIDATION_SNAPSHOT_SQL}

    def test_duplicate_and_null_keys_fail_without_corrupting_target(self, project):
        assert len(run_dbt(["run"])) == 1
        duplicate = run_dbt(["snapshot"], expect_pass=False)
        assert "duplicate unique_key" in duplicate[0].message

        write_file(VALID_SOURCE_SQL, "models", "validated_source.sql")
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1
        relation = relation_from_name(project.adapter, "snap_validated")
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 1

        write_file(NULL_KEY_SOURCE_SQL, "models", "validated_source.sql")
        assert len(run_dbt(["run"])) == 1
        null_key = run_dbt(["snapshot"], expect_pass=False)
        assert "NULL unique_key" in null_key[0].message
        assert project.run_sql(
            f"select count(*) from {relation}", fetch="one"
        )[0] == 1


WRONG_TYPE_SNAPSHOT_SQL = SNAPSHOT_SQL.replace("snap_users", "snap_wrong_type")


class TestDorisSnapshotWrongTargetType:
    @pytest.fixture(scope="class")
    def models(self):
        return {"snap_source.sql": SOURCE_TABLE_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_wrong_type.sql": WRONG_TYPE_SNAPSHOT_SQL}

    def test_existing_view_is_not_dropped_or_replaced(self, project):
        assert len(run_dbt(["run"])) == 1
        relation = relation_from_name(project.adapter, "snap_wrong_type")
        project.run_sql(
            f"create view {relation} as select 99 as id, 'keep-me' as marker"
        )

        failed = run_dbt(["snapshot"], expect_pass=False)

        assert len(failed) == 1
        assert project.run_sql(
            f"select id, marker from {relation}", fetch="all"
        ) == [(99, "keep-me")]


DOCUMENTED_SNAPSHOT_SQL = """
{% snapshot snap_documented %}
{{
    config(
        target_schema=target.schema,
        unique_key='id',
        strategy='check',
        check_cols=['name', 'score']
    )
}}
select * from {{ ref('snap_source') }}
{% endsnapshot %}
"""

DOCUMENTED_SNAPSHOT_YML = """
version: 2
snapshots:
  - name: snap_documented
    description: Customer history maintained by dbt Snapshot
    config:
      persist_docs:
        relation: true
        columns: true
    columns:
      - name: id
        description: Stable customer identifier
      - name: score
        description: Historical customer score
"""


class TestDorisSnapshotPersistDocs:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "snap_source.sql": SOURCE_TABLE_SQL,
            "snap_documented.yml": DOCUMENTED_SNAPSHOT_YML,
        }

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_documented.sql": DOCUMENTED_SNAPSHOT_SQL}

    def test_docs_survive_atomic_replacement(self, project):
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1
        relation = relation_from_name(project.adapter, "snap_documented")

        write_file(SOURCE_TABLE_UPDATED_SQL, "models", "snap_source.sql")
        assert len(run_dbt(["run"])) == 1
        assert len(run_dbt(["snapshot"])) == 1

        create_table = project.run_sql(
            f"show create table {relation}", fetch="one"
        )[1]
        assert "Customer history maintained by dbt Snapshot" in create_table
        columns = project.run_sql(
            f"show full columns from {relation}", fetch="all"
        )
        comments = {column[0]: column[8] for column in columns}
        assert comments["id"] == "Stable customer identifier"
        assert comments["score"] == "Historical customer score"


class DorisOfficialSnapshotSourceMixin:
    """Run dbt's official Snapshot cases with an update-capable Doris source."""

    def create_fact_from_seed(self, where=None):
        fact = relation_from_name(self.project.adapter, "fact")
        seed = relation_from_name(self.project.adapter, "seed")
        where_clause = where or "1 = 1"
        self.project.run_sql(f"drop table if exists {fact}")
        self.project.run_sql(
            f"""
            create table {fact} (
                id int,
                first_name varchar(100),
                last_name varchar(100),
                email varchar(200),
                gender varchar(20),
                ip_address varchar(50),
                updated_at date
            )
            unique key(id)
            distributed by hash(id) buckets 1
            properties('replication_num' = '1',
                       'enable_unique_key_merge_on_write' = 'true')
            """
        )
        self.project.run_sql(
            f"insert into {fact} select * from {seed} where {where_clause}"
        )

    def update_fact_records(self, updates, where=None):
        compatible_updates = {
            key: value.replace(
                "first_name || ' ' || last_name",
                "concat(first_name, ' ', last_name)",
            ).replace(
                "updated_at + interval '1 day'",
                "date_add(updated_at, interval 1 day)",
            )
            for key, value in updates.items()
        }
        return super().update_fact_records(compatible_updates, where)

    def delete_snapshot_records(self):
        snapshot = relation_from_name(self.project.adapter, "snapshot")
        self.project.run_sql(f"drop table if exists {snapshot}")


class TestDorisOfficialTimestampSnapshot(
    DorisOfficialSnapshotSourceMixin,
    BaseSimpleSnapshot,
):
    pass


class TestDorisOfficialCheckSnapshot(
    DorisOfficialSnapshotSourceMixin,
    BaseSnapshotCheck,
):
    pass
