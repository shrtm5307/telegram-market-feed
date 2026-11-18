"""Minimal Google Drive client for the Telegram media archive.

Uses an OAuth *user* credential limited to the ``drive.file`` scope, so the app
can only see files and folders it created itself. Standard library only; the
HTTP layer is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_FOLDER_NAME = "Telegram_Media_Archive"
SCOPE = "https://www.googleapis.com/auth/drive.file"

# (method, url, headers, body, timeout) -> (status, response_headers, body)
Http = Callable[[str, str, dict, "bytes | None", float], "tuple[int, dict, bytes]"]


class DriveError(Exception):
    """Drive call failed. Messages never contain credentials."""


class DriveAuthError(DriveError):
    """The OAuth credential was rejected; retrying in this run is pointless."""


def credentials_from_env(env: Mapping[str, str] | None = None) -> tuple[str, str, str] | None:
    env = os.environ if env is None else env
    values = tuple((env.get(name) or "").strip() for name in (
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"))
    return values if all(values) else None  # type: ignore[return-value]


def urllib_http(method: str, url: str, headers: dict, body: bytes | None,
                timeout: float) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DriveError(f"network error: {type(exc).__name__}") from None


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _error_text(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or "")[:200]
        return str(error or data.get("error_description") or "")[:200]
    except (ValueError, AttributeError):
        return ""


class DriveClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 http: Http = urllib_http, timeout: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._http = http
        self._timeout = timeout
        self._clock = clock
        self._token = ""
        self._token_expiry = 0.0
        self._folders: dict[tuple[str | None, str], str] = {}

    # -- auth -----------------------------------------------------------------
    def _access_token(self, force: bool = False) -> str:
        if self._token and not force and self._clock() < self._token_expiry - 60:
            return self._token
        body = urllib.parse.urlencode({
            "client_id": self._client_id, "client_secret": self._client_secret,
            "refresh_token": self._refresh_token, "grant_type": "refresh_token",
        }).encode()
        status, _, raw = self._http(
            "POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
            body, self._timeout)
        if status in (400, 401):
            raise DriveAuthError(f"token refresh rejected ({status}): {_error_text(raw)}")
        if status != 200:
            raise DriveError(f"token refresh failed ({status})")
        try:
            data = json.loads(raw)
            self._token = data["access_token"]
            self._token_expiry = self._clock() + float(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise DriveError("token response malformed") from None
        return self._token

    def _call(self, method: str, url: str, headers: dict | None = None,
              body: bytes | None = None) -> tuple[int, dict, bytes]:
        for attempt in (0, 1):
            merged = {"Authorization": f"Bearer {self._access_token(force=attempt == 1)}"}
            merged.update(headers or {})
            status, response_headers, raw = self._http(method, url, merged, body, self._timeout)
            if status == 401 and attempt == 0:
                continue
            if status == 401:
                raise DriveAuthError("access token rejected")
            return status, response_headers, raw
        raise DriveError("unreachable")  # pragma: no cover

    def _json(self, method: str, url: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        status, _, raw = self._call(method, url, headers, body)
        if status >= 400:
            raise DriveError(f"drive {status}: {_error_text(raw)}")
        return json.loads(raw) if raw else {}

    # -- folders --------------------------------------------------------------
    def _find(self, query: str, fields: str = "files(id,name)") -> list[dict]:
        url = f"{API}/files?" + urllib.parse.urlencode({
            "q": query, "fields": fields, "pageSize": 10, "spaces": "drive"})
        return self._json("GET", url).get("files", [])

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        cache_key = (parent_id, name)
        if cache_key in self._folders:
            return self._folders[cache_key]
        parent_clause = f"'{_q(parent_id)}' in parents" if parent_id else "'root' in parents"
        found = self._find(f"mimeType='{FOLDER_MIME}' and name='{_q(name)}' "
                           f"and trashed=false and {parent_clause}")
        if found:
            folder_id = found[0]["id"]
        else:
            metadata = {"name": name, "mimeType": FOLDER_MIME}
            if parent_id:
                metadata["parents"] = [parent_id]
            folder_id = self._json("POST", f"{API}/files?fields=id", metadata)["id"]
        self._folders[cache_key] = folder_id
        return folder_id

    def ensure_path(self, parts: list[str]) -> str:
        """Create ``Telegram_Media_Archive/<parts...>`` and return the leaf id."""
        folder_id = self.ensure_folder(ROOT_FOLDER_NAME)
        for part in parts:
            folder_id = self.ensure_folder(part, folder_id)
        return folder_id

    # -- files ----------------------------------------------------------------
    def find_by_key(self, app_key: str, parent_id: str) -> str | None:
        found = self._find(
            f"appProperties has {{ key='tg_key' and value='{_q(app_key)}' }} "
            f"and '{_q(parent_id)}' in parents and trashed=false")
        return found[0]["id"] if found else None

    def upload(self, name: str, data: bytes, mime_type: str, parent_id: str,
               app_key: str) -> str:
        """Upload via a resumable session. Idempotent per ``app_key``."""
        existing = self.find_by_key(app_key, parent_id)
        if existing:
            return existing
        metadata = {"name": name, "parents": [parent_id],
                    "appProperties": {"tg_key": app_key}}
        status, headers, raw = self._call(
            "POST", f"{UPLOAD_API}?uploadType=resumable&fields=id",
            {"Content-Type": "application/json; charset=UTF-8",
             "X-Upload-Content-Type": mime_type or "application/octet-stream",
             "X-Upload-Content-Length": str(len(data))},
            json.dumps(metadata).encode())
        if status >= 400:
            raise DriveError(f"upload start {status}: {_error_text(raw)}")
        location = next((v for k, v in headers.items() if k.lower() == "location"), "")
        if not location.startswith("https://"):
            raise DriveError("upload session missing")
        status, _, raw = self._http(
            "PUT", location, {"Content-Type": mime_type or "application/octet-stream"},
            data, max(self._timeout, 120.0))
        if status >= 400:
            raise DriveError(f"upload {status}: {_error_text(raw)}")
        try:
            return json.loads(raw)["id"]
        except (ValueError, KeyError, TypeError):
            raise DriveError("upload response malformed") from None

    def storage_quota(self) -> dict:
        """Return ``{usage_bytes, limit_bytes}`` (limit may be None if unlimited)."""
        data = self._json("GET", f"{API}/about?fields=storageQuota")
        quota = data.get("storageQuota", {})

        def num(key: str):
            try:
                return int(quota[key])
            except (KeyError, TypeError, ValueError):
                return None
        return {"usage_bytes": num("usage"), "limit_bytes": num("limit")}
