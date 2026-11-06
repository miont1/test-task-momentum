#!/bin/sh
set -e

echo "Running database migrations..."
uv run alembic upgrade head

echo "Seeding knowledge base..."
uv run python -m app.seed

echo "Starting application..."
exec "$@"

