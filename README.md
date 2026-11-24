# telegram-market-feed

GitHub Actions가 Telegram 공개 피드를 주기적으로 수집해 `snapshot-data` 브랜치에 저장합니다. Railway 상시 실행 서비스 없이도 6시간·24시간 공개 피드를 유지합니다.

## GitHub Actions 설정

저장소 Settings → Secrets and variables → Actions에 아래 Repository secrets를 등록합니다.

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_SESSION_STRING`

Telegram 세션 문자열은 코드나 공개 브랜치에 넣지 않습니다. 수집 workflow는 15분 간격으로 실행되며, GitHub Actions 스케줄 특성상 실행 시각이 지연될 수 있습니다. `workflow_dispatch`로 수동 실행할 수도 있습니다.

수집 workflow는 다음 파일을 `snapshot-data` 브랜치에 갱신합니다.

- `latest_snapshot.json`
- `data/telegram/latest_6h.json`
- `data/telegram/latest_24h.json`

공개 윈도우 파일은 `generated_utc`, `freshness_status`, `latest_message_at`, `count`, `items`를 포함합니다. 각 항목은 채널명, 게시시각, 메시지, Telegram URL만 공개합니다. 빈 메시지는 제외합니다.

## 로컬 실행

실제 Telegram 접속 없이 기존 테스트를 실행하려면:

```sh
pip install -r requirements.txt httpx==0.28.1
python -m unittest discover -v
```

일회성 수집은 다음 환경변수와 함께 실행할 수 있습니다.

```sh
python telegram_snapshot.py output/latest_snapshot.json
```

기존 `app.py`의 FastAPI `/feed`·`/snapshot` 엔드포인트는 로컬 또는 별도 서버에서 사용할 수 있습니다. 해당 실행 방식에는 `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`, `FEED_TOKEN`이 필요합니다.

## Media archive, link expansion, health and watchdog

* **Drive archive (optional).** Photos, PDFs and other documents up to 50 MB are
  uploaded to Google Drive under `Telegram_Media_Archive/YYYY-MM/DD/<channel>/`
  (dates in KST). PDFs also get a `.txt` from `pdftotext` in the same folder.
  Video/audio are not archived. File bodies never enter this repository; the public
  `data/telegram/media_index.json` holds only Drive file id, name, size, SHA-256 and
  status. Feed attachments keep `type`/`name`/`mime_type` and gain `drive_file_id`,
  `size`, `sha256`, `status`.
* **Secrets** (GitHub Actions): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `GOOGLE_REFRESH_TOKEN` (OAuth user credential, scope `drive.file`). Create the
  refresh token locally with `python make_drive_token.py`. Without them uploads are
  skipped and the text feed is unchanged. Failed/pending files are retried on the
  next run; per-run limits: `MEDIA_MAX_FILES_PER_RUN`, `MEDIA_MAX_MB_PER_RUN`,
  `MEDIA_MAX_SECONDS_PER_RUN`.
* **Links.** Each feed item has `links[]` with `url`, `final_url`, `status`
  (`ok`, `blocked`, `not_found`, `timeout`, `error`, `invalid`, `pending`) and `title`.
  5 s limit per link, results cached in `data/telegram/link_cache.json`; sites that
  refuse automated clients are reported as `blocked`, never bypassed.
* **What is collected.** Every channel post except advertisements
  (`message_fields.py`). Service events (pins, title changes) are not posts and are
  skipped. A post with no text and no file is still kept: its attachment `type`
  says what it was (`poll` and `todo` carry the question/items in `name`,
  `webpage` the preview title, then `story`, `paid_media`, `giveaway`, `geo`,
  `contact`, `unsupported`, `other` = unknown media class in `name`, `empty`).
  Only `photo`, `pdf` and `document` are downloaded for the Drive archive.
  "Rich" posts (long text with inline photos, headings, lists, tables) are read from
  `rich_message` and flattened to plain text, with `rich_photo`/`rich_document`
  attachments that only count the embedded files (they are not downloaded). This
  needs Telethon 1.45+ (TL layer 229); with 1.44 such posts arrived as empty text.
* **Health.** `data/health/telegram.json`: `last_run_utc`, `last_success_utc`,
  `trigger`, `error`, `items_count`, Drive usage, and `collect`: channels read,
  channel errors, `skipped_counts` by reason, and `skipped_posts` (up to 50 ids and
  reasons, never content) for anything dropped other than ads and service events.
* **Watchdog.** `telegram-watchdog.yml` dispatches the collector when
  `last_success_utc` is older than 40 minutes.
