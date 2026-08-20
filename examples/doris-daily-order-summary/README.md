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

- 已启动 Doris FE，MySQL 端口可访问；
- 已安装当前 checkout 的 adapter：从仓库根目录执行 `python -m pip install -e .`；
- 可使用 `mysql` 客户端连接 Doris。

## 运行

默认连接 `127.0.0.1:9030`。如果 Doris FE 使用 19030 端口：

```bash
cd examples/doris-daily-order-summary
DORIS_PORT=19030 ./scripts/run.sh
```

可以用 `DBT_BIN` 指向特定的 Python dbt CLI，例如：

```bash
DORIS_PORT=19030 DBT_BIN=/path/to/venv/bin/dbt ./scripts/run.sh
```

脚本会依次初始化 fixture、执行 `dbt debug`、创建 Table 和四项 data test、创建 MV、
再次选中 MV 提交刷新，然后校验数据、Doris DDL 和最近的 MV Task 状态。

## 一键运行五个发布 Demo

从仓库根目录执行下面两条命令，可以创建固定为 Python 3.12.13、dbt Core 1.12.2
和 dbt-for-apache-doris 1.1.0 的隔离环境，然后依次运行每日汇总、地域分析、广告合并、
迟到订单增量和 Snapshot 五个 Demo：

```bash
examples/doris-demos/scripts/prepare-python-env.sh
DORIS_PORT=19030 \
  examples/doris-demos/scripts/run-all.sh
```

每次执行的环境信息、逐 Demo 日志和 `summary.tsv` 都保存在
`examples/doris-demos/artifacts/<run-id>/`。如果要使用其他结果目录，设置
`DEMO_RESULTS_DIR`。

预期每日结果：

| order_date | order_count | total_revenue |
| --- | ---: | ---: |
| 2026-08-01 | 1 | 100.00 |
| 2026-08-02 | 1 | 80.00 |
| 2026-08-03 | 1 | 40.20 |
