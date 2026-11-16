"""Google ADK (Agent Development Kit) Harness for SEC Filings Agent.

Implements the Google ADK agent hierarchy:
- ``ADKStepAgent``: Deterministic or composite step agent operating on session state
  with ``before_agent_callback`` and ``after_agent_callback``.
- ``ADKSequentialAgent``: Executes sub-agents in order, respecting conditional routes
  and ``EventActions(escalate=True)`` short-circuits.
- ``ADKLoopAgent``: Repeats sub-agents up to ``max_iterations`` until a verifier or
  tool sets ``EventActions(escalate=True)`` (e.g., verify-until-clean hierarchy &
  extraction audit loops).
- ``ADKLlmAgent``: Tool-calling LLM agent with ``before_tool_callback``,
  ``after_tool_callback``, output schema validation, and ``output_key`` state writing.
- ``ADKGraphRunner``: Compiles an ``ADKSequentialAgent`` pipeline into an invocable
  runner (``.invoke(state)`` and ``.get_graph()``) backed by ``InMemorySessionService``.

When the ``google-adk`` package is installed in the environment, native ADK classes
and ``LiteLlm`` model adapters are bridged automatically.
"""
from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .builtin_tools import (
    InMemoryArtifactService,
    InMemoryMemoryService,
    build_adk_exit_loop_tool,
    build_adk_load_artifacts_tool,
    build_adk_load_memory_tool,
    wrap_custom_function_tool,
)

logger = logging.getLogger(__name__)

# Optional native google-adk detection
try:
    import google.adk as _native_adk  # type: ignore[import-untyped]
    _HAS_NATIVE_ADK = True
except ImportError:
    _native_adk = None
    _HAS_NATIVE_ADK = False


def has_native_google_adk() -> bool:
    """Return True if the ``google-adk`` package is importable."""
    return _HAS_NATIVE_ADK


def build_adk_model(provider: Optional[str] = None) -> Any:
    """Build an ADK model specification (native Gemini string or LiteLlm wrapper)."""
    from filings_agent import config

    prov = (provider or config.LLM_PROVIDER).strip().lower()
    model_name = config.get_model_name(prov)
    if prov == "gemini":
        return model_name

    litellm_model = f"{prov}/{model_name}"
    if _HAS_NATIVE_ADK:
        try:
            from google.adk.models.lite_llm import LiteLlm  # type: ignore[import-untyped]

            return LiteLlm(model=litellm_model)
        except Exception:
            pass
    return litellm_model


@dataclass
class EventActions:
    """ADK EventActions — controls state deltas and loop/pipeline escalation."""

    state_delta: dict[str, Any] = field(default_factory=dict)
    escalate: bool = False
    transfer_to_agent: Optional[str] = None
    skip_summarization: bool = False


@dataclass
class Event:
    """ADK execution event emitted by agents and tools."""

    author: str
    content: Any = None
    actions: EventActions = field(default_factory=EventActions)
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ADKSession:
    """ADK Session holding shared mutable state and event trajectory."""

    id: str
    app_name: str
    user_id: str
    state: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)


