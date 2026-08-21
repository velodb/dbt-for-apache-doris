# Doris 每日订单汇总 Demo

这个示例用一张订单源表创建每日订单汇总 Table，并在其上创建一个 Doris 异步物化视图。

执行后会创建：

```text
dbt_demo_daily_source.orders                 源订单表
dbt_demo_daily.daily_order_summary           dbt Table model
dbt_demo_daily.monthly_order_summary_mv      Doris Async Materialized View
```

`scripts/setup.sql` 只会删除和重建 `dbt_demo_daily`、`dbt_demo_daily_source` 两个专用
Demo database，请勿将它们用于业务数据。

## 前提

- 已安装 `uv` 和 MySQL 兼容客户端；
- 已有可连接的 Doris FE 和至少一个健康的 BE，或者使用官方 all-in-one Docker
  image；
- Doris 用户可以创建和修改本 Demo 使用的 `dbt_demo_daily*` database。

完整的环境要求、all-in-one Docker 启动命令以及 JupyterLab 使用方法见
[Doris dbt demos Quick Start](../doris-demos/README.md#prerequisites)。

## 运行

从仓库根目录准备固定版本的 dbt 环境。该脚本会安装 dbt Core 和
`dbt-for-apache-doris`，无需手工执行 editable install：

```bash
examples/doris-demos/scripts/prepare-python-env.sh
```

已有 Doris 集群默认连接 `root@127.0.0.1:9030`。运行每日订单汇总 Demo：

```bash
examples/doris-daily-order-summary/scripts/run.sh
```

使用中央 Quick Start 中的 all-in-one Docker image 时，FE 映射到宿主机
`29030` 端口：

```bash
DORIS_PORT=29030 \
  examples/doris-daily-order-summary/scripts/run.sh
```

通过 `DORIS_HOST`、`DORIS_PORT`、`DORIS_USER` 和 `DORIS_PASSWORD` 可以连接
其他 Doris 环境。脚本默认使用中央准备步骤创建的 dbt CLI；`DBT_BIN` 也可以
指向自定义的 dbt CLI。

脚本会依次初始化 fixture、执行 `dbt debug`、创建 Table 和四项 data test、创建 MV、
再次选中 MV 提交刷新，然后校验数据、Doris DDL 和最近的 MV Task 状态。

## 一键运行五个发布 Demo

从仓库根目录执行下面两条命令，可以创建固定为 Python 3.12.13、dbt Core 1.12.2
和 dbt-for-apache-doris 1.1.0 的隔离环境，然后依次运行每日汇总、地域分析、广告合并、
迟到订单增量和 Snapshot 五个 Demo：

```bash
examples/doris-demos/scripts/prepare-python-env.sh
examples/doris-demos/scripts/run-all.sh
```

如果 all-in-one Docker image 使用宿主机端口 `29030`，在第二条命令前增加
`DORIS_PORT=29030`。

每次执行的环境信息、逐 Demo 日志和 `summary.tsv` 都保存在
`examples/doris-demos/artifacts/<run-id>/`。如果要使用其他结果目录，设置
`DEMO_RESULTS_DIR`。

预期每日结果：

| order_date | order_count | total_revenue |
| --- | ---: | ---: |
| 2026-08-01 | 1 | 100.00 |
| 2026-08-02 | 1 | 80.00 |
| 2026-08-03 | 1 | 40.20 |
