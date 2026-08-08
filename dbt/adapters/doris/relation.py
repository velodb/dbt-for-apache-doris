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

from dataclasses import dataclass, field
from datetime import timezone

from dbt.adapters.base.relation import BaseRelation, EventTimeFilter, Policy
from dbt.exceptions import DbtRuntimeError


@dataclass
class DorisQuotePolicy(Policy):
    database: bool = False
    schema: bool = True
    identifier: bool = True


@dataclass
class DorisIncludePolicy(Policy):
    database: bool = False
    schema: bool = True
    identifier: bool = True


@dataclass(frozen=True, eq=False, repr=False)
class DorisRelation(BaseRelation):
    quote_policy: DorisQuotePolicy = field(default_factory=lambda: DorisQuotePolicy())
    include_policy: DorisIncludePolicy = field(default_factory=lambda: DorisIncludePolicy())
    quote_character: str = "`"

    def __post_init__(self):
        # In Doris, database and schema are the same concept — there is only
        # one namespace level.  When a source or model sets "database" to a
        # value that differs from "schema", treat database AS the schema so
        # that cross-database references like {{ source(...) }} work correctly.
        if self.database and self.database != self.schema:
            self.path.schema = self.database
        # Normalize dbt's empty-string model database and metadata's NULL
        # database to one cache namespace.
        self.path.database = None

    def render(self):
        if self.include_policy.database and self.include_policy.schema:
            raise DbtRuntimeError(
                "Got a Doris relation with schema and database set to include, but only one can be set"
            )
        return super().render()

    @staticmethod
    def _format_event_time_boundary(boundary):
        """Render dbt's UTC boundary for Doris's timezone-naive DATETIME."""
        if boundary.tzinfo is not None:
            boundary = boundary.astimezone(timezone.utc).replace(tzinfo=None)
        return boundary.strftime("%Y-%m-%d %H:%M:%S.%f").rstrip("0").rstrip(".")

    def _render_event_time_filtered(
        self,
        event_time_filter: EventTimeFilter,
    ) -> str:
        filters = []
        if event_time_filter.start:
            start = self._format_event_time_boundary(event_time_filter.start)
            filters.append(f"{event_time_filter.field_name} >= '{start}'")
        if event_time_filter.end:
            end = self._format_event_time_boundary(event_time_filter.end)
            filters.append(f"{event_time_filter.field_name} < '{end}'")
        return " and ".join(filters)
