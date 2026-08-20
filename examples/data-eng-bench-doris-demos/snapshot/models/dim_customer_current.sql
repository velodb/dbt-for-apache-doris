{{ config(materialized='table', distributed_by=['customer_id'], buckets=1) }}

with current_records as (
    select * from {{ ref('customer_snapshot') }} where dbt_valid_to is null
),
version_counts as (
    select customer_id, count(*) as total_versions
    from {{ ref('customer_snapshot') }}
    group by customer_id
)
select
    c.customer_id, c.customer_number, c.customer_type, c.email, c.email_verified,
    c.phone_primary, c.phone_verified, c.first_name, c.last_name, c.company_name,
    c.acquisition_source, c.acquisition_campaign, c.dbt_valid_from,
    v.total_versions,
    case when v.total_versions > 1 then 1 else 0 end as has_history
from current_records c
join version_counts v using (customer_id)
