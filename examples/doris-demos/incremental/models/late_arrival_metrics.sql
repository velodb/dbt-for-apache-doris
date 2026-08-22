{{ config(materialized='table') }}

select order_date, count(*) as order_count,
    max(datediff(created_at, cast(order_date as datetime))) as max_arrival_latency_days
from {{ ref('incremental_daily_sales') }}
group by order_date
