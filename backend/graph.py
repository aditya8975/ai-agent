"""
graph.py — Multi-agent LangGraph task automation pipeline.

Architecture
------------
    HumanMessage
         |
         v
    [Planner Agent]  (fast Groq model, no tools)
         |  decides: which specialist route to use (RESEARCH/REPORT/FILES/GENERAL)
         |            + a short numbered plan (1-4 concrete steps)
         |            + a hard tool-call budget for the whole turn
         v
    [Specialist Agent]  (quality Groq model, bound to the route's tools,
                          prompted with the exact plan + remaining budget) <---+
         |                                                                     |
         +-- tool call? (budget > 0) --> [Tool Executor] ------------------- --+
         |                                       |
         |                          budget == 0 --> tells the model to stop
         v (no more tool calls, or budget exhausted)
        END

Why the planner + budget exist
-------------------------------
Without an explicit plan and a hard ceiling on tool calls, a ReAct loop has
no notion of "done" beyond "the model decided to stop calling tools" — which
in practice means it keeps finding more to do (extra searches, extra files
on topics nobody asked for) until it hits the recursion limit. The planner
fixes the request's *scope* up front and the tool budget enforces it even if
the model tries to ignore its instructions.

Streaming
---------
`stream_agent_events()` yields one small JSON-able dict per meaningful step
(plan created, tool called, tool result, final answer) as the graph runs, so
a UI can show live progress instead of a single opaque "thinking..." spinner.
"""

import os
import re
import json
import logging
import operator
from typing import TypedDict, Annotated, Iterator

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    SystemMessage,
    ToolMessage,
    BaseMessage,
)
from langgraph.graph import StateGraph, END
from langgraph.errors import GraphRecursionError

from llm_provider import get_llm
from tools.search_tool import search_tool
from tools.file_tool import read_file_tool, write_file_tool
from tools.report_tool import save_word_tool, save_excel_tool

log = logging.getLogger(__name__)

BASE_SYSTEM_PROMPT = """You are part of a multi-agent task automation system.
Be concise and accurate. Never invent facts — use web_search for anything
that could be current or that you're unsure of. When you use a tool, briefly
summarise the result for the user afterwards instead of just repeating raw
output. Output files are saved in the output/ folder on the server.

STRICT SCOPE RULE: only do what the plan below asks. Do not research, write,
or save anything beyond it — no bonus topics, no extra files. Once every
step in the plan is satisfied, reply with your final answer as plain text
and make NO further tool calls."""

ROUTES = {
    "RESEARCH": {
        "tools": [search_tool],
        "persona": "You are the Research Agent. Find accurate, current "
                   "information using web_search and summarise it clearly, "
                   "citing sources by name where relevant.",
    },
    "REPORT": {
        "tools": [search_tool, save_word_tool, save_excel_tool],
        "persona": "You are the Report Agent. Gather any needed information "
                   "(one or two web_search calls at most), then call "
                   "save_word_report or save_excel_report exactly once to "
                   "persist the output — never more than the plan asks for.",
    },
    "FILES": {
        "tools": [read_file_tool, write_file_tool],
        "persona": "You are the File Operations Agent. Read and write local "
                   "files precisely as requested.",
    },
    "GENERAL": {
        "tools": [search_tool, read_file_tool, write_file_tool, save_word_tool, save_excel_tool],
        "persona": "You are the General Agent, handling conversation, reasoning, "
                   "writing, coding, and math. Use tools only if the task genuinely "
                   "needs web data or files.",
    },
}

ALL_TOOLS = [search_tool, read_file_tool, write_file_tool, save_word_tool, save_excel_tool]
TOOL_MAP = {t.name: t for t in ALL_TOOLS}

PLANNER_PROMPT = """Analyse the user's request and respond with ONLY a JSON object
(no markdown fences, no commentary) in this exact shape:

{{
  "route": "RESEARCH" | "REPORT" | "FILES" | "GENERAL",
  "plan": ["short imperative step 1", "short imperative step 2", ...],
  "tool_budget": <integer>
}}

Guidance:
- route: RESEARCH = needs current web info only. REPORT = needs a Word/Excel
  file saved. FILES = needs a local text file read/written. GENERAL =
  everything else (chat, writing, code, math — no tools needed at all).
- plan: 1 to 4 short steps, the MINIMUM needed to fully satisfy the request.
  Do not add steps for things the user didn't ask for.
- tool_budget: total number of tool calls allowed to complete the whole plan
  (typically 1-2 per plan step; a simple report is usually 2-3 total).

User request: {query}

JSON:"""


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    route: str
    plan: list
    tool_budget: int


