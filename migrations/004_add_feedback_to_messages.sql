-- Add feedback column to app_message for thumbs up/down on assistant responses
ALTER TABLE app_message ADD COLUMN IF NOT EXISTS feedback VARCHAR(10) DEFAULT NULL;
