"""A tiny in-process stand-in for Supabase GoTrue, enough for supabase-js email/password flows."""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt

from tests.fakes.supabase import FakeSupabase


class FakeGoTrue:
    def __init__(self, db: FakeSupabase, secret: str, require_confirmation: bool = False) -> None:
        self.db, self.secret = db, secret
        self.require_confirmation = require_confirmation
        self.users: dict[str, dict[str, Any]] = {}  # email -> record
        self.refresh: dict[str, str] = {}  # refresh token -> user id
        self.server: ThreadingHTTPServer | None = None
        self.port = 0

    # -------------------------------------------------------------------- helpers
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def issuer(self) -> str:
        return f"{self.url}/auth/v1"

    def add_user(self, email: str, password: str, name: str = "Test User") -> str:
        user_id = self.db.add_user(name=name)
        self.users[email] = {"id": user_id, "password": password, "name": name, "confirmed": True}
        return user_id

    def _user_json(self, rec: dict[str, Any], email: str) -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "id": rec["id"],
            "aud": "authenticated",
            "role": "authenticated",
            "email": email,
            "email_confirmed_at": now,
            "created_at": now,
            "updated_at": now,
            "app_metadata": {"provider": "email"},
            "user_metadata": {"full_name": rec["name"]},
        }

    def session_for(self, email: str) -> dict[str, Any]:
        rec = self.users[email]
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": rec["id"],
                "aud": "authenticated",
                "iss": self.issuer,
                "role": "authenticated",
                "email": email,
                "iat": now,
                "exp": now + 3600,
            },
            self.secret,
            algorithm="HS256",
        )
        refresh = uuid.uuid4().hex
        self.refresh[refresh] = email
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": 3600,
            "expires_at": now + 3600,
            "refresh_token": refresh,
            "user": self._user_json(rec, email),
        }

    # --------------------------------------------------------------------- server
    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def _send(self, status: int, body: Any = None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Methods", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def do_OPTIONS(self) -> None:
                self._send(204)

            def do_GET(self) -> None:
                if urlparse(self.path).path.endswith("/user"):
                    auth = self.headers.get("Authorization", "")
                    try:
                        claims = jwt.decode(
                            auth[7:], outer.secret, algorithms=["HS256"], audience="authenticated"
                        )
                    except jwt.PyJWTError:
                        return self._send(401, {"code": 401, "msg": "bad jwt"})
                    for email, rec in outer.users.items():
                        if rec["id"] == claims["sub"]:
                            return self._send(200, outer._user_json(rec, email))
                    return self._send(
                        404, {"code": 404, "error_code": "user_not_found", "msg": "gone"}
                    )
                self._send(404, {"msg": "not found"})
                return None

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                body = self._json()
                if parsed.path.endswith("/token"):
                    grant = query.get("grant_type", [""])[0]
                    if grant == "password":
                        rec = outer.users.get(body.get("email", ""))
                        if not rec or rec["password"] != body.get("password"):
                            return self._send(
                                400,
                                {
                                    "code": 400,
                                    "error_code": "invalid_credentials",
                                    "msg": "Invalid login credentials",
                                },
                            )
                        if not rec["confirmed"]:
                            return self._send(
                                400,
                                {
                                    "code": 400,
                                    "error_code": "email_not_confirmed",
                                    "msg": "Email not confirmed",
                                },
                            )
                        return self._send(200, outer.session_for(body["email"]))
                    if grant == "refresh_token":
                        email = outer.refresh.pop(body.get("refresh_token", ""), None)
                        if email is None or outer.users[email]["id"] not in outer.db.auth_users:
                            return self._send(
                                400,
                                {
                                    "code": 400,
                                    "error_code": "refresh_token_not_found",
                                    "msg": "Invalid Refresh Token",
                                },
                            )
                        return self._send(200, outer.session_for(email))
                if parsed.path.endswith("/signup"):
                    email = body.get("email", "")
                    if email in outer.users:
                        return self._send(
                            422,
                            {
                                "code": 422,
                                "error_code": "user_already_exists",
                                "msg": "User already registered",
                            },
                        )
                    name = (body.get("data") or {}).get("full_name", "")
                    user_id = outer.db.add_user(name=name)
                    outer.users[email] = {
                        "id": user_id,
                        "password": body["password"],
                        "name": name,
                        "confirmed": not outer.require_confirmation,
                    }
                    if outer.require_confirmation:
                        return self._send(200, outer._user_json(outer.users[email], email))
                    return self._send(200, outer.session_for(email))
                if parsed.path.endswith("/logout"):
                    return self._send(204)
                if parsed.path.endswith("/recover"):
                    return self._send(200, {})
                self._send(404, {"msg": "not found"})
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
