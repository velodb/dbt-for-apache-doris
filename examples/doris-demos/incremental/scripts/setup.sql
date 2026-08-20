DROP DATABASE IF EXISTS dbt_demo_incremental;
CREATE DATABASE IF NOT EXISTS dbt_demo_incremental_source;
CREATE DATABASE IF NOT EXISTS dbt_demo_incremental;
DROP TABLE IF EXISTS dbt_demo_incremental_source.ORDERS;
CREATE TABLE dbt_demo_incremental_source.ORDERS (
    event_id BIGINT, order_id BIGINT, customer_id BIGINT, channel_id VARCHAR(32),
    grand_total DECIMAL(18,2), status VARCHAR(32), ordered_at DATETIME, created_at DATETIME
) DUPLICATE KEY(event_id) DISTRIBUTED BY HASH(event_id) BUCKETS 1 PROPERTIES('replication_num'='1');
INSERT INTO dbt_demo_incremental_source.ORDERS VALUES
    (1,101,1,'web',100.00,'COMPLETED','2026-08-01 09:00:00','2026-08-01 09:00:00'),
    (2,102,2,'store',50.00,'COMPLETED','2026-08-01 10:00:00','2026-08-02 08:00:00'),
    (3,103,1,'web',80.00,'SHIPPED','2026-08-02 11:00:00','2026-08-03 09:00:00');
