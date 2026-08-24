{{ config(materialized='table') }}

with unioned as (
    select 'google' as source, * from {{ ref('stg__ads_googleads') }}
    union all
    select 'meta' as source, * from {{ ref('stg__ads_metaads') }}
    union all
    select 'tiktok' as source, * from {{ ref('stg__ads_tiktokads') }}
)
select * from unioned
