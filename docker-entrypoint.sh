#!/bin/bash
set -e

# MaverickMCP Docker Entrypoint
# Handles database migrations and optional seeding before starting the server

echo "=== MaverickMCP Container Starting ==="

# Function to wait for database
wait_for_db() {
    echo "Checking database connection..."

    # For SQLite, just ensure the directory exists
    if [[ "$DATABASE_URL" == sqlite* ]]; then
        DB_PATH=$(echo "$DATABASE_URL" | sed 's|sqlite:///||' | sed 's|sqlite://||')
        DB_DIR=$(dirname "$DB_PATH")

        if [ "$DB_DIR" != "." ] && [ "$DB_DIR" != "" ]; then
            mkdir -p "$DB_DIR"
            echo "SQLite database directory ready: $DB_DIR"
        fi
        return 0
    fi

    # For PostgreSQL, wait for connection
    if [[ "$DATABASE_URL" == postgresql* ]]; then
        MAX_RETRIES=30
        RETRY_COUNT=0

        while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
            if uv run python -c "
from sqlalchemy import create_engine, text
try:
    engine = create_engine('$DATABASE_URL')
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    exit(0)
except:
    exit(1)
" 2>/dev/null; then
                echo "Database connection successful"
                return 0
            fi

            RETRY_COUNT=$((RETRY_COUNT + 1))
            echo "Waiting for database... ($RETRY_COUNT/$MAX_RETRIES)"
            sleep 2
        done

        echo "ERROR: Could not connect to database after $MAX_RETRIES attempts"
        exit 1
    fi
}

# Function to run migrations
run_migrations() {
    echo "Running database migrations..."

    # Check if alembic is available
    if [ -f "alembic.ini" ] && [ -d "alembic" ]; then
        # Run migrations using the migrate_db.py script if available
        if [ -f "scripts/migrate_db.py" ]; then
            uv run python scripts/migrate_db.py
        else
            # Fallback to direct alembic
            uv run alembic upgrade head
        fi
        echo "Migrations completed"
    else
        echo "No alembic configuration found, skipping migrations"
    fi
}

# Function to seed database
seed_database() {
    echo "Checking if database needs seeding..."

    # Check if seed script exists
    if [ ! -f "scripts/seed_db.py" ]; then
        echo "No seed script found, skipping"
        return 0
    fi

    # Check if database already has data
    STOCK_COUNT=$(uv run python -c "
from sqlalchemy import create_engine, text
import os
try:
    engine = create_engine(os.environ.get('DATABASE_URL', 'sqlite:///maverick_mcp.db'))
    with engine.connect() as conn:
        result = conn.execute(text('SELECT COUNT(*) FROM mcp_stocks'))
        count = result.scalar()
        print(count)
except Exception as e:
    print('0')
" 2>/dev/null || echo "0")

    if [ "$STOCK_COUNT" -gt "0" ]; then
        echo "Database already has $STOCK_COUNT stocks, skipping seed"
        return 0
    fi

    echo "Seeding database with sample data..."
    uv run python scripts/seed_db.py
    echo "Database seeded successfully"
}

# Main execution
cd /app

# Wait for database to be ready
wait_for_db

# Run migrations if SKIP_MIGRATIONS is not set
if [ "${SKIP_MIGRATIONS:-false}" != "true" ]; then
    run_migrations
fi

# Seed database if AUTO_SEED is enabled
if [ "${AUTO_SEED:-false}" == "true" ]; then
    seed_database
fi

echo "=== Starting MaverickMCP Server ==="

# Execute the command passed to the container
exec "$@"
