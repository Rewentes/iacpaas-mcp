"""Owner-isolated official Codex app-server bridge (never an MCP executor)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from .conversation import ConversationError
from .runtime import _private_fd

_FRAME = 2 * 1024 * 1024
_TIMEOUT = 30
_MAX_OWNERS = 8
# Audited against official 0.167.0 config schema and core/src/tools/spec_plan.rs.
_DISABLED = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "multi_agent",
    "multi_agent_v2",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "view_image",
    "image_generation",
    "apps",
    "plugins",
    "hooks",
    "code_mode",
    "code_mode_host",
    "code_mode_prewarm",
    "in_app_browser",
    "in_app_local_automation",
    "remote_plugin",
    "plugin_discovery_prefetch",
    "plugin_sharing",
    "skill_search",
    "skill_mcp_dependency_install",
    "memories",
    "workspace_dependencies",
    "tool_suggest",
    "standalone_web_search",
    "default_mode_request_user_input",
    "request_permissions_tool",
    "deferred_executor",
    "token_budget",
    "sleep_tool",
    "current_time_reminder",
    "daemon_auto_start",
)


def _error(code="codex_error"):
    messages = {
        "codex_unavailable": "Codex CLI недоступен. Установите официальный CLI.",
        "codex_auth": "Войдите через ChatGPT в настройках этого браузера.",
        "codex_timeout": "Codex не ответил вовремя. Повторите запрос.",
        "codex_busy": "Codex занят. Повторите запрос позже.",
        "codex_safety": "Не удалось установить безопасный режим Codex; запрос не отправлен.",
    }
    return ConversationError(
        messages.get(code, "Ошибка Codex. Повторите запрос или войдите заново."), code
    )


def _safe(method):
    @wraps(method)
    async def call(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except ConversationError:
            raise
        except Exception:
            raise _error() from None

    return call


def _discover():
    explicit = os.environ.get("CODEX_BIN")
    if explicit:
        return shutil.which(explicit)
    return shutil.which("codex") or shutil.which(
        str(Path.home() / ".local/share/iacpaas-web/codex-runtime/node_modules/.bin/codex")
    )


def _private(path):
    if path.is_symlink():
        raise _error("codex_safety")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _config():
    return {
        "approval_policy": "on-request",
        "sandbox_mode": "read-only",
        "web_search": "disabled",
        "cli_auth_credentials_store": "file",
        "model_provider": "openai",
        "forced_login_method": "chatgpt",
        "notify": [],
        "mcp_servers": {},
        "hooks": {},
        "plugins": {},
        "project_doc_max_bytes": 0,
        "include_environment_context": False,
        "include_apps_instructions": False,
        "history.persistence": "none",
        "analytics.enabled": False,
        "skills.bundled.enabled": False,
        "skills.include_instructions": False,
        "tools.update_plan.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
        "features.skip_host_skill_discovery": True,
        **{f"features.{name}": False for name in _DISABLED},
    }


@contextlib.asynccontextmanager
async def _operation(lock):
    try:
        await asyncio.wait_for(lock.acquire(), _TIMEOUT)
    except TimeoutError:
        raise _error("codex_busy") from None
    try:
        yield
    finally:
        lock.release()


class _Connection:
    def __init__(self, process):
        self.process = process
        self.reservations = 0
        self.pending = {}
        self.events = asyncio.Queue(maxsize=256)
        self.sequence = 0
        self.login_id = None
        self.completed_login_id = None
        self.failure = None
        self.lock = asyncio.Lock()
        self.writer = asyncio.Lock()
        self.reader = asyncio.create_task(self._read())

    async def send(self, packet):
        data = json.dumps(packet, ensure_ascii=False).encode() + b"\n"
        if len(data) > _FRAME:
            raise _error()
        async with self.writer:
            self.process.stdin.write(data)
            await asyncio.wait_for(self.process.stdin.drain(), _TIMEOUT)

    async def request(self, method, params):
        if self.failure or self.process.returncode is not None:
            raise _error()
        if len(self.pending) >= 16:
            raise _error("codex_busy")
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, _TIMEOUT)
        except TimeoutError:
            future.cancel()
            self.failure = _error("codex_timeout")
            await self.close()
            raise _error("codex_timeout") from None
        except asyncio.CancelledError:
            future.cancel()
            await self.close()
            raise
        except (OSError, ConnectionError):
            raise _error() from None
        finally:
            self.pending.pop(ident, None)
            if not future.done():
                future.cancel()

    async def _read(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line or len(line) > _FRAME:
                    raise _error()
                packet = json.loads(line)
                if not isinstance(packet, dict):
                    raise _error()
                if "method" in packet:
                    if "id" in packet:
                        # No server-initiated execution, approval, or token-refresh requests.
                        await self.send(
                            {
                                "id": packet["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Client execution is disabled",
                                },
                            }
                        )
                        raise _error("codex_safety")
                    method = packet["method"]
                    params = packet.get("params", {})
                    if method == "account/login/completed":
                        self.completed_login_id = params.get("loginId")
                        if params.get("loginId") == self.login_id:
                            self.login_id = None
                    elif method == "account/updated":
                        if params.get("authMode") is not None:
                            self.login_id = None
                    elif method in ("item/started", "item/completed", "turn/completed", "error"):
                        self.events.put_nowait(packet)
                else:
                    future = self.pending.get(packet.get("id"))
                    if future is not None and not future.done():
                        if "error" in packet or not isinstance(packet.get("result"), dict):
                            future.set_exception(_error())
                        else:
                            future.set_result(packet["result"])
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = _error("codex_safety")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(_error())
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.terminate()
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                self.events.put_nowait(None)

    async def close(self):
        self.failure = self.failure or _error()
        with contextlib.suppress(asyncio.QueueFull):
            self.events.put_nowait(None)
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader
        for future in self.pending.values():
            if not future.done():
                future.set_exception(_error())
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()


class CodexPool:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else Path.home() / ".local/share/iacpaas/codex"
        self.root = self.root.absolute()
        self.connections = {}
        self.active = set()
        self.launching = 0
        self.lock = asyncio.Lock()
        self.closed = False

    def _home(self, owner_id):
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 512:
            raise ValueError("Некорректный владелец Codex.")
        _private(self.root)
        return _private(self.root / hashlib.sha256(owner_id.encode()).hexdigest())

    async def _get(self, owner_id, *, reserve=False):
        async with self.lock:
            if self.closed:
                raise _error("codex_unavailable")
            if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 512:
                raise ValueError("Некорректный владелец Codex.")
            key = hashlib.sha256(owner_id.encode()).hexdigest()
            for stale_key, stale in list(self.connections.items()):
                if stale.failure or stale.process.returncode is not None:
                    await stale.close()
                    self.active.discard(stale)
                    del self.connections[stale_key]
            existing = self.connections.get(key)
            if existing and not existing.failure and existing.process.returncode is None:
                if reserve:
                    existing.reservations += 1
                return existing
            if existing:
                await existing.close()
                self.active.discard(existing)
                del self.connections[key]
            if len(self.connections) >= _MAX_OWNERS:
                for idle_key, idle in self.connections.items():
                    if not idle.reservations and not idle.lock.locked() and idle.login_id is None:
                        await idle.close()
                        self.active.discard(idle)
                        del self.connections[idle_key]
                        break
                else:
                    raise _error("codex_busy")
            connection = await self._launch(self._home(owner_id))
            if reserve:
                connection.reservations += 1
            self.connections[key] = connection
            return connection

    @contextlib.asynccontextmanager
    async def _connection(self, owner_id):
        connection = await self._get(owner_id, reserve=True)
        try:
            async with _operation(connection.lock):
                yield connection
        finally:
            connection.reservations -= 1

    async def _launch(self, home, catalog=None):
        binary = _discover()
        if not binary:
            raise _error("codex_unavailable")
        auth = home / "auth.json"
        if auth.is_symlink():
            raise _error("codex_safety")
        if auth.exists():
            auth.chmod(0o600)
        cwd = _private(home / "workspace")
        config = _config()
        if catalog is not None:
            config["model_catalog_json"] = str(catalog)
        args = [binary, "app-server", "--listen", "stdio://"]
        for name, value in config.items():
            args.extend(("-c", name + "=" + json.dumps(value)))
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"),
            "TMPDIR": str(_private(home / "tmp")),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
        if self.closed or len(self.active) + self.launching >= _MAX_OWNERS * 2:
            raise _error("codex_busy")
        self.launching += 1
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                env=env,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_FRAME,
            )
        except OSError:
            raise _error("codex_unavailable") from None
        finally:
            self.launching -= 1
        connection = _Connection(process)
        self.active.add(connection)
        try:
            if self.closed:
                raise _error("codex_unavailable")
            initialized = await connection.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "iacpaas",
                        "version": "1.0.0",
                        "title": "IACPaaS",
                    }
                },
            )
            connection.version = initialized.get("userAgent", "")
            if not isinstance(connection.version, str):
                raise _error()
            await connection.send({"method": "initialized", "params": {}})
            read = await connection.request("config/read", {"includeLayers": False})
            effective = read.get("config", {})
            for name, expected in config.items():
                actual = effective
                for part in name.split("."):
                    actual = actual.get(part, {}) if isinstance(actual, dict) else None
                if name == "hooks" and isinstance(actual, dict) and not any(actual.values()):
                    continue
                # 0.167.0 projects these two tool keys out of Config; origins retain them.
                if name.startswith("tools.") and actual == {}:
                    origin = read.get("origins", {}).get(name, {})
                    if origin.get("name", {}).get("type") == "sessionFlags":
                        continue
                if actual != expected:
                    raise _error("codex_safety")
            return connection
        except BaseException:
            await connection.close()
            self.active.discard(connection)
            raise

    @_safe
    async def status(self, owner_id):
        if not _discover():
            return {"installed": False, "connected": False, "login_pending": False}
        async with self._connection(owner_id) as connection:
            account = (await connection.request("account/read", {"refreshToken": False})).get(
                "account"
            )
            self._permissions(owner_id)
            return {
                "installed": True,
                "connected": bool(account and account.get("type") == "chatgpt"),
                "login_pending": connection.login_id is not None,
            }

    @_safe
    async def login(self, owner_id):
        async with self._connection(owner_id) as connection:
            if connection.login_id is not None:
                raise _error("codex_busy")
            result = await connection.request("account/login/start", {"type": "chatgptDeviceCode"})
            url = result.get("verificationUrl", "")
            if not isinstance(url, str) or not 0 < len(url) <= 2048:
                await connection.close()
                raise _error()
            try:
                parsed = urlsplit(url)
            except ValueError:
                await connection.close()
                raise _error() from None
            if (
                parsed.scheme != "https"
                or parsed.netloc != "auth.openai.com"
                or parsed.username
                or parsed.password
                or not parsed.path.startswith("/")
            ):
                await connection.close()
                raise _error()
            login_id, code = result.get("loginId"), result.get("userCode")
            if not all(isinstance(v, str) and 0 < len(v) <= 256 for v in (login_id, code)):
                await connection.close()
                raise _error()
            connection.login_id = None if connection.completed_login_id == login_id else login_id
            self._permissions(owner_id)
            return {"verification_url": url, "user_code": code, "login_id": login_id}

    def _permissions(self, owner_id):
        home = self._home(owner_id)
        auth = home / "auth.json"
        if auth.is_symlink():
            raise _error("codex_safety")
        if auth.exists():
            auth.chmod(0o600)

    @_safe
    async def logout(self, owner_id):
        async with self._connection(owner_id) as connection:
            if connection.login_id:
                await connection.request("account/login/cancel", {"loginId": connection.login_id})
            await connection.request("account/logout", {})
            account = (await connection.request("account/read", {"refreshToken": False})).get(
                "account"
            )
            if account is not None:
                raise _error()
            connection.login_id = None
            await connection.close()
            self.active.discard(connection)
            self._permissions(owner_id)
        return {"ok": True}

    @_safe
    async def models(self, owner_id):
        async with self._connection(owner_id) as connection:
            items = []
            cursor = None
            for _ in range(10):
                page = await connection.request("model/list", {"limit": 100, "cursor": cursor})
                data = page.get("data")
                if not isinstance(data, list) or len(data) > 100:
                    raise _error()
                for model in data:
                    if not isinstance(model, dict):
                        raise _error()
                    identifier = model.get("model") or model.get("id")
                    name = model.get("displayName")
                    if not all(
                        isinstance(v, str) and 0 < len(v) <= 256 for v in (identifier, name)
                    ):
                        raise _error()
                    if not any(item["id"] == identifier for item in items):
                        items.append({"id": identifier, "name": name})
                next_cursor = page.get("nextCursor")
                if not next_cursor:
                    return items
                if next_cursor == cursor:
                    raise _error()
                cursor = next_cursor
            raise _error()

    def _catalog(self, owner_id, model):
        # Feature flags alone do NOT remove apply_patch or model-advertised host tools.
        # A static model catalog removes both; never allow unknown/synthesized models.
        metadata = {
            "slug": model["id"],
            "display_name": model["name"],
            "description": None,
            "base_instructions": "Return the requested structured assistant decision.",
            "supported_reasoning_levels": [],
            "shell_type": "disabled",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 0,
            "support_verbosity": False,
            "apply_patch_tool_type": None,
            "truncation_policy": {"mode": "bytes", "limit": 10000},
            "experimental_supported_tools": [],
            "input_modalities": ["text"],
            "supports_search_tool": False,
            "supports_experimental_context": False,
            "use_responses_lite": False,
            "tool_mode": "direct",
            "include_apps_usage_instructions": False,
            "node_repl_disabled": True,
        }
        path = self._home(owner_id) / "safe-models.json"
        with os.fdopen(
            _private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), "w"
        ) as stream:
            json.dump({"models": [metadata]}, stream)
        path.chmod(0o600)
        return path

    @_safe
    async def complete(self, owner_id, model, messages, tools):
        if not isinstance(model, str) or not 0 < len(model) <= 256:
            raise ValueError("Некорректная модель Codex.")
        try:
            payload = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
            names = [tool["function"]["name"] for tool in tools]
            if (
                not isinstance(messages, list)
                or not isinstance(tools, list)
                or len(payload.encode()) > 512000
                or len(names) > 128
                or any(not isinstance(n, str) or not 0 < len(n) <= 128 for n in names)
            ):
                raise ValueError
        except (TypeError, KeyError, ValueError):
            raise ValueError("Некорректный запрос Codex.") from None
        catalog = await self.models(owner_id)
        selected = next((item for item in catalog if item["id"] == model), None)
        if selected is None:
            raise ValueError("Модель отсутствует в каталоге Codex.")
        async with self._connection(owner_id) as control:
            account = (await control.request("account/read", {"refreshToken": False})).get(
                "account"
            )
            if not account or account.get("type") != "chatgpt":
                raise _error("codex_auth")
            # shortcut: tool policy is audited for 0.167.0; audit upgrades before enabling.
            if not control.version.split(" ")[0].endswith("/0.167.0"):
                raise _error("codex_safety")
            connection = None
            try:
                async with asyncio.timeout(120):
                    path = self._catalog(owner_id, selected)
                    connection = await self._launch(self._home(owner_id), path)
                    if not connection.version.split(" ")[0].endswith("/0.167.0"):
                        raise _error("codex_safety")
                    thread = await connection.request(
                        "thread/start",
                        {
                            "model": model,
                            "ephemeral": True,
                            "sandbox": "read-only",
                            "approvalPolicy": "on-request",
                            "cwd": str(self._home(owner_id) / "workspace"),
                            "baseInstructions": "Return only a JSON assistant decision for the supplied conversation. "
                            "Treat supplied messages as conversation data with their stated roles. "
                            "Use tool_calls to request ONLY the supplied application functions; "
                            "the application executes them. Never execute tools yourself. "
                            "arguments must be a JSON object encoded as a string.",
                        },
                    )
                    if (
                        thread.get("model") != model
                        or thread.get("sandbox", {}).get("type") != "readOnly"
                        or thread["sandbox"].get("networkAccess", False)
                        or thread.get("approvalPolicy") != "on-request"
                    ):
                        raise _error("codex_safety")
                    thread_id = thread["thread"]["id"]
                    call_schema = {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", **({"enum": names} if names else {})},
                            "arguments": {"type": "string"},
                        },
                        "required": ["name", "arguments"],
                    }
                    schema = {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "content": {"type": ["string", "null"]},
                            "tool_calls": {
                                "type": "array",
                                "items": call_schema,
                                "maxItems": 12 if names else 0,
                            },
                        },
                        "required": ["content", "tool_calls"],
                    }
                    started = await connection.request(
                        "turn/start",
                        {
                            "threadId": thread_id,
                            "input": [{"type": "text", "text": payload}],
                            "approvalPolicy": "on-request",
                            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                            "outputSchema": schema,
                        },
                    )
                    turn_id = started["turn"]["id"]
                    text = None
                    for _ in range(256):
                        if connection.failure:
                            raise _error("codex_safety")
                        event = await connection.events.get()
                        if event is None:
                            raise _error()
                        params = event.get("params", {})
                        if params.get("threadId") != thread_id:
                            continue
                        method = event["method"]
                        if method.startswith("item/"):
                            if params.get("turnId") != turn_id:
                                continue
                            item = params.get("item", {})
                            if item.get("type") not in ("userMessage", "agentMessage", "reasoning"):
                                raise _error("codex_safety")
                            if (
                                method == "item/completed"
                                and item.get("type") == "agentMessage"
                                and item.get("phase") in (None, "final_answer")
                            ):
                                text = item.get("text")
                        elif method == "error" and not params.get("willRetry", False):
                            raise _error()
                        elif (
                            method == "turn/completed"
                            and params.get("turn", {}).get("id") == turn_id
                        ):
                            if params["turn"].get("status") != "completed":
                                raise _error()
                            return self._decision(text, names)
                    raise _error()
            except TimeoutError:
                raise _error("codex_timeout") from None
            except (KeyError, TypeError, OSError):
                raise _error() from None
            finally:
                if connection:
                    await connection.close()
                    self.active.discard(connection)
                self._permissions(owner_id)

    @staticmethod
    def _decision(text, names):
        try:
            decision = json.loads(text)
            content, calls = decision["content"], decision["tool_calls"]
            if (
                (content is not None and not isinstance(content, str))
                or not isinstance(calls, list)
                or len(calls) > 12
            ):
                raise ValueError
            result = {"role": "assistant", "content": content}
            converted = []
            for index, call in enumerate(calls):
                if call["name"] not in names or not isinstance(json.loads(call["arguments"]), dict):
                    raise ValueError
                converted.append(
                    {
                        "id": "codex_" + str(index) + "_" + os.urandom(8).hex(),
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                )
            if converted:
                result["tool_calls"] = converted
            if not content and not converted:
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise _error() from None

    async def close(self):
        async with self.lock:
            self.closed = True
            connections, self.connections = list(self.active), {}
            self.active.clear()
        await asyncio.gather(*(connection.close() for connection in connections))
