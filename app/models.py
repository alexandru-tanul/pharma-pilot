from tortoise import fields
from tortoise.models import Model


class Chat(Model):
    """Chat model for storing chat history.

    Rich domain model with methods for managing messages and interfacing with LLM API.
    """
    id = fields.BigIntField(pk=True)
    title = fields.CharField(max_length=200, default="")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    session_key = fields.CharField(max_length=40, null=True)

    class Meta:
        table = "app_chat"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Chat {self.id}"

    async def add_user_message(self, content: str) -> "Message":
        """Add a user message to this chat."""
        msg = await Message.create(chat=self, role="user", content=content)
        await self.save(update_fields=["updated_at"])
        return msg

    async def add_assistant_message(self, content: str | None, tool_calls: list | None = None) -> "Message":
        """Add an assistant message to this chat."""
        return await Message.create(
            chat=self,
            role="assistant",
            content=content,
            tool_calls=tool_calls
        )

    async def add_tool_message(self, tool_call_id: str, content: str, name: str | None = None, usage: dict | None = None) -> "Message":
        """Add a tool response message to this chat."""
        return await Message.create(
            chat=self,
            role="tool",
            tool_call_id=tool_call_id,
            content=content,
            name=name,
            usage=usage
        )

    async def as_openai_api_format(self) -> list[dict]:
        """Return messages in OpenAI chat completion API format.

        Example output:
        [
            {"role": "user", "content": "What drugs target EGFR?"},
            {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", ...}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "..."},
            {"role": "assistant", "content": "Here are the results..."}
        ]
        """
        await self.fetch_related("messages")
        return [msg.as_llm_dict() for msg in self.messages]


class Message(Model):
    """Message model for individual chat messages.

    Follows OpenAI Chat Completions API format used by LiteLLM:
    - User/assistant messages have role + content
    - Assistant messages can have tool_calls array
    - Tool response messages have role="tool" + tool_call_id + content
    """
    id = fields.BigIntField(pk=True)
    chat = fields.ForeignKeyField(
        "models.Chat", related_name="messages", on_delete=fields.CASCADE
    )
    role = fields.CharField(max_length=20)  # 'user', 'assistant', 'tool', 'system'
    content = fields.TextField(null=True)  # Nullable for assistant messages with only tool_calls

    # For assistant messages that call tools (OpenAI format)
    tool_calls = fields.JSONField(null=True)  # [{"id": str, "type": "function", "function": {"name": str, "arguments": str}}]

    # For tool response messages (role="tool")
    tool_call_id = fields.CharField(max_length=100, null=True)  # Links to tool_calls[].id
    name = fields.CharField(max_length=100, null=True)  # Function name

    # Token usage (from LLM API response.usage)
    usage = fields.JSONField(null=True)  # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}

    # User feedback on assistant responses
    feedback = fields.CharField(max_length=10, null=True)  # 'positive', 'negative', or null

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "app_message"
        ordering = ["created_at"]

    def __str__(self):
        if self.role == "tool":
            return f"tool({self.name}): {self.content[:30] if self.content else ''}..."
        return f"{self.role}: {self.content[:30] if self.content else 'tool_calls'}..."

    def as_llm_dict(self) -> dict:
        """Convert to LiteLLM/OpenAI chat completion message format.

        Includes `msg_id` for template use (stripped before LLM API calls).
        """
        msg = {"role": self.role, "msg_id": self.id}

        if self.content is not None:
            msg["content"] = self.content

        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls

        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id

        if self.name:
            msg["name"] = self.name

        if self.feedback:
            msg["feedback"] = self.feedback

        return msg


