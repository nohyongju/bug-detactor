---
marp: true
theme: default
paginate: true
size: 16:9
header: 'Bug Detactor — Jira × Claude Code'
footer: '© 2026 yjnoh'
style: |
  section { font-size: 26px; }
  h1 { color: #2563eb; }
  h2 { color: #1e40af; border-bottom: 2px solid #93c5fd; padding-bottom: 6px; }
  code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
  pre { background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 14px; }
  table { font-size: 22px; }
---

<!-- _class: lead -->

# 🐛 Bug Detactor

### Jira 이슈를 코드 수정 PR까지 자동화하는 봇

Jira 댓글 한 줄 → 분석 리포트 → 코드 수정 → PR 생성

발표자: **yjnoh** · 2026

---

## 📌 왜 만들었나?

Jira에 버그 이슈가 등록되면 개발자는 매번 같은 작업을 반복합니다.

1. 이슈 본문을 읽고 **어떤 레포 / 어떤 모듈** 문제인지 수동 추적
2. 관련 코드 **검색하고 읽기**
3. 원인 분석 후 **로컬에서 수정**
4. 브랜치 만들고 **커밋 → push → PR 생성 → Jira에 PR 링크 코멘트**

> 한 이슈당 평균 30분~1시간이 "코드 작성 외 작업"에 소비됨

**👉 이 반복 과정을 Jira 댓글 한 줄로 끝내자**

---

## 💡 핵심 아이디어

> **Jira 이슈 그 자리에서**, **댓글 한 줄**로,
> Claude Code가 **로컬 레포에서** 분석·수정·PR을 처리한다.

- 이슈 **summary + description + 모든 댓글**이 자동으로 프롬프트에 주입
- 개발자는 **검토와 의사결정**에만 집중

---

## 🏗️ 시스템 아키텍처

```
┌─────────────────┐    REST API polling (60s)    ┌─────────────────────┐
│  Jira Cloud     │ ◀────────────────────────────│                     │
│  (이슈 + 댓글)   │ ────────────────────────────▶│      bot.py         │
└─────────────────┘    issue/comment fetch       │  (Jira poller +     │
        ▲                                        │   command parser)   │
        │ 봇 댓글 (분석/diff/PR링크)             └──────┬──────────────┘
        └───────────────────────────────────────────────│ subprocess
                                                        ▼
                                            ┌──────────────────────┐
                                            │  claude --print      │
                                            │  (로컬 레포 cwd)       │
                                            └──────┬───────────────┘
                                                   │ git / gh
                                                   ▼
                                            ┌──────────────────────┐
                                            │ Local Repos + GitHub │
                                            └──────────────────────┘
```

---

## 🔄 워크플로우 4단계

| Step | 액션 | 트리거 (Jira 댓글) |
|------|------|---------------------|
| 1 | 이슈 등록 | 품질팀/PM이 Jira에 이슈 작성 |
| 2 | 레포 추천 | `analyze: 지시` |
| 3 | 코드 분석 | `analyze: repo: 지시` |
| 4 | 코드 수정 | `fix: repo: 지시` (브랜치 자동: `fix/{이슈키}`) |
| 5 | PR 생성 | `pr: repo` (→ `develop` 머지 타겟) |

> 모든 명령어는 **이슈 댓글로** 입력 → 이슈별 컨텍스트 자동 격리

---

## 🛠️ 명령어 요약 (Jira 댓글)

| 명령어 | 설명 |
|--------|------|
| `analyze: 지시` | 관련 레포 추천 |
| `analyze: repo: 지시` | 해당 레포 코드 분석 리포트 |
| `analyze: repo1, repo2: 지시` | **여러 레포 동시 분석** |
| `fix: repo: 지시` | 코드 수정 + diff + commit (브랜치: `fix/{이슈키}`) |
| `fix: repo1, repo2: 지시` | **여러 레포 동시 수정** |
| `pr: repo` | fix에서 만든 브랜치로 PR 생성 |
| `talk: 질문` | 이슈 컨텍스트 기반 자유 대화 |


---

## 🎬 데모 (Jira)

```
https://enomix.atlassian.net/browse/DWFLOW-3187
```

---


## 📦 기술 스택

- **Python 3.11+**
- `requests` + **Jira REST API v3** (메인 트리거)
- `slack-bolt` (Socket Mode) — 보조 채널
- `pyyaml` — `repository_information.yaml` 로 서비스 메타데이터 관리
- **Claude Code CLI** — `claude --print --dangerously-skip-permissions`
- **GitHub CLI** — `gh pr create`
- `subprocess` + `threading` — 비동기 명령 처리

```
slack-bolt>=1.18.0
python-dotenv>=1.0.0
pyyaml>=6.0
requests>=2.31.0
```

---

## 📄 repository_information.yaml

봇이 "어느 레포가 무슨 일을 하는지" 알게 해주는 **단일 진실 원천(Single Source of Truth)**

```yaml
dworks-cstalk:
  description: "CS Talk 메인 서비스 (상담 톡, 배정, AI 에이전트 등)"
  type: "backend"
  domains: [AI 에이전트, 답변 대기열, 배분, 게스트, 화상톡, ...]
  modules:
    aiagent: "AI 에이전트"
    assign: "배분"
    guest: "게스트 관리"
    ...
```

> 새 레포 추가 = YAML에 한 블록 추가. 코드 변경 불필요.
> 현재 **20개 레포 / backend·frontend·infra·library** 등록됨

---

## 📈 기대 효과

| 항목 | Before | After |
| :--- | :--- | :--- |
| 이슈 → 레포 매칭 | 수동 검색 (5~15분) | `analyze:` 댓글 한 줄 (~10초) |
| 원인 분석 | 코드 검색 + 디버깅 (20~60분) | 분석 리포트 자동 (~1~5분) |
| 수정 + PR | IDE 작업 + 수동 PR (15~30분) | `fix:` + `pr:` 댓글 + 검토 |
| 컨텍스트 스위칭 | Jira → IDE → GitHub → Jira | **Jira 한 곳에서 모두** |

> 개발자는 **"무엇을 어떻게 고칠지"** 의사결정에만 집중

---

## ⚠️ 한계 & 고려사항

- Claude가 항상 정답을 내지 않음 → **사람의 diff 리뷰 필수**
- 로컬 레포 동기화 필요 (`REPOS_BASE_DIR` 아래 클론된 상태)
- `claude` / `gh` CLI 인증 사전 셋업
- **Jira 폴링 방식** (실시간 X, 최대 60초 지연)
- 대규모 리팩토링은 부적합 — **버그 픽스 / 소규모 변경에 최적화**

---

## 🚀 향후 개선 아이디어

- [ ] PR 머지 후 이슈 자동 코멘트 / 상태 전이
- [ ] 테스트 자동 실행 후 통과 시에만 PR
- [ ] 멀티 모델 라우팅 (간단=Haiku, 분석=Sonnet, 복잡=Opus)

---
