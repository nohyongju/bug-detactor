# Slack ↔ Claude Code 봇

품질팀 이슈 → 이모지 반응 → Claude Code 자동 분석/수정 → PR 생성

## 흐름

```
품질팀 슬랙 채널에 이슈 작성
  → 개발자가 👀 이모지 반응
  → 봇이 Claude API로 분석 초안 스레드에 게시
  → 개발자가 스레드에 "repo: cstalk-api" 댓글
  → 봇이 로컬 Claude Code 실행 + diff 스레드에 전송
  → 개발자가 "PR 요청해줘"
  → 봇이 gh pr create 실행
```

---

## 1. Slack App 생성

1. https://api.slack.com/apps → Create New App → From scratch
2. **Socket Mode** 활성화 (App-Level Token 생성, scope: `connections:write`)
3. **Event Subscriptions** 활성화 후 Subscribe:
   - `reaction_added`
   - `message.channels`
4. **OAuth & Permissions** → Bot Token Scopes 추가:
   - `channels:history`
   - `chat:write`
   - `reactions:read`
5. 워크스페이스에 앱 설치 → Bot Token 복사

---

## 2. 로컬 설정

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일 편집
```

### .env 설정

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ANTHROPIC_API_KEY=sk-ant-...
TRIGGER_EMOJI=eyes          # 트리거 이모지 (기본: 👀)
REPOS_BASE_DIR=/Users/yourname/projects  # 로컬 레포 루트
```

### Windows 경로 예시

```env
REPOS_BASE_DIR=C:/Users/yourname/projects
```

---

## 3. 사전 요구사항

- Python 3.11+
- Claude Code 설치 (`claude` 명령어 사용 가능)
- GitHub CLI 설치 (`gh auth login` 완료)
- 레포들이 `REPOS_BASE_DIR` 아래에 클론되어 있을 것

---

## 4. 실행

```bash
# 환경변수 로드 후 실행
source .env  # Mac/Linux
# Windows: set 명령어로 각각 설정

python bot.py
```

---

## 5. 사용법

| 액션 | 방법 |
|------|------|
| 이슈 분석 요청 | 품질팀 메시지에 👀 이모지 반응 |
| 레포 지정 + 작업 시작 | 스레드에 `repo: cstalk-api` |
| diff 확인 | 봇이 자동으로 스레드에 파일별 게시 |
| PR 생성 | 스레드에 `PR 요청해줘` |

---

## 6. 여러 명이 동시에 이모지 달면?

각자 독립적으로 분석 초안이 달려요. 의도된 동작이에요.  
`repo:` 지정은 각자 본인 스레드 흐름에서 독립적으로 처리돼요.

---

## 트러블슈팅

**`claude` 명령어를 못 찾는 경우**  
→ Claude Code PATH 확인: `which claude` (Mac) / `where claude` (Windows)

**`gh pr create` 실패**  
→ `gh auth login` 완료 여부 확인

**레포를 못 찾는 경우**  
→ `REPOS_BASE_DIR` 경로와 실제 폴더명 확인



python.exe -m pip install -r requirements.txt
pip install python-dotenv
