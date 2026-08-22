{{
  config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual',
    refresh_on_run=true,
    duplicate_key=['order_month'],
    distributed_by=['order_month'],
    buckets=1,
    properties={'replication_num': '1'}
  )
}}

select
    date_trunc(order_date, 'month') as order_month,
    sum(order_count) as order_count,
    round(sum(total_revenue), 2) as total_revenue
from {{ ref('daily_order_summary') }}
group by date_trunc(order_date, 'month')
