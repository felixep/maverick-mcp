#!/bin/bash
set -e

# MaverickMCP Docker Entrypoint
# Handles database migrations and optional seeding before starting the server

echo "=== MaverickMCP Container Starting ==="

# Normalize API key env vars (different scripts expect different names)
export TIINGO_API_TOKEN="${TIINGO_API_TOKEN:-$TIINGO_API_KEY}"

# Default pool sizes that fit within PostgreSQL Alpine's max_connections=20
# Settings reads DB_POOL_SIZE + DB_POOL_MAX_OVERFLOW; DatabasePoolConfig reads DB_MAX_OVERFLOW
export DB_POOL_SIZE="${DB_POOL_SIZE:-5}"
export DB_MAX_OVERFLOW="${DB_MAX_OVERFLOW:-3}"
export DB_POOL_MAX_OVERFLOW="${DB_POOL_MAX_OVERFLOW:-$DB_MAX_OVERFLOW}"

# Debug: show database URL (mask password)
echo "DATABASE_URL: $(echo "$DATABASE_URL" | sed -E 's|(://[^:]+:)[^@]+(@)|\1****\2|')"

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
import os
from sqlalchemy import create_engine, text
try:
    url = os.environ['DATABASE_URL']
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    exit(0)
except Exception as e:
    print(f'Connection failed: {e}')
    exit(1)
"; then
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

    # Determine which seed script to use
    # SEED_MODE: "sample" (default), "sp500", or "tiingo"
    SEED_MODE="${SEED_MODE:-sample}"

    case "$SEED_MODE" in
        sp500)
            SEED_SCRIPT="scripts/seed_sp500.py"
            SEED_DESC="S&P 500 stocks"
            ;;
        tiingo)
            SEED_SCRIPT="scripts/load_tiingo_data.py"
            SEED_DESC="Tiingo market data"
            ;;
        *)
            SEED_SCRIPT="scripts/seed_db.py"
            SEED_DESC="sample data"
            ;;
    esac

    # Check if seed script exists
    if [ ! -f "$SEED_SCRIPT" ]; then
        echo "Seed script not found: $SEED_SCRIPT, skipping"
        return 0
    fi

    # Build the seed command with proper arguments
    build_seed_command() {
        if [ "$SEED_MODE" == "tiingo" ]; then
            # Tiingo loader needs explicit flags
            echo "uv run python $SEED_SCRIPT --sp500 --calculate-indicators --run-screening --years ${TIINGO_YEARS:-2}"
        else
            echo "uv run python $SEED_SCRIPT"
        fi
    }

    SEED_CMD=$(build_seed_command)

    # Force seed if FORCE_SEED is set
    if [ "${FORCE_SEED:-false}" == "true" ]; then
        echo "FORCE_SEED enabled, seeding database with $SEED_DESC..."
        eval "$SEED_CMD"
        run_full_screening
        echo "Database seeded successfully"
        return 0
    fi

    # Check if screening tables have data (more important than just stocks)
    SCREENING_COUNT=$(uv run python -c "
from sqlalchemy import create_engine, text
import os
try:
    engine = create_engine(os.environ.get('DATABASE_URL', 'sqlite:///maverick_mcp.db'))
    with engine.connect() as conn:
        result = conn.execute(text('SELECT COUNT(*) FROM mcp_maverick_stocks'))
        count = result.scalar()
        print(count)
except Exception as e:
    print('0')
" 2>/dev/null || echo "0")

    if [ "$SCREENING_COUNT" -gt "0" ]; then
        echo "Database already has $SCREENING_COUNT screening results, skipping seed"
        return 0
    fi

    # Check if we already have price data (survives reboots via upsert)
    PRICE_COUNT=$(uv run python -c "
from sqlalchemy import create_engine, text
import os
try:
    engine = create_engine(os.environ.get('DATABASE_URL', 'sqlite:///maverick_mcp.db'))
    with engine.connect() as conn:
        result = conn.execute(text('SELECT COUNT(*) FROM mcp_price_cache'))
        print(result.scalar())
except Exception:
    print('0')
" 2>/dev/null || echo "0")

    if [ "$PRICE_COUNT" -gt "10000" ]; then
        echo "Price data exists ($PRICE_COUNT records) but screening is empty."
        echo "Running screening only (skipping Tiingo download)..."
        run_full_screening
        echo "Screening completed"
        return 0
    fi

    echo "No data found, seeding database with $SEED_DESC..."
    eval "$SEED_CMD"
    run_full_screening
    echo "Database seeded successfully"
}

# Function to run the full TA-Lib screening pipeline (more sophisticated than Tiingo's built-in)
run_full_screening() {
    if [ -f "scripts/run_stock_screening.py" ]; then
        echo "Running full TA-Lib screening pipeline..."
        # Note: run_stock_screening.py reads DATABASE_URL from env if --database-url not given
        uv run python scripts/run_stock_screening.py --all || {
            echo "WARNING: Full screening failed (non-fatal), Tiingo screening results still available"
        }
    fi
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
