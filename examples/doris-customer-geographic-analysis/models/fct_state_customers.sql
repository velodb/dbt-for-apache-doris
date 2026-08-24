{{ config(materialized='table') }}

select
    ca.state_province,
    count(distinct ca.customer_id) as customer_count,
    count(distinct o.order_id) as order_count,
    round(coalesce(sum(o.grand_total), 0), 2) as total_revenue,
    round(coalesce(avg(o.grand_total), 0), 2) as avg_order_value,
    round(coalesce(sum(o.grand_total), 0) / nullif(count(distinct ca.customer_id), 0), 2) as revenue_per_customer,
    round(cast(count(distinct o.order_id) as double) / nullif(count(distinct ca.customer_id), 0), 2) as orders_per_customer
from {{ ref('stg_customer_addresses') }} ca
left join {{ ref('stg_orders') }} o on ca.customer_id = o.customer_id
group by ca.state_province
