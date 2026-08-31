{{ config(materialized='table') }}

select order_id,
    case when grand_total < 0 then 0 else 1 end as is_valid_amount,
    case when customer_id is null then 0 else 1 end as is_valid_customer,
    case when created_at < cast(order_date as datetime) then 0 else 1 end as is_valid_arrival
from {{ ref('incremental_daily_sales') }}
