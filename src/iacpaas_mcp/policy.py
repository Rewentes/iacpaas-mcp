"""Trusted startup policy; tool arguments can never enable writes."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WritePolicy:
    enabled: bool = False
    prefix: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.environ.get("IACPAAS_ALLOW_WRITES") == "1",
            prefix=os.environ.get("IACPAAS_WRITABLE_PREFIX", ""),
        )

    def authorize(self, path: str) -> None:
        canonical = (
            isinstance(path, str)
            and path.startswith("/demo/")
            and all(part and part not in {".", ".."} for part in path[1:].split("/"))
            and not any(c in path for c in ("%", "\\", "?", "#", "\x00"))
        )
        if not (self.enabled and self.prefix == "/demo/" and canonical):
            raise PermissionError("Mock writes disabled or path outside /demo/ allowlist")
