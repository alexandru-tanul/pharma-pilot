"""LLM orchestration service for generating chat responses."""

import asyncio
import json
import logging
import re
import time
from litellm import acompletion

from app.config import settings
from app.models import Chat, Message
from app.prompts import SYSTEM_PROMPT, LLM_TOOLS
from app.services.llm.context import (
    get_context_budget,
    count_messages_tokens,
    trim_conversation,
)
from app.services.sse import notify_chat_update, notify_content_stream
from app.services.clinical_trials import query_clinical_trials
from app.services.drugcentral import query_drugcentral
from app.services.pharos import query_pharos

log = logging.getLogger(__name__)


def _strip_metadata(messages: list[dict]) -> list[dict]:
    """Remove non-API keys (msg_id, feedback) before sending to the LLM."""
    return [{k: v for k, v in m.items() if k not in _NON_API_KEYS} for m in messages]


_NON_API_KEYS = frozenset({"msg_id", "feedback"})


# Safety limits
MAX_CONSECUTIVE_ERRORS = 3
MAX_TOKEN_BUDGET = 200_000  # cumulative tokens across all iterations

# Marker prefix used to detect error results from tools
_ERROR_PREFIXES = ("SQL ERROR", "Clinical Trials error", "DrugCentral error",
                   "Pharos error", "Unexpected error", "JSON parse error")


