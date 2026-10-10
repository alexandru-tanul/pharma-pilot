-- Add usage field to app_message table for storing token usage
ALTER TABLE app_message ADD COLUMN IF NOT EXISTS usage JSONB;

COMMENT ON COLUMN app_message.usage IS 'Token usage from LLM API (prompt_tokens, completion_tokens, total_tokens)';