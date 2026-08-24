{{ config(materialized='table') }}

with versioned as (
    select
        order_id, event_id, customer_id, channel_id, grand_total, status,
        ordered_at, created_at,
        row_number() over (partition by order_id order by created_at desc, event_id desc) as version_rank,
        row_number() over (partition by order_id order by created_at asc, event_id asc) as version_num,
        lead(created_at) over (partition by order_id order by created_at asc, event_id asc) as next_created_at
    from {{ source('orders', 'ORDERS') }}
)
select
    order_id, event_id, customer_id, channel_id, grand_total, status, ordered_at,
    created_at as valid_from, next_created_at as valid_to, version_num,
    case when version_rank = 1 then 1 else 0 end as is_current
from versioned
