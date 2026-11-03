"""kosa8 sandbox tools for the OpenAI Agents SDK.

    from agents import Agent, Runner
    from kosa8 import Kosa8Client
    from kosa8.integrations.openai_agents import sandbox_tools

    client = Kosa8Client()
    sbx = client.create_sandbox(name="agent-1", ephemeral=True)
    agent = Agent(name="coder", instructions="...", tools=sandbox_tools(client, sbx["id"]))
    Runner.run_sync(agent, "write and test fizzbuzz in /workspace")

Needs ``pip install 'kosa8[openai-agents]'``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Sequence

from ._common import sandbox_functions

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Kosa8Client

__all__ = ["sandbox_tools"]


def sandbox_tools(
    client: "Kosa8Client",
    sandbox: str,
    *,
    tools: Optional[Sequence[str]] = None,
    session: str = "agent",
    approval: Optional[str] = None,
    name_prefix: str = "sandbox_",
) -> List[Any]:
    """Return ``FunctionTool`` objects that run in one kosa8 sandbox.

    Tool names are ``name_prefix`` + the function name (``sandbox_shell``…).
    See :func:`kosa8.integrations.sandbox_functions` for the arguments.
    """
    try:
        from agents import function_tool
    except ImportError as err:  # pragma: no cover - depends on the environment
        raise ImportError("the OpenAI Agents SDK is not installed: pip install 'kosa8[openai-agents]'") from err
    fns = sandbox_functions(client, sandbox, tools=tools, session=session, approval=approval)
    return [function_tool(fn, name_override=name_prefix + fn.__name__) for fn in fns]
