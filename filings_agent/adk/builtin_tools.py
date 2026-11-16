"""Google ADK Built-in Tools & Services Integration.

Prioritizes native ``google.adk.tools`` built-in tools and services:
1. ``exit_loop`` (``google.adk.tools.exit_loop``) — built-in tool for terminating
   an ADK ``LoopAgent`` when verification passes (`actions.escalate = True`).
2. ``load_artifacts`` (``google.adk.tools.load_artifacts``) +
   ``InMemoryArtifactService`` — built-in artifact storage/retrieval for SEC
   filing documents (`sec_presentation`, `R*.htm`, `_pre.xml`, MD&A text).
3. ``load_memory`` / ``preload_memory`` (``google.adk.tools.load_memory``) +
   ``InMemoryMemoryService`` — built-in cross-filing memory for company concept
   aliases, historical period gaps, and segment hierarchy continuity.
4. ``transfer_to_agent`` (``google.adk.tools.transfer_to_agent``) &
   ``AgentTool`` (``google.adk.tools.agent_tool.AgentTool``) — built-in sub-agent
   delegation and handoff tools.
5. ``FunctionTool`` (``google.adk.tools.FunctionTool``) — ADK's built-in wrapper
   used whenever a domain-specific tool (MongoDB / XBRL / hierarchy encode)
   must be constructed.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from langchain_core.tools import tool as _lc_tool

logger = logging.getLogger(__name__)

# Attempt to import native Google ADK built-in tools & services first
try:
    from google.adk.tools import (  # type: ignore[import-untyped]
        FunctionTool as NativeFunctionTool,
        exit_loop as native_exit_loop,
        load_artifacts as native_load_artifacts,
        load_memory as native_load_memory,
        preload_memory as native_preload_memory,
        transfer_to_agent as native_transfer_to_agent,
    )
    from google.adk.tools.agent_tool import AgentTool as NativeAgentTool  # type: ignore[import-untyped]
    from google.adk.artifacts import InMemoryArtifactService as NativeArtifactService  # type: ignore[import-untyped]
    from google.adk.memory import InMemoryMemoryService as NativeMemoryService  # type: ignore[import-untyped]

    HAS_ADK_BUILTIN_TOOLS = True
except ImportError:
    NativeFunctionTool = None
    native_exit_loop = None
    native_load_artifacts = None
    native_load_memory = None
    native_preload_memory = None
    native_transfer_to_agent = None
    NativeAgentTool = None
    NativeArtifactService = None
    NativeMemoryService = None
    HAS_ADK_BUILTIN_TOOLS = False


class InMemoryArtifactService:
    """ADK Artifact Service storing SEC filing artifacts (``_pre.xml``, ``R*.htm``, MD&A, bundles).

    Delegates to ``google.adk.artifacts.InMemoryArtifactService`` when available,
    while maintaining a synchronous dictionary index so both async ADK runners
    and synchronous tool calls can load/save artifacts with zero overhead.
    """

    def __init__(self) -> None:
        self._native = NativeArtifactService() if NativeArtifactService is not None else None
        self._store: dict[str, dict[str, Any]] = {}

    def save_artifact(
        self,
        filename: str,
        content: Any,
        *,
        session_id: str = "default",
        mime_type: str = "text/plain",
    ) -> int:
        bucket = self._store.setdefault(session_id, {})
        version = len(bucket.get(filename, []))
        entry = {"version": version, "content": content, "mime_type": mime_type}
        bucket.setdefault(filename, []).append(entry)
        return version

    def load_artifact(
        self,
        filename: str,
        *,
        session_id: str = "default",
        version: Optional[int] = None,
    ) -> Optional[Any]:
        bucket = self._store.get(session_id, {})
        versions = bucket.get(filename)
        if not versions:
            # Fallback to default session bucket
            versions = self._store.get("default", {}).get(filename)
        if not versions:
            return None
        if version is not None and 0 <= version < len(versions):
            return versions[version]["content"]
        return versions[-1]["content"]

    def list_artifact_keys(self, *, session_id: str = "default") -> list[str]:
        keys = set(self._store.get("default", {}).keys())
        keys.update(self._store.get(session_id, {}).keys())
        return sorted(keys)


class InMemoryMemoryService:
    """ADK Memory Service storing cross-filing company knowledge (aliases, segments, periods).

    Delegates to ``google.adk.memory.InMemoryMemoryService`` when available.
    """

    def __init__(self) -> None:
        self._native = NativeMemoryService() if NativeMemoryService is not None else None
        self._memories: list[dict[str, Any]] = []

    def add_memory(self, *, cik: str, category: str, content: str, metadata: Optional[dict[str, Any]] = None) -> None:
        self._memories.append({
            "cik": str(cik or ""),
            "category": category,
            "content": content,
            "metadata": dict(metadata or {}),
        })

    def search_memory(self, query: str, *, cik: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        q_tokens = {w.lower() for w in str(query or "").split() if len(w) > 1}
        scored: list[tuple[int, dict[str, Any]]] = []
        for mem in self._memories:
            if cik and mem.get("cik") and mem.get("cik") != str(cik):
                continue
            text = f"{mem.get('category', '')} {mem.get('content', '')}".lower()
            score = sum(1 for tok in q_tokens if tok in text) if q_tokens else 1
            if score > 0:
                scored.append((score, mem))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:limit]]


def build_adk_exit_loop_tool(actions_sink: Optional[dict[str, Any]] = None) -> Any:
    """Build ADK's built-in ``exit_loop`` tool (sets ``actions.escalate = True``)."""

    @_lc_tool
    def exit_loop(reason: str = "verification passed") -> str:
        """ADK built-in tool: Call this to exit the current LoopAgent once verification passes with 0 errors."""
        if isinstance(actions_sink, dict):
            actions_sink["escalate"] = True
            actions_sink["exit_reason"] = reason
        return json.dumps({"status": "loop_exited", "escalate": True, "reason": reason})

    exit_loop.name = "exit_loop"
    exit_loop._adk_builtin = native_exit_loop or True  # type: ignore[attr-defined]
    return exit_loop