def _last_human_content(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content
    return ""


def _parse_plan_json(text: str) -> dict:
    """Best-effort JSON extraction — models sometimes wrap JSON in prose or fences."""
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
        route = str(data.get("route", "GENERAL")).upper()
        if route not in ROUTES:
            route = "GENERAL"
        plan = data.get("plan") or ["Complete the user's request."]
        if not isinstance(plan, list):
            plan = [str(plan)]
        budget = data.get("tool_budget", 4)
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            budget = 4
        budget = max(1, min(budget, 10))
        return {"route": route, "plan": [str(s) for s in plan][:6], "tool_budget": budget}
    except Exception:
        return {"route": "GENERAL", "plan": ["Complete the user's request."], "tool_budget": 4}


def _clean_ai_message(m: BaseMessage) -> BaseMessage:
    """Strip provider-specific extra fields (e.g. reasoning/channel metadata
    some Groq models attach) before replaying a message back into a fresh
    request — carrying those through verbatim can trigger 400s on some
    models when a fallback provider is used mid-conversation."""
    if isinstance(m, AIMessage):
        return AIMessage(content=m.content, tool_calls=getattr(m, "tool_calls", None) or [])
    return m


def build_graph(recursion_limit: int = 25):
    """Build and compile the multi-agent LangGraph. Requires GROQ_API_KEY."""
    if not os.getenv("GROQ_API_KEY"):
        raise ValueError(
            "GROQ_API_KEY not set. Get a free key (no card required) at "
            "https://console.groq.com/keys and add it to your .env / Render env vars."
        )

    planner_llm = get_llm("fast")
    route_llms = {name: get_llm("quality", tools=cfg["tools"]) for name, cfg in ROUTES.items()}
    # No tools bound at all — used once the budget is exhausted so the model
    # is *structurally* unable to emit another tool call, no matter how
    # insistent it is. This guarantees the loop terminates.
    forced_final_llm = get_llm("quality")

    def planner_node(state: AgentState):
        query = _last_human_content(state["messages"])
        parsed = {"route": "GENERAL", "plan": ["Complete the user's request."], "tool_budget": 4}
        try:
            resp = planner_llm.invoke([HumanMessage(content=PLANNER_PROMPT.format(query=query))])
            parsed = _parse_plan_json(resp.content or "")
        except Exception as e:
            log.warning("Planner failed (%s); defaulting to GENERAL, budget=4.", e)
        log.info("Plan -> route=%s budget=%s steps=%s", parsed["route"], parsed["tool_budget"], parsed["plan"])
        return parsed

    def agent_node(state: AgentState):
        route = state.get("route", "GENERAL")
        plan = state.get("plan") or ["Complete the user's request."]
        budget = state.get("tool_budget", 4)
        exhausted = budget <= 0
        llm_with_tools = forced_final_llm if exhausted else route_llms.get(route, route_llms["GENERAL"])

        plan_text = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan))
        if exhausted:
            budget_note = ("Your tool budget for this task is used up. Tools are no longer "
                           "available — write your final answer now, summarising what you accomplished.")
        else:
            budget_note = (f"Tool calls remaining: {budget}. When this reaches 0 you MUST answer "
                           f"with plain text instead of calling a tool.")
        system = SystemMessage(content=(
            f"{BASE_SYSTEM_PROMPT}\n\n{ROUTES[route]['persona']}\n\n"
            f"PLAN FOR THIS REQUEST:\n{plan_text}\n\n{budget_note}"
        ))
        convo = [system] + [
            _clean_ai_message(m) for m in state["messages"] if not isinstance(m, SystemMessage)
        ]
        response = llm_with_tools.invoke(convo)
        return {"messages": [response]}

    def tool_node(state: AgentState):
        last_msg = state["messages"][-1]
        calls = getattr(last_msg, "tool_calls", []) or []
        budget = state.get("tool_budget", 0)
        results = []
        for call in calls:
            name = call["name"]
            args = call.get("args", {})
            call_id = call.get("id") or name

            if budget <= 0:
                content = ("Tool budget exhausted for this task — do not call any more "
                           "tools. Summarise what you've completed so far as your final answer.")
                log.info("Tool call blocked (budget exhausted): %s", name)
            else:
                log.info("Tool call: %s | args=%s | budget_before=%d", name, args, budget)
                if name not in TOOL_MAP:
                    content = f"Error: tool '{name}' not found."
                else:
                    try:
                        content = TOOL_MAP[name].invoke(args)
                    except Exception as e:
                        content = f"Tool error: {e}"
                budget -= 1
                log.info("Tool result preview: %s", str(content)[:200])

            # Proper ToolMessage objects are required here — plain dicts break
            # the Groq/OpenAI-compatible message schema and cause 400 errors.
            results.append(ToolMessage(content=str(content), tool_call_id=call_id, name=name))
        return {"messages": results, "tool_budget": budget}

    def should_continue(state: AgentState):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("planner", planner_node)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("planner")
    graph.add_edge("planner", "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    compiled = graph.compile()
    compiled.__recursion_limit__ = recursion_limit
    return compiled


def _final_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return "Task completed. Check the output/ folder for any saved files."


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    if "rate limit" in msg.lower() or "429" in msg:
        return ("All configured LLM providers are currently rate-limited. "
                "Wait a minute and try again, or add a second free provider "
                "key (GOOGLE_API_KEY / OPENROUTER_API_KEY / CEREBRAS_API_KEY) "
                "for automatic fallback.")
    return f"Sorry, something went wrong: {msg}"


def run_agent(app, user_input: str, history: list) -> tuple[str, list]:
    """Run one turn of the agent (non-streaming). Returns (response_text, updated_history)."""
    messages = list(history) + [HumanMessage(content=user_input)]
    recursion_limit = getattr(app, "__recursion_limit__", 25)

    try:
        result = app.invoke(
            {"messages": messages, "route": "GENERAL", "plan": [], "tool_budget": 4},
            config={"recursion_limit": recursion_limit},
        )
        all_messages = result["messages"]
        return _final_text(all_messages), all_messages
    except GraphRecursionError:
        log.error("Agent hit recursion limit (%d) without finishing.", recursion_limit)
        return ("I wasn't able to finish this task within my step limit — it may be too "
                "complex for one turn. Try breaking it into smaller steps."), messages
    except Exception as e:
        log.error("Agent error: %s", e)
        return _friendly_error(e), messages


def stream_agent_events(app, user_input: str, history: list) -> Iterator[dict]:
    """Run one turn of the agent, yielding a small event dict after every
    meaningful step (plan, tool call, tool result, final answer) so a UI can
    show live progress instead of a single spinner. The final event has
    type 'done' and carries the full updated history for persistence.
    """
    messages = list(history) + [HumanMessage(content=user_input)]
    recursion_limit = getattr(app, "__recursion_limit__", 25)
    init_state = {"messages": messages, "route": "GENERAL", "plan": [], "tool_budget": 4}

    step_counter = 0
    final_state = init_state

    try:
        for mode, chunk in app.stream(init_state, config={"recursion_limit": recursion_limit},
                                       stream_mode=["updates", "values"]):
            if mode == "values":
                final_state = chunk
                continue

            # mode == "updates": {node_name: partial_state}
            for node_name, partial in chunk.items():
                if node_name == "planner":
                    yield {
                        "type": "plan",
                        "route": partial.get("route", "GENERAL"),
                        "steps": partial.get("plan", []),
                        "tool_budget": partial.get("tool_budget", 4),
                    }
                elif node_name == "agent":
                    for m in partial.get("messages", []):
                        if not isinstance(m, AIMessage):
                            continue
                        if getattr(m, "tool_calls", None):
                            for call in m.tool_calls:
                                step_counter += 1
                                yield {
                                    "type": "tool_call",
                                    "step": step_counter,
                                    "tool": call["name"],
                                    "args": call.get("args", {}),
                                }
                        elif m.content:
                            yield {"type": "final", "text": m.content}
                elif node_name == "tools":
                    for m in partial.get("messages", []):
                        if isinstance(m, ToolMessage):
                            yield {
                                "type": "tool_result",
                                "tool": m.name,
                                "preview": str(m.content)[:400],
                            }

        all_messages = final_state.get("messages", messages)
        yield {"type": "done", "response": _final_text(all_messages), "messages": all_messages}

    except GraphRecursionError:
        log.error("Agent hit recursion limit (%d) without finishing.", recursion_limit)
        text = ("I wasn't able to finish this task within my step limit — it may be too "
                "complex for one turn. Try breaking it into smaller steps.")
        yield {"type": "error", "message": text}
        yield {"type": "done", "response": text, "messages": messages}
    except Exception as e:
        log.error("Agent stream error: %s", e)
        text = _friendly_error(e)
        yield {"type": "error", "message": text}
        yield {"type": "done", "response": text, "messages": messages}
