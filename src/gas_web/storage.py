"""SQLite persistence; each operation owns its connection and transaction."""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from standalone_agent.runtime import redact


class Conflict(Exception):
    """An idempotency key was reused with different content."""


class Storage:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS owners (
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                csrf TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                summary TEXT,
                context TEXT NOT NULL DEFAULT '{"estimated_input_tokens":0,"limit":0,"compactions":0}')""")
            db.execute("""CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                state TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]', verification TEXT)""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS message_history ON messages(conversation_id,seq)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                conversation_id TEXT NOT NULL REFERENCES conversations(id), request_id TEXT NOT NULL,
                content TEXT NOT NULL, user_message_id TEXT NOT NULL REFERENCES messages(id),
                state TEXT NOT NULL, response TEXT, status INTEGER,
                PRIMARY KEY(conversation_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                request_id TEXT NOT NULL, assistant_message_id TEXT NOT NULL REFERENCES messages(id),
                event TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_revisions (
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                request_id TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES messages(id),
                action TEXT NOT NULL, content TEXT NOT NULL,
                branch_id TEXT NOT NULL REFERENCES conversations(id),
                PRIMARY KEY(conversation_id,request_id))""")
            if "archived" not in {row[1] for row in db.execute("PRAGMA table_info(conversations)")}:
                db.execute("ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def owner(self, token):
        if not token or len(token) > 256:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT id, csrf FROM owners WHERE token_hash = ?", (digest,)
            ).fetchone()
            return dict(row) if row else None

    def new_owner(self):
        token = secrets.token_urlsafe(32)
        owner = {"id": secrets.token_urlsafe(24), "csrf": secrets.token_urlsafe(32)}
        with self.connect() as db:
            db.execute(
                "INSERT INTO owners VALUES (?, ?, ?)",
                (owner["id"], hashlib.sha256(token.encode()).hexdigest(), owner["csrf"]),
            )
        return owner, token

    def create_conversation(self, owner_id, title):
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id": secrets.token_urlsafe(24),
            "title": title,
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO conversations (id,owner_id,title,created_at,updated_at) VALUES (?, ?, ?, ?, ?)",
                (item["id"], owner_id, title, now, now),
            )
        return item

    def conversations(self, owner_id):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,created_at,updated_at,archived FROM conversations WHERE owner_id=? ORDER BY updated_at DESC,id",
                    (owner_id,),
                )
            ]

    @staticmethod
    def _message(row):
        return {
            key: (json.loads(row[key]) if row[key] is not None else None)
            if key in ("sources", "verification")
            else row[key]
            for key in ("id", "role", "content", "created_at", "state", "sources", "verification")
        }

    @staticmethod
    def _insert_message(db, conversation_id, role, content, state, sources=None, verification=None):
        message_id = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO messages
            (id,conversation_id,role,content,created_at,state,sources,verification) VALUES (?,?,?,?,?,?,?,?)""",
            (
                message_id,
                conversation_id,
                role,
                content,
                now,
                state,
                json.dumps(sources or []),
                json.dumps(verification) if verification is not None else None,
            ),
        )
        return Storage._message(
            db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        )

    def history(self, conversation_id):
        with self.connect() as db:
            return [
                self._message(row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq",
                    (conversation_id,),
                )
            ]

    def page(self, conversation_id, limit, before):
        with self.connect() as db:
            params = [conversation_id]
            where = "conversation_id=?"
            if before is not None:
                cursor = db.execute(
                    "SELECT seq FROM messages WHERE conversation_id=? AND id=?",
                    (conversation_id, before),
                ).fetchone()
                if cursor is None:
                    raise ValueError("Invalid history cursor.")
                where += " AND seq<?"
                params.append(cursor["seq"])
            rows = db.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY seq DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            messages = [self._message(row) for row in reversed(rows[:limit])]
            return {
                "messages": messages,
                "has_more": has_more,
                "next_before": messages[0]["id"] if has_more else None,
            }

    def context(self, conversation_id):
        with self.connect() as db:
            return json.loads(
                db.execute(
                    "SELECT context FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()[0]
            )

    def summary(self, conversation_id):
        with self.connect() as db:
            value = db.execute(
                "SELECT summary FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()[0]
            return json.loads(value) if value is not None else None

    def start_turn(self, conversation_id, request_id, content):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT archived FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None or row["archived"]:
                raise Conflict("Диалог удалён или находится в архиве. Восстановите его перед отправкой.")
            previous = db.execute(
                "SELECT * FROM requests WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            if previous:
                if previous["content"] != content:
                    raise Conflict("request_id already used with different content.")
                if previous["response"] is not None:
                    return None, json.loads(previous["response"]), previous["status"]
                # shortcut: crash-pending turns need operator reconciliation; never auto-replay an unknown outcome.
                raise Conflict("Request is already pending; its outcome is not yet known.")
            if db.execute(
                "SELECT 1 FROM requests WHERE conversation_id=? AND state=?",
                (conversation_id, "pending"),
            ).fetchone():
                raise Conflict("Conversation is busy; wait for the pending request.")
            message = self._insert_message(db, conversation_id, "user", content, "pending")
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                (conversation_id, request_id, content, message["id"], "pending", None, None),
            )
            return message, None, None

    def fail_turn(self, conversation_id, request_id, user_message, response, status):
        with self.connect() as db:
            db.execute("UPDATE messages SET state=? WHERE id=?", ("failed", user_message["id"]))
            db.execute(
                "UPDATE requests SET state=?,response=?,status=? WHERE conversation_id=? AND request_id=?",
                ("failed", json.dumps(response), status, conversation_id, request_id),
            )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), conversation_id),
            )

    @staticmethod
    def _validate_result(result):
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not isinstance(result["answer"], str):
            raise ValueError("Invalid answer.")
        for key in ("sources", "events"):
            if not isinstance(result[key], list) or any(
                not isinstance(item, dict) for item in result[key]
            ):
                raise ValueError("Invalid evidence.")
        verification = result["verification"]
        if verification["status"] not in (
            "verified",
            "grounded",
            "reviewed",
            "partial",
            "rejected",
            "review_unavailable",
            "insufficient_data",
            "not_applicable",
            "error",
        ) or not isinstance(verification["reason"], str):
            raise ValueError("Invalid verification.")
        context = result["context"]
        if any(
            type(context[key]) is not int or context[key] < 0
            for key in ("estimated_input_tokens", "limit", "compactions")
        ):
            raise ValueError("Invalid context counters.")
        summary = result.get("summary_update")
        if summary is not None and (
            not isinstance(summary["text"], str)
            or len(summary["text"]) > 20000
            or not isinstance(summary["covered_through_message_id"], str)
        ):
            raise ValueError("Invalid summary.")

    def finish_turn(self, conversation_id, request_id, user_message, result):
        self._validate_result(result)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            context = dict(result["context"])
            previous = json.loads(
                db.execute(
                    "SELECT context FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()[0]
            )
            context["compactions"] += previous["compactions"]
            db.execute("UPDATE messages SET state=? WHERE id=?", ("complete", user_message["id"]))
            assistant = self._insert_message(
                db,
                conversation_id,
                "assistant",
                result["answer"],
                "complete",
                result["sources"],
                result["verification"],
            )
            db.execute(
                "UPDATE conversations SET context=?, updated_at=? WHERE id=?",
                (json.dumps(context), assistant["created_at"], conversation_id),
            )
            if result.get("summary_update") is not None:
                marker = db.execute(
                    "SELECT seq FROM messages WHERE id=? AND conversation_id=? AND role=? AND state=?",
                    (
                        result["summary_update"]["covered_through_message_id"],
                        conversation_id,
                        "assistant",
                        "complete",
                    ),
                ).fetchone()
                current = db.execute(
                    "SELECT seq FROM messages WHERE id=?", (user_message["id"],)
                ).fetchone()
                if marker is None or marker["seq"] >= current["seq"]:
                    raise ValueError("Invalid summary coverage.")
                db.execute(
                    "UPDATE conversations SET summary=? WHERE id=?",
                    (json.dumps(result["summary_update"]), conversation_id),
                )
            for event in result["events"]:
                db.execute(
                    "INSERT INTO events (conversation_id,request_id,assistant_message_id,event) VALUES (?,?,?,?)",
                    (conversation_id, request_id, assistant["id"], json.dumps(redact(event))),
                )
            response = {
                "user_message": dict(user_message, state="complete"),
                "assistant_message": assistant,
                "context": context,
            }
            db.execute(
                "UPDATE requests SET state=?,response=?,status=? WHERE conversation_id=? AND request_id=?",
                ("complete", json.dumps(response), 200, conversation_id, request_id),
            )
        return response

    def conversation(self, owner_id, conversation_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT id,title,created_at,updated_at,archived FROM conversations WHERE id=? AND owner_id=?",
                (conversation_id, owner_id),
            ).fetchone()
            return dict(row) if row else None

    def manage_conversation(self, owner_id, conversation_id, action):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM conversations WHERE id=? AND owner_id=?", (conversation_id, owner_id)).fetchone()
            if row is None:
                return False
            if db.execute("SELECT 1 FROM requests WHERE conversation_id=? AND state='pending'", (conversation_id,)).fetchone():
                raise Conflict("Дождитесь завершения запроса перед изменением диалога.")
            if action in ("archive", "restore"):
                db.execute("UPDATE conversations SET archived=? WHERE id=?", (int(action == "archive"), conversation_id))
            elif action == "delete":
                # A branch keeps its own copied messages; remove only links to this conversation.
                db.execute("DELETE FROM conversation_revisions WHERE conversation_id=? OR branch_id=?", (conversation_id, conversation_id))
                for table in ("events", "requests", "messages"):
                    db.execute(f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
                db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            else:
                raise ValueError("Unknown conversation action")
            return True

    def revise(self, owner_id, conversation_id, request_id, message_id, action, content=None):
        """Create an idempotent branch before a question, without running a model."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            origin = db.execute(
                "SELECT * FROM conversations WHERE id=? AND owner_id=?",
                (conversation_id, owner_id),
            ).fetchone()
            if origin is None:
                return None
            if origin["archived"]:
                raise Conflict("Восстановите диалог из архива перед созданием новой версии.")
            target = db.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND id=?",
                (conversation_id, message_id),
            ).fetchone()
            if target is None:
                return None
            if action == "regenerate":
                if target["role"] != "assistant" or target["state"] != "complete":
                    raise ValueError("Выберите завершённый ответ помощника.")
                question = db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? AND seq<? AND role='user' ORDER BY seq DESC LIMIT 1",
                    (conversation_id, target["seq"]),
                ).fetchone()
                if question is None or question["state"] != "complete":
                    raise ValueError("Не найден исходный вопрос.")
                content = question["content"]
            else:
                if (
                    action != "edit"
                    or target["role"] != "user"
                    or target["state"] not in ("complete", "failed")
                ):
                    raise ValueError("Этот запрос пока нельзя изменить.")
                if not isinstance(content, str) or not content.strip() or len(content) > 20000:
                    raise ValueError("Введите запрос длиной до 20000 символов.")
                question = target
            previous = db.execute(
                "SELECT * FROM conversation_revisions WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            if previous:
                if (previous["message_id"], previous["action"], previous["content"]) != (
                    message_id,
                    action,
                    content,
                ):
                    raise Conflict("Этот идентификатор уже использован для другой версии.")
                branch = db.execute(
                    "SELECT id,title,created_at,updated_at FROM conversations WHERE id=?",
                    (previous["branch_id"],),
                ).fetchone()
                return {"conversation": dict(branch), "content": content, "request_id": request_id}
            prefix = db.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND seq<? ORDER BY seq",
                (conversation_id, question["seq"]),
            ).fetchall()
            if any(row["state"] == "pending" for row in prefix):
                raise Conflict("Дождитесь завершения предыдущего запроса.")
            branch_id = secrets.token_urlsafe(24)
            now = datetime.now(timezone.utc).isoformat()
            title = origin["title"][:180] + " · версия"
            db.execute(
                "INSERT INTO conversations (id,owner_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (branch_id, owner_id, title, now, now),
            )
            for row in prefix:
                message = self._insert_message(
                    db,
                    branch_id,
                    row["role"],
                    row["content"],
                    row["state"],
                    json.loads(row["sources"]),
                    json.loads(row["verification"]) if row["verification"] else None,
                )
                db.execute(
                    "UPDATE messages SET created_at=? WHERE id=?",
                    (row["created_at"], message["id"]),
                )
                for event in db.execute(
                    "SELECT request_id,event FROM events WHERE assistant_message_id=?", (row["id"],)
                ).fetchall():
                    db.execute(
                        "INSERT INTO events (conversation_id,request_id,assistant_message_id,event) VALUES (?,?,?,?)",
                        (branch_id, event["request_id"], message["id"], event["event"]),
                    )
            db.execute(
                "INSERT INTO conversation_revisions VALUES (?,?,?,?,?,?)",
                (conversation_id, request_id, message_id, action, content, branch_id),
            )
            return {
                "conversation": {
                    "id": branch_id,
                    "title": title,
                    "created_at": now,
                    "updated_at": now,
                },
                "content": content,
                "request_id": request_id,
            }
