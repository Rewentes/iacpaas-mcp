import pytest
from starlette.testclient import TestClient
from test_portable_chat_revisions import Agent, boot

from gas_web.app import create_app


def test_archive_restore_delete_preserves_branch_and_other_owners(tmp_path):
    app = create_app(db_path=tmp_path / "chat.db", agent=Agent())
    with TestClient(app) as client, TestClient(app) as other:
        chat = boot(client)
        turn = client.post(
            f"/api/conversations/{chat}/messages", json={"request_id": "r", "content": "Тест"}
        ).json()
        branch = client.post(
            f"/api/conversations/{chat}/revisions",
            json={
                "action": "edit",
                "request_id": "v",
                "message_id": turn["user_message"]["id"],
                "content": "Новый",
            },
        ).json()["conversation"]["id"]
        endpoint = f"/api/conversations/{chat}/manage"
        boot(other)
        assert other.post(endpoint, json={"action": "delete"}).status_code == 404
        assert (
            client.post(
                endpoint, json={"action": "archive"}, headers={"X-CSRF-Token": "wrong"}
            ).status_code
            == 403
        )
        assert client.post(endpoint, json={"action": "archive"}).status_code == 200
        assert (
            client.get(f"/api/conversations/{chat}/messages").json()["conversation"]["archived"]
            == 1
        )
        assert len(client.get(f"/api/conversations/{chat}/messages").json()["messages"]) == 2
        assert (
            client.post(
                f"/api/conversations/{chat}/messages", json={"request_id": "r2", "content": "Ещё"}
            ).status_code
            == 409
        )
        assert client.post(endpoint, json={"action": "restore"}).status_code == 200
        assert client.post(endpoint, json={"action": "delete"}).status_code == 200
        assert client.get(f"/api/conversations/{chat}/messages").status_code == 404
        assert client.get(f"/api/conversations/{branch}/messages").status_code == 200
        for table in ("events", "requests", "messages", "conversation_revisions"):
            with app.state.storage.connect() as db:
                assert (
                    db.execute(
                        f"SELECT count(*) FROM {table} WHERE conversation_id=?", (chat,)
                    ).fetchone()[0]
                    == 0
                )


@pytest.mark.parametrize("action", ["archive", "restore", "delete"])
def test_pending_turn_prevents_lifecycle_mutation(tmp_path, action):
    app = create_app(db_path=tmp_path / "chat.db", agent=Agent())
    with TestClient(app) as client:
        chat = boot(client)
        app.state.storage.start_turn(chat, "pending", "Тест")
        assert (
            client.post(f"/api/conversations/{chat}/manage", json={"action": action}).status_code
            == 409
        )
