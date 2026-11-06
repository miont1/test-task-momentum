#!/bin/sh
set -e

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    case "${DATABASE_URL:-}" in
        sqlite*)
            echo "SQLite database configured; initializing schema via seed script..."
            ;;
        *)
            echo "Running database migrations..."
            uv run alembic upgrade head
            ;;
    esac

    echo "Seeding knowledge base..."
    uv run python -m app.seed
fi

echo "Starting application..."
exec "$@"
