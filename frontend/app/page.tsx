"use client";
import { useState, useRef, useEffect } from "react";
import { Send, Trash2, Bot, User, Loader2, Zap, Search, FileText, ChevronDown, AlertTriangle } from "lucide-react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type ActivityEvent =
  | { type: "plan"; route: string; steps: string[]; tool_budget: number }
  | { type: "tool_call"; step: number; tool: string; args: Record<string, unknown> }
  | { type: "tool_result"; tool: string; preview: string }
  | { type: "final"; text: string }
  | { type: "error"; message: string };

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  activity?: ActivityEvent[];
  streaming?: boolean;
};

function getSessionId(): string {
  if (typeof window === "undefined") return "server";
  const key = "ai-agent-session-id";
  let id = window.localStorage.getItem(key);
  if (!id) {
    id = "user-" + Math.random().toString(36).slice(2, 9);
    window.localStorage.setItem(key, id);
  }
  return id;
}

const SESSION_ID = getSessionId();

const EXAMPLES = [
  "Search for the top AI news today and save a Word report",
  "Find the latest Python releases and make an Excel table",
  "Search for LangChain tutorials and write a summary to output/notes.txt",
  "What are the top 5 free tools for building AI agents?",
];

const ROUTE_LABEL: Record<string, string> = {
  RESEARCH: "Research Agent",
  REPORT: "Report Agent",
  FILES: "File Agent",
  GENERAL: "General Agent",
};

