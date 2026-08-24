DROP DATABASE IF EXISTS dbt_demo_snapshot;
DROP DATABASE IF EXISTS dbt_demo_snapshot_history;
CREATE DATABASE IF NOT EXISTS dbt_demo_snapshot_source;
CREATE DATABASE IF NOT EXISTS dbt_demo_snapshot;
CREATE DATABASE IF NOT EXISTS dbt_demo_snapshot_history;
DROP TABLE IF EXISTS dbt_demo_snapshot_source.CUSTOMERS;
DROP TABLE IF EXISTS dbt_demo_snapshot_history.customer_snapshot;
CREATE TABLE dbt_demo_snapshot_source.CUSTOMERS (
    customer_id BIGINT, customer_number VARCHAR(32), customer_type VARCHAR(32),
    email VARCHAR(128), email_verified TINYINT, phone_primary VARCHAR(32),
    phone_verified TINYINT, first_name VARCHAR(64), last_name VARCHAR(64),
    company_name VARCHAR(128), acquisition_source VARCHAR(64), acquisition_campaign VARCHAR(64)
) UNIQUE KEY(customer_id) DISTRIBUTED BY HASH(customer_id) BUCKETS 1 PROPERTIES('replication_num'='1');
INSERT INTO dbt_demo_snapshot_source.CUSTOMERS VALUES
    (1,'C001','INDIVIDUAL','alice@example.com',1,'13800000001',1,'Alice','Zhang',NULL,'web','summer'),
    (2,'C002','BUSINESS','bob@example.com',1,'13800000002',1,'Bob','Li','Bob Co','partner','enterprise');
