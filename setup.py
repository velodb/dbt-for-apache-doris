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

from pathlib import Path
from runpy import run_path

from setuptools import find_namespace_packages, setup

package_name = "dbt-for-apache-doris"
version_file = Path(__file__).parent / "dbt" / "adapters" / "doris" / "__version__.py"
package_version = run_path(str(version_file))["version"]
dbt_core_version = "1.12.0"
description = "A dbt adapter for VeloDB and Apache Doris"
repository_url = "https://github.com/velodb/dbt-for-apache-doris"
long_description = Path("README.md").read_text(encoding="utf-8")

setup(
    name=package_name,
    version=package_version,
    description=description,
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Apache Doris contributors",
    maintainer="VeloDB contributors",
    url=repository_url,
    project_urls={
        "Documentation": repository_url + "#readme",
        "Issues": repository_url + "/issues",
        "Source": repository_url,
        "Upstream": "https://github.com/apache/doris/tree/master/extension/dbt-doris",
    },
    packages=find_namespace_packages(include=["dbt", "dbt.*"]),
    include_package_data=True,
    license="Apache-2.0",
    license_files=("LICENSE", "NOTICE"),
    install_requires=[
        "dbt-core~={}".format(dbt_core_version),
        "mysql-connector-python>=8.0.33",
    ],
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    ],
)
