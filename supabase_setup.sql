-- Run this SQL in your Supabase project → SQL Editor → New query
-- https://supabase.com → your project → SQL Editor

-- 1. Create the chat history table
CREATE TABLE IF NOT EXISTS chat_history (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Index for fast session lookups
CREATE INDEX IF NOT EXISTS idx_chat_history_session 
    ON chat_history(session_id);

-- 3. Optional: auto-delete messages older than 30 days
-- (requires pg_cron extension, enable in Supabase dashboard → Extensions)
-- SELECT cron.schedule('delete-old-messages', '0 2 * * *',
--   $$DELETE FROM chat_history WHERE created_at < NOW() - INTERVAL '30 days'$$);

-- 4. Row Level Security (recommended for production)
-- ALTER TABLE chat_history ENABLE ROW LEVEL SECURITY;
