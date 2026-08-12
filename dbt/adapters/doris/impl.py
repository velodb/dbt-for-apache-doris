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

from dbt.adapters.sql import SQLAdapter

from dataclasses import dataclass
from enum import Enum
import re
import time
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Tuple,
    Union,
)

import agate
import dbt.exceptions
from dbt.adapters.base import available
from dbt.adapters.base.relation import BaseRelation
from dbt.adapters.contracts.connection import AdapterResponse
from dbt.adapters.doris.column import DorisColumn
from dbt.adapters.doris.connections import DorisConnectionManager
from dbt.adapters.doris.relation import DorisRelation
from dbt.adapters.protocol import AdapterConfig
from dbt.adapters.contracts.relation import RelationType
from dbt.adapters.sql.impl import LIST_RELATIONS_MACRO_NAME, LIST_SCHEMAS_MACRO_NAME
from dbt_common.clients.agate_helper import table_from_rows
from dbt.adapters.doris.doris_column_item import DorisColumnItem


_DORIS_VERSION = re.compile(
    r"(?:^|doris-)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
)
_DORIS_DEVELOPMENT_VERSION = re.compile(
    r"^doris-0\.0\.0-[0-9a-f]{7,64}$"
)


@dataclass
class DorisMaterializedViewAdapterResponse(AdapterResponse):
    """dbt result metadata for a Doris asynchronous MV refresh task."""

    task_id: Optional[str] = None
    task_status: Optional[str] = None
    task_error: Optional[str] = None


def _validate_doris_materialized_view_version(
        frontends_table: agate.Table,
) -> None:
    """Validate release gates while allowing identifiable source builds."""
    if "Version" not in frontends_table.column_names or not frontends_table.rows:
        raise dbt.exceptions.DbtRuntimeError(
            "Could not determine the connected Doris FE version from "
            "SHOW FRONTENDS."
        )

    required_rows = [
        row
        for row in frontends_table.rows
        if (
            "CurrentConnected" in frontends_table.column_names
            and str(row["CurrentConnected"]).casefold() in {"yes", "true"}
        )
        or (
            "IsMaster" in frontends_table.column_names
            and str(row["IsMaster"]).casefold() in {"yes", "true"}
        )
    ]
    if not required_rows:
        required_rows = [frontends_table.rows[0]]

    for row in required_rows:
        version_text = str(row["Version"])
        match = _DORIS_VERSION.search(version_text)
        if match is None:
            raise dbt.exceptions.DbtRuntimeError(
                "Could not parse a required Doris FE version "
                f"{version_text!r} from SHOW FRONTENDS."
            )

        version = tuple(
            int(match.group(component))
            for component in ("major", "minor", "patch")
        )
        supported = (
            _DORIS_DEVELOPMENT_VERSION.fullmatch(version_text) is not None
            or (version[0] == 2 and version >= (2, 1, 5))
            or (
                version[0] == 3
                and (version[1] >= 1 or version[2] >= 1)
            )
            or version[0] >= 4
        )
        if not supported:
            raise dbt.exceptions.DbtRuntimeError(
                f"Doris FE version {version_text} does not pass the adapter's "
                "current asynchronous-materialized-view version gate. The "
                "gate accepts identifiable Doris source builds reported as "
                "doris-0.0.0-<git sha>, Doris 2.x >= 2.1.5, Doris 3.x except "
                "3.0.0, or Doris major version >= 4. These gate boundaries are "
                "runtime conditions, not a live-cluster compatibility matrix."
            )


class Engine(str, Enum):
    olap = "olap"
    mysql = "mysql"
    elasticsearch = "elasticsearch"
    hive = "hive"
    iceberg = "iceberg"


class PartitionType(str, Enum):
    list = "LIST"
    range = "RANGE"


@dataclass
class DorisConfig(AdapterConfig):
    """Doris-specific model configuration understood by dbt Core."""

    engine: Optional[str] = None
    duplicate_key: Optional[Union[str, List[str]]] = None
    partition_by: Optional[Union[str, List[str]]] = None
    partition_type: str = PartitionType.range.value
    partition_by_init: Optional[List[str]] = None
    distributed_by: Optional[Union[str, List[str]]] = None
    buckets: Optional[Union[int, str]] = None
    properties: Optional[Dict[str, Any]] = None
    replication_num: Optional[Union[int, str]] = None

    # Doris asynchronous materialized-view configuration.
    build_mode: str = "immediate"
    refresh_method: str = "auto"
    refresh_trigger: str = "manual"
    refresh_schedule: Optional[Dict[str, Any]] = None
    distribution_type: Optional[str] = None
    wait_for_refresh: bool = True
    refresh_wait_timeout: int = 300
    refresh_poll_interval: int = 1