def _try_parse_json(raw: str) -> dict:
    """Parse JSON from LLM tool arguments with repair fallback.

    LLMs sometimes produce malformed JSON: trailing commas, unescaped
    newlines inside strings, single quotes instead of double quotes.
    This function attempts a plain parse first, then a light repair.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Light repair: strip trailing commas before } or ], fix newlines in strings
    repaired = re.sub(r",\s*([}\]])", r"\1", raw)
    repaired = repaired.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return json.loads(repaired)


async def execute_tool(
    tool_name: str,
    arguments: dict,
    max_result_chars: int = 50_000,
) -> tuple[str, dict | None]:
    """Execute a tool by name with given arguments.

    Each tool returns (result_string, token_usage_dict_or_None).
    Three tools, all SQL-based:
        - query_clinical_trials: SQL on AACT database
        - query_drugcentral: SQL on DrugCentral database
        - query_pharos: SQL on Pharos/TCRD database
    """
    if tool_name == "query_clinical_trials":
        sql = arguments.get("sql", "")
        result = await query_clinical_trials(sql, max_result_chars=max_result_chars)
        return result, None

    elif tool_name == "query_drugcentral":
        sql = arguments.get("sql", "")
        result = await query_drugcentral(sql, max_result_chars=max_result_chars)
        return result, None

    elif tool_name == "query_pharos":
        sql = arguments.get("sql", "")
        result = await query_pharos(sql, max_result_chars=max_result_chars)
        return result, None

    else:
        raise ValueError(f"Unknown tool: {tool_name}")


async def generate_response(chat: Chat) -> str:
    """Generate an LLM response for the given chat with streaming support.

    Uses agentic loop to:
    1. Call LLM with tools (streaming enabled)
    2. Execute any requested tools
    3. Feed results back to LLM
    4. Repeat until LLM provides final answer

    Streams responses to UI via SSE for real-time updates.
    Saves all messages to database in standard OpenAI format.

    Returns the final assistant message content.
    """
    try:
        # Calculate context budget for the current model
        budget = get_context_budget()
        max_result_chars = budget["max_result_chars"]
        available = budget["available"]
        log.info(
            "Context budget: %d available tokens, %d max chars per result (model %s)",
            available, max_result_chars, settings.MODEL,
        )

        # Build message history for LLM
        llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        llm_messages.extend(await chat.as_openai_api_format())

        # Agentic loop: Keep calling tools until the LLM provides a final answer
        final_message = ""
        consecutive_errors = 0
        cumulative_tokens = 0

        while True:

            # Pre-flight check: trim conversation if it exceeds the context budget
            current_tokens = count_messages_tokens(
                llm_messages, tools=LLM_TOOLS, model=settings.MODEL,
            )
            cumulative_tokens += current_tokens

            if current_tokens > available:
                target = int(available * 0.85)
                log.warning(
                    "Context overflow: %d tokens > %d available. Trimming to %d.",
                    current_tokens, available, target,
                )
                llm_messages = trim_conversation(
                    llm_messages,
                    target_tokens=target,
                    tools=LLM_TOOLS,
                    model=settings.MODEL,
                )

            # Token budget guard: force final synthesis if cumulative spend is too high
            if cumulative_tokens > MAX_TOKEN_BUDGET:
                log.warning(
                    "Token budget exceeded: %d cumulative tokens > %d limit. "
                    "Forcing final synthesis.",
                    cumulative_tokens, MAX_TOKEN_BUDGET,
                )
                llm_messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM: Token budget exceeded. Provide your best answer "
                        "using the data you have gathered so far. Do not call any "
                        "more tools.]"
                    ),
                })
                # Fall through to the acompletion call below with tool_choice="none"
                assistant_msg = await chat.add_assistant_message(content="", tool_calls=None)
                await notify_chat_update(chat.id)

                response = await acompletion(
                    model=settings.MODEL,
                    messages=_strip_metadata(llm_messages),
                    tools=LLM_TOOLS,
                    tool_choice="none",
                    stream=True,
                    timeout=60.0,
                    max_tokens=4096,
                )
                content_buffer = ""
                last_stream_time = time.time()
                budget_content_el = False
                async for chunk in response:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, "content") and delta.content:
                        content_buffer += delta.content
                        now = time.time()
                        if now - last_stream_time > 0.05:
                            assistant_msg.content = content_buffer
                            await assistant_msg.save()
                            if not budget_content_el:
                                await notify_chat_update(chat.id)
                                budget_content_el = True
                            else:
                                await notify_content_stream(chat.id, assistant_msg.id, content_buffer)
                            last_stream_time = now
                assistant_msg.content = content_buffer
                await assistant_msg.save()
                await notify_chat_update(chat.id)
                return content_buffer

            # Create placeholder assistant message for streaming
            assistant_msg = await chat.add_assistant_message(content="", tool_calls=None)
            await notify_chat_update(chat.id)

            # Call LLM with streaming enabled
            response = await acompletion(
                model=settings.MODEL,
                messages=_strip_metadata(llm_messages),
                tools=LLM_TOOLS,
                tool_choice="auto",
                stream=True,
                timeout=60.0,
                max_tokens=4096,
            )

            # Accumulate streaming response
            content_buffer = ""
            tool_calls_buffer = []
            tool_calls_dict = {}
            tool_calls_notified = set()
            last_update_time = time.time()
            last_update_length = 0
            content_element_exists = False

            async for chunk in response:
                delta = chunk.choices[0].delta

                # Accumulate content
                if hasattr(delta, "content") and delta.content:
                    content_buffer += delta.content

                    current_time = time.time()
                    should_update = (
                        current_time - last_update_time > 0.05
                        or len(content_buffer) - last_update_length >= 5
                    )

                    if should_update:
                        assistant_msg.content = content_buffer
                        await assistant_msg.save()
                        if not content_element_exists:
                            # First content chunk: full morph to create the prose element
                            await notify_chat_update(chat.id)
                            content_element_exists = True
                        else:
                            await notify_content_stream(chat.id, assistant_msg.id, content_buffer)
                        last_update_time = current_time
                        last_update_length = len(content_buffer)

                # Accumulate tool calls (they come as deltas)
                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }

                        if hasattr(tc_delta, "id") and tc_delta.id:
                            tool_calls_dict[idx]["id"] = tc_delta.id

                        if hasattr(tc_delta, "function"):
                            if hasattr(tc_delta.function, "name") and tc_delta.function.name:
                                tool_calls_dict[idx]["function"]["name"] = tc_delta.function.name

                                # Save to DB immediately when we detect a tool name
                                # so the UI shows "Querying DrugCentral..." in real time
                                if tc_delta.function.name not in tool_calls_notified:
                                    tool_calls_notified.add(tc_delta.function.name)
                                    partial_tool_calls = [
                                        tool_calls_dict[i]
                                        for i in sorted(tool_calls_dict.keys())
                                    ]
                                    assistant_msg.tool_calls = partial_tool_calls
                                    await assistant_msg.save()
                                    await notify_chat_update(chat.id)

                            if (
                                hasattr(tc_delta.function, "arguments")
                                and tc_delta.function.arguments
                            ):
                                tool_calls_dict[idx]["function"][
                                    "arguments"
                                ] += tc_delta.function.arguments

            # Convert accumulated tool calls dict to list
            if tool_calls_dict:
                tool_calls_buffer = [
                    tool_calls_dict[i] for i in sorted(tool_calls_dict.keys())
                ]

            # Final update to database
            assistant_msg.content = content_buffer or None
            assistant_msg.tool_calls = tool_calls_buffer if tool_calls_buffer else None
            await assistant_msg.save()
            await notify_chat_update(chat.id)

            # If no tool calls, LLM is done
            if not tool_calls_buffer:
                final_message = content_buffer
                break

            # Add to conversation
            llm_messages.append({
                "role": "assistant",
                "content": content_buffer or None,
                "tool_calls": tool_calls_buffer,
            })

            # Execute tools and save results
            iteration_had_error = False

            for tool_call in tool_calls_buffer:
                tool_name = tool_call["function"]["name"]
                raw_args = tool_call["function"]["arguments"]

                # Guard: parse JSON with repair fallback
                try:
                    args = _try_parse_json(raw_args)
                except (json.JSONDecodeError, Exception) as parse_err:
                    log.warning(
                        "JSON parse failed for %s: %s | raw: %s",
                        tool_name, parse_err, raw_args[:200],
                    )
                    result = (
                        f"JSON parse error: Could not parse tool arguments.\n"
                        f"Raw arguments: {raw_args[:500]}\n"
                        f"Error: {parse_err}\n"
                        f"Please provide valid JSON with a 'sql' key."
                    )
                    iteration_had_error = True

                    await chat.add_tool_message(
                        tool_call_id=tool_call["id"],
                        content=result,
                        name=tool_name,
                    )
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result,
                    })
                    continue

                log.info("%s called with: %s", tool_name, args)

                # Execute the tool
                result, usage = await execute_tool(
                    tool_name, args, max_result_chars=max_result_chars,
                )
                log.info("%s returned %d characters", tool_name, len(result))

                # Check if the result is an error
                if any(result.startswith(prefix) for prefix in _ERROR_PREFIXES):
                    iteration_had_error = True

                # Save tool result message to database
                await chat.add_tool_message(
                    tool_call_id=tool_call["id"],
                    content=result,
                    name=tool_name,
                    usage=usage,
                )

                # Add to LLM conversation
                llm_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": result,
                })

            await notify_chat_update(chat.id)

            # Track consecutive errors and trigger rethink if needed
            if iteration_had_error:
                consecutive_errors += 1
                log.warning(
                    "Tool error streak: %d / %d",
                    consecutive_errors, MAX_CONSECUTIVE_ERRORS,
                )

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log.warning(
                        "Hit %d consecutive tool errors. Injecting rethink.",
                        MAX_CONSECUTIVE_ERRORS,
                    )
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM: Your last several queries returned errors. "
                            "Stop and reconsider your approach. Re-read the schema "
                            "descriptions in the tool definitions. Think about which "
                            "view has the columns you need, then try a different "
                            "query strategy. Write your new plan before calling "
                            "any tool.]"
                        ),
                    })
                    consecutive_errors = 0
            else:
                consecutive_errors = 0

        return final_message

    except Exception as e:
        # Update the last empty assistant message with the error so the UI shows it
        try:
            await chat.fetch_related("messages")
            messages = list(chat.messages)
            if messages and messages[-1].role == "assistant" and not messages[-1].content:
                messages[-1].content = f"**Error:** {str(e)}\n\nPlease try again."
                await messages[-1].save()
        except Exception:
            pass
        await notify_chat_update(chat.id)
        raise
