{{ config(materialized='table') }}

select channel_id, count(*) as order_count,
    round(avg(datediff(created_at, cast(order_date as datetime))), 2) as avg_latency_days,
    max(datediff(created_at, cast(order_date as datetime))) as max_latency_days
from {{ ref('incremental_daily_sales') }}
group by channel_id
