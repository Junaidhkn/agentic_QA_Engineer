-- Creates the three runtime roles and the database (ADR-0003). Run once as a superuser.
-- Passwords here are for local development only; production roles come from Terraform + Secrets Manager.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 're_owner') THEN
    CREATE ROLE re_owner LOGIN PASSWORD 're_owner';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 're_app') THEN
    CREATE ROLE re_app LOGIN PASSWORD 're_app' NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 're_system') THEN
    CREATE ROLE re_system LOGIN PASSWORD 're_system' BYPASSRLS;
  END IF;
END
$$;
SELECT 'CREATE DATABASE reliability OWNER re_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'reliability')\gexec