class InMemorySessionService:
    """ADK InMemorySessionService managing per-filing and per-company sessions."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str], ADKSession] = {}

    def create_session(
        self,
        *,
        app_name: str = "filings_agent",
        user_id: str = "system",
        session_id: Optional[str] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> ADKSession:
        sid = session_id or uuid.uuid4().hex
        session = ADKSession(
            id=sid,
            app_name=app_name,
            user_id=user_id,
            state=dict(state or {}),
        )
        self._sessions[(app_name, user_id, sid)] = session
        return session

    def get_session(
        self,
        *,
        app_name: str = "filings_agent",
        user_id: str = "system",
        session_id: str,
    ) -> Optional[ADKSession]:
        return self._sessions.get((app_name, user_id, session_id))

    def append_event(self, session: ADKSession, event: Event) -> None:
        if event.actions and event.actions.state_delta:
            session.state.update(event.actions.state_delta)
        session.events.append(event)


@dataclass
class InvocationContext:
    """ADK InvocationContext passed to every agent and callback."""

    session: ADKSession
    session_service: InMemorySessionService
    artifact_service: InMemoryArtifactService = field(default_factory=InMemoryArtifactService)
    memory_service: InMemoryMemoryService = field(default_factory=InMemoryMemoryService)
    agent_name: str = ""
    iteration: int = 0
    end_invocation: bool = False

    @property
    def state(self) -> dict[str, Any]:
        return self.session.state


@dataclass
class ToolContext:
    """ADK ToolContext passed to tools and tool callbacks."""

    invocation_context: InvocationContext
    tool_name: str
    actions: EventActions = field(default_factory=EventActions)

    @property
    def state(self) -> dict[str, Any]:
        return self.invocation_context.state


class FunctionTool:
    """ADK FunctionTool wrapping a Python callable or LangChain @tool."""

    def __init__(self, func: Any, *, name: Optional[str] = None, description: Optional[str] = None) -> None:
        self.func = func
        self.name = name or getattr(func, "name", None) or getattr(func, "__name__", "tool")
        self.description = (
            description
            or getattr(func, "description", None)
            or (getattr(func, "__doc__", "") or "").strip()
        )

    def invoke(self, args: dict[str, Any], tool_context: Optional[ToolContext] = None) -> Any:
        if hasattr(self.func, "invoke") and callable(self.func.invoke):
            return self.func.invoke(args)
        if callable(self.func):
            return self.func(**(args or {}))
        raise TypeError(f"Tool {self.name!r} is not callable")


class ADKStepAgent:
    """Single deterministic or composite ADK agent step in a workflow."""

    def __init__(
        self,
        name: str,
        fn: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        description: str = "",
        output_key: Optional[str] = None,
        before_agent_callback: Optional[Callable[[InvocationContext], Optional[dict[str, Any]]]] = None,
        after_agent_callback: Optional[Callable[[InvocationContext, dict[str, Any]], Optional[dict[str, Any]]]] = None,
    ) -> None:
        self.name = name
        self.fn = fn
        self.description = description
        self.output_key = output_key
        self.before_agent_callback = before_agent_callback
        self.after_agent_callback = after_agent_callback

    def run(self, ctx: InvocationContext) -> Event:
        ctx.agent_name = self.name
        if callable(self.before_agent_callback):
            override = self.before_agent_callback(ctx)
            if isinstance(override, dict):
                event = Event(
                    author=self.name,
                    content="before_agent_callback override",
                    actions=EventActions(state_delta=override),
                )
                ctx.session_service.append_event(ctx.session, event)
                return event

        new_state = self.fn(dict(ctx.state))
        if not isinstance(new_state, dict):
            new_state = dict(ctx.state)

        if self.output_key and self.output_key in new_state:
            new_state[self.output_key] = new_state[self.output_key]

        if callable(self.after_agent_callback):
            post = self.after_agent_callback(ctx, new_state)
            if isinstance(post, dict):
                new_state = post

        escalate = new_state.get("status") in ("failed", "skipped")
        event = Event(
            author=self.name,
            content=f"status={new_state.get('status')}",
            actions=EventActions(state_delta=new_state, escalate=escalate),
        )
        ctx.session.state.clear()
        ctx.session.state.update(new_state)
        ctx.session.events.append(event)
        return event


class ADKLoopAgent:
    """ADK LoopAgent — repeats ``sub_agents`` until ``escalate=True`` or ``max_iterations``."""

    def __init__(
        self,
        name: str,
        sub_agents: list[Any],
        *,
        max_iterations: int = 4,
        description: str = "",
        condition_fn: Optional[Callable[[dict[str, Any], int], bool]] = None,
    ) -> None:
        self.name = name
        self.sub_agents = list(sub_agents)
        self.max_iterations = max(1, int(max_iterations))
        self.description = description
        self.condition_fn = condition_fn

    def run(self, ctx: InvocationContext) -> Event:
        last_event: Optional[Event] = None
        for i in range(self.max_iterations):
            ctx.iteration = i
            if callable(self.condition_fn) and not self.condition_fn(ctx.state, i):
                break
            escalated = False
            for sub in self.sub_agents:
                last_event = sub.run(ctx)
                if last_event.actions.escalate:
                    escalated = True
                    break
            if escalated:
                break
        return last_event or Event(author=self.name, content="loop_completed")


class ADKSequentialAgent:
    """ADK SequentialAgent — orchestrates sub-agents with optional routing table."""

    def __init__(
        self,
        name: str,
        sub_agents: list[Any],
        *,
        description: str = "",
        entry_point: Optional[str] = None,
        edges: Optional[dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.sub_agents = list(sub_agents)
        self.description = description
        self._agent_map: dict[str, Any] = {a.name: a for a in self.sub_agents}
        self.entry_point = entry_point or (self.sub_agents[0].name if self.sub_agents else "")
        self.edges: dict[str, Any] = dict(edges or {})

    def run(self, ctx: InvocationContext) -> Event:
        last_event: Optional[Event] = None
        if not self.edges:
            for agent in self.sub_agents:
                last_event = agent.run(ctx)
                if last_event.actions.escalate:
                    break
            return last_event or Event(author=self.name, content="empty")

        current: Optional[str] = self.entry_point
        visited_steps = 0
        max_steps = max(len(self.sub_agents) * 4, 32)
        while current and current not in ("__end__", "END") and visited_steps < max_steps:
            visited_steps += 1
            agent = self._agent_map.get(current)
            if agent is None:
                logger.error("ADKSequentialAgent %s: unknown sub-agent %r", self.name, current)
                break
            last_event = agent.run(ctx)
            route_spec = self.edges.get(current)
            if route_spec is None:
                break
            if callable(route_spec):
                next_target = route_spec(ctx.state)
                current = next_target
            elif isinstance(route_spec, str):
                current = route_spec
            else:
                break

        return last_event or Event(author=self.name, content="completed")


class ADKLlmAgent:
    """ADK LlmAgent — executes tool-calling reasoning with tool callbacks and state output."""

    def __init__(
        self,
        name: str,
        *,
        instruction: str,
        tools: list[Any],
        chat_llm: Any = None,
        finalize_name: str = "finalize",
        finalize_description: Optional[str] = None,
        output_key: Optional[str] = None,
        max_steps: Optional[int] = None,
        before_tool_callback: Optional[Callable[[ToolContext, dict[str, Any]], Optional[Any]]] = None,
        after_tool_callback: Optional[Callable[[ToolContext, dict[str, Any], Any], Optional[Any]]] = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.name = name
        self.instruction = instruction
        self.tools = [wrap_custom_function_tool(t) for t in tools]
        self.model = build_adk_model()
        self.chat_llm = chat_llm
        self.finalize_name = finalize_name
        self.finalize_description = finalize_description
        self.output_key = output_key
        self.max_steps = max_steps
        self.before_tool_callback = before_tool_callback
        self.after_tool_callback = after_tool_callback
        self.on_event = on_event

    def invoke_prompt(
        self,
        user_prompt: str,
        *,
        ticker: str = "?",
        session: Optional[ADKSession] = None,
        artifact_service: Optional[InMemoryArtifactService] = None,
        memory_service: Optional[InMemoryMemoryService] = None,
        include_adk_builtins: bool = True,
        finalize_validator: Optional[Callable[[dict[str, Any]], Optional[str]]] = None,
    ) -> dict[str, Any]:
        """Run the ADK LlmAgent tool-calling loop and return the structured decision dict."""
        from filings_agent.agent.loop import run_agent_loop

        active_tools = list(self.tools)
        existing_names = {getattr(t, "name", "") for t in active_tools}
        if include_adk_builtins:
            if artifact_service is not None and "load_artifacts" not in existing_names:
                active_tools.append(
                    build_adk_load_artifacts_tool(
                        artifact_service,
                        session_id=session.id if session else "default",
                    )
                )
            if memory_service is not None and "load_memory" not in existing_names:
                active_tools.append(
                    build_adk_load_memory_tool(
                        memory_service,
                        cik=str((session.state if session else {}).get("cik") or ""),
                    )
                )

        result = run_agent_loop(
            self.instruction,
            user_prompt,
            active_tools,
            ticker=ticker,
            max_steps=self.max_steps,
            finalize_name=self.finalize_name,
            finalize_description=self.finalize_description,
            chat_llm=self.chat_llm,
            on_event=self.on_event,
            before_tool_callback=self.before_tool_callback,
            after_tool_callback=self.after_tool_callback,
            finalize_validator=finalize_validator,
        )
        if session is not None and self.output_key:
            session.state[self.output_key] = result
        return result


@dataclass
class _GraphStructureView:
    """Compatibility view for ``graph.get_graph().nodes`` inspection in tests."""

    nodes: dict[str, Any]
    edges: dict[str, Any]


class ADKGraphRunner:
    """Compiles an ``ADKSequentialAgent`` into a stateful runner with ``.invoke(state)``."""

    def __init__(
        self,
        root_agent: ADKSequentialAgent,
        *,
        session_service: Optional[InMemorySessionService] = None,
        artifact_service: Optional[InMemoryArtifactService] = None,
        memory_service: Optional[InMemoryMemoryService] = None,
        app_name: str = "filings_agent",
    ) -> None:
        self.root_agent = root_agent
        self.session_service = session_service or InMemorySessionService()
        self.artifact_service = artifact_service or InMemoryArtifactService()
        self.memory_service = memory_service or InMemoryMemoryService()
        self.app_name = app_name
        self.last_session: Optional[ADKSession] = None

    def invoke(self, state: dict[str, Any], config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Execute the ADK agent pipeline on *state* and return the final state dict."""
        session = self.session_service.create_session(
            app_name=self.app_name,
            user_id=str(state.get("cik") or state.get("ticker") or "system"),
            state=dict(state),
        )
        # Stage SEC filing documents into ADK's InMemoryArtifactService so agents can
        # inspect them via ADK's built-in `load_artifacts` tool.
        for artifact_key in ("sec_presentation", "mda_text", "html_text", "reporting_period"):
            if state.get(artifact_key) is not None:
                self.artifact_service.save_artifact(
                    artifact_key,
                    state[artifact_key],
                    session_id=session.id,
                )
        ctx = InvocationContext(
            session=session,
            session_service=self.session_service,
            artifact_service=self.artifact_service,
            memory_service=self.memory_service,
            agent_name=self.root_agent.name,
        )
        self.root_agent.run(ctx)
        self.last_session = session
        return dict(session.state)

    def get_graph(self) -> _GraphStructureView:
        """Return a graph structure view with ``.nodes`` and ``.edges``."""
        nodes = {"__start__": "__start__", "__end__": "__end__"}
        for agent in self.root_agent.sub_agents:
            nodes[agent.name] = agent
        return _GraphStructureView(nodes=nodes, edges=dict(self.root_agent.edges))
