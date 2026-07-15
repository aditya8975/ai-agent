"""
tools/registry.py — lightweight tool auto-discovery.

Instead of hand-importing every tool in graph.py and hand-maintaining which
route gets which tool, each tool module calls `register(tool, routes=[...])`
once at import time. graph.py calls `load_tools()` once at startup and gets
back a route -> [tools] map and a name -> tool map, built automatically from
whatever tool modules exist in this package.

Adding a new capability is now: drop a new file in tools/, call register()
at the bottom of it, done — no other file needs to change.
"""

import importlib
import pkgutil
import logging

log = logging.getLogger(__name__)

_REGISTRY: list[tuple] = []  # [(tool, {route, ...}), ...]


def register(tool, routes):
    """Call at module load time: register(my_tool, routes=["RESEARCH", "GENERAL"])"""
    _REGISTRY.append((tool, set(routes)))
    return tool


def load_tools():
    """Import every tool module so their register() calls run, then return
    (route_tools: dict[str, list[Tool]], all_tools: list[Tool], tool_map: dict[str, Tool])."""
    _REGISTRY.clear()
    import tools

    for _, modname, _ in pkgutil.iter_modules(tools.__path__):
        if modname == "registry":
            continue
        importlib.import_module(f"tools.{modname}")
        log.debug("Loaded tool module: tools.%s", modname)

    route_tools: dict = {}
    all_tools = []
    tool_map = {}
    for tool, routes in _REGISTRY:
        if tool.name not in tool_map:
            all_tools.append(tool)
            tool_map[tool.name] = tool
        for r in routes:
            bucket = route_tools.setdefault(r, [])
            if tool.name not in [t.name for t in bucket]:
                bucket.append(tool)

    log.info("Tool registry loaded: %d tools across routes %s",
              len(all_tools), {r: [t.name for t in ts] for r, ts in route_tools.items()})
    return route_tools, all_tools, tool_map