class DorisAdapter(SQLAdapter):
    ConnectionManager = DorisConnectionManager
    Relation = DorisRelation
    AdapterSpecificConfigs = DorisConfig
    Column = DorisColumn

    def valid_incremental_strategies(self):
        """Return the built-in incremental strategies implemented by dbt-doris."""
        return ["append", "merge", "insert_overwrite", "microbatch"]

    def rename_relation(self, from_relation, to_relation):
        """Reject View renames before dbt mutates its relation cache.

        Doris cannot safely move a View through dbt's generic rename path.
        Callers that replace a View must first snapshot its currently evaluated
        rows to a Table.
        """
        if from_relation.is_view or to_relation.is_view:
            raise dbt.exceptions.DbtRuntimeError(
                "Doris cannot safely rename a View. Snapshot the View to a "
                "Table first."
            )
        return super().rename_relation(from_relation, to_relation)

    def expand_column_types(self, goal, current):
        """Widen string columns using Doris's case-insensitive name rules."""
        reference_columns = {
            column.name.casefold(): column
            for column in self.get_columns_in_relation(goal)
        }
        target_columns = {
            column.name.casefold(): column
            for column in self.get_columns_in_relation(current)
        }

        for normalized_name, reference_column in reference_columns.items():
            target_column = target_columns.get(normalized_name)
            if (
                target_column is not None
                and target_column.can_expand_to(reference_column)
            ):
                new_type = self.Column.string_type(
                    reference_column.string_size()
                )
                self.alter_column_type(
                    current,
                    target_column.name,
                    new_type,
                )

    def _latest_schema_change_job(self, relation: BaseRelation):
        schema = self.quote(relation.schema)
        table_name = relation.identifier.replace("'", "''")
        _, table = self.execute(
            "show alter table column from {} "
            "where TableName = '{}' "
            "order by JobId desc limit 1".format(schema, table_name),
            auto_begin=False,
            fetch=True,
        )
        if len(table.rows) == 0:
            return None

        row = table.rows[0]
        return {
            "job_id": str(row[0]),
            "state": str(row[9]).upper(),
            "message": str(row[10] or ""),
        }

    @available
    def get_latest_schema_change_job_id(self, relation: BaseRelation):
        """Return the newest column-alter job id for a Doris table."""
        job = self._latest_schema_change_job(relation)
        return None if job is None else job["job_id"]

    @available
    def wait_for_schema_change(
        self,
        relation: BaseRelation,
        previous_job_id=None,
        timeout_seconds: int = 300,
    ):
        """Wait for the column-alter job started by the preceding DDL."""
        deadline = time.monotonic() + timeout_seconds

        while True:
            job = self._latest_schema_change_job(relation)
            if job is None or job["job_id"] == previous_job_id:
                if time.monotonic() >= deadline:
                    raise dbt.exceptions.DbtRuntimeError(
                        "Timed out after {} seconds waiting for a new Doris "
                        "schema change job on {}".format(
                            timeout_seconds,
                            relation,
                        )
                    )
                time.sleep(0.2)
                continue
            if job["state"] == "FINISHED":
                return
            if job["state"] == "CANCELLED":
                raise dbt.exceptions.DbtRuntimeError(
                    "Doris schema change job {} for {} was cancelled: {}".format(
                        job["job_id"],
                        relation,
                        job["message"],
                    )
                )
            if time.monotonic() >= deadline:
                raise dbt.exceptions.DbtRuntimeError(
                    "Timed out after {} seconds waiting for Doris schema "
                    "change job {} on {} (state: {})".format(
                        timeout_seconds,
                        job["job_id"],
                        relation,
                        job["state"],
                    )
                )
            time.sleep(0.2)

    @available
    def materialized_view_adapter_response(
            self,
            action: str,
            relation: BaseRelation,
            refresh_task: Optional[Dict[str, Any]] = None,
    ) -> DorisMaterializedViewAdapterResponse:
        """Build the structured adapter response stored in run_results.json."""
        codes = {
            "create": "CREATE MATERIALIZED VIEW",
            "replace": "REPLACE MATERIALIZED VIEW",
            "replace_type": "CREATE MATERIALIZED VIEW",
            "refresh": "REFRESH MATERIALIZED VIEW",
            "skip": "skip",
            "continue": "skip",
        }
        code = codes[action]
        message = (
            f"skip {relation}"
            if code == "skip"
            else f"{code} {relation}"
        )
        task_id = None
        task_status = None
        task_error = None
        query_id = None
        if refresh_task is not None:
            task_id = str(refresh_task["task_id"])
            task_status = str(refresh_task["status"])
            task_error = str(refresh_task.get("error_message") or "") or None
            query_id = str(refresh_task.get("last_query_id") or "") or None
            message += f"; refresh task {task_id} {task_status}"
            if query_id is not None:
                message += f", query {query_id}"

        return DorisMaterializedViewAdapterResponse(
            _message=message,
            code=code,
            rows_affected=-1,
            query_id=query_id,
            task_id=task_id,
            task_status=task_status,
            task_error=task_error,
        )

    @available
    def validate_materialized_view_version(
            self, frontends_table: agate.Table
    ) -> None:
        _validate_doris_materialized_view_version(frontends_table)

    @classmethod
    def date_function(cls) -> str:
        return "current_date()"

    @classmethod
    def convert_datetime_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "datetime"

    @classmethod
    def convert_text_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "string"

    @classmethod
    def quote(cls, identifier):
        return "`{}`".format(identifier)

    def check_schema_exists(self, database, schema):
        results = self.execute_macro(LIST_SCHEMAS_MACRO_NAME, kwargs={"database": database})

        exists = True if schema in [row[0] for row in results] else False
        return exists

    def get_relation(self, database: Optional[str], schema: str, identifier: str):
        return super().get_relation(None, schema, identifier)

    def drop_schema(self, relation: BaseRelation):
        relations = self.list_relations(
            database=relation.database,
            schema=relation.schema
        )
        for relation in relations:
            self.drop_relation(relation)
        super().drop_schema(relation)

    def list_relations_without_caching(self, schema_relation: DorisRelation) -> List[DorisRelation]:
        if not self.check_schema_exists(
            schema_relation.database,
            schema_relation.schema,
        ):
            return []

        kwargs = {"schema_relation": schema_relation}
        results = self.execute_macro(LIST_RELATIONS_MACRO_NAME, kwargs=kwargs)

        relations = []
        for row in results:
            if len(row) != 4:
                raise dbt.exceptions.DbtRuntimeError(
                    f"Invalid value from 'show table extended ...', "
                    f"got {len(row)} values, expected 4"
                )
            _database, name, schema, type_info = row
            normalized_type = type_info.lower()
            if normalized_type == RelationType.MaterializedView.value:
                rel_type = RelationType.MaterializedView
            elif normalized_type == RelationType.View.value:
                rel_type = RelationType.View
            else:
                rel_type = RelationType.Table
            relation = self.Relation.create(
                database=None,
                schema=schema,
                identifier=name,
                type=rel_type,
            )
            relations.append(relation)

        return relations

    @classmethod
    def _catalog_filter_table(
            cls, table: agate.Table, used_schemas: FrozenSet[Tuple[str, str]]
    ) -> agate.Table:
        table = table_from_rows(
            table.rows,
            table.column_names,
            text_only_columns=[
                "table_database",
                "table_schema",
                "table_name",
                "table_type",
                "table_comment",
                "table_owner",
                "column_name",
                "column_type",
                "column_comment",
            ],
        )
        return table.where(cls._catalog_filter_schemas(used_schemas))

    @staticmethod
    def _catalog_filter_schemas(
            used_schemas: FrozenSet[Tuple[str, str]]
    ):
        schemas = frozenset(((d or ""), s.lower()) for d, s in used_schemas)

        def predicate(row: agate.Row) -> bool:
            table_database = row.get("table_database") or ""
            table_schema = row.get("table_schema")
            if table_schema is None:
                return False
            return (table_database, table_schema.lower()) in schemas

        return predicate

    def get_filtered_catalog(self, relation_configs, used_schemas, relations=None):
        """Match dbt's empty database name to Doris' single namespace.

        ``DorisRelation`` normalizes database to ``None`` because Doris has no
        catalog level between a connection and a database/schema. Manifest
        nodes, however, carry ``database=''``. dbt Core's selected-relation
        filter treats those as different keys and removes every catalog row.
        Apply the same filter with both representations normalized to the empty
        string, which is also the value returned by ``doris__get_catalog``.
        """
        catalogs, exceptions = super().get_filtered_catalog(
            relation_configs,
            used_schemas,
            relations=None,
        )
        if relations and catalogs:
            relation_map = {
                (
                    (relation.database or "").casefold(),
                    relation.schema.casefold() if relation.schema else None,
                    relation.identifier.casefold() if relation.identifier else None,
                )
                for relation in relations
            }

            def in_map(row):
                database = (row.get("table_database") or "").casefold()
                schema = row.get("table_schema")
                identifier = row.get("table_name")
                schema = schema.casefold() if schema else None
                identifier = identifier.casefold() if identifier else None
                return (database, schema, identifier) in relation_map

            catalogs = catalogs.where(in_map)

        return catalogs, exceptions

    @classmethod
    def convert_number_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        decimals = agate_table.aggregate(agate.HasNulls(col_idx))
        return "double" if decimals else "bigint"

    @classmethod
    def convert_boolean_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "boolean"

    def quote_seed_column(self, column: str, quote_config: Optional[bool]) -> str:
        if quote_config is None or quote_config:
            return self.quote(column)
        return column

    # Methods used in adapter tests
    def timestamp_add_sql(self, add_to: str, number: int = 1, interval: str = "hour") -> str:
        # for backwards compatibility, we're compelled to set some sort of
        # default. A lot of searching has lead me to believe that the
        # '+ interval' syntax used in postgres/redshift is relatively common
        # and might even be the SQL standard's intention.
        return f"{add_to} + interval {number} {interval}"

    @classmethod
    def render_raw_columns_constraints(cls, raw_columns: Dict[str, Dict[str, Any]]) -> List:
        rendered_column_constraints = []
        for v in raw_columns.values():
            # DorisColumnItem quotes identifiers when it renders SQL. Passing an
            # already quoted name for `quote: true` produced invalid double
            # backticks such as ``order`` in contracted model projections.
            cols_name = v["name"]
            data_type = v.get('data_type')
            comment = v.get('description')

            column = DorisColumnItem(cols_name, data_type, comment, "")
            rendered_column_constraints.append(column)

        return rendered_column_constraints
