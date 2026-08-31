DROP DATABASE IF EXISTS dbt_demo_daily;
DROP DATABASE IF EXISTS dbt_demo_daily_source;

CREATE DATABASE dbt_demo_daily_source;
CREATE DATABASE dbt_demo_daily;

CREATE TABLE dbt_demo_daily_source.orders (
    order_id BIGINT,
    ordered_at DATETIME,
    grand_total DECIMAL(18, 2),
    status VARCHAR(32)
)
DUPLICATE KEY(order_id)
DISTRIBUTED BY HASH(order_id) BUCKETS 1
PROPERTIES ('replication_num' = '1');

INSERT INTO dbt_demo_daily_source.orders VALUES
    (1, '2026-08-01 09:00:00', 100.00, 'COMPLETED'),
    (2, '2026-08-01 10:00:00', 30.00, 'CANCELLED'),
    (3, '2026-08-02 11:00:00', 80.00, 'SHIPPED'),
    (4, '2026-08-02 12:00:00', 20.00, 'RETURNED'),
    (5, '2026-08-03 13:00:00', 25.55, 'FAILED'),
    (6, '2026-08-03 14:00:00', 40.20, 'DELIVERED');
