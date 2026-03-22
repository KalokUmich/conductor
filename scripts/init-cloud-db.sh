#!/usr/bin/env bash
# ============================================================
# Initialize Conductor databases on a cloud PostgreSQL instance
# (AWS RDS, GCP Cloud SQL, Azure Database for PostgreSQL, etc.)
#
# This script creates:
#   1. A dedicated role (DB user)
#   2. Two databases owned by that role:
#      - conductor  (backend OLTP data — tables managed by Liquibase)
#      - langfuse   (Langfuse observability — tables managed by Langfuse internally)
#
# Usage:
#   # Interactive — prompts for admin password
#   ./scripts/init-cloud-db.sh --host mydb.xxx.rds.amazonaws.com --admin-user postgres
#
#   # Non-interactive — reads admin password from env
#   PGPASSWORD=admin-secret ./scripts/init-cloud-db.sh \
#       --host mydb.xxx.rds.amazonaws.com \
#       --admin-user postgres \
#       --app-user conductor \
#       --app-password conductor-secret
#
# After this script completes, run Liquibase to create tables:
#   POSTGRES_HOST=mydb.xxx.rds.amazonaws.com \
#   POSTGRES_USER=conductor \
#   POSTGRES_PASSWORD=conductor-secret \
#   make db-update
#
# Then start Langfuse pointing to the langfuse database.
# Langfuse auto-creates its own tables on first startup.
# ============================================================

set -euo pipefail

# Defaults
DB_HOST="localhost"
DB_PORT="5432"
ADMIN_USER="postgres"
APP_USER="conductor"
APP_PASSWORD=""

usage() {
    echo "Usage: $0 --host <host> [--port <port>] --admin-user <user> [--app-user <user>] [--app-password <pass>]"
    echo ""
    echo "Options:"
    echo "  --host          PostgreSQL host (required)"
    echo "  --port          PostgreSQL port (default: 5432)"
    echo "  --admin-user    Admin user for initial setup (default: postgres)"
    echo "  --app-user      Application user/role to create (default: conductor)"
    echo "  --app-password  Password for the application user (prompted if not set)"
    exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --host)         DB_HOST="$2";       shift 2 ;;
        --port)         DB_PORT="$2";       shift 2 ;;
        --admin-user)   ADMIN_USER="$2";    shift 2 ;;
        --app-user)     APP_USER="$2";      shift 2 ;;
        --app-password) APP_PASSWORD="$2";  shift 2 ;;
        -h|--help)      usage ;;
        *)              echo "Unknown option: $1"; usage ;;
    esac
done

if [[ "$DB_HOST" == "localhost" ]]; then
    echo "WARNING: --host not specified. Use --host to point to your cloud DB."
    echo ""
fi

# Prompt for app password if not provided
if [[ -z "$APP_PASSWORD" ]]; then
    read -rsp "Enter password for role '$APP_USER': " APP_PASSWORD
    echo ""
fi

echo "=== Conductor Cloud DB Initialization ==="
echo "Host:       $DB_HOST:$DB_PORT"
echo "Admin user: $ADMIN_USER"
echo "App role:   $APP_USER"
echo ""

PSQL="psql -h $DB_HOST -p $DB_PORT -U $ADMIN_USER"

# 1. Create application role (idempotent)
echo "Creating role '$APP_USER'..."
$PSQL -d postgres -c "
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$APP_USER') THEN
        CREATE ROLE $APP_USER WITH LOGIN PASSWORD '$APP_PASSWORD';
        RAISE NOTICE 'Role $APP_USER created.';
    ELSE
        ALTER ROLE $APP_USER WITH PASSWORD '$APP_PASSWORD';
        RAISE NOTICE 'Role $APP_USER already exists — password updated.';
    END IF;
END
\$\$;
"

# 2. Create conductor database
echo "Creating database 'conductor'..."
$PSQL -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'conductor'" | grep -q 1 \
    || $PSQL -d postgres -c "CREATE DATABASE conductor OWNER $APP_USER;"
echo "  conductor — OK"

# 3. Create langfuse database
echo "Creating database 'langfuse'..."
$PSQL -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'langfuse'" | grep -q 1 \
    || $PSQL -d postgres -c "CREATE DATABASE langfuse OWNER $APP_USER;"
echo "  langfuse — OK"

echo ""
echo "=== Done ==="
echo ""
echo "Next steps:"
echo "  1. Run Liquibase to create conductor tables:"
echo "     POSTGRES_HOST=$DB_HOST POSTGRES_USER=$APP_USER POSTGRES_PASSWORD=*** make db-update"
echo ""
echo "  2. Start Langfuse with DATABASE_URL pointing to the langfuse database:"
echo "     DATABASE_URL=postgresql://$APP_USER:***@$DB_HOST:$DB_PORT/langfuse"
echo ""
echo "  3. Start the backend with DATABASE_URL pointing to the conductor database:"
echo "     DATABASE_URL=postgresql+asyncpg://$APP_USER:***@$DB_HOST:$DB_PORT/conductor"
