#!/bin/sh
set -e

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "Running database migrations..."
    uv run alembic upgrade head

    echo "Seeding knowledge base..."
    uv run python -m app.seed
fi

echo "Starting application..."
exec "$@"
