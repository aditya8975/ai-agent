"""
memory/supabase_memory.py
Saves and loads conversation history using Supabase (free PostgreSQL).

Supabase setup (one-time):
1. Go to https://supabase.com → New project
2. Run this SQL in the SQL editor:

    CREATE TABLE chat_history (
        id SERIAL PRIMARY KEY,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_session ON chat_history(session_id);

3. Copy your project URL and anon key to .env
"""

import os
import logging
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

log = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
MAX_HISTORY = int(os.getenv("MAX_HISTORY_TURNS", "30"))


def _get_client():
    """Return a Supabase client, or None if not configured."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except ImportError:
        log.warning("supabase-py not installed. Run: pip install supabase")
        return None
    except Exception as e:
        log.warning("Supabase connection failed: %s", e)
        return None


def save_message(session_id: str, role: str, content: str):
    """Save a single message to Supabase."""
    client = _get_client()
    if not client:
        return  # Gracefully skip if Supabase not configured
    try:
        client.table("chat_history").insert({
            "session_id": session_id,
            "role": role,
            "content": content,
        }).execute()
    except Exception as e:
        log.warning("Could not save message to Supabase: %s", e)


def get_history(session_id: str) -> list:
    """Load conversation history from Supabase as LangChain messages."""
    client = _get_client()
    if not client:
        return []  # Return empty history if Supabase not configured
    try:
        result = (
            client.table("chat_history")
            .select("role, content")
            .eq("session_id", session_id)
            .order("created_at")
            .limit(MAX_HISTORY)
            .execute()
        )
        messages = []
        for row in result.data:
            role = row["role"]
            content = row["content"]
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
        return messages
    except Exception as e:
        log.warning("Could not load history from Supabase: %s", e)
        return []


def clear_session(session_id: str):
    """Delete all messages for a session."""
    client = _get_client()
    if not client:
        return
    try:
        client.table("chat_history").delete().eq("session_id", session_id).execute()
        log.info("Cleared session: %s", session_id)
    except Exception as e:
        log.warning("Could not clear session: %s", e)
