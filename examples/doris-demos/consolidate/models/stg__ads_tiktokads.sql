with source as (
    select ad_date, clicks, impressions, views_1 as views, conversions from {{ ref('tiktokads') }}
)
select * from source
where ad_date is not null
qualify row_number() over (
    partition by ad_date, clicks, impressions, views, conversions order by ad_date
) = 1
