"""Server-Sent Events utilities for real-time chat updates.

Two notification channels:
- notify_chat_update: structural changes (new messages, tool calls, stream end).
  Routes through PostgreSQL NOTIFY for cross-process reliability.
- notify_content_stream: text streaming updates during LLM generation.
  Direct in-memory queue push, bypassing PostgreSQL for low latency.
"""

import asyncio
from tortoise import connections


# Shared subscriber registry: {chat_id: [asyncio.Queue, ...]}
# Each connected SSE client registers a queue here.
chat_update_subscribers: dict[int, list[asyncio.Queue]] = {}


async def notify_chat_update(chat_id: int):
    """Notify subscribers of a structural change (new message, tool call, etc.).

    Goes through PostgreSQL NOTIFY so it works across processes.
    The SSE endpoint performs a full template re-render on receipt.
    """
    conn = connections.get("default")
    await conn.execute_query(f"NOTIFY chat_updates, '{chat_id}'")


async def notify_content_stream(chat_id: int, msg_id: int, content: str):
    """Push a streaming content update directly to subscriber queues.

    Bypasses PostgreSQL for speed. The SSE endpoint renders the markdown
    and sends a lightweight content-stream event targeting a single element.
    """
    if chat_id not in chat_update_subscribers:
        return
    for queue in chat_update_subscribers[chat_id]:
        try:
            await queue.put({
                "type": "stream",
                "msg_id": msg_id,
                "content": content,
            })
        except Exception:
            pass
