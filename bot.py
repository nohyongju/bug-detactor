"""
Slack ↔ Claude Code 연동 봇

[플로우]
1. 품질팀이 채널에 이슈 메시지 작성
2. 개발자가 스레드에 댓글:
   - "analyze: 상세 지시"                                → repo 자동 추천
   - "analyze: repo_name: 상세 지시"                     → 해당 repo 코드 분석
   - "fix: repo_name: 브랜치명: 상세 지시"               → 새 브랜치에서 코드 수정 + commit (멀티 repo 지원)
   - "pr: repo_name"                                     → fix에서 만든 브랜치로 PR 생성
"""

import logging
import os
import re
import platform
import subprocess
import threading
from datetime import datetime
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


# ── 사용자 명령 로거 ─────────────────────────────────────
_user_log = logging.getLogger("user_commands")
_user_log.setLevel(logging.INFO)
_user_log_handler = logging.FileHandler("user_commands.log", encoding="utf-8")
_user_log_handler.setFormatter(logging.Formatter("%(message)s"))
_user_log.addHandler(_user_log_handler)

# 사용자 ID → 이름 캐시
_user_name_cache: dict[str, str] = {}


def _get_user_name(client, user_id: str) -> str:
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    try:
        info = client.users_info(user=user_id)
        name = info["user"]["real_name"] or info["user"]["name"]
        _user_name_cache[user_id] = name
        return name
    except Exception:
        return user_id


def _log_command(client, user_id: str, command: str, detail: str):
    name = _get_user_name(client, user_id)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _user_log.info(f"[{ts}] {name} ({user_id}) | {command} | {detail}")


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


PROTECTED_BRANCHES = {"main", "master", "develop"}


