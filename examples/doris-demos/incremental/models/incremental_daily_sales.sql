{{ config(
    materialized='incremental',
    unique_key='order_id',
    incremental_strategy='merge',
    on_schema_change='append_new_columns',
    distributed_by=['order_id'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select
    h.order_id,
    cast(h.ordered_at as date) as order_date,
    h.customer_id,
    h.channel_id,
    h.grand_total,
    h.status,
    h.valid_from as created_at,
    h.version_num
from {{ ref('order_version_history') }} h
where h.is_current = 1
{% if is_incremental() %}
  and h.valid_from >= (select coalesce(max(created_at), cast('1900-01-01' as datetime)) from {{ this }})
{% endif %}
