"""Adapters that hand a kosa8 sandbox to agent frameworks as tools.

- :mod:`kosa8.integrations.openai_agents` for the OpenAI Agents SDK
- :mod:`kosa8.integrations.langchain` for LangChain and LangGraph

Both build on :func:`sandbox_functions`: plain Python functions, one per tool,
bound to one sandbox, that take simple arguments and return text. Use those
directly with any other framework that turns typed functions into tools.
"""

from ._common import DEFAULT_TOOLS, sandbox_functions

__all__ = ["DEFAULT_TOOLS", "sandbox_functions"]
