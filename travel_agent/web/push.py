"""Notifications push (B01) : Web Push standard, clés VAPID, sans service tiers à part celui du navigateur.

Les abonnements sont rangés dans le `TripRecord`, par membre. Le contenu d'une notification se limite
à un titre, une phrase et le lien du voyage : jamais de budget ni de vote.
"""

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)

MAX_SUBSCRIPTIONS_PER_MEMBER = 5
# Le serveur envoie une requête à l'URL fournie par le navigateur : seuls les services de push des
# navigateurs sont acceptés (sinon, n'importe qui ferait appeler une URL arbitraire au serveur).
_PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    ".push.apple.com",
    ".notify.windows.com",
)
_GONE = {404, 410}  # abonnement expiré ou révoqué : on l'oublie


@dataclass(frozen=True)
class PushConfig:
    public_key: str
    private_key: str
    subject: str

    @classmethod
    def from_env(cls) -> "PushConfig | None":
        public_key = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
        private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
        if not (public_key and private_key):
            return None
        return cls(public_key, private_key, os.environ.get("VAPID_SUBJECT", "").strip() or "mailto:contact@example.com")


def subscription_error(endpoint: str) -> str | None:
    url = urlparse(endpoint)
    host = url.hostname or ""
    if url.scheme != "https" or not any(host == h or (h.startswith(".") and host.endswith(h)) for h in _PUSH_HOSTS):
        return "Service de notifications non reconnu."
    return None


Sender = Callable[..., object]


class PushNotifier:
    """Envoie une notification à une liste d'abonnements et renvoie les points d'accès à oublier."""

    def __init__(self, config: PushConfig, sender: Sender = webpush):
        self.config = config
        self._send = sender

    def send(self, subscriptions: list[dict], title: str, body: str, url: str) -> set[str]:
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": url}, ensure_ascii=False)
        gone: set[str] = set()
        for subscription in subscriptions:
            try:
                self._send(
                    subscription_info=subscription,
                    data=payload,
                    vapid_private_key=self.config.private_key,
                    vapid_claims={"sub": self.config.subject},
                    ttl=24 * 3600,
                    timeout=10,
                )
            except WebPushException as exc:
                if exc.status_code in _GONE:
                    gone.add(subscription["endpoint"])
                else:
                    logger.warning("Notification push non envoyée : %s", exc.message)
            except Exception:
                logger.exception("Notification push non envoyée")
        return gone


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_vapid_keys() -> tuple[str, str]:
    """(clé publique, clé privée) au format attendu par les navigateurs et par pywebpush."""
    vapid = Vapid()
    vapid.generate_keys()
    public = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public), _b64url(private)


def main() -> None:
    """`uv run travel-vapid` : génère une paire de clés à copier dans `.env` (une seule fois)."""
    public, private = generate_vapid_keys()
    print(f"VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}\nVAPID_SUBJECT=mailto:toi@example.com")
