-- The dashboard's read-only role. Idempotent, and correct whether it runs before or after dbt has
-- built anything.
--
-- WHY A ROLE AND NOT A CONVENTION. The dashboard's claim is that every number on screen comes from
-- a warehouse mart and the app recomputes nothing. Two static guards check that
-- (tests/dashboard/test_mart_only.py), but a static guard cannot see a query a future page
-- composes at runtime. This one can: `dashboard_ro` holds SELECT on the marts schema and nothing
-- else, so `raw`, `derived`, `analytics_staging` and `analytics_intermediate` are not merely
-- unread by the dashboard, they are unreadable by it. The invariant is structural rather than
-- documented -- the same move as the AST fence around the scientific stack.
--
-- APPLIED FROM TWO PLACES, hence the belt and braces below:
--   * docker-compose mounts this into the postgres service's /docker-entrypoint-initdb.d/, where
--     it runs once on a fresh volume -- BEFORE dbt exists, let alone its tables. ALTER DEFAULT
--     PRIVILEGES covers everything dbt creates later, and the CREATE SCHEMA above it exists only
--     so that statement is legal at that point (dbt is happy to materialise into a schema it did
--     not create).
--   * `just dashboard-grants` runs it on demand, typically AFTER a build, where the default
--     privileges have already missed the existing tables -- which is what GRANT ON ALL TABLES
--     picks up.
-- Neither statement alone is sufficient, and running both twice changes nothing.
--
-- The password is the compose-stack default and is meant to be: this database ships with
-- dagster/dagster and binds to localhost for a demo. Override with ENERGY_DASHBOARD_PG_PASSWORD
-- (and this file's literal) for anything that is not a laptop.

-- CREATE ROLE has no IF NOT EXISTS, so it is guarded rather than repeated.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
        create role dashboard_ro login password 'dashboard_ro';
    end if;
end
$$;

-- Read-only sessions by default: even a statement that slipped past the static guards cannot
-- write. Belongs on the role rather than the connection so it holds for psql too.
alter role dashboard_ro set default_transaction_read_only = on;

grant connect on database dagster to dashboard_ro;

create schema if not exists analytics_marts authorization dagster;
grant usage on schema analytics_marts to dashboard_ro;

-- Existing tables (the after-a-build case) ...
grant select on all tables in schema analytics_marts to dashboard_ro;
-- ... and everything dbt creates from here on (the fresh-volume case).
alter default privileges for role dagster in schema analytics_marts
    grant select on tables to dashboard_ro;

-- Deliberately NOT granted: raw, derived, analytics_staging, analytics_intermediate. Their absence
-- is the point of this file. Adding one here would silently retire the runtime half of the
-- mart-only rule, so it should be as hard to do by accident as it is easy to do here.
