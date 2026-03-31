"""
Slack ↔ Claude Code 연동 봇

[플로우]
1. 품질팀이 채널에 이슈 메시지 작성
2. 개발자가 스레드에 댓글:
   - "analyze: 상세 지시"                        → repo 자동 추천
   - "analyze: repo_name: 상세 지시"             → 해당 repo 코드 분석
   - "fix: repo1, repo2: 상세 지시"              → 코드 수정 후 diff 전송 (멀티 repo 지원)
   - "PR 요청해줘"                                → PR 생성
"""

import os
import re
import platform
import subprocess
import threading
from pathlib import Path

import yaml
from dotenv import load_dotenv
load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ── 환경변수 ──────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
REPOS_BASE_DIR  = os.environ.get("REPOS_BASE_DIR", str(Path.home() / "projects"))

# repos_info.yaml 경로 (bot.py 와 같은 디렉토리)
REPOS_INFO_PATH = Path(__file__).parent / "repository_information.yaml"

app = App(token=SLACK_BOT_TOKEN)

IS_WINDOWS = platform.system() == "Windows"
TRIGGER_EMOJI = os.environ.get("TRIGGER_EMOJI", "eyes")

# ── 진행 중 작업 추적 (thread_ts → [{repo, repo_path}, ...]) ─
active_jobs: dict[str, list[dict]] = {}


# ══════════════════════════════════════════════════════════
# 유틸
# ══════════════════════════════════════════════════════════

def get_claude_cmd() -> str:
    return "claude.cmd" if IS_WINDOWS else "claude"


def load_repos_info() -> dict:
    if REPOS_INFO_PATH.exists():
        with open(REPOS_INFO_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f).get("repos", {})
    return {}


def build_repos_context() -> str:
    """repos_info.yaml을 Claude에 넘길 텍스트로 변환"""
    repos = load_repos_info()
    lines = ["[서비스 목록]"]
    for name, info in repos.items():
        desc = info.get("description", "")
        domains = info.get("domains", [])
        modules = info.get("modules", {})
        line = f"- {name}: {desc}"
        if domains:
            line += f"\n  도메인: {', '.join(domains)}"
        if modules:
            module_str = ", ".join(f"{k}({v})" for k, v in modules.items())
            line += f"\n  컴포넌트: {module_str}"
        lines.append(line)
    return "\n".join(lines)


def find_repo_path(repo_name: str) -> Path | None:
    base = Path(REPOS_BASE_DIR)
    candidate = base / repo_name
    if candidate.exists():
        return candidate
    for p in base.iterdir():
        if p.is_dir() and p.name.lower() == repo_name.lower():
            return p
    return None


