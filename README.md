# telegram-market-feed

Railway에서 24시간 실행되는 Telegram 뉴스 수집기입니다.

필수 Railway 환경변수:
- TELEGRAM_API_ID
- TELEGRAM_API_HASH
- TELEGRAM_SESSION_STRING
- FEED_TOKEN

선택 Railway 환경변수:
- SNAPSHOT_MAX_ITEMS (기본값 500): /app/feed_snapshot.json 에 저장되는 최신 피드 아이템 수

실행 명령:
`uvicorn app:app --host 0.0.0.0 --port $PORT`
