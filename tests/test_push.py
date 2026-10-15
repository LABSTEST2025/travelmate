"""B01 : notifications push (abonnements, envois aux étapes clés, abonnements expirés)."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.store.sqlite import SqliteStore
from py_vapid import Vapid
from pywebpush import WebPushException

from tests.test_api import _create, _group, _vote, graph  # noqa: F401  (fixture)
from travel_agent.web.app import create_app
from travel_agent.web.push import PushConfig, PushNotifier, generate_vapid_keys, subscription_error
from travel_agent.web.repository import TripRepository
from travel_agent.web.runner import TripRunner

FCM = "https://fcm.googleapis.com/fcm/send/"


def _subscription(name: str) -> dict:
    return {"endpoint": FCM + name, "keys": {"p256dh": "BPk" + name, "auth": "au" + name}}


class FakeSender:
    def __init__(self):
        self.sent: list[dict] = []
        self.gone: set[str] = set()

    def __call__(self, *, subscription_info, data, **_kwargs):
        if subscription_info["endpoint"] in self.gone:
            raise WebPushException("Push failed: 410 Gone", response=SimpleNamespace(status_code=410, text=""))
        self.sent.append({"endpoint": subscription_info["endpoint"], "data": data})


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def executor():
    # Comme en production, les runs tournent hors de la requête (et hors du verrou du voyage).
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


def _drain(executor: ThreadPoolExecutor) -> None:
    """Attend la fin du run en cours et de ses notifications (un seul thread de travail)."""
    executor.submit(lambda: None).result(timeout=10)


@pytest.fixture
def client(graph, sender, executor):  # noqa: F811
    store = SqliteStore(sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None))
    store.setup()
    notifier = PushNotifier(PushConfig("PUBLIC", "PRIVATE", "mailto:test@example.com"), sender=sender)
    app = create_app(TripRepository(store), TripRunner(graph, executor), notifier=notifier)
    return lambda: TestClient(app)


def test_vapid_keys_sign_push_requests():
    public, private = generate_vapid_keys()

    headers = Vapid.from_string(private_key=private).sign(
        {"sub": "mailto:a@example.com", "aud": "https://fcm.googleapis.com", "exp": int(time.time()) + 60}
    )

    assert len(public) == 87 and public in headers["Authorization"]


@pytest.mark.parametrize(
    "endpoint, accepted",
    [
        (FCM + "abc", True),
        ("https://web.push.apple.com/abc", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
        ("http://fcm.googleapis.com/fcm/send/abc", False),
        ("https://fcm.googleapis.com.evil.example/abc", False),
        ("https://127.0.0.1/abc", False),
    ],
)
def test_only_browser_push_services_are_accepted(endpoint, accepted):
    assert (subscription_error(endpoint) is None) is accepted


def test_push_config_and_subscription(client):
    organizer, stranger = client(), client()
    trip_id = _create(organizer)

    assert organizer.get("/api/push/config").json() == {"public_key": "PUBLIC"}
    assert stranger.post(f"/api/trips/{trip_id}/push", json=_subscription("x")).status_code == 401
    bad = _subscription("x") | {"endpoint": "https://evil.example/x"}
    assert organizer.post(f"/api/trips/{trip_id}/push", json=bad).status_code == 422

    view = organizer.post(f"/api/trips/{trip_id}/push", json=_subscription("maya")).json()
    assert view["me"]["push_endpoints"] == [FCM + "maya"]
    view = organizer.post(f"/api/trips/{trip_id}/push", json=_subscription("maya")).json()
    assert view["me"]["push_endpoints"] == [FCM + "maya"]  # pas de doublon

    view = organizer.post(f"/api/trips/{trip_id}/push/remove", json={"endpoint": FCM + "maya"}).json()
    assert view["me"]["push_endpoints"] == []


def test_members_are_told_when_ideas_and_plan_are_ready(client, sender, executor):
    trip_id, (organizer, friend) = _group(client, [1500, 1500])
    organizer.post(f"/api/trips/{trip_id}/push", json=_subscription("maya"))
    friend.post(f"/api/trips/{trip_id}/push", json=_subscription("leo"))

    organizer.post(f"/api/trips/{trip_id}/launch")
    _drain(executor)

    assert {s["endpoint"] for s in sender.sent} == {FCM + "maya", FCM + "leo"}
    assert "Les idées sont prêtes" in sender.sent[0]["data"] and f"/t/{trip_id}" in sender.sent[0]["data"]
    assert "1500" not in sender.sent[0]["data"]  # jamais de budget dans une notification

    sender.sent.clear()
    _vote(organizer, trip_id, "Portugal")
    _vote(friend, trip_id, "Portugal")
    _drain(executor)

    assert len(sender.sent) == 2 and all("Le plan est prêt" in s["data"] for s in sender.sent)


def test_only_the_organizer_is_told_about_the_draft(client, sender, executor, monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_HITL", "true")
    trip_id, (organizer, friend) = _group(client, [1500, 1500])
    organizer.post(f"/api/trips/{trip_id}/push", json=_subscription("maya"))
    friend.post(f"/api/trips/{trip_id}/push", json=_subscription("leo"))
    organizer.post(f"/api/trips/{trip_id}/launch")
    _drain(executor)
    sender.sent.clear()

    _vote(organizer, trip_id, "Portugal")
    _vote(friend, trip_id, "Portugal")
    _drain(executor)

    assert [s["endpoint"] for s in sender.sent] == [FCM + "maya"]
    assert "brouillon" in sender.sent[0]["data"]


def test_expired_subscriptions_are_forgotten(client, sender, executor):
    trip_id, (organizer, friend) = _group(client, [1500, 1500])
    organizer.post(f"/api/trips/{trip_id}/push", json=_subscription("maya"))
    friend.post(f"/api/trips/{trip_id}/push", json=_subscription("leo"))
    sender.gone.add(FCM + "leo")

    organizer.post(f"/api/trips/{trip_id}/launch")
    _drain(executor)

    assert friend.get(f"/api/trips/{trip_id}").json()["me"]["push_endpoints"] == []
    assert organizer.get(f"/api/trips/{trip_id}").json()["me"]["push_endpoints"] == [FCM + "maya"]


def test_push_is_off_without_vapid_keys(graph):  # noqa: F811
    store = SqliteStore(sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None))
    store.setup()
    organizer = TestClient(create_app(TripRepository(store), TripRunner(graph)))
    trip_id = _create(organizer)

    assert organizer.get("/api/push/config").json() == {"public_key": None}
    assert organizer.post(f"/api/trips/{trip_id}/push", json=_subscription("x")).status_code == 404
