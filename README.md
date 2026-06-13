# YouTube 투자 자동 보고

GitHub Actions가 매일 YouTube 채널 8개의 최근 24시간 콘텐츠를 확인하고, OpenAI API로 한국어 보고서를 만든 뒤 텔레그램으로 보냅니다.

현재 산출물은 사람이 폴더를 직접 탐색하기보다 LLM과 자동화가 다시 파싱하기 쉬운 구조로 저장됩니다.

## 포함 채널

주식:

- 힐링여행자
- asset.x2
- 잼투리
- 한경 글로벌마켓

부동산:

- 부읽남TV
- 작가 송희구
- 새벽보기Live
- 집코노미

부동산 채널은 제목에 게스트 표기가 있는 영상은 제외합니다. `asset.x2`는 영상 RSS와 커뮤니티 게시물을 함께 확인합니다.

## GitHub 설정

1. 이 폴더를 GitHub 저장소로 올립니다.
2. GitHub 저장소의 `Settings → Secrets and variables → Actions → Secrets`에 아래 값을 추가합니다.
   - `OPENAI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: Google Drive 업로드용 서비스 계정 JSON 전체 내용
3. 필요하면 `Settings → Secrets and variables → Actions → Variables`에 `OPENAI_MODEL`을 추가합니다.
   - 기본값은 `gpt-5.4-mini`입니다.
4. Google Drive 자동 저장을 쓰려면 `Settings → Secrets and variables → Actions → Variables`에 아래 값을 추가합니다.
   - `GOOGLE_DRIVE_FOLDER_ID`: 저장할 Drive 폴더 ID
   - 현재 `#1 Codex` 폴더 ID는 `1BARJn6eLdb_OL558mEDT3ztYWErYDBbR`입니다.
5. Google Cloud에서 만든 서비스 계정 이메일을 Google Drive의 `#1 Codex` 폴더에 편집자 권한으로 공유합니다.
6. `Actions → YouTube Investment Report → Run workflow`로 수동 실행해 테스트합니다.

## 저장 구조

실행이 끝나면 `out/` 아래에 아래 구조가 생성되고, GitHub Actions artifact `investment-insights-data`로 30일간 보관됩니다.

```text
out
├── manifest
│   ├── reports_manifest.jsonl
│   ├── items_manifest.jsonl
│   └── latest.json
├── raw
│   └── YYYY/MM/DD/raw_sources.json
├── normalized
│   └── YYYY/MM/DD/
│       ├── structured_report.json
│       ├── items.jsonl
│       ├── insights.jsonl
│       ├── keywords.jsonl
│       └── terms.jsonl
├── reports
│   ├── html/YYYY/MM/DD/report.html
│   └── telegram/YYYY/MM/DD/telegram.html
├── database
│   ├── investment_insights.sqlite
│   └── schema.sql
└── backups
    └── YYYY/MM/DD/investment_insights.sqlite
```

역할:

- `manifest`: LLM이 어떤 날짜의 어떤 파일을 읽어야 하는지 찾는 색인입니다.
- `raw`: YouTube RSS와 커뮤니티 게시물 원본 수집 데이터입니다.
- `normalized`: LLM 파싱용 핵심 JSON/JSONL 데이터입니다.
- `reports/html`: 사람이 읽는 상세 HTML 리포트입니다.
- `reports/telegram`: 텔레그램으로 보낸 HTML 메시지 원본입니다.
- `database`: SQLite 최신 누적본입니다.
- `backups`: 날짜별 SQLite 백업입니다.

Google Drive 자동 저장을 켜면 위 `out/` 내부 구조가 지정한 Drive 폴더 안에 그대로 만들어집니다. 같은 경로의 파일이 이미 있으면 새 파일을 중복 생성하지 않고 갱신합니다.

예를 들어 `#1 Codex` 폴더에는 아래처럼 저장됩니다.

```text
#1 Codex
├── manifest
│   ├── latest.json
│   ├── drive_uploads.json
│   ├── reports_manifest.jsonl
│   └── items_manifest.jsonl
├── raw/YYYY/MM/DD/raw_sources.json
├── normalized/YYYY/MM/DD/*.jsonl
├── reports/html/YYYY/MM/DD/report.html
├── reports/telegram/YYYY/MM/DD/telegram.html
├── database/investment_insights.sqlite
└── backups/YYYY/MM/DD/investment_insights.sqlite
```

LLM이 나중에 읽을 때는 `manifest/latest.json`을 먼저 읽고, 필요한 파일 경로와 Drive URL을 따라가면 됩니다. 최신 누적 데이터베이스는 항상 `database/investment_insights.sqlite`입니다.

## 텔레그램 봇 만들기

1. 텔레그램에서 `@BotFather`에게 `/newbot`을 보내 봇을 만듭니다.
2. 받은 토큰을 `TELEGRAM_BOT_TOKEN`에 저장합니다.
3. 만든 봇에게 아무 메시지나 보냅니다.
4. 브라우저에서 아래 주소를 열어 `chat.id`를 확인합니다.

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

5. `chat.id` 값을 `TELEGRAM_CHAT_ID`에 저장합니다.

## 로컬 테스트

API를 호출하지 않고 수집 결과만 확인:

```bash
DRY_RUN=1 python youtube_report.py
```

실제 보고서 생성:

```bash
export OPENAI_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python youtube_report.py
```

텔레그램은 Bot API의 `parse_mode=HTML`을 사용합니다. 텔레그램 HTML은 `<b>`, `<i>`, `<u>`, `<a>` 같은 제한된 태그만 지원하므로, 상세 리포트용 HTML과 텔레그램용 HTML은 별도 파일로 생성됩니다.

## 일정 변경

현재 `.github/workflows/youtube-report.yml`은 매일 08:50 KST에 실행됩니다.
GitHub Actions의 cron은 UTC 기준이므로, KST에서 9시간을 빼서 적어야 합니다.
