"""Conversation titles survive reload, old databases and manual overrides."""

import sqlite3

import pytest
from starlette.testclient import TestClient
from test_portable_chat_revisions import Agent

from gas_web.app import create_app
from gas_web.storage import Storage


def boot(client, title="Новый диалог"):
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
    return client.post("/api/conversations", json={"title": title}).json()["id"]


def test_first_question_names_chat_and_later_messages_keep_it(tmp_path):
    agent = Agent()
    app = create_app(db_path=tmp_path / "chat.db", agent=agent)
    with TestClient(app) as client:
        chat = boot(client)
        uri = f"/api/conversations/{chat}/messages"
        body = {"request_id": "first", "content": "Сравни чистоту аргона и гелия"}
        response = client.post(uri, json=body).json()
        assert response["conversation"]["title"] == body["content"]
        assert client.get(uri).json()["conversation"]["title"] == body["content"]
        assert client.get("/api/conversations").json()["items"][0]["title"] == body["content"]
        assert client.post(uri, json=body).json() == response
        client.post(uri, json={"request_id": "second", "content": "Теперь обсудим другую тему"})
        assert client.get(uri).json()["conversation"]["title"] == body["content"]
        assert len(agent.calls) == 2  # Naming/replay adds no model calls.


@pytest.mark.parametrize("title", ["Новый диалог", "Мои заметки"])
def test_manual_rename_is_not_overwritten_by_first_question_or_reload(tmp_path, title):
    db = tmp_path / "chat.db"
    app = create_app(db_path=db, agent=Agent())
    with TestClient(app) as client:
        chat = boot(client)
        endpoint = f"/api/conversations/{chat}/manage"
        renamed = client.post(endpoint, json={"action": "rename", "title": title})
        assert renamed.status_code == 200
        assert renamed.json()["conversation"]["title"] == title
        client.post(
            f"/api/conversations/{chat}/messages",
            json={"request_id": "q", "content": "Тема первого вопроса"},
        )
        assert client.get("/api/conversations").json()["items"][0]["title"] == title
        # Re-opening Storage applies migrations again but must preserve a manual default name.
        Storage(db)
        assert client.get("/api/conversations").json()["items"][0]["title"] == title


@pytest.mark.parametrize("value", ["", "   ", "x" * 201, None, 1])
def test_rename_rejects_invalid_names_without_changing_history(tmp_path, value):
    app = create_app(db_path=tmp_path / "chat.db", agent=Agent())
    with TestClient(app) as client:
        chat = boot(client, "Сохранённое название")
        response = client.post(
            f"/api/conversations/{chat}/manage", json={"action": "rename", "title": value}
        )
        assert response.status_code == 400
        assert (
            client.get("/api/conversations").json()["items"][0]["title"] == "Сохранённое название"
        )


def test_rename_checks_owner_csrf_pending_and_supports_archives(tmp_path):
    app = create_app(db_path=tmp_path / "chat.db", agent=Agent())
    with TestClient(app) as client, TestClient(app) as other:
        chat = boot(client, "Исходное")
        boot(other)
        endpoint = f"/api/conversations/{chat}/manage"
        body = {"action": "rename", "title": "  Аргон   и гелий  "}
        assert other.post(endpoint, json=body).status_code == 404
        assert (
            client.post(endpoint, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        )
        assert client.post(endpoint, json={"action": "archive"}).status_code == 200
        renamed = client.post(endpoint, json=body)
        assert renamed.status_code == 200 and renamed.json()["conversation"]["archived"] == 1
        assert renamed.json()["conversation"]["title"] == "Аргон и гелий"
        client.post(endpoint, json={"action": "restore"})
        app.state.storage.start_turn(chat, "pending", "Вопрос")
        assert client.post(endpoint, json=body).status_code == 409


def test_upgrade_titles_existing_chats_without_touching_messages_or_activity(tmp_path):
    path = tmp_path / "old.db"
    store = Storage(path)
    owner, _ = store.new_owner()
    generic = store.create_conversation(owner["id"], "Новый чат")
    custom = store.create_conversation(owner["id"], "Моё название")
    with store.connect() as db:
        store._insert_message(db, generic["id"], "user", "Какие есть марки гелия?", "complete")
        store._insert_message(db, custom["id"], "user", "Другой вопрос", "complete")
        original = db.execute("SELECT id,content FROM messages ORDER BY seq").fetchall()
        original = [tuple(row) for row in original]
    # Simulate the previous schema, which had no automatic/manual title marker.
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE conversations DROP COLUMN title_auto")
    upgraded = Storage(path)
    assert upgraded.conversation(owner["id"], generic["id"])["title"] == "Какие есть марки гелия?"
    assert upgraded.conversation(owner["id"], custom["id"])["title"] == "Моё название"
    assert upgraded.conversation(owner["id"], generic["id"])["updated_at"] == generic["updated_at"]
    with upgraded.connect() as db:
        assert [
            tuple(row) for row in db.execute("SELECT id,content FROM messages ORDER BY seq")
        ] == original


def test_failed_first_turn_still_has_topic_and_long_title_is_bounded(tmp_path):
    class Broken:
        async def respond(self, *args, **kwargs):
            raise ValueError("test failure")

    app = create_app(db_path=tmp_path / "chat.db", agent=Broken())
    with TestClient(app) as client:
        chat = boot(client)
        question = "Сравнение характеристик газов " * 15
        assert (
            client.post(
                f"/api/conversations/{chat}/messages", json={"request_id": "q", "content": question}
            ).status_code
            == 502
        )
        title = client.get("/api/conversations").json()["items"][0]["title"]
        assert title.startswith("Сравнение характеристик газов") and len(title) <= 72
        assert title.endswith("…")


def test_rename_module_is_served_to_the_browser(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db", agent=Agent())) as client:
        assert client.get("/static/conversation_titles.js").status_code == 200
        assert "rename-dialog" in client.get("/").text
