# telegram-market-feed

Railway에서 24시간 실행되는 Telegram 뉴스 수집기입니다.

필수 Railway 환경변수:
- TELEGRAM_API_ID
- TELEGRAM_API_HASH
- TELEGRAM_SESSION_STRING
- FEED_TOKEN

선택 Railway 환경변수:
- SNAPSHOT_MAX_ITEMS (기본값 500) — 외부 분석(ChatGPT 등)을 위한 sanitized snapshot.json에 보관할 최근 메시지 개수
- SNAPSHOT_PATH (기본값 snapshot.json) — snapshot 파일 경로

실행 명령:
`uvicorn app:app --host 0.0.0.0 --port $PORT`

## Snapshot

앱은 시작 시 backfill이 끝난 뒤와 새 메시지를 처리할 때마다 `snapshot.json` 파일을 원자적으로(.tmp 파일에 쓴 뒤 rename) 갱신합니다.
이 파일은 자격 증명이나 토큰을 전혀 포함하지 않는 안전한 필드만 담고 있어 외부 도구(ChatGPT 등)로 시장 트렌드를 분석하는 용도로 안전하게 공유할 수 있습니다.

snapshot.json 구조:
```json
{
  "timestamp": "2024-01-01T00:00:00+00:00",
  "message_count": 12345,
  "status": "running",
  "snapshot_items_count": 500,
  "items": [
    {
      "message_id": 1,
      "channel_id": 100,
      "channel_name": "Example Channel",
      "channel_username": "example",
      "date_utc": "2024-01-01T00:00:00+00:00",
      "message": "message text",
      "url": "https://t.me/example/1"
    }
  ]
}
```
