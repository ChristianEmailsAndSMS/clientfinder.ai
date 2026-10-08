-- Least-privilege database login for the running app.
--
-- The API and scheduler only ever read and write rows. They never need to create, alter or drop tables, create roles,
-- or run as a superuser. With this role a bug or injection in the app cannot destroy the schema or touch other databases.
-- Schema changes keep using the owner login via MIGRATION_DATABASE_URL (alembic only).
--
-- Run once on the server (the password is generated first so you can see it and put it in DATABASE_URL):
--   APP_PW=$(openssl rand -hex 24); echo "SAVE THIS: $APP_PW"
--   docker exec -i clientfinder_pg psql -U clientfinder -d clientfinder -v app_password="$APP_PW" -f - < deploy/db_least_privilege.sql
--   then in backend/.env:  DATABASE_URL=postgresql+psycopg://clientfinder_app:$APP_PW@localhost:5433/clientfinder
--                          MIGRATION_DATABASE_URL=<the old owner URL (user clientfinder)>   (only alembic uses this)
-- Re-running is safe.

SELECT format('CREATE ROLE clientfinder_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clientfinder_app') \gexec
SELECT format('ALTER ROLE clientfinder_app PASSWORD %L', :'app_password') \gexec

REVOKE ALL ON DATABASE clientfinder FROM PUBLIC;
GRANT CONNECT ON DATABASE clientfinder TO clientfinder_app;

GRANT USAGE ON SCHEMA public TO clientfinder_app;
REVOKE CREATE ON SCHEMA public FROM clientfinder_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO clientfinder_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO clientfinder_app;

-- Tables added by future migrations (run by the owner) get the same grants automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clientfinder_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO clientfinder_app;

-- The credit ledger is append-only in the app: it may insert, but never rewrite or delete history.
REVOKE UPDATE, DELETE ON credit_ledger FROM clientfinder_app;
REVOKE UPDATE, DELETE ON auth_events FROM clientfinder_app;
