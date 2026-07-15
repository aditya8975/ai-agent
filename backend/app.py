"""
app.py — FastAPI backend for the AI Task Automation Agent
Deploy this to Render.com
"""

import os
import json
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from graph import build_graph, run_agent, stream_agent_events
from memory.supabase_memory import save_message, get_history
from llm_provider import configured_providers

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="AI Task Automation Agent API", version="2.0.0")

# Allow requests from Vercel frontend
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Build the multi-agent graph once at startup. Fails fast with a clear
# message if GROQ_API_KEY is missing, instead of crashing on first request.
try:
    agent_app = build_graph()
    _startup_error = None
except Exception as e:
    log.error("Failed to build agent graph: %s", e)
    agent_app = None
    _startup_error = str(e)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    session_id: str


@app.get("/")
def root():
    return {"status": "ok", "message": "AI Agent API is running"}


@app.get("/health")
def health():
    return {"status": "healthy" if agent_app else "degraded", "error": _startup_error}


@app.get("/status")
def status():
    """Show which LLM providers are configured (for debugging free-tier setups)."""
    return {
        "agent_ready": agent_app is not None,
        "startup_error": _startup_error,
        "providers": configured_providers(),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send a message to the agent and get a response."""
    if agent_app is None:
        raise HTTPException(
            status_code=503,
            detail=f"Agent not initialized: {_startup_error}. "
                    "Set GROQ_API_KEY (free at https://console.groq.com/keys) and restart.",
        )
    try:
        log.info("Chat request | session=%s | message=%s", req.session_id, req.message[:80])

        # Load history from Supabase
        history = get_history(req.session_id)

        # Run agent
        response, updated_history = run_agent(agent_app, req.message, history)

        # Save updated history to Supabase
        save_message(req.session_id, "user", req.message)
        save_message(req.session_id, "assistant", response)

        return ChatResponse(response=response, session_id=req.session_id)

    except Exception as e:
        log.error("Chat error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/{session_id}")
def history(session_id: str):
    """Get conversation history for a session."""
    try:
        msgs = get_history(session_id)
        return {"session_id": session_id, "messages": [
            {"role": getattr(m, "type", "unknown"), "content": getattr(m, "content", str(m))}
            for m in msgs
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """Same as /chat, but streams live progress events over Server-Sent
    Events as the agent works: plan created, each tool call, each tool
    result, and the final answer — instead of one opaque wait.

    Event shapes (one JSON object per `data:` line):
      {"type": "plan", "route": "...", "steps": [...], "tool_budget": N}
      {"type": "tool_call", "step": N, "tool": "...", "args": {...}}
      {"type": "tool_result", "tool": "...", "preview": "..."}
      {"type": "final", "text": "..."}
      {"type": "error", "message": "..."}
      {"type": "done", "response": "...", "session_id": "..."}   <- always last
    """
    if agent_app is None:
        raise HTTPException(
            status_code=503,
            detail=f"Agent not initialized: {_startup_error}. "
                    "Set GROQ_API_KEY (free at https://console.groq.com/keys) and restart.",
        )

    history = get_history(req.session_id)

    def event_gen():
        final_text = ""
        log.info("Stream chat request | session=%s | message=%s", req.session_id, req.message[:80])
        try:
            for event in stream_agent_events(agent_app, req.message, history):
                if event["type"] == "done":
                    final_text = event.get("response", "")
                    continue  # don't leak internal message objects to the client
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            log.error("Stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            save_message(req.session_id, "user", req.message)
            save_message(req.session_id, "assistant", final_text or "")
            yield f"data: {json.dumps({'type': 'done', 'response': final_text, 'session_id': req.session_id})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.delete("/history/{session_id}")
def clear_history(session_id: str):
    """Clear conversation history for a session."""
    from memory.supabase_memory import clear_session
    clear_session(session_id)
    return {"status": "cleared", "session_id": session_id}
