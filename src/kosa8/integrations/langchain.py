"""kosa8 sandbox tools for LangChain and LangGraph.

    from langgraph.prebuilt import create_react_agent
    from kosa8 import Kosa8Client
    from kosa8.integrations.langchain import sandbox_tools

    client = Kosa8Client()
    sbx = client.create_sandbox(name="agent-1", ephemeral=True)
    agent = create_react_agent(model, sandbox_tools(client, sbx["id"]))

Needs ``pip install 'kosa8[langchain]'``.
"""

from __future__ import annotations

import inspect
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
    """Return LangChain ``StructuredTool`` objects that run in one kosa8 sandbox.

    See :func:`kosa8.integrations.sandbox_functions` for the arguments.
    """
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as err:  # pragma: no cover - depends on the environment
        raise ImportError("langchain-core is not installed: pip install 'kosa8[langchain]'") from err
    fns = sandbox_functions(client, sandbox, tools=tools, session=session, approval=approval)
    return [
        StructuredTool.from_function(
            fn, name=name_prefix + fn.__name__, description=inspect.getdoc(fn) or fn.__name__,
            parse_docstring=True,
        )
        for fn in fns
    ]
