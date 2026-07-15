"""tools/search_tool.py — free, keyless web search.

Primary: DuckDuckGo via the `ddgs` package (the maintained successor to the
now-stale `duckduckgo_search` package). `ddgs`'s default backend="auto" will
cascade through up to 6 search engines one at a time on failure (DuckDuckGo,
Google, Startpage, Brave, Mojeek, Grokipedia...) which can take 8-10+
seconds per call and eats into the agent's tool-call budget. We instead pin
a short, fast, reliable backend list and enforce a hard wall-clock timeout,
falling back to Wikipedia's REST API (always free, no key) if that's exceeded.
"""

import logging
import time
import concurrent.futures
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

log = logging.getLogger(__name__)

# Short, fast list instead of ddgs's default "auto" (which tries ~6 engines
# sequentially). Order = fastest/most-reliable first.
SEARCH_BACKENDS = "duckduckgo,brave,mojeek"
SEARCH_TIMEOUT_SECONDS = 8


class SearchInput(BaseModel):
    query: str


def _ddg_search(query: str, max_results: int = 5):
    try:
        from ddgs import DDGS  # maintained package (pip install ddgs)
    except ImportError:
        from duckduckgo_search import DDGS  # legacy fallback if only old pkg installed

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results, backend=SEARCH_BACKENDS))


def _ddg_search_with_timeout(query: str, max_results: int = 5):
    """Run the DuckDuckGo search in a worker thread with a hard timeout, so a
    slow/hanging backend can never stall the whole agent turn."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_ddg_search, query, max_results)
        return future.result(timeout=SEARCH_TIMEOUT_SECONDS)


def _wikipedia_fallback(query: str) -> str:
    try:
        import requests
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 5,
            },
            headers={"User-Agent": "ai-task-agent/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        hits = resp.json().get("query", {}).get("search", [])
        if not hits:
            return f"No results found for: {query}"
        lines = []
        for i, h in enumerate(hits, 1):
            snippet = h.get("snippet", "").replace('<span class="searchmatch">', "").replace("</span>", "")
            title = h.get("title", "")
            lines.append(f"[{i}] {title}\n{snippet}\nSource: https://en.wikipedia.org/wiki/{title.replace(' ', '_')}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Search failed on all providers: {e}"


def web_search(query: str) -> str:
    """Search DuckDuckGo (bounded backend list + hard timeout), falling back
    to Wikipedia if DuckDuckGo is unavailable or too slow."""
    last_err = None
    for attempt in range(2):
        try:
            results = _ddg_search_with_timeout(query)
            if results:
                lines = []
                for i, r in enumerate(results, 1):
                    lines.append(
                        f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\nSource: {r.get('href', '')}"
                    )
                return "\n\n".join(lines)
            break
        except concurrent.futures.TimeoutError:
            last_err = f"timed out after {SEARCH_TIMEOUT_SECONDS}s"
            log.warning("DuckDuckGo search attempt %d timed out for: %s", attempt + 1, query)
            break  # don't retry a timeout — go straight to Wikipedia
        except Exception as e:
            last_err = e
            log.warning("DuckDuckGo search attempt %d failed: %s", attempt + 1, e)
            time.sleep(1)

    log.info("Falling back to Wikipedia search for: %s (ddg error: %s)", query, last_err)
    return _wikipedia_fallback(query)


search_tool = StructuredTool.from_function(
    func=web_search,
    name="web_search",
    description="Search the internet for current information like news, facts, prices, or explanations.",
    args_schema=SearchInput,
)

from tools.registry import register  # noqa: E402
register(search_tool, routes=["RESEARCH", "REPORT", "GENERAL"])
