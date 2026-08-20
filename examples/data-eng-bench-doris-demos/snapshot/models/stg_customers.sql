{{ config(materialized='view') }}

select
    customer_id, customer_number, customer_type, email, email_verified,
    phone_primary, phone_verified, first_name, last_name, company_name,
    acquisition_source, acquisition_campaign
from {{ source('customer_source', 'CUSTOMERS') }}
