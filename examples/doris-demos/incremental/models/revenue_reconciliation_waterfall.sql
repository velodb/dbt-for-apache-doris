{{ config(materialized='table') }}

select order_date,
    sum(case when datediff(created_at, cast(order_date as datetime)) <= 1 then grand_total else 0 end) as on_time_revenue,
    sum(case when datediff(created_at, cast(order_date as datetime)) > 1 then grand_total else 0 end) as late_revenue
from {{ ref('incremental_daily_sales') }}
group by order_date