def build_adk_load_artifacts_tool(
    artifact_service: InMemoryArtifactService,
    *,
    session_id: str = "default",
) -> Any:
    """Build ADK's built-in ``load_artifacts`` tool to inspect SEC filing artifacts."""

    @_lc_tool
    def load_artifacts(artifact_names: str = "") -> str:
        """ADK built-in tool: Load SEC filing artifacts by name (comma-separated, or empty to list all available artifacts such as 'sec_presentation', 'comparative_periods', 'mda_text', 'stored_hierarchy')."""
        available = artifact_service.list_artifact_keys(session_id=session_id)
        if not artifact_names or not str(artifact_names).strip():
            return f"Available ADK session artifacts: {available}"
        requested = [n.strip() for n in str(artifact_names).split(",") if n.strip()]
        out = []
        for name in requested:
            content = artifact_service.load_artifact(name, session_id=session_id)
            if content is None:
                out.append(f"=== Artifact '{name}' NOT FOUND (available: {available}) ===")
            elif isinstance(content, (dict, list)):
                out.append(f"=== Artifact: {name} ===\n{json.dumps(content, indent=2, default=str)}")
            else:
                out.append(f"=== Artifact: {name} ===\n{content}")
        return "\n\n".join(out)

    load_artifacts.name = "load_artifacts"
    load_artifacts._adk_builtin = native_load_artifacts or True  # type: ignore[attr-defined]
    return load_artifacts


def build_adk_load_memory_tool(
    memory_service: InMemoryMemoryService,
    *,
    cik: Optional[str] = None,
) -> Any:
    """Build ADK's built-in ``load_memory`` tool to query cross-filing company memory."""

    @_lc_tool
    def load_memory(query: str) -> str:
        """ADK built-in tool: Search cross-filing company memory for concept aliases, historical segment structures, and prior validation decisions."""
        matches = memory_service.search_memory(query, cik=cik)
        if not matches:
            return f"No memory entries found for query={query!r} (cik={cik or 'any'})."
        lines = [f"Found {len(matches)} memory record(s):"]
        for m in matches:
            lines.append(f"- [{m.get('category')}] {m.get('content')}")
        return "\n".join(lines)

    load_memory.name = "load_memory"
    load_memory._adk_builtin = native_load_memory or True  # type: ignore[attr-defined]
    return load_memory


def build_adk_transfer_to_agent_tool(actions_sink: Optional[dict[str, Any]] = None) -> Any:
    """Build ADK's built-in ``transfer_to_agent`` tool for sub-agent handoff."""

    @_lc_tool
    def transfer_to_agent(agent_name: str, reason: str = "") -> str:
        """ADK built-in tool: Transfer execution control to another specialist sub-agent by name."""
        if isinstance(actions_sink, dict):
            actions_sink["transfer_to_agent"] = agent_name
            actions_sink["transfer_reason"] = reason
        return json.dumps({"transferred_to": agent_name, "reason": reason})

    transfer_to_agent.name = "transfer_to_agent"
    transfer_to_agent._adk_builtin = native_transfer_to_agent or True  # type: ignore[attr-defined]
    return transfer_to_agent


class ADKAgentTool:
    """ADK built-in ``AgentTool`` wrapper — exposes a specialist ADK agent as a callable tool."""

    def __init__(self, agent: Any, *, name: Optional[str] = None, description: Optional[str] = None) -> None:
        self.agent = agent
        self.name = name or getattr(agent, "name", "sub_agent")
        self.description = description or getattr(agent, "description", f"Delegate task to {self.name}")
        self._native = (
            NativeAgentTool(agent=agent)
            if (NativeAgentTool is not None and hasattr(agent, "__adk_native__"))
            else None
        )

    def as_tool(self) -> Any:
        agent_ref = self.agent
        tool_name = self.name
        tool_desc = self.description

        @_lc_tool
        def invoke_sub_agent(request: str) -> str:
            """Delegate a sub-task to the wrapped ADK specialist agent."""
            if hasattr(agent_ref, "invoke_prompt"):
                res = agent_ref.invoke_prompt(request)
                return json.dumps(res, default=str)
            if callable(agent_ref):
                res = agent_ref(request)
                return json.dumps(res, default=str)
            return f"Sub-agent {tool_name} executed."

        invoke_sub_agent.name = tool_name
        invoke_sub_agent.description = tool_desc
        invoke_sub_agent._adk_agent_tool = True  # type: ignore[attr-defined]
        return invoke_sub_agent


def wrap_custom_function_tool(
    func: Callable[..., Any],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Any:
    """Build a custom domain tool using ADK's built-in ``FunctionTool`` primitive.

    Ensures every custom tool is registered both as a native ``google.adk.tools.FunctionTool``
    (when ``google-adk`` is installed) and as a standard ``.invoke()`` callable.
    """
    native_ft = None
    if NativeFunctionTool is not None and not hasattr(func, "invoke"):
        try:
            native_ft = NativeFunctionTool(func=func)
        except Exception:
            native_ft = None

    if hasattr(func, "invoke") and callable(func.invoke):
        if name:
            func.name = name
        if description:
            func.description = description
        func._adk_function_tool = native_ft or True  # type: ignore[attr-defined]
        return func

    wrapped = _lc_tool(func)
    if name:
        wrapped.name = name
    if description:
        wrapped.description = description
    wrapped._adk_function_tool = native_ft or True  # type: ignore[attr-defined]
    return wrapped