def post_thread(client, channel: str, thread_ts: str, text: str, code: str = None):
    if code:
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{code[:2800]}\n```"}},
        ]
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, blocks=blocks)
    else:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def get_message_text(client, channel: str, ts: str) -> str:
    result = client.conversations_history(channel=channel, latest=ts, limit=1, inclusive=True)
    messages = result.get("messages", [])
    return messages[0].get("text", "") if messages else ""


def get_thread_root_text(client, channel: str, thread_ts: str) -> str:
    return get_message_text(client, channel, thread_ts)


def get_thread_context(client, channel: str, thread_ts: str) -> str:
    """스레드 전체 대화 내용 (루트 이슈 + 분석 결과 + 댓글 모두)"""
    result = client.conversations_replies(channel=channel, ts=thread_ts)
    messages = result.get("messages", [])
    lines = []
    for m in messages:
        text = m.get("text", "").strip()
        if text:
            # 봇 메시지와 사람 메시지 구분
            prefix = "[봇]" if m.get("bot_id") else "[사람]"
            lines.append(f"{prefix} {text}")
    return "\n\n".join(lines)


def run_claude(prompt: str, cwd: str = None, timeout: int = 300, max_turns: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        [get_claude_cmd(), "--print", "--dangerously-skip-permissions", "--max-turns", str(max_turns)],
        input=prompt,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def parse_repo_list(repo_str: str) -> list[str]:
    """'repo1, repo2' 또는 'repo1' → ['repo1', 'repo2']"""
    return [r.strip() for r in repo_str.split(",") if r.strip()]


# ══════════════════════════════════════════════════════════
# 이모지 반응 → repo 추천
# ══════════════════════════════════════════════════════════

@app.event("reaction_added")
def handle_reaction(event, client, logger):
    if event.get("reaction") != TRIGGER_EMOJI:
        return

    item    = event.get("item", {})
    channel = item.get("channel")
    msg_ts  = item.get("ts")

    if not channel or not msg_ts:
        return

    threading.Thread(
        target=_recommend_repos,
        args=(client, channel, msg_ts, "어디가 문제일까요?", logger),
        daemon=True,
    ).start()


# ══════════════════════════════════════════════════════════
# 메시지 이벤트 핸들러
# ══════════════════════════════════════════════════════════

@app.event("message")
def handle_message(event, client, logger):
    if event.get("bot_id") or event.get("subtype"):
        return

    text      = event.get("text", "").strip()
    channel   = event.get("channel")
    thread_ts = event.get("thread_ts")

    if not thread_ts:
        return

    # ── analyze: repo: 지시 또는 analyze: 지시 (repo 생략) ──
    analyze_with_repo = re.match(r"analyze\s*:\s*([^:]+)\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    analyze_no_repo   = re.match(r"analyze\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

    if analyze_with_repo:
        repo_str    = analyze_with_repo.group(1).strip()
        instruction = analyze_with_repo.group(2).strip()
        threading.Thread(
            target=_do_analyze,
            args=(client, channel, thread_ts, repo_str, instruction, logger),
            daemon=True,
        ).start()
        return

    if analyze_no_repo:
        instruction = analyze_no_repo.group(1).strip()
        threading.Thread(
            target=_recommend_repos,
            args=(client, channel, thread_ts, instruction, logger),
            daemon=True,
        ).start()
        return

    # ── fix: repo(s): 지시 ──
    fix_match = re.match(r"fix\s*:\s*([^:]+)\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if fix_match:
        repo_str    = fix_match.group(1).strip()
        instruction = fix_match.group(2).strip()
        threading.Thread(
            target=_do_fix_multi,
            args=(client, channel, thread_ts, repo_str, instruction, logger),
            daemon=True,
        ).start()
        return

    # ── PR 요청 ──
    if re.search(r"pr\s*요청|pr\s*올려|pull\s*request", text, re.IGNORECASE):
        threading.Thread(
            target=_create_pr,
            args=(client, channel, thread_ts, logger),
            daemon=True,
        ).start()


# ══════════════════════════════════════════════════════════
# repo 추천 (analyze: 지시내용 — repo 생략)
# ══════════════════════════════════════════════════════════

def _recommend_repos(client, channel, thread_ts, instruction, logger):
    issue_text = get_thread_context(client, channel, thread_ts)
    repos_context = build_repos_context()

    post_thread(client, channel, thread_ts, "🔎 관련 레포 분석 중...")

    prompt = f"""아래 이슈와 개발자 지시를 보고, 관련 있는 레포지토리를 추천해주세요.
코드를 탐색하지 말고 서비스 목록의 설명만 보고 판단해주세요.

{repos_context}

[품질팀 이슈]
{issue_text}

[개발자 지시]
{instruction}

다음 형식으로 답해주세요:
## 추천 레포지토리
- `repo-name`: 이유 한 줄
- `repo-name`: 이유 한 줄

## 다음 단계
analyze 또는 fix 명령어 예시를 알려주세요."""

    try:
        result = run_claude(prompt, timeout=60, max_turns=1)
        logger.info(f"[recommend] returncode={result.returncode}")

        output = (result.stdout or "").strip()
        if not output or result.returncode != 0:
            post_thread(client, channel, thread_ts, f"❌ 추천 실패:\n```{result.stderr[:300]}```")
            return

        post_thread(client, channel, thread_ts, output)

    except subprocess.TimeoutExpired:
        post_thread(client, channel, thread_ts, "⏰ 추천 시간 초과.")
    except Exception as e:
        logger.error(f"추천 오류: {e}")
        post_thread(client, channel, thread_ts, f"❌ 추천 오류: {e}")


# ══════════════════════════════════════════════════════════
# analyze: 코드 탐색 후 분석 리포트
# ══════════════════════════════════════════════════════════

def _do_analyze(client, channel, thread_ts, repo_str, instruction, logger):
    repo_names = parse_repo_list(repo_str)
    issue_text = get_thread_context(client, channel, thread_ts)

    for repo_name in repo_names:
        repo_path = find_repo_path(repo_name)
        if not repo_path:
            post_thread(client, channel, thread_ts,
                        f"❌ `{repo_name}` 레포를 찾을 수 없어요. (`{REPOS_BASE_DIR}` 아래 확인)")
            continue

        post_thread(client, channel, thread_ts, f"🔍 `{repo_name}` 분석 중...")

        prompt = f"""아래 품질팀 이슈와 개발자 지시를 바탕으로 코드를 분석해주세요.
코드를 직접 수정하지 말고 분석 리포트만 작성해주세요.
개발자 지시에 언급된 경로/키워드 관련 파일만 탐색하세요. 전체 repo를 탐색하지 마세요.

[품질팀 이슈]
{issue_text}

[개발자 지시]
{instruction}

다음 형식으로 작성해주세요:
## 🐛 이슈 요약
(한 줄 요약)

## 📍 원인 분석
(코드 레벨 원인, 관련 파일/함수 명시)

## 🔧 수정 필요 범위
(어떤 파일/함수를 수정해야 하는지)

## ✅ 완료 조건
(어떻게 되면 해결된 것인지)"""

        try:
            logger.info(f"[analyze] 시작 repo_path={repo_path}")
            result = run_claude(prompt, cwd=str(repo_path), timeout=300)
            logger.info(f"[analyze] returncode={result.returncode} stdout_len={len(result.stdout)} stderr={repr(result.stderr[:300])}")

            if result.returncode != 0:
                post_thread(client, channel, thread_ts, f"❌ `{repo_name}` 분석 실패:\n```{result.stderr[:500]}```")
                continue

            output = (result.stdout or "").strip()
            if not output:
                post_thread(client, channel, thread_ts, f"❌ `{repo_name}` 분석 실패: Claude 응답이 비어있습니다.\nstderr: {result.stderr[:300]}")
                continue

            post_thread(client, channel, thread_ts, f"*[{repo_name}]*\n{output}")

        except subprocess.TimeoutExpired:
            post_thread(client, channel, thread_ts, f"⏰ `{repo_name}` 분석 시간 초과.")
        except Exception as e:
            logger.error(f"분석 오류: {e}")
            post_thread(client, channel, thread_ts, f"❌ `{repo_name}` 분석 오류: {e}")

    repo_list = ", ".join(repo_names)
    post_thread(client, channel, thread_ts,
                f"수정을 시작하려면 `fix: {repo_list}: <지시내용>` 을 입력해주세요.")


# ══════════════════════════════════════════════════════════
# fix: 코드 수정 + diff 전송 (멀티 repo)
# ══════════════════════════════════════════════════════════

def _do_fix_multi(client, channel, thread_ts, repo_str, instruction, logger):
    repo_names = parse_repo_list(repo_str)
    issue_text = get_thread_context(client, channel, thread_ts)

    jobs = []
    for repo_name in repo_names:
        repo_path = find_repo_path(repo_name)
        if not repo_path:
            post_thread(client, channel, thread_ts,
                        f"❌ `{repo_name}` 레포를 찾을 수 없어요. (`{REPOS_BASE_DIR}` 아래 확인)")
            continue
        jobs.append({"repo": repo_name, "repo_path": str(repo_path)})

    if not jobs:
        return

    active_jobs[thread_ts] = jobs

    for job in jobs:
        repo_name = job["repo"]
        repo_path = Path(job["repo_path"])

        post_thread(client, channel, thread_ts, f"⚙️ `{repo_name}` 코드 수정 중...")

        prompt = f"""아래 품질팀 이슈와 개발자 지시를 바탕으로 코드를 수정해주세요.
개발자 지시에 언급된 경로/키워드 관련 파일만 탐색하세요. 전체 repo를 탐색하지 마세요.

[품질팀 이슈]
{issue_text}

[개발자 지시]
{instruction}

주의사항:
- 기존 코드 스타일을 유지해주세요
- 변경 범위를 최소화해주세요
- 테스트가 있다면 함께 수정해주세요"""

        try:
            result = run_claude(prompt, cwd=str(repo_path), timeout=300)
            logger.info(f"[fix] {repo_name} returncode={result.returncode}")

            if result.returncode != 0:
                post_thread(client, channel, thread_ts, f"❌ `{repo_name}` 수정 실패:\n```{result.stderr[:500]}```")
                continue

            _post_diff(client, channel, thread_ts, repo_path, repo_name, logger)

        except subprocess.TimeoutExpired:
            post_thread(client, channel, thread_ts, f"⏰ `{repo_name}` 작업 시간 초과 (5분).")
        except Exception as e:
            logger.error(f"fix 오류: {e}")
            post_thread(client, channel, thread_ts, f"❌ `{repo_name}` 오류 발생: {e}")

    post_thread(client, channel, thread_ts, "PR을 올리려면 `PR 요청해줘` 라고 입력해주세요 🚀")


# ══════════════════════════════════════════════════════════
# diff 전송
# ══════════════════════════════════════════════════════════

def _post_diff(client, channel, thread_ts, repo_path, repo_name, logger):
    try:
        stat_result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(repo_path),
            capture_output=True, text=True,
        )

        if not stat_result.stdout.strip():
            post_thread(client, channel, thread_ts, f"ℹ️ `{repo_name}` 변경된 파일이 없어요.")
            return

        post_thread(client, channel, thread_ts,
                    f"✅ `{repo_name}` 수정 완료!\n```\n{stat_result.stdout}\n```")

        files_result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(repo_path),
            capture_output=True, text=True,
        )

        for file_path in files_result.stdout.strip().split("\n"):
            if not file_path:
                continue
            file_diff = subprocess.run(
                ["git", "diff", "--", file_path],
                cwd=str(repo_path),
                capture_output=True, text=True,
            )
            diff_text = file_diff.stdout
            if not diff_text:
                continue
            post_thread(
                client, channel, thread_ts,
                f"📄 `{repo_name} / {file_path}`",
                code=diff_text[:2800] + ("\n... (truncated)" if len(diff_text) > 2800 else ""),
            )

    except Exception as e:
        logger.error(f"diff 전송 오류: {e}")
        post_thread(client, channel, thread_ts, f"❌ `{repo_name}` diff 전송 실패: {e}")


# ══════════════════════════════════════════════════════════
# PR 생성 (멀티 repo)
# ══════════════════════════════════════════════════════════

def _create_pr(client, channel, thread_ts, logger):
    jobs = active_jobs.get(thread_ts)
    if not jobs:
        post_thread(client, channel, thread_ts,
                    "❌ 연결된 작업을 찾을 수 없어요. `fix: repo_name: 지시내용` 먼저 실행해주세요.")
        return

    for job in jobs:
        repo_path = Path(job["repo_path"])
        repo_name = job["repo"]

        post_thread(client, channel, thread_ts, f"🚀 `{repo_name}` PR 생성 중...")

        try:
            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=str(repo_path),
                capture_output=True, text=True,
            )
            branch = branch_result.stdout.strip()

            subprocess.run(["git", "add", "-A"], cwd=str(repo_path))
            subprocess.run(
                ["git", "commit", "-m", f"fix: Claude Code 자동 수정 ({repo_name})"],
                cwd=str(repo_path),
                capture_output=True,
            )

            push_result = subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=str(repo_path),
                capture_output=True, text=True,
            )
            if push_result.returncode != 0:
                post_thread(client, channel, thread_ts,
                            f"❌ `{repo_name}` push 실패:\n```{push_result.stderr[:500]}```")
                continue

            pr_result = subprocess.run(
                ["gh", "pr", "create", "--fill"],
                cwd=str(repo_path),
                capture_output=True, text=True,
            )

            if pr_result.returncode == 0:
                post_thread(client, channel, thread_ts,
                            f"✅ `{repo_name}` PR 생성 완료!\n{pr_result.stdout.strip()}")
            else:
                post_thread(client, channel, thread_ts,
                            f"❌ `{repo_name}` PR 생성 실패:\n```{pr_result.stderr[:500]}```")

        except Exception as e:
            logger.error(f"PR 생성 오류: {e}")
            post_thread(client, channel, thread_ts, f"❌ `{repo_name}` PR 생성 오류: {e}")

    active_jobs.pop(thread_ts, None)


# ══════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"🤖 봇 시작 | repos: {REPOS_BASE_DIR}")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
