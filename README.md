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

공개 스냅샷:
- `GET /snapshot`은 토큰 없이 현재 `/app/feed_snapshot.json`을 읽습니다.
- 응답에는 `timestamp`, `count`, `status`, `items`만 포함합니다.
- 아이템은 `message_id`, `channel_id`, `channel_name`, `channel_username`,
  `date_utc`, `message`, `url`만 공개하며 count는 반환 아이템 수입니다.
- 파일 누락, 읽기 오류, 잘못된 데이터 또는 설정된 인증정보 값이 포함된 경우
  HTTP 503과 `{"timestamp":null,"count":0,"status":"unavailable","items":[]}`을 반환합니다.
  내부 오류, 파일 경로, 환경변수는 응답에 포함하지 않습니다.
- 모든 스냅샷 응답은 `Cache-Control: no-store`를 사용합니다.
- 설정된 비밀값이 일반 메시지나 숫자와 우연히 일치해도 공개를 차단합니다.
- 기존 `/feed?token=...` 인증과 조회 동작은 유지합니다.

테스트 (실제 Telegram 접속이나 인증정보 불필요):
```sh
pip install -r requirements.txt httpx==0.28.1
python -m unittest discover -v
```
