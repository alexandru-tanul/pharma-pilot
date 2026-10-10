"""Context window management for LLM calls.

Provides token counting, budget calculation, and conversation trimming
so the agentic loop never exceeds the model's context window.
"""

import logging

from litellm import token_counter, get_model_info

from app.config import settings

log = logging.getLogger(__name__)

# Reserve a margin for token-counting inaccuracy and provider overhead
SAFETY_MARGIN_TOKENS = 1024

# Conservative chars-per-token ratio (works across models)
CHARS_PER_TOKEN = 4


def get_context_budget(model: str | None = None) -> dict:
    """Calculate the context window budget for the current model.

    Returns dict with:
        max_input_tokens: total input context window
        output_budget: tokens reserved for generation
        available: tokens available for messages + tool results
        max_result_chars: recommended per-result character limit
    """
    model = model or settings.MODEL

    try:
        info = get_model_info(model)
        max_input = info.get("max_input_tokens") or info.get("max_tokens") or 128_000
        max_output = info.get("max_output_tokens") or 4_096
    except Exception:
        log.warning("Could not get model info for %s, assuming 128K context", model)
        max_input = 128_000
        max_output = 4_096

    # Cap output budget to what we actually request in acompletion()
    output_budget = min(max_output, 4_096)

    available = max_input - output_budget - SAFETY_MARGIN_TOKENS

    # A single tool result should consume at most 40% of the available budget
    max_result_tokens = int(available * 0.4)
    max_result_chars = max_result_tokens * CHARS_PER_TOKEN

    return {
        "max_input_tokens": max_input,
        "output_budget": output_budget,
        "available": available,
        "max_result_chars": max_result_chars,
    }


def count_messages_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str | None = None,
) -> int:
    """Count tokens in a message list, including tool definitions.

    Falls back to character-based estimation if litellm fails.
    """
    model = model or settings.MODEL
    try:
        return token_counter(model=model, messages=messages, tools=tools)
    except Exception:
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if tools:
            total_chars += len(str(tools))
        return total_chars // CHARS_PER_TOKEN


def trim_conversation(
    messages: list[dict],
    target_tokens: int,
    tools: list[dict] | None = None,
    model: str | None = None,
) -> list[dict]:
    """Trim conversation messages to fit within target_tokens.

    Three stages, applied in order:
      1. Compress old tool results (keep only metadata)
      2. Drop middle conversation turns (sliding window)
      3. Truncate remaining large tool results

    Always preserves: system message, most recent turn.
    """
    model = model or settings.MODEL
    messages = [m.copy() for m in messages]

    # --- Stage 1: compress old tool results ---

    # Find the last user message; everything from there onward is the "current turn"
    last_user_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            last_user_idx = i
            break

    for i in range(last_user_idx):
        msg = messages[i]
        if msg["role"] == "tool" and msg.get("content") and len(msg["content"]) > 200:
            first_line = msg["content"].split("\n", 1)[0]
            tool_name = msg.get("name", "tool")
            msg["content"] = (
                f"[Compressed. {first_line}. "
                f"Tool: {tool_name}. See assistant response for summary.]"
            )

    if count_messages_tokens(messages, tools=tools, model=model) <= target_tokens:
        return messages

    # --- Stage 2: sliding window over conversation turns ---

    system_msg = messages[0] if messages and messages[0]["role"] == "system" else None
    body = messages[1:] if system_msg else messages

    # Group into turns (each starts with a user message)
    turns: list[list[dict]] = []
    current_turn: list[dict] = []
    for msg in body:
        if msg["role"] == "user" and current_turn:
            turns.append(current_turn)
            current_turn = []
        current_turn.append(msg)
    if current_turn:
        turns.append(current_turn)

    if len(turns) > 2:
        first_turn = turns[0]

        # Try keeping progressively fewer recent turns
        for keep_count in range(len(turns) - 1, 0, -1):
            candidate = []
            if system_msg:
                candidate.append(system_msg)
            candidate.extend(first_turn)

            if keep_count < len(turns) - 1:
                dropped = len(turns) - 1 - keep_count
                candidate.append({
                    "role": "user",
                    "content": (
                        f"[{dropped} earlier conversation turn(s) removed "
                        f"for context management.]"
                    ),
                })

            for turn in turns[-keep_count:]:
                candidate.extend(turn)

            if count_messages_tokens(candidate, tools=tools, model=model) <= target_tokens:
                return candidate

        # Even one turn is too much; fall through to Stage 3
        messages = []
        if system_msg:
            messages.append(system_msg)
        messages.extend(turns[-1])

    # --- Stage 3: halve the largest tool results until we fit ---

    for _ in range(10):
        if count_messages_tokens(messages, tools=tools, model=model) <= target_tokens:
            return messages

        max_len = 0
        max_idx = None
        for i, msg in enumerate(messages):
            content = msg.get("content") or ""
            if msg["role"] == "tool" and len(content) > max_len:
                max_len = len(content)
                max_idx = i

        if max_idx is None or max_len < 300:
            break

        content = messages[max_idx]["content"]
        cut = content.rfind("\n", 0, len(content) // 2)
        if cut == -1:
            cut = len(content) // 2

        tool_name = messages[max_idx].get("name", "tool")
        messages[max_idx]["content"] = (
            content[:cut]
            + f"\n\n[Truncated for context management. Tool: {tool_name}. "
            f"Refine query for more specific results.]"
        )

    return messages
