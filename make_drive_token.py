"""Create the Google Drive refresh token used by the collector (run locally).

1. Google Cloud Console -> APIs & Services -> enable "Google Drive API".
2. OAuth consent screen: set publishing status to "In production"
   (a "Testing" app's refresh tokens expire after 7 days).
3. Credentials -> Create OAuth client ID -> type "Desktop app".
4. Run this script, sign in, and approve the single scope ``drive.file``
   (the app can then only touch files/folders it created itself).
5. Save the printed values as GitHub Actions secrets:
   GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN.

Usage:
    pip install google-auth-oauthlib
    python make_drive_token.py                      # prompts for id/secret
    python make_drive_token.py client_secret.json   # or the downloaded JSON

Nothing is written to disk; keep the printed token private.
"""

from __future__ import annotations

import getpass
import json
import sys

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def load_client_config(argv: list[str]) -> dict:
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as handle:
            data = json.load(handle)
        return data if "installed" in data else {"installed": data.get("web", data)}
    client_id = input("GOOGLE_CLIENT_ID: ").strip()
    client_secret = getpass.getpass("GOOGLE_CLIENT_SECRET (hidden): ").strip()
    if not client_id or not client_secret:
        raise SystemExit("Client ID and secret are required.")
    return {"installed": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit("먼저 설치하세요: pip install google-auth-oauthlib")

    config = load_client_config(sys.argv)
    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh token is issued.
    credentials = flow.run_local_server(
        port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise SystemExit("refresh token이 발급되지 않았습니다. "
                         "https://myaccount.google.com/permissions 에서 앱 권한을 "
                         "삭제한 뒤 다시 실행하세요.")
    client = config["installed"]
    print("\nGitHub 저장소 Settings -> Secrets and variables -> Actions 에 아래 3개를 등록하세요.\n")
    print(f"GOOGLE_CLIENT_ID={client['client_id']}")
    print(f"GOOGLE_CLIENT_SECRET={client['client_secret']}")
    print(f"GOOGLE_REFRESH_TOKEN={credentials.refresh_token}")


if __name__ == "__main__":
    main()
