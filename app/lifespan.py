import asyncio
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from tortoise import Tortoise

from app.config import settings
from app import duckdb as duckdb_manager
from app import data_sync
from app.services.sse import chat_update_subscribers


# Global state for PostgreSQL LISTEN/NOTIFY
pg_listener_conn = None
pg_listener_task = None


async def pg_listener():
    """Background task to listen for PostgreSQL notifications."""
    global pg_listener_conn

    # Connect to PostgreSQL
    pg_listener_conn = await asyncpg.connect(settings.DATABASE_URL)

    # Listen to chat_updates channel
    await pg_listener_conn.add_listener('chat_updates', handle_chat_update)

    # Keep the connection alive
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        await pg_listener_conn.close()


async def handle_chat_update(connection, pid, channel, payload):
    """Handle incoming chat update notifications (structural changes only)."""
    chat_id = int(payload)

    if chat_id in chat_update_subscribers:
        for queue in chat_update_subscribers[chat_id]:
            try:
                await queue.put({"type": "full", "chat_id": chat_id})
            except:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown."""
    global pg_listener_task

    # Startup: DuckDB (Parquet data sources). Missing datasets are skipped;
    # the /data page downloads them and triggers a reload.
    duckdb_manager.init()
    data_sync.set_reload_hook(duckdb_manager.reload)

    # Startup: PostgreSQL (app_data)
    await Tortoise.init(config=settings.TORTOISE_ORM)
    try:
        await Tortoise.generate_schemas(safe=True)  # Create tables if they don't exist
    except Exception as e:
        # Log but don't fail on schema generation errors (likely already exists)
        print(f"Schema generation warning: {e}")
        print("Assuming tables already exist in database")

    # Start PostgreSQL listener
    pg_listener_task = asyncio.create_task(pg_listener())

    yield

    # Shutdown
    if pg_listener_task:
        pg_listener_task.cancel()
        try:
            await pg_listener_task
        except asyncio.CancelledError:
            pass

    await Tortoise.close_connections()
    duckdb_manager.close()