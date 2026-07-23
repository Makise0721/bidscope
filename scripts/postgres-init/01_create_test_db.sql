-- Create the dedicated test databases on first container start.
-- The postgres image executes scripts in /docker-entrypoint-initdb.d/ once, when
-- the data directory is initialized. Running as the postgres superuser lets us
-- CREATE DATABASE before any application connection.
SELECT 'CREATE DATABASE bidscope_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'bidscope_test')\gexec

SELECT 'CREATE DATABASE bidscope_e2e'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'bidscope_e2e')\gexec
