# Doris dbt demos

这组示例把四类常见 dbt 数据工程行为实现成可独立运行的 Doris 项目：

| 目录 | 演示内容 | 主要 dbt 对象 |
| --- | --- | --- |
| `geographic` | 客户地址与订单关联，按州汇总 | Source、View、Table、`ref()`、Data Test |
| `consolidate` | 三个广告平台 CSV 去重后合并 | Seed、`dbt_utils`、`QUALIFY`、Table |
| `incremental` | 迟到数据和订单版本更新 | Window、Incremental `merge`、Unique Key、Data Test |
| `snapshot` | 客户属性变更和删除的历史追踪 | Snapshot、SCD Type 2、当前维表 |

每个目录都有 `scripts/setup.sql`、`scripts/run.sh` 和 `scripts/verify.sh`。脚本使用专用
Doris database，不依赖其他 demo 的表。

## 前提

- Doris FE 的 MySQL 端口可访问，默认 `127.0.0.1:9030`；
- 如需使用仓库提供的固定 Python/dbt 环境脚本，需先安装 `uv`；
- 已安装 `mysql` 客户端；
- `DBT_BIN` 指向 dbt Core CLI。需要换端口时设置 `DORIS_PORT`。

例如 Doris FE 使用 19030 端口：

```bash
export DBT_BIN=/path/to/dbt
export DORIS_PORT=19030
cd examples/doris-demos
./geographic/scripts/run.sh
```

四个 demo 的 `run.sh` 都会重建自己名字空间中的 fixture、执行 dbt，并在最后调用
`verify.sh` 检查 Doris 结果，因此可以按任意顺序独立执行。

如需使用仓库提供的固定版本环境，从仓库根目录执行：

```bash
examples/doris-demos/scripts/prepare-python-env.sh
export DBT_BIN="$PWD/examples/doris-demos/.venv/bin/dbt"
```

该脚本固定 Python 3.12.13、dbt Core 1.12.2 和 `dbt-for-apache-doris` 1.1.0。

## 运行 Jupyter Notebook

[`dbt-for-apache-doris-demos.ipynb`](dbt-for-apache-doris-demos.ipynb)
把每日订单 Demo 和本目录中的四个 Demo 组织成 5 个可依次执行的分步流程。

不要把示例中的 `/path/to/dbt` 原样设置为 `DBT_BIN`。先在仓库根目录创建固定版本环境，
并安装 JupyterLab：

```bash
INSTALL_JUPYTER=1 examples/doris-demos/scripts/prepare-python-env.sh
source examples/doris-demos/.venv/bin/activate

export DBT_BIN="$PWD/examples/doris-demos/.venv/bin/dbt"
export DORIS_HOST=127.0.0.1
export DORIS_PORT=9030
export JUPYTER_PORT=18888

examples/doris-demos/scripts/start-notebook.sh
```

启动脚本会启动 JupyterLab 并输出带 token 的访问地址，在浏览器中打开该地址即可。
如果已有可用的 `jupyter-lab`，也可以直接使用它打开本 Notebook；只需确保当前环境中的
`DBT_BIN`、`DORIS_HOST`、`DORIS_PORT` 和 `mysql` 客户端配置正确。

打开后先执行环境检查。5 个 Demo 都被拆成有前后依赖的单元格，分别展示 Source/Seed、staging、
业务 Model、Data Test、Incremental/Snapshot 数据变化和 verifier；每一步都能看到使用的 dbt 文件、
Doris 输入、中间结果或最终结果。每个 Demo 必须按章节编号顺序执行，也可以直接使用 Jupyter 的
**Run All**。完整 dbt 日志默认折叠在“查看完整运行日志”中，需要排错时再展开。

Notebook 更新后，刷新页面并重新执行第一个“检查执行环境”单元格。该单元格会从仓库路径
重新加载展示 helper，不需要重启 Jupyter 服务或 Kernel。
