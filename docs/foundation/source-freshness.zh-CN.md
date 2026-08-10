# Doris Source Freshness

Source Freshness 用源表中最新一条数据的时间，判断上游数据是否按预期到达。它不会修改
源表，只执行聚合查询，并把结果写入 `target/sources.json`。

## 使用 `loaded_at_field`

在 Properties YAML 中为 Source 配置时间列和阈值：

```yaml
version: 2

sources:
  - name: raw
    schema: raw
    tables:
      - name: events
        config:
          loaded_at_field: loaded_at
          freshness:
            warn_after:
              count: 1
              period: hour
            error_after:
              count: 2
              period: hour
```

执行检查：

```bash
dbt source freshness
dbt source freshness --select source:raw.events
```

dbt 计算 `max(loaded_at)` 与检查时刻的差值：小于 1 小时为 `pass`，1～2 小时为
`warn`，超过 2 小时为 `error`。`period` 支持 `minute`、`hour` 和 `day`。

## 过滤无效数据

`filter` 会直接加入获取最大时间的 Doris 查询。例如，只检查已经完成摄取的数据：

```yaml
config:
  loaded_at_field: loaded_at
  freshness:
    filter: ingestion_status = 'complete'
    warn_after: {count: 30, period: minute}
    error_after: {count: 1, period: hour}
```

## 使用 `loaded_at_query`

当最新时间需要关联、过滤或从其他元数据推导时，可以提供返回单个时间值、单行单列的
查询。`this` 表示当前 Source Relation：

```yaml
config:
  loaded_at_query: |
    select max(loaded_at)
    from {{ this }}
    where ingestion_status = 'complete'
  freshness:
    warn_after: {count: 30, period: minute}
    error_after: {count: 1, period: hour}
```

同一个 Source 不能同时配置 `loaded_at_field` 和 `loaded_at_query`。

## UTC 约定

dbt Core 把数据库驱动返回的无时区 `DATETIME` 当作 UTC。dbt-doris 使用 Doris
`utc_timestamp()` 记录检查时刻，因此检查结果不受 FE Session `time_zone` 影响。

`loaded_at_field` 或 `loaded_at_query` 也应返回 UTC 时间。如果源表保存的是本地时间，
应在表达式或查询中明确转换，例如：

```yaml
config:
  loaded_at_field: convert_tz(loaded_at, 'Asia/Shanghai', '+00:00')
```

这样 `age`、`max_loaded_at` 和 `snapshotted_at` 在本机、CI 与不同时区的 Doris 集群上
都具有一致含义。
