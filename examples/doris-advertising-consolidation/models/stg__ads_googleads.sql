with source as (
    select ad_date, clicks, impressions, views, conversions from {{ ref('googleads') }}
)
select * from source
where ad_date is not null
qualify row_number() over (
    partition by ad_date, clicks, impressions, views, conversions order by ad_date
) = 1
