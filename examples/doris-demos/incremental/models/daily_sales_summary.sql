{{ config(materialized='table') }}

select
    s.order_date, count(*) as order_count, round(sum(s.grand_total), 2) as total_revenue,
    sum(case when q.is_valid_amount = 1 and q.is_valid_customer = 1 and q.is_valid_arrival = 1 then 1 else 0 end) as valid_order_count,
    max(m.max_arrival_latency_days) as max_arrival_latency_days
from {{ ref('incremental_daily_sales') }} s
join {{ ref('order_data_quality') }} q on s.order_id = q.order_id
left join {{ ref('late_arrival_metrics') }} m on s.order_date = m.order_date
group by s.order_date