function ActivityLine({ event }: { event: ActivityEvent }) {
  if (event.type === "plan") {
    return (
      <div>
        <div className="flex items-center gap-1.5 text-blue-700 font-medium">
          <Zap className="w-3 h-3" /> Routed to {ROUTE_LABEL[event.route] || event.route}
        </div>
        <ol className="mt-1 ml-4 list-decimal space-y-0.5 text-gray-500">
          {event.steps.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ol>
      </div>
    );
  }
  if (event.type === "tool_call") {
    const argStr = JSON.stringify(event.args);
    return (
      <div className="flex items-start gap-1.5 text-amber-700">
        <Search className="w-3 h-3 mt-0.5 flex-shrink-0" />
        <span>
          Step {event.step}: calling <code className="bg-amber-50 px-1 rounded">{event.tool}</code>
          <span className="text-gray-400">({argStr.length > 80 ? argStr.slice(0, 80) + "…" : argStr})</span>
        </span>
      </div>
    );
  }
  if (event.type === "tool_result") {
    return (
      <div className="flex items-start gap-1.5 text-emerald-700">
        <FileText className="w-3 h-3 mt-0.5 flex-shrink-0" />
        <span className="text-gray-500">
          <span className="text-emerald-700 font-medium">{event.tool}</span> result:{" "}
          {event.preview.length > 140 ? event.preview.slice(0, 140) + "…" : event.preview}
        </span>
      </div>
    );
  }
  if (event.type === "error") {
    return (
      <div className="flex items-start gap-1.5 text-red-600">
        <AlertTriangle className="w-3 h-3 mt-0.5 flex-shrink-0" /> {event.message}
      </div>
    );
  }
  return null;
}

function ActivityPanel({ activity, streaming }: { activity: ActivityEvent[]; streaming?: boolean }) {
  const [open, setOpen] = useState(true);
  if (!activity || activity.length === 0) return null;
  const visible = activity.filter((e) => e.type !== "final");
  return (
    <div className="mt-2 rounded-lg border border-gray-100 bg-gray-50 text-xs overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-3 py-1.5 text-gray-500 hover:bg-gray-100 transition-colors"
      >
        <span className="flex items-center gap-1.5">
          {streaming && <Loader2 className="w-3 h-3 animate-spin" />}
          {streaming ? "Working…" : `Agent steps (${visible.length})`}
        </span>
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="px-3 pb-2.5 pt-1 space-y-1.5 font-mono border-t border-gray-100">
          {visible.map((e, i) => (
            <ActivityLine key={i} event={e} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // Restore prior conversation for this session on first load (if Supabase
  // is configured on the backend; otherwise this just returns an empty list).
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(`${API_URL}/history/${SESSION_ID}`);
        if (!res.ok) return;
        const data = await res.json();
        const restored: Message[] = (data.messages || [])
          .filter((m: any) => m.role === "human" || m.role === "ai")
          .map((m: any, i: number) => ({
            id: `restored-${i}`,
            role: m.role === "human" ? "user" : "assistant",
            content: m.content,
            timestamp: new Date(),
          }));
        if (restored.length) setMessages(restored);
      } catch {
        // Backend may be cold-starting or offline — fail silently, chat still works.
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function sendMessage(text?: string) {
    const msg = (text || input).trim();
    if (!msg || loading) return;
    setInput("");
    setError("");

    const userMsg: Message = {
      id: Date.now().toString(),
      role: "user",
      content: msg,
      timestamp: new Date(),
    };
    const assistantId = (Date.now() + 1).toString();
    setMessages((prev) => [
      ...prev,
      userMsg,
      { id: assistantId, role: "assistant", content: "", timestamp: new Date(), activity: [], streaming: true },
    ]);
    setLoading(true);

    const liveActivity: ActivityEvent[] = [];
    const updateAssistant = (patch: Partial<Message>) => {
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, ...patch } : m)));
    };

    try {
      const res = await fetch(`${API_URL}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg, session_id: SESSION_ID }),
      });
      if (!res.ok || !res.body) throw new Error(`Server error: ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finalText = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data:")) continue;
          const jsonStr = line.slice(5).trim();
          if (!jsonStr) continue;

          let evt: any;
          try {
            evt = JSON.parse(jsonStr);
          } catch {
            continue;
          }

          if (evt.type === "done") {
            finalText = evt.response || finalText;
            continue;
          }
          liveActivity.push(evt);
          updateAssistant({ activity: [...liveActivity] });
        }
      }

      updateAssistant({
        content: finalText || "Task completed. Check the output/ folder for any saved files.",
        streaming: false,
      });
    } catch (e: any) {
      setError(e.message || "Failed to reach the agent. Is the backend running?");
      setMessages((prev) => prev.filter((m) => m.id !== assistantId));
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  async function clearHistory() {
    try {
      await fetch(`${API_URL}/history/${SESSION_ID}`, { method: "DELETE" });
    } catch {}
    setMessages([]);
    setError("");
  }

  function handleKey(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  return (
    <div className="flex flex-col h-screen max-w-3xl mx-auto px-4">
      {/* Header */}
      <header className="flex items-center justify-between py-4 border-b border-gray-200">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center">
            <Zap className="w-4 h-4 text-white" />
          </div>
          <div>
            <h1 className="font-semibold text-gray-900">AI Task Agent</h1>
            <p className="text-xs text-gray-500">Multi-agent · Groq + LangGraph</p>
          </div>
        </div>
        {messages.length > 0 && (
          <button
            onClick={clearHistory}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-red-500 transition-colors px-2 py-1 rounded hover:bg-red-50"
          >
            <Trash2 className="w-3 h-3" /> Clear
          </button>
        )}
      </header>

      {/* Messages */}
      <main className="flex-1 overflow-y-auto py-6 space-y-4">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-center gap-6">
            <div className="w-14 h-14 bg-blue-50 rounded-2xl flex items-center justify-center">
              <Bot className="w-7 h-7 text-blue-600" />
            </div>
            <div>
              <h2 className="font-semibold text-gray-800 mb-1">What can I help you automate?</h2>
              <p className="text-sm text-gray-500">I can search the web, create reports, and read/write files — and show you every step live.</p>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-lg">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  onClick={() => sendMessage(ex)}
                  className="text-left text-sm px-4 py-3 rounded-xl border border-gray-200 hover:border-blue-300 hover:bg-blue-50 transition-all text-gray-600 hover:text-blue-700"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) => (
          <div key={m.id} className={`flex gap-3 ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            {m.role === "assistant" && (
              <div className="w-7 h-7 bg-blue-600 rounded-full flex items-center justify-center flex-shrink-0 mt-1">
                <Bot className="w-4 h-4 text-white" />
              </div>
            )}
            <div
              className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap ${
                m.role === "user"
                  ? "bg-blue-600 text-white rounded-br-sm"
                  : "bg-white border border-gray-200 text-gray-800 rounded-bl-sm shadow-sm"
              }`}
            >
              {m.role === "assistant" && <ActivityPanel activity={m.activity || []} streaming={m.streaming} />}
              {m.content && (
                <div className={m.activity && m.activity.length ? "mt-2" : ""}>{m.content}</div>
              )}
              {m.role === "assistant" && m.streaming && !m.content && (!m.activity || m.activity.length === 0) && (
                <Loader2 className="w-4 h-4 animate-spin text-blue-500" />
              )}
              <div className={`text-[10px] mt-1 ${m.role === "user" ? "text-blue-200" : "text-gray-400"}`}>
                {m.timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </div>
            </div>
            {m.role === "user" && (
              <div className="w-7 h-7 bg-gray-200 rounded-full flex items-center justify-center flex-shrink-0 mt-1">
                <User className="w-4 h-4 text-gray-600" />
              </div>
            )}
          </div>
        ))}

        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 text-sm px-4 py-3 rounded-xl">
            {error}
          </div>
        )}

        <div ref={bottomRef} />
      </main>

      {/* Input */}
      <div className="py-4 border-t border-gray-200">
        <div className="flex gap-2 items-end bg-white border border-gray-200 rounded-2xl px-4 py-3 shadow-sm focus-within:border-blue-400 focus-within:ring-1 focus-within:ring-blue-100 transition-all">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Type a task… (Enter to send, Shift+Enter for new line)"
            rows={1}
            className="flex-1 resize-none outline-none text-sm text-gray-800 placeholder-gray-400 bg-transparent max-h-32"
          />
          <button
            onClick={() => sendMessage()}
            disabled={!input.trim() || loading}
            className="w-8 h-8 bg-blue-600 rounded-xl flex items-center justify-center disabled:opacity-40 hover:bg-blue-700 transition-colors flex-shrink-0"
          >
            <Send className="w-4 h-4 text-white" />
          </button>
        </div>
        <p className="text-center text-xs text-gray-400 mt-2">
          Agent can search the web, create Word/Excel files, and read/write local files — watch it work in "Agent steps" above each reply
        </p>
      </div>
    </div>
  );
}
