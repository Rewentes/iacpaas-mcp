import copy

import pytest
from starlette.testclient import TestClient

from gas_web.app import create_app


class Agent:
    def __init__(self):
        self.calls = []

    async def respond(self, content, history, summary=None):
        self.calls.append((content, copy.deepcopy(history), summary))
        return {
            "answer": "Ответ: " + content,
            "sources": [{"id": "e1", "value": 42}],
            "verification": {"status": "verified", "reason": "Источник"},
            "events": [{"name": "read", "result": {"value": 42}}],
            "context": {"estimated_input_tokens": 100, "limit": 48000, "compactions": 0},
            "summary_update": None,
        }


def boot(client):
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf_token"]
    return client.post("/api/conversations", json={"title": "Сравнение газов"}).json()["id"]


@pytest.mark.parametrize("action", ["edit", "regenerate"])
def test_revisions_preserve_original_exclude_future_and_replay_without_duplicate_inference(
    tmp_path, action
):
    agent = Agent()
    app = create_app(db_path=tmp_path / "chat.db", agent=agent)
    with TestClient(app) as client:
        origin = boot(client)
        uri = f"/api/conversations/{origin}/messages"
        turns = [
            client.post(uri, json={"request_id": str(i), "content": f"Вопрос {i}"}).json()
            for i in range(3)
        ]
        original = client.get(uri).json()["messages"]
        body = {
            "action": action,
            "request_id": "revision",
            "message_id": turns[1]["user_message" if action == "edit" else "assistant_message"][
                "id"
            ],
        }
        if action == "edit":
            body["content"] = "Исправленный вопрос"
        result = client.post(f"/api/conversations/{origin}/revisions", json=body)
        assert result.status_code == 200, result.text
        branch = result.json()
        assert len(agent.calls) == 3  # Creating/replaying a version never calls a model.
        assert branch["conversation"]["id"] != origin
        branch_uri = f"/api/conversations/{branch['conversation']['id']}/messages"
        history = client.get(branch_uri).json()
        assert [m["content"] for m in history["messages"]] == ["Вопрос 0", "Ответ: Вопрос 0"]
        assert history["messages"][1]["sources"] == original[1]["sources"]
        assert history["messages"][0]["created_at"] == original[0]["created_at"]
        assert history["messages"][0]["id"] != original[0]["id"]
        assert app.state.storage.summary(branch["conversation"]["id"]) is None
        assert client.post(f"/api/conversations/{origin}/revisions", json=body).json() == branch
        assert len(client.get("/api/conversations").json()["items"]) == 2
        submission = {"content": branch["content"], "request_id": branch["request_id"]}
        answer = client.post(branch_uri, json=submission)
        assert answer.status_code == 200
        assert client.post(branch_uri, json=submission).json() == answer.json()
        assert len(agent.calls) == 4
        assert agent.calls[-1][0] == ("Исправленный вопрос" if action == "edit" else "Вопрос 1")
        assert [m["content"] for m in agent.calls[-1][1]] == ["Вопрос 0", "Ответ: Вопрос 0"]
        assert client.get(uri).json()["messages"] == original
        if action == "edit":
            assert (
                client.post(
                    f"/api/conversations/{origin}/revisions", json={**body, "content": "Иное"}
                ).status_code
                == 409
            )
        cookies = dict(client.cookies)
    with TestClient(
        create_app(db_path=tmp_path / "chat.db", agent=agent), cookies=cookies
    ) as restarted:
        restarted.headers["X-CSRF-Token"] = restarted.get("/api/me").json()["csrf_token"]
        replay = restarted.post(f"/api/conversations/{origin}/revisions", json=body).json()
        assert replay["conversation"]["id"] == branch["conversation"]["id"]
        assert restarted.get(uri).json()["messages"] == original


def test_revision_requires_owner_csrf_and_message_from_this_conversation(tmp_path):
    app = create_app(db_path=tmp_path / "chat.db", agent=Agent())
    with TestClient(app) as client, TestClient(app) as other:
        origin = boot(client)
        turn = client.post(
            f"/api/conversations/{origin}/messages", json={"request_id": "first", "content": "Газ"}
        ).json()
        body = {
            "action": "edit",
            "message_id": turn["user_message"]["id"],
            "content": "Аргон",
            "request_id": "revision",
        }
        endpoint = f"/api/conversations/{origin}/revisions"
        boot(other)
        assert other.post(endpoint, json=body).status_code == 404
        assert (
            client.post(endpoint, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        )
        assert (
            client.post(
                endpoint, json=body, headers={"Origin": "https://elsewhere.test"}
            ).status_code
            == 403
        )
        unrelated = client.post("/api/conversations", json={"title": "Другой"}).json()["id"]
        assert (
            client.post(f"/api/conversations/{unrelated}/revisions", json=body).status_code == 404
        )
        assert (
            client.post(
                endpoint, json={**body, "message_id": turn["assistant_message"]["id"]}
            ).status_code
            == 400
        )
        assert client.post(endpoint, json={**body, "action": "regenerate"}).status_code == 400


def test_failed_question_can_be_corrected_and_pending_question_cannot(tmp_path):
    app = create_app(db_path=tmp_path / "chat.db", agent=Agent())
    with TestClient(app) as client:
        origin = boot(client)
        store = app.state.storage
        message, _, _ = store.start_turn(origin, "broken", "Неудачный запрос")
        body = {
            "action": "edit",
            "message_id": message["id"],
            "content": "Новый вопрос",
            "request_id": "revision",
        }
        endpoint = f"/api/conversations/{origin}/revisions"
        assert client.post(endpoint, json=body).status_code == 400
        store.fail_turn(origin, "broken", message, {"error": {}}, 502)
        assert client.post(endpoint, json=body).status_code == 200
        assert store.history(origin)[0]["state"] == "failed"


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"action": "delete"},
        {"action": "edit", "request_id": "r", "message_id": "m", "content": ""},
    ],
)
def test_malformed_revision_is_rejected(tmp_path, body):
    with TestClient(create_app(db_path=tmp_path / "chat.db", agent=Agent())) as client:
        origin = boot(client)
        assert client.post(f"/api/conversations/{origin}/revisions", json=body).status_code == 400
