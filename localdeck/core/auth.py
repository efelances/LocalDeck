"""Single-use QR LAN pairing tokens, hashed session cookies, and desktop token auth."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_device_name(user_agent: str) -> str:
    """Return a friendly device label derived from the HTTP User-Agent string."""
    ua = (user_agent or "").lower()
    if "iphone" in ua:
        return "iPhone"
    if "ipad" in ua:
        return "iPad"
    if "android" in ua:
        if "mobile" in ua:
            return "Android Phone"
        return "Android Tablet"
    if "windows" in ua or "macintosh" in ua or "linux" in ua:
        return "Desktop Browser"
    return "Mobile Device"


@dataclass
class PairingToken:
    token: str
    expires_at: float
    used: bool = False

    def is_valid(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return not self.used and current < self.expires_at

    def seconds_remaining(self, now: float | None = None) -> int:
        current = time.time() if now is None else now
        return max(0, int(self.expires_at - current))


@dataclass
class PairedSession:
    session_id: str
    token_hash: str
    device_name: str
    client_ip: str
    created_at: float
    last_seen: float


class AuthManager:
    """Manages desktop API token, one-time QR pairing tokens, and paired LAN sessions."""

    COOKIE_NAME = "localdeck_session"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_file = self._data_dir / "sessions.json"
        self._lock = threading.RLock()
        self.desktop_token: str = secrets.token_urlsafe(32)
        self._pairing_tokens: dict[str, PairingToken] = {}
        self._current_pairing_token: str | None = None
        self._sessions: dict[str, PairedSession] = self._load_sessions()

    def _load_sessions(self) -> dict[str, PairedSession]:
        if not self._sessions_file.exists():
            return {}
        try:
            raw = self._sessions_file.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            data = json.loads(raw)
            sessions: dict[str, PairedSession] = {}
            for item in data:
                sess = PairedSession(**item)
                sessions[sess.session_id] = sess
            return sessions
        except (OSError, TypeError, ValueError):
            return {}

    def _save_sessions_locked(self) -> None:
        tmp_path = self._sessions_file.with_suffix(".json.tmp")
        payload = json.dumps([asdict(s) for s in self._sessions.values()], indent=2)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self._sessions_file)

    def create_pairing_token(self, ttl_seconds: int = 300) -> PairingToken:
        with self._lock:
            self._pairing_tokens.clear()
            token_value = secrets.token_urlsafe(24)
            pt = PairingToken(token=token_value, expires_at=time.time() + ttl_seconds, used=False)
            self._pairing_tokens[token_value] = pt
            self._current_pairing_token = token_value
            return pt

    def get_or_create_pairing_token(self, ttl_seconds: int = 300) -> PairingToken:
        with self._lock:
            if self._current_pairing_token:
                current = self._pairing_tokens.get(self._current_pairing_token)
                if current and current.is_valid():
                    return current
            return self.create_pairing_token(ttl_seconds=ttl_seconds)

    def redeem_pairing_token(
        self,
        token: str,
        user_agent: str,
        client_ip: str,
    ) -> tuple[PairedSession, str] | None:
        if not token:
            return None
        with self._lock:
            now = time.time()
            pt = self._pairing_tokens.get(token)
            if pt is None or not pt.is_valid(now=now):
                return None
            pt.used = True
            session_id = secrets.token_hex(8)
            raw_secret = secrets.token_urlsafe(32)
            cookie_value = f"{session_id}.{raw_secret}"
            session = PairedSession(
                session_id=session_id,
                token_hash=_hash_secret(raw_secret),
                device_name=parse_device_name(user_agent),
                client_ip=client_ip or "unknown",
                created_at=now,
                last_seen=now,
            )
            self._sessions[session_id] = session
            self._save_sessions_locked()
            return session, cookie_value

    def validate_session_cookie(self, cookie_value: str | None) -> PairedSession | None:
        if not cookie_value or "." not in cookie_value:
            return None
        session_id, raw_secret = cookie_value.split(".", 1)
        if not session_id or not raw_secret:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            candidate_hash = _hash_secret(raw_secret)
            if not hmac.compare_digest(candidate_hash, session.token_hash):
                return None
            session.last_seen = time.time()
            return session

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "session_id": s.session_id,
                    "device_name": s.device_name,
                    "client_ip": s.client_ip,
                    "created_at": s.created_at,
                    "last_seen": s.last_seen,
                }
                for s in sorted(self._sessions.values(), key=lambda x: x.created_at, reverse=True)
            ]

    def revoke_session(self, session_id: str) -> bool:
        with self._lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            self._save_sessions_locked()
            return True

    def revoke_all_sessions(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            self._save_sessions_locked()
            return count