def get_current_branch(repo_path: str | Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_path),
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


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
    user_id = event.get("user", "unknown")

    if not channel or not msg_ts:
        return

    _log_command(client, user_id, "emoji", f":{TRIGGER_EMOJI}: 반응")
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
    user_id   = event.get("user", "unknown")

    if not thread_ts:
        return

    # ── analyze: repo: 지시 또는 analyze: 지시 (repo 생략) ──
    analyze_with_repo = re.match(r"analyze\s*:\s*([^:]+)\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    analyze_no_repo   = re.match(r"analyze\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

    if analyze_with_repo:
        repo_str    = analyze_with_repo.group(1).strip()
        instruction = analyze_with_repo.group(2).strip()
        _log_command(client, user_id, "analyze", f"repo={repo_str} | {instruction}")
        threading.Thread(
            target=_do_analyze,
            args=(client, channel, thread_ts, repo_str, instruction, logger),
            daemon=True,
        ).start()
        return

    if analyze_no_repo:
        instruction = analyze_no_repo.group(1).strip()
        _log_command(client, user_id, "recommend", instruction)
        threading.Thread(
            target=_recommend_repos,
            args=(client, channel, thread_ts, instruction, logger),
            daemon=True,
        ).start()
        return

    # ── fix: repo(s): 브랜치명: 지시 ──
    fix_match = re.match(r"fix\s*:\s*([^:]+)\s*:\s*([^:]+)\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if fix_match:
        repo_str    = fix_match.group(1).strip()
        branch_name = fix_match.group(2).strip()
        instruction = fix_match.group(3).strip()
        _log_command(client, user_id, "fix", f"repo={repo_str} | branch={branch_name} | {instruction}")
        threading.Thread(
            target=_do_fix_multi,
            args=(client, channel, thread_ts, repo_str, branch_name, instruction, logger),
            daemon=True,
        ).start()
        return

    # ── pr: repo(s) ──
    # "pr: repo_name" 또는 "pr: repo1, repo2"
    pr_match = re.match(r"pr\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if pr_match:
        repo_str = pr_match.group(1).strip()
        _log_command(client, user_id, "pr", f"repo={repo_str}")
        threading.Thread(
            target=_create_pr,
            args=(client, channel, thread_ts, repo_str, logger),
            daemon=True,
        ).start()
        return

    # ── talk: 일반 대화 (자유 프롬프팅) ──
    talk_match = re.match(r"talk\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if talk_match:
        user_prompt = talk_match.group(1).strip()
        _log_command(client, user_id, "talk", user_prompt[:100])
        threading.Thread(
            target=_do_talk,
            args=(client, channel, thread_ts, user_prompt, logger),
            daemon=True,
        ).start()


# ══════════════════════════════════════════════════════════
# talk: 일반 대화 (자유 프롬프팅)
# ══════════════════════════════════════════════════════════

def _do_talk(client, channel, thread_ts, user_prompt, logger):
    thread_context = get_thread_context(client, channel, thread_ts)

    prompt = f"""아래 Slack 스레드의 대화 맥락과 사용자 질문을 참고하여 답변해주세요.

[스레드 대화 내용]
{thread_context}

[사용자 질문]
{user_prompt}

자연스럽고 도움이 되는 답변을 한국어로 작성해주세요."""

    post_thread(client, channel, thread_ts, "💬 답변 생성 중...")

    try:
        result = run_claude(prompt, timeout=120, max_turns=1)
        if result.returncode == 0 and result.stdout.strip():
            post_thread(client, channel, thread_ts, result.stdout.strip())
        else:
            post_thread(client, channel, thread_ts,
                        f"❌ 답변 생성 실패:\n```{result.stderr[:500]}```")
    except subprocess.TimeoutExpired:
        post_thread(client, channel, thread_ts, "⏰ 답변 생성 시간 초과 (2분).")
    except Exception as e:
        logger.error(f"talk 오류: {e}")
        post_thread(client, channel, thread_ts, f"❌ 오류 발생: {e}")


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
아래 형식의 명령어 예시만 안내해주세요 (다른 형식은 절대 사용하지 마세요):
- `analyze: 레포명: 지시내용` — 코드 분석
- `fix: 레포명: 브랜치명: 지시내용` — 새 브랜치에서 코드 수정"""

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
            result = run_claude(prompt, cwd=str(repo_path), timeout=600, max_turns=50)
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
                f"수정을 시작하려면 `fix: {repo_list}: 브랜치명: <지시내용>` 을 입력해주세요.")


# ══════════════════════════════════════════════════════════
# fix: 코드 수정 + diff 전송 (멀티 repo)
# ══════════════════════════════════════════════════════════

def _do_fix_multi(client, channel, thread_ts, repo_str, branch_name, instruction, logger):
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

        try:
            # 1) develop 최신화
            subprocess.run(
                ["git", "fetch", "origin", "develop"],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
            )

            # 2) develop 기반으로 새 브랜치 생성
            checkout_result = subprocess.run(
                ["git", "checkout", "-b", branch_name, "origin/develop"],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
            )
            if checkout_result.returncode != 0:
                # 이미 존재하면 체크아웃만
                checkout_result = subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=str(repo_path),
                    capture_output=True, text=True, encoding="utf-8",
                )
                if checkout_result.returncode != 0:
                    post_thread(client, channel, thread_ts,
                                f"❌ `{repo_name}` 브랜치 `{branch_name}` 체크아웃 실패:\n```{checkout_result.stderr[:500]}```")
                    continue

            # 브랜치 안전 검증
            actual_branch = get_current_branch(repo_path)
            if actual_branch in PROTECTED_BRANCHES:
                post_thread(client, channel, thread_ts,
                            f"❌ `{repo_name}` 현재 브랜치가 `{actual_branch}`입니다. "
                            f"보호 브랜치에서는 작업할 수 없어요.")
                continue

            post_thread(client, channel, thread_ts,
                        f"⚙️ `{repo_name}` 코드 수정 중... (브랜치: `{branch_name}`)")

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

            result = run_claude(prompt, cwd=str(repo_path), timeout=300)
            logger.info(f"[fix] {repo_name} returncode={result.returncode}")

            if result.returncode != 0:
                post_thread(client, channel, thread_ts, f"❌ `{repo_name}` 수정 실패:\n```{result.stderr[:500]}```")
                continue

            # 수정된 파일 목록 기록
            changed_files_result = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
            )
            untracked_files_result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
            )
            changed = [f for f in changed_files_result.stdout.strip().split("\n") if f]
            untracked = [f for f in untracked_files_result.stdout.strip().split("\n") if f]
            all_changed = changed + untracked
            job["changed_files"] = all_changed
            job["branch_name"] = branch_name

            if not all_changed:
                post_thread(client, channel, thread_ts,
                            f"ℹ️ `{repo_name}` 변경된 파일이 없어요.")
                continue

            # diff 전송
            _post_diff(client, channel, thread_ts, repo_path, repo_name, logger)

            # commit
            subprocess.run(["git", "add", "--"] + all_changed,
                           cwd=str(repo_path), capture_output=True, text=True, encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-m", f"fix: Claude Code 자동 수정 ({repo_name})"],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
            )

        except subprocess.TimeoutExpired:
            post_thread(client, channel, thread_ts, f"⏰ `{repo_name}` 작업 시간 초과 (5분).")
        except Exception as e:
            logger.error(f"fix 오류: {e}")
            post_thread(client, channel, thread_ts, f"❌ `{repo_name}` 오류 발생: {e}")

    repo_list = ", ".join(repo_names)
    post_thread(client, channel, thread_ts,
                f"PR을 올리려면 `pr: {repo_list}` 을 입력해주세요 🚀")


# ══════════════════════════════════════════════════════════
# diff 전송
# ══════════════════════════════════════════════════════════

def _post_diff(client, channel, thread_ts, repo_path, repo_name, logger):
    try:
        stat_result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(repo_path),
            capture_output=True, text=True, encoding="utf-8",
        )

        if not stat_result.stdout.strip():
            post_thread(client, channel, thread_ts, f"ℹ️ `{repo_name}` 변경된 파일이 없어요.")
            return

        post_thread(client, channel, thread_ts,
                    f"✅ `{repo_name}` 수정 완료!\n```\n{stat_result.stdout}\n```")

        files_result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(repo_path),
            capture_output=True, text=True, encoding="utf-8",
        )

        for file_path in files_result.stdout.strip().split("\n"):
            if not file_path:
                continue
            file_diff = subprocess.run(
                ["git", "diff", "--", file_path],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
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

def _generate_pr_description(client, channel, thread_ts, repo_name, changed_files, logger) -> dict:
    """스레드 컨텍스트를 기반으로 Claude가 PR title과 description을 생성"""
    thread_context = get_thread_context(client, channel, thread_ts)
    files_str = "\n".join(changed_files) if changed_files else "(없음)"

    prompt = f"""아래 Slack 스레드 대화 내용을 바탕으로 GitHub PR의 title과 body를 작성해주세요.

[스레드 대화 내용]
{thread_context}

[대상 레포지토리]
{repo_name}

[변경된 파일 목록]
{files_str}

다음 형식으로 **정확히** 작성해주세요 (구분자 `---` 를 반드시 포함):

TITLE:
(한 줄 PR 제목, 예: fix: OOO 버그 수정)
---
BODY:
## 🐛 버그 내용
(어떤 버그/이슈가 있었는지 요약)

## 🔧 수정 사항
(무엇을 어떻게 수정했는지, 변경된 파일과 핵심 변경 포인트)

## 📝 리뷰 포인트
(리뷰어가 특히 확인해야 할 사항, 사이드이펙트 가능성 등)

주의: 마크다운 형식을 유지하고, 스레드 대화에서 파악한 실제 내용을 기반으로 구체적으로 작성해주세요."""

    try:
        result = run_claude(prompt, timeout=60, max_turns=1)
        if result.returncode == 0 and result.stdout.strip():
            output = result.stdout.strip()
            if "---" in output:
                parts = output.split("---", 1)
                title_part = parts[0].strip()
                body_part = parts[1].strip()
                # TITLE: 접두사 제거
                title = title_part.replace("TITLE:", "").strip().split("\n")[0].strip()
                # BODY: 접두사 제거
                body = body_part.replace("BODY:", "", 1).strip()
                return {"title": title, "body": body}
        logger.warning(f"PR description 생성 실패, 기본값 사용: {result.stderr[:200]}")
    except Exception as e:
        logger.warning(f"PR description 생성 오류, 기본값 사용: {e}")

    # fallback: Claude 생성 실패 시 기본값 반환
    return {
        "title": f"fix: Claude Code 자동 수정 ({repo_name})",
        "body": "Claude Code에 의해 자동 생성된 PR입니다."
    }


def _create_pr(client, channel, thread_ts, repo_str, logger):
    all_jobs = active_jobs.get(thread_ts)
    if not all_jobs:
        post_thread(client, channel, thread_ts,
                    "❌ 연결된 작업을 찾을 수 없어요. `fix: repo_name: 브랜치명: 지시내용` 먼저 실행해주세요.")
        return

    # 요청된 repo만 필터링
    requested_repos = parse_repo_list(repo_str)
    jobs = [j for j in all_jobs if j["repo"] in requested_repos]

    if not jobs:
        available = ", ".join(j["repo"] for j in all_jobs)
        post_thread(client, channel, thread_ts,
                    f"❌ `{repo_str}`에 해당하는 작업을 찾을 수 없어요.\n현재 작업된 레포: `{available}`")
        return

    all_success = True
    for job in jobs:
        repo_path = Path(job["repo_path"])
        repo_name = job["repo"]
        branch_name = job.get("branch_name")

        if not branch_name:
            post_thread(client, channel, thread_ts,
                        f"❌ `{repo_name}` 브랜치 정보가 없어요. `fix:` 를 다시 실행해주세요.")
            all_success = False
            continue

        post_thread(client, channel, thread_ts,
                    f"🚀 `{repo_name}` PR 생성 중... (브랜치: `{branch_name}` → `develop`)")

        try:
            # 보호 브랜치 안전 검증
            actual_branch = get_current_branch(repo_path)
            if actual_branch in PROTECTED_BRANCHES:
                post_thread(client, channel, thread_ts,
                            f"❌ `{repo_name}` 현재 브랜치가 `{actual_branch}`입니다. "
                            f"보호 브랜치에 직접 push할 수 없어요.")
                all_success = False
                continue

            push_result = subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
            )
            if push_result.returncode != 0:
                post_thread(client, channel, thread_ts,
                            f"❌ `{repo_name}` push 실패:\n```{push_result.stderr[:500]}```")
                all_success = False
                continue

            # PR description 생성
            changed_files = job.get("changed_files", [])
            pr_info = _generate_pr_description(client, channel, thread_ts, repo_name, changed_files, logger)

            pr_result = subprocess.run(
                ["gh", "pr", "create", "--base", "develop",
                 "--title", pr_info["title"], "--body", pr_info["body"]],
                cwd=str(repo_path),
                capture_output=True, text=True, encoding="utf-8",
            )

            if pr_result.returncode == 0:
                post_thread(client, channel, thread_ts,
                            f"✅ `{repo_name}` PR 생성 완료!\n{pr_result.stdout.strip()}")
            else:
                post_thread(client, channel, thread_ts,
                            f"❌ `{repo_name}` PR 생성 실패:\n```{pr_result.stderr[:500]}```\n`pr: {repo_name}` 으로 재시도할 수 있어요.")
                all_success = False

        except Exception as e:
            logger.error(f"PR 생성 오류: {e}")
            post_thread(client, channel, thread_ts,
                        f"❌ `{repo_name}` PR 생성 오류: {e}\n`pr: {repo_name}` 으로 재시도할 수 있어요.")
            all_success = False

    # 모두 성공한 경우에만 작업 정보 제거
    if all_success:
        active_jobs.pop(thread_ts, None)


# ══════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"🤖 봇 시작 | repos: {REPOS_BASE_DIR}")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
