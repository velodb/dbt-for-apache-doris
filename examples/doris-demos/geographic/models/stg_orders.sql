{{ config(materialized='view') }}

select order_id, customer_id, coalesce(grand_total, 0) as grand_total
from {{ source('orders_schema', 'ORDERS') }}
where upper(status) in ('COMPLETED', 'DELIVERED', 'SHIPPED')
