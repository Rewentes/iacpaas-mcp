"""MCP transport over the explicit local mock backend."""

import hmac
import json
import logging
import os
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError

from .domain import DomainError, GraphService
from .policy import WritePolicy
from .schemas import Depth, Limit, NodeLimit, Path, Text, ToolResponse


class PolicyMCP(FastMCP):
    """Keep execution failures as JSON text + isError, not success envelopes."""

    def streamable_http_app(self):
        return create_http_app(self)

    async def call_tool(self, name, arguments):
        try:
            if (
                len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
                > self.max_argument_bytes
            ):
                raise DomainError("limit_exceeded", "Tool arguments exceed byte budget")
            return await super().call_tool(name, arguments)
        except Exception as exc:
            cause = exc
            while cause.__cause__ is not None and not isinstance(
                cause, (DomainError, ValidationError, PermissionError, TimeoutError)
            ):
                cause = cause.__cause__
            if isinstance(cause, DomainError):
                code, message, retryable = cause.code, cause.message, cause.retryable
            elif isinstance(cause, ValidationError):
                code, message, retryable = "invalid_data", "Invalid tool arguments", False
            elif isinstance(cause, PermissionError):
                code, message, retryable = "permission_denied", str(cause), False
            elif isinstance(cause, TimeoutError):
                code, message, retryable = "timeout", "Operation timed out", True
            else:
                code, message, retryable = "platform_error", "Mock operation failed", False
            error = ToolError(json.dumps(dict(code=code, message=message, retryable=retryable)))
            return CallToolResult(isError=True, content=[TextContent(type="text", text=str(error))])


def create_server(
    service=None,
    *,
    host="127.0.0.1",
    port=8000,
    max_result_bytes=1_048_576,
    max_argument_bytes=1_048_576,
):
    service = service if service is not None else GraphService()
    server = PolicyMCP("iacpaas-mock", host=host, port=port)
    server.max_argument_bytes = max_argument_bytes
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
    policy = WritePolicy.from_env()
    lock = threading.RLock()
    audit = logging.getLogger("iacpaas_mcp.audit")

    def invoke(name, _write=False, **kwargs):
        if _write:
            acknowledgement = ToolResponse(
                data={"applied": True, "result_omitted": True},
                complete=False,
                warnings=[
                    "volatile mock write: in-memory only; no native API called",
                    "Full result omitted: exceeds byte budget; mutation applied",
                ],
            )
            # Reserve a truthful, bounded success response before any state change.
            if len(acknowledgement.model_dump_json().encode("utf-8")) > max_result_bytes:
                raise DomainError("limit_exceeded", "Write acknowledgement exceeds byte budget")
        with lock:
            data = getattr(service, name)(**kwargs)
        result = ToolResponse(
            data=data, complete=data.get("complete", True), warnings=data.get("warnings", [])
        )
        if _write:
            result.warnings.append("volatile mock write: in-memory only; no native API called")
        if len(result.model_dump_json().encode("utf-8")) > max_result_bytes:
            if _write:
                return acknowledgement
            raise DomainError("limit_exceeded", "Tool result exceeds byte budget")
        return result

    def mutate(name, target_path, **kwargs):
        with lock:
            try:
                policy.authorize(target_path)
                result = invoke(name, _write=True, **kwargs)
            except Exception:
                audit.info("mock_write operation=%s status=denied_or_failed", name)
                raise
            audit.info("mock_write operation=%s status=success", name)
            return result

    @server.tool(annotations=read)
    def list_inforesources(filter: Text = "", limit: Limit = 100) -> ToolResponse:
        """List the limited configured mock catalog."""
        return invoke("list_inforesources", filter=filter, limit=limit)

    @server.tool(annotations=read)
    def get_inforesource(path: Path) -> ToolResponse:
        """Read metadata of a mock resource."""
        return invoke("get_inforesource", path=path)

    @server.tool(annotations=read)
    def get_subgraph(
        path: Path, root: Path = "root", depth: Depth = 2, max_nodes: NodeLimit = 2000
    ) -> ToolResponse:
        """Bounded local mock BFS; external targets are not fetched."""
        return invoke("get_subgraph", path=path, root=root, depth=depth, max_nodes=max_nodes)

    @server.tool(annotations=read)
    def count_graph(
        path: Path, root: Path | None = None, depth: Depth | None = None
    ) -> ToolResponse:
        """Count mock node and edge instances within the requested scope."""
        return invoke("count_graph", path=path, root=root, depth=depth)

    @server.tool(annotations=read)
    def query_graph(
        path: Path, field: Text = "name", value: Text = "", limit: Limit = 100
    ) -> ToolResponse:
        """Search a mock graph by an allowed domain field."""
        return invoke("query_graph", path=path, field=field, value=value, limit=limit)

    @server.tool(annotations=read)
    def export_inforesource(path: Path) -> ToolResponse:
        """Export normalized-demo-v1, not a native IACPaaS format."""
        return invoke("export_inforesource", path=path)

    @server.tool(annotations=read)
    def validate_resource(payload: dict) -> ToolResponse:
        """Validate normalized demo data without mutating any resource."""
        return invoke("validate_resource", payload=payload)

    @server.tool(annotations=write)
    def update_concept(
        path: Path, concept_id: Path, mark: Text | None = None, comment: Text | None = None
    ) -> ToolResponse:
        """Volatile mock mutation. Requires operator startup policy AND agent human confirmation."""
        return mutate(
            "update_concept", path, path=path, concept_id=concept_id, mark=mark, comment=comment
        )

    @server.tool(annotations=write)
    def import_inforesource(payload: dict, clear_if_exists: bool = False) -> ToolResponse:
        """Import only normalized demo JSON into volatile mock; startup policy required."""
        return mutate(
            "import_inforesource",
            payload.get("path"),
            payload=payload,
            clear_if_exists=clear_if_exists,
        )

    return server


def create_http_app(server):
    """Local bearer stand, not an OAuth authorization server. Captures env once."""
    token = os.environ.get("MCP_HTTP_TOKEN", "")
    if not token or token.isspace():
        raise ValueError("MCP_HTTP_TOKEN is mandatory for HTTP")
    if (
        server.settings.host not in {"127.0.0.1", "localhost", "::1"}
        and os.environ.get("MCP_HTTP_ALLOW_REMOTE") != "1"
    ):
        raise ValueError(
            "HTTP binding must be localhost; explicit MCP_HTTP_ALLOW_REMOTE=1 required"
        )
    app = FastMCP.streamable_http_app(server)
    expected = ("Bearer " + token).encode("utf-8")

    async def authenticated(scope, receive, send):
        if scope["type"] == "http":
            values = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
            supplied = values[0] if len(values) == 1 else b""
            if not hmac.compare_digest(supplied, expected):
                from starlette.responses import JSONResponse

                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await app(scope, receive, send)

    return authenticated


def main(argv=None):
    """CLI uses only explicit inherited environment; does not search for .env."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Local mock IACPaaS MCP stand (native API not connected)"
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--backend", choices=["mock", "iacpaas"], default=os.environ.get("IACPAAS_BACKEND", "mock")
    )
    args = parser.parse_args(argv)
    if args.backend != "mock":
        parser.error(
            "unsupported_backend: native IACPaaS API is not connected; use --backend mock explicitly"
        )
    server = create_server(host=args.host, port=args.port)
    if args.transport == "stdio":
        print("MOCK backend: normalized-demo-v1; native IACPaaS API not connected", file=sys.stderr)
        server.run(transport="stdio")
    else:
        import uvicorn

        try:
            app = create_http_app(server)
        except ValueError as exc:
            parser.error(str(exc))
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
