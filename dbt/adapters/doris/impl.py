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

from enum import Enum
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Tuple,
)

import agate
import dbt.exceptions
from dbt.adapters.base.relation import BaseRelation
from dbt.adapters.doris.column import DorisColumn
from dbt.adapters.doris.connections import DorisConnectionManager
from dbt.adapters.doris.relation import DorisRelation
from dbt.adapters.protocol import AdapterConfig
from dbt.adapters.contracts.relation import RelationType
from dbt.adapters.sql.impl import LIST_RELATIONS_MACRO_NAME, LIST_SCHEMAS_MACRO_NAME
from dbt_common.clients.agate_helper import table_from_rows
from dbt.adapters.doris.doris_column_item import DorisColumnItem


class Engine(str, Enum):
    olap = "olap"
    mysql = "mysql"
    elasticsearch = "elasticsearch"
    hive = "hive"
    iceberg = "iceberg"


class PartitionType(str, Enum):
    list = "LIST"
    range = "RANGE"


class DorisConfig(AdapterConfig):
    engine: Engine
    duplicate_key: Tuple[str]
    partition_by: Tuple[str]
    partition_type: PartitionType
    partition_by_init: List[str]
    distributed_by: Tuple[str]
    buckets: int
    properties: Dict[str, str]


class DorisAdapter(SQLAdapter):
    ConnectionManager = DorisConnectionManager
    Relation = DorisRelation
    AdapterSpecificConfigs = DorisConfig
    Column = DorisColumn

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
