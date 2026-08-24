{% snapshot customer_snapshot %}

{{
    config(
        target_database='dbt_demo_snapshot_history',
        target_schema='dbt_demo_snapshot_history',
        unique_key='customer_id',
        distributed_by=['customer_id'],
        buckets=1,
        replication_num='1',
        strategy='check',
        check_cols=['email', 'customer_type', 'phone_primary', 'first_name', 'last_name', 'company_name'],
        invalidate_hard_deletes=True
    )
}}

select
    customer_id, customer_number, customer_type, email, email_verified,
    phone_primary, phone_verified, first_name, last_name, company_name,
    acquisition_source, acquisition_campaign
from {{ ref('stg_customers') }}

{% endsnapshot %}
