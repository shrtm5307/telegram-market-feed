
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = 0
API_HASH = ""

if not API_ID or not API_HASH:
    raise SystemExit("이 파일의 API_ID / API_HASH를 먼저 입력하세요.")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n아래 TELEGRAM_SESSION_STRING 전체를 복사해 Railway 환경변수에 넣으세요.\n")
    print(client.session.save())
