default:
    @just --list

# Initialize project: download data and prepare environment
init:
    #!/usr/bin/env bash
    set -euo pipefail
    cp -n .env.example .env
    echo "Building Docker images..."
    docker compose build
    echo ""
    echo "Downloading datasets (about 4 GB, skipped if already present)..."
    just download-data
    echo ""
    echo "========================================="
    echo "Project initialized successfully!"
    echo "========================================="
    echo ""
    echo "IMPORTANT: Before starting the services, please update the .env file with the correct values:"
    echo ""
    echo "  Required configuration:"
    echo "    - SECRET_KEY: Generate a secure random key"
    echo "    - ANTHROPIC_API_KEY: Your Anthropic API key"
    echo "    - OPENAI_API_KEY: Your OpenAI API key (if using OpenAI models)"
    echo "    - GEMINI_API_KEY: Your Google Gemini API key (if using Gemini models)"
    echo "    - POSTGRES_PASSWORD: Secure password for PostgreSQL"
    echo ""
    echo "Once configured, start the services with:"
    echo "  just up"
    echo ""
    echo "Services will be available at:"
    echo "  - FastAPI: http://localhost:8000"
    echo "  - PgWeb:   http://localhost:8081"
    echo ""

# Download the Parquet datasets that are missing (no gh CLI needed)
download-data:
    docker compose run --rm --no-deps fastapi python -m app.data_sync download

# Update every dataset to the newest monthly release
update-data:
    docker compose run --rm --no-deps fastapi python -m app.data_sync update

# Show local vs latest dataset versions
data-status:
    docker compose run --rm --no-deps fastapi python -m app.data_sync status

# Build Docker images
build:
    docker compose build

# Start services
up:
    docker compose up

# Stop services
down:
    docker compose down

# View logs
logs:
    docker compose logs -f

# Create a new migration (like Django's makemigrations)
makemigrations name:
    docker compose exec fastapi aerich migrate --name {{name}}

# Apply all pending migrations (like Django's migrate)
migrate:
    docker compose exec fastapi sh -c "aerich upgrade 2>/dev/null || (aerich init-db && aerich upgrade)"
