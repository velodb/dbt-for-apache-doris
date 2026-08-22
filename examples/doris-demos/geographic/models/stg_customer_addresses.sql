{{ config(materialized='view') }}

select address_id, customer_id, state_province
from {{ source('customer_schema', 'CUSTOMER_ADDRESSES') }}
where is_default_shipping = 1
  and state_province is not null
