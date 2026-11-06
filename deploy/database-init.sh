#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=runtime_password="$RUNTIME_DATABASE_PASSWORD" --set=ON_ERROR_STOP=1 <<'SQL'
SELECT format('CREATE ROLE travel_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'runtime_password') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA dbos AUTHORIZATION travel_runtime;
SQL
