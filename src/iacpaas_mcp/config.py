"""Shared source selection for MCP and web; mock requires an explicit choice."""

import os

DEFAULT_BACKEND = "iacpaas"


def backend_from_env():
    return os.environ.get("IACPAAS_BACKEND", DEFAULT_BACKEND)
