# YouTube 투자 자동 보고

GitHub Actions가 매일 YouTube 채널 8개의 최근 24시간 콘텐츠를 확인하고, OpenAI API로 한국어 보고서를 만든 뒤 텔레그램으로 보냅니다.

## 포함 채널

주식:

- 힐링여행자
- asset.x2
- 잼투리
- 한경 글로벌마켓

부동산:

- 부읽남TV
- 작가 송희구
- 월급쟁이부자들TV
- 집코노미

부동산 채널은 제목에 게스트 표기가 있는 영상은 제외합니다. `asset.x2`는 영상 RSS와 커뮤니티 게시물을 함께 확인합니다.

## GitHub 설정

1. 이 폴더를 GitHub 저장소로 올립니다.
2. GitHub 저장소의 `Settings → Secrets and variables → Actions → Secrets`에 아래 값을 추가합니다.
   - `OPENAI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. 필요하면 `Settings → Secrets and variables → Actions → Variables`에 `OPENAI_MODEL`을 추가합니다.
   - 기본값은 `gpt-5.4-mini`입니다.
4. `Actions → YouTube Investment Report → Run workflow`로 수동 실행해 테스트합니다.

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

## 일정 변경

현재 `.github/workflows/youtube-report.yml`은 매일 08:50 KST에 실행됩니다.
GitHub Actions의 cron은 UTC 기준이므로, KST에서 9시간을 빼서 적어야 합니다.
