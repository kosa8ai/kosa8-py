"""kosa8: microVM sandboxes for AI agents, with checkpoint, rewind, fork and explore.

The Python counterpart of ``@kosa8/sdk``. No dependencies: it speaks HTTP to the
local kosa8 daemon over its unix socket, or to a remote worker (``KOSA8_HOST``).

>>> from kosa8 import Kosa8Client
>>> k = Kosa8Client()
>>> sb = k.sandbox(k.create_sandbox(name="demo", egress="deny")["sandbox"]["id"])
>>> sb.pull("alpine:latest")
>>> sb.run("alpine:latest", ["echo", "hi"]).stdout
'hi\\n'
"""

from .checkpoints import (
    MAX_CHECKPOINTS_PER_SANDBOX,
    CheckpointBook,
    FileCheckpointStore,
    MemoryCheckpointStore,
)
from .cloud import CloudExecResult, CloudFunctions, CloudSandbox, CloudSandboxes, Kosa8Cloud
from .client import ExecResult, ExploreBranch, ExploreResult, Kosa8Client, Sandbox
from .toolbox import Artifact, CodeResult, GitResult, ShellResult
from .errors import (
    Kosa8ApiError,
    Kosa8ApprovalRequiredError,
    Kosa8AuthError,
    Kosa8BadRequestError,
    Kosa8CheckpointError,
    Kosa8ConflictError,
    Kosa8ConnectionError,
    Kosa8Error,
    Kosa8NotFoundError,
    Kosa8PaymentRequiredError,
    Kosa8PolicyError,
    Kosa8RateLimitError,
    Kosa8StreamError,
    Kosa8TimeoutError,
    Kosa8ProtocolError,
    Kosa8UnsupportedError,
    error_from_response,
)
from .transport import Transport, default_socket_path, default_worker_file, resolve_endpoint

__version__ = "0.2.0"

__all__ = [
    "Kosa8Client",
    "Kosa8Cloud",
    "CloudSandbox",
    "CloudSandboxes",
    "CloudFunctions",
    "CloudExecResult",
    "Kosa8PaymentRequiredError",
    "Kosa8RateLimitError",
    "Sandbox",
    "ExecResult",
    "ExploreBranch",
    "ExploreResult",
    "CodeResult",
    "ShellResult",
    "GitResult",
    "Artifact",
    "Transport",
    "default_socket_path",
    "default_worker_file",
    "resolve_endpoint",
    "CheckpointBook",
    "FileCheckpointStore",
    "MemoryCheckpointStore",
    "MAX_CHECKPOINTS_PER_SANDBOX",
    "Kosa8Error",
    "Kosa8ConnectionError",
    "Kosa8ApiError",
    "Kosa8BadRequestError",
    "Kosa8AuthError",
    "Kosa8PolicyError",
    "Kosa8ApprovalRequiredError",
    "Kosa8NotFoundError",
    "Kosa8ConflictError",
    "Kosa8UnsupportedError",
    "Kosa8StreamError",
    "Kosa8CheckpointError",
    "Kosa8TimeoutError",
    "Kosa8ProtocolError",
    "error_from_response",
    "__version__",
]
