DROP DATABASE IF EXISTS dbt_demo_geographic;
CREATE DATABASE IF NOT EXISTS dbt_demo_geographic_customer;
CREATE DATABASE IF NOT EXISTS dbt_demo_geographic_orders;
CREATE DATABASE IF NOT EXISTS dbt_demo_geographic;
DROP TABLE IF EXISTS dbt_demo_geographic_customer.CUSTOMER_ADDRESSES;
DROP TABLE IF EXISTS dbt_demo_geographic_orders.ORDERS;
CREATE TABLE dbt_demo_geographic_customer.CUSTOMER_ADDRESSES (
    address_id BIGINT, customer_id BIGINT, state_province VARCHAR(32), is_default_shipping TINYINT
) DUPLICATE KEY(address_id) DISTRIBUTED BY HASH(address_id) BUCKETS 1 PROPERTIES('replication_num'='1');
CREATE TABLE dbt_demo_geographic_orders.ORDERS (
    order_id BIGINT, customer_id BIGINT, grand_total DECIMAL(18,2), status VARCHAR(32)
) DUPLICATE KEY(order_id) DISTRIBUTED BY HASH(order_id) BUCKETS 1 PROPERTIES('replication_num'='1');
INSERT INTO dbt_demo_geographic_customer.CUSTOMER_ADDRESSES VALUES
    (1, 101, 'CA', 1), (2, 102, 'CA', 1), (3, 103, 'NY', 1), (4, 104, 'TX', 0);
INSERT INTO dbt_demo_geographic_orders.ORDERS VALUES
    (1001, 101, 100.00, 'COMPLETED'), (1002, 102, 45.00, 'SHIPPED'),
    (1003, 103, 50.00, 'DELIVERED'), (1004, 104, 70.00, 'CANCELLED');
