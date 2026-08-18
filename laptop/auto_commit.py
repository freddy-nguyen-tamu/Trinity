#!/usr/bin/env python3
"""
auto_commit.py for the ytb repository.

Default:
  python auto_commit.py

This script lives in laptop/, but always operates on the parent Git repo:
  C:\\Users\\qacer\\Downloads\\ytb

It runs:
  git add .

Then sends `git diff --cached` to Groq. If the diff is too large, it
summarizes the diff in chunks first, then asks Groq for the final commit
message using the exact required prompt.

Important ytb behavior:
- Runtime folders, histories, logs, cookie files, local tokens, and generated
  auto-commit output files are unstaged after `git add .`.
- This script also unstages itself, matching the template behavior.
- Loads GROQ_API_KEY, then GROQ_API_KEY1 through GROQ_API_KEY9.
- Rotates the starting Groq key between requests.

Optional:
  GROQ_MODEL=auto, or set it to an active Groq chat model override
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import time
from pathlib import Path

import requests

import groq_dynamic

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

OUTPUT_MESSAGE_FILE = "laptop/COMMIT_EDITMSG.generated.txt"
OUTPUT_DIFF_FILE = "laptop/STAGED_DIFF.generated.patch"
OUTPUT_SUMMARY_FILE = "laptop/STAGED_DIFF_SUMMARY.generated.txt"

REQUEST_TIMEOUT = 120
MAX_RETRY_ROUNDS = 8

MAX_DIRECT_DIFF_CHARS = 18_000
MAX_CHUNK_CHARS = 11_000
MAX_SUMMARY_CHARS = 14_000

NEXT_KEY_START_INDEX = 0

IGNORED_DIR_NAME_RE = re.compile(r"^playwright-.+-profile$", re.I)

YTBTREE_PROTECTED_PATTERNS = [
    OUTPUT_MESSAGE_FILE,
    OUTPUT_DIFF_FILE,
    OUTPUT_SUMMARY_FILE,
    "COMMIT_EDITMSG.generated.txt",
    "STAGED_DIFF.generated.patch",
    "STAGED_DIFF_SUMMARY.generated.txt",
    "desktop.ini",
    "laptop/_working_downloads/*",
    "laptop/finished/*",
    "laptop/build/*",
    "laptop/dist/*",
    "laptop/__pycache__/*",
    "laptop/*.pyc",
    "laptop/*.pyo",
    "laptop/*.exe",
    "laptop/*.log",
    "laptop/trinity_run_log.txt",
    "laptop/upload_debug_*.html",
    "laptop/upload_debug_*.png",
    "laptop/download_history.json",
    "laptop/*_history.json",
    "laptop/_last_*.json",
    "laptop/_manual_*.json",
    "laptop/filenames.json",
    "laptop/youtube_cookies.txt",
    "laptop/youtube_cookies_raw.json",
    "laptop/youtube_ids_to_clear_from_histories.txt",
    "laptop/_google_drive_oauth_token.json",
    "laptop/_trinity_android_token.json",
    "laptop/google_drive_oauth_client.json",
    "laptop/client_secret*.json",
    "laptop/*.service-account.json",
    "laptop/*service_account*.json",
    "laptop/*service-account*.json",
    "laptop/*serviceaccount*.json",
    "laptop/wavestack2-*.json",
    "android/**/.gradle/*",
    "android/**/build/*",
    "android/**/local.properties",
    "android/**/*.apk",
    "android/**/*.aab",
]


EXACT_COMMIT_PROMPT = """write git commit message for this commit following these requirements strictly:
Commit messages provide both a summary of the code changes ("What") and the motivation or reason behind them ("Why")
Commit messages have a subject and a body
Separate subject from body with a blank line
Limit the subject line to 50 characters
Capitalize the subject line
Do not end the subject line with a period
Use the imperative mood in the subject line
Wrap the body at 72 characters
Use the body to explain what and why vs. how
Commit messages are:
Rational: the message provides a logical explanation for the changes and clearly states the commit type
Comprehensive: the message includes a summary of the changes and covers all affected files
Concise: the essential information is conveyed succinctly
Expressive: the message has grammatical correctness and fluency
Type: title must contain commit type

Example:
test: Add comprehensive authentication test suite
Implement complete test coverage for user authentication to ensure sign-in, sign-up, and sign-out flows work correctly and remain stable during future development.
Changes include:
-	Configure RSpec, Cucumber, and SimpleCov
-	Add testing gems: shoulda-matchers, factory_bot_rails, rails-controller-testing, capybara, database_cleaner
-	Create controller specs for PagesController and Users::SessionsController testing redirects and access
-	Add User model specs validating authentication and OAuth methods
-	Implement feature specs and Cucumber scenarios for complete authentication workflows
-	Configure Devise test helpers and Capybara support"""


EXTRA_OUTPUT_INSTRUCTIONS = """
Additional output rules:
Return only the commit message.
Do not include markdown fences.
Do not include explanations.
Do not include "Here is".
Do not include extra commentary before or after.
Do not include commands.
Do not include quotes around the message.
The output must be directly usable as a Git commit message file.
"""


class RequestTooLargeError(RuntimeError):
    pass


COMMIT_SUBJECT_RE = re.compile(
    r"^(?P<type>[a-z]+(?:\([A-Za-z0-9_.-]+\))?): (?P<summary>.+)$"
)
MOTIVATION_WORD_RE = re.compile(
    r"\b(to|so that|because|ensure|prevent|avoid|support|allow|make|keep|"
    r"so|therefore|in order to)\b",
    re.I,
)
CHANGE_WORD_RE = re.compile(
    r"\b(add|adds|added|update|updates|updated|change|changes|changed|"
    r"implement|implements|implemented|remove|removes|removed|fix|fixes|"
    r"fixed|preserve|preserves|preserved|move|moves|moved|create|creates|"
    r"created)\b",
    re.I,
)


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def stream_cmd(args: list[str], *, check: bool = True) -> None:
    print(f"\n$ {' '.join(args)}")
    proc = subprocess.run(args, cwd=REPO_ROOT, check=False)
    if check and proc.returncode != 0:
        raise SystemExit(proc.returncode)


def load_groq_keys() -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []

    base_key = os.getenv("GROQ_API_KEY")
    if base_key:
        keys.append(("GROQ_API_KEY", base_key))

    for i in range(1, 10):
        name = f"GROQ_API_KEY{i}"
        value = os.getenv(name)
        if value:
            keys.append((name, value))

    if not keys:
        raise SystemExit(
            "No Groq API keys found. Set GROQ_API_KEY and optionally "
            "GROQ_API_KEY1 through GROQ_API_KEY9."
        )

    return keys


def rotate_keys(keys: list[tuple[str, str]], start_index: int) -> list[tuple[str, str]]:
    if not keys:
        return keys

    start_index = start_index % len(keys)
    return keys[start_index:] + keys[:start_index]


def parse_retry_after_seconds(resp: requests.Response, fallback: float) -> float:
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass

    try:
        message = resp.json().get("error", {}).get("message", "")
        match = re.search(r"try again in\s+([0-9]*\.?[0-9]+)s", message, re.I)
        if match:
            return float(match.group(1))
    except Exception:
        pass

    return fallback


def make_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def is_request_too_large(resp: requests.Response) -> bool:
    return resp.status_code == 413


def groq_chat_try_all_keys(messages: list[dict[str, str]], *, max_tokens: int = 900) -> str:
    data = groq_dynamic.groq_chat_try_all_models(
        messages,
        max_tokens=max_tokens,
        temperature=0.2,
        timeout=REQUEST_TIMEOUT,
        max_retry_rounds=MAX_RETRY_ROUNDS,
        request_too_large_error_cls=RequestTooLargeError,
    )

    return str(data["choices"][0]["message"]["content"]).strip()


def copy_to_clipboard(text: str) -> bool:
    clipboard_commands: list[list[str]] = [
        ["clip"],
        ["clip.exe"],
        ["pbcopy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["wl-copy"],
    ]

    for cmd in clipboard_commands:
        try:
            subprocess.run(
                cmd,
                input=text,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            return True
        except Exception:
            continue

    return False


def get_staged_diff() -> str:
    result = run_cmd(["git", "diff", "--cached"], check=True)
    return result.stdout or ""


def validate_git_repo() -> None:
    result = run_cmd(["git", "rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise SystemExit(f"{REPO_ROOT} is not inside a Git repository.")

    git_root = Path(result.stdout.strip()).resolve()
    if git_root != REPO_ROOT:
        raise SystemExit(
            f"Script expected ytb repo root {REPO_ROOT}, but Git root is {git_root}."
        )


def path_has_ignored_dir(git_path: str) -> bool:
    parts = Path(git_path).parts
    return any(IGNORED_DIR_NAME_RE.match(part) for part in parts)


def path_matches_protected_pattern(git_path: str) -> bool:
    git_path = git_path.replace("\\", "/")
    if path_has_ignored_dir(git_path):
        return True

    return any(
        fnmatch.fnmatchcase(git_path, pattern)
        for pattern in YTBTREE_PROTECTED_PATTERNS
    )


def get_staged_paths() -> list[str]:
    result = run_cmd(["git", "diff", "--cached", "--name-only"], check=True)
    return [
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def reset_staged_paths(paths: list[str], reason: str) -> None:
    if not paths:
        return

    print(f"\nExcluding {reason}:")
    for path in paths:
        print(f"  - {path}")

    proc = subprocess.run(
        ["git", "reset", "--pathspec-from-file=-"],
        cwd=REPO_ROOT,
        input="\n".join(paths) + "\n",
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    if proc.stdout.strip():
        print(proc.stdout.strip())

    if proc.returncode != 0:
        if proc.stderr.strip():
            print(proc.stderr.strip())
        raise SystemExit(proc.returncode)


def unstage_self() -> None:
    try:
        self_git_path = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return

    reset_staged_paths(
        [path for path in get_staged_paths() if path == self_git_path],
        "this automation script",
    )


def unstage_protected_paths() -> None:
    protected_paths = [
        path for path in get_staged_paths()
        if path_matches_protected_pattern(path)
    ]
    reset_staged_paths(protected_paths, "ytb runtime/local/generated files")


def stage_changes(mode: str) -> None:
    if mode == "update":
        stream_cmd(["git", "add", "-u"])
    elif mode == "all":
        stream_cmd(["git", "add", "."])
    else:
        raise ValueError(f"Unknown stage mode: {mode}")

    unstage_self()
    unstage_protected_paths()


def split_large_text(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = min(start + max_chars, len(text))

        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start + max_chars // 2:
                end = newline

        chunks.append(text[start:end].strip())
        start = end

    return [chunk for chunk in chunks if chunk]


def split_diff_by_file(diff_text: str) -> list[str]:
    parts = re.split(r"(?=^diff --git )", diff_text, flags=re.M)
    chunks: list[str] = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if len(part) <= MAX_CHUNK_CHARS:
            chunks.append(part)
            continue

        chunks.extend(split_large_text(part, MAX_CHUNK_CHARS))

    return chunks


def summarize_diff_chunk(chunk: str, index: int, total: int) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize staged git diff chunks for a later commit message. "
                "Be concise, factual, and include file names and functional changes. "
                "Do not write the final commit message."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Summarize chunk {index}/{total} of this staged git diff.\n"
                "Focus on what changed, why it matters, and affected files.\n"
                "Use short bullets only.\n\n"
                + chunk
            ),
        },
    ]

    return groq_chat_try_all_keys(messages, max_tokens=450)


def summarize_large_diff(diff_text: str) -> str:
    chunks = split_diff_by_file(diff_text)
    print(f"Diff is large. Summarizing in {len(chunks)} chunk(s)...")

    summaries: list[str] = []

    for i, chunk in enumerate(chunks, start=1):
        print(f"\nSummarizing diff chunk {i}/{len(chunks)}...")
        summary = summarize_diff_chunk(chunk, i, len(chunks))
        summaries.append(f"Chunk {i}/{len(chunks)}:\n{summary}")

    combined = "\n\n".join(summaries)

    if len(combined) <= MAX_SUMMARY_CHARS:
        return combined

    print("\nCombined summaries are still large. Compressing summaries once more...")

    messages = [
        {
            "role": "system",
            "content": (
                "Compress git diff summaries into a concise commit-message brief. "
                "Keep affected files, major changes, and motivation. "
                "Do not write the final commit message."
            ),
        },
        {
            "role": "user",
            "content": combined[:MAX_SUMMARY_CHARS],
        },
    ]

    return groq_chat_try_all_keys(messages, max_tokens=650)


def build_final_messages(change_context: str, *, context_is_summary: bool) -> list[dict[str, str]]:
    context_label = (
        "Here is a concise summary of the staged git diff. "
        "Use this summary to write the commit message:"
        if context_is_summary
        else "Here is the staged git diff:"
    )

    user_content = (
        EXACT_COMMIT_PROMPT
        + "\n\n"
        + EXTRA_OUTPUT_INSTRUCTIONS
        + "\n\n"
        + context_label
        + "\n\n"
        + change_context
    )

    return [
        {
            "role": "system",
            "content": (
                "You write high-quality Git commit messages. "
                "Output only the commit message."
            ),
        },
        {
            "role": "user",
            "content": user_content,
        },
    ]


def validate_commit_message(message: str) -> list[str]:
    problems: list[str] = []
    message = message.strip()

    if not message:
        return ["Commit message is empty."]

    if "```" in message:
        problems.append("Commit message must not contain markdown fences.")
    if re.search(r"\bhere is\b", message, re.I):
        problems.append('Commit message must not contain "Here is" commentary.')
    if re.search(r"^\s*(git|python|cd)\s+", message, re.I | re.M):
        problems.append("Commit message must not contain commands.")

    lines = message.splitlines()
    if len(lines) < 3:
        problems.append("Commit message must contain a subject, blank line, and body.")
        return problems

    subject = lines[0]
    if subject != subject.strip():
        problems.append("Subject line must not have leading or trailing spaces.")
    subject = subject.strip()

    if lines[1].strip():
        problems.append("Subject and body must be separated by exactly one blank line.")

    if len(subject) > 50:
        problems.append("Subject line must be 50 characters or fewer.")
    if subject.endswith("."):
        problems.append("Subject line must not end with a period.")

    subject_match = COMMIT_SUBJECT_RE.match(subject)
    if not subject_match:
        problems.append("Subject must start with a commit type like feat:, fix:, or chore:.")
    else:
        summary = subject_match.group("summary").strip()
        if not summary:
            problems.append("Subject must include a summary after the commit type.")
        elif summary[0].isalpha() and not summary[0].isupper():
            problems.append("Subject summary after the type must be capitalized.")

    body_lines = lines[2:]
    body_text = "\n".join(body_lines).strip()
    if not body_text:
        problems.append("Commit message body must not be empty.")

    for index, line in enumerate(body_lines, start=3):
        if line.rstrip() != line:
            problems.append(f"Body line {index} has trailing whitespace.")
        if len(line) > 72:
            problems.append(f"Body line {index} is longer than 72 characters.")

    folded_body = " ".join(line.strip() for line in body_lines if line.strip())
    if folded_body and not CHANGE_WORD_RE.search(folded_body):
        problems.append("Body must describe what changed.")
    if folded_body and not MOTIVATION_WORD_RE.search(folded_body):
        problems.append("Body must explain why or the motivation for the change.")

    return problems


def build_commit_repair_messages(
    bad_message: str,
    problems: list[str],
    change_context: str,
    *,
    context_is_summary: bool,
) -> list[dict[str, str]]:
    context_label = (
        "Use this existing staged-diff summary only for change context:"
        if context_is_summary
        else "Use this staged diff only for change context:"
    )
    validation_report = "\n".join(f"- {problem}" for problem in problems)

    return [
        {
            "role": "system",
            "content": (
                "You repair Git commit messages. Output only the corrected "
                "commit message, with no markdown and no explanations."
            ),
        },
        {
            "role": "user",
            "content": (
                EXACT_COMMIT_PROMPT
                + "\n\n"
                + EXTRA_OUTPUT_INSTRUCTIONS
                + "\n\n"
                + "The previous commit message failed this deterministic validation:\n"
                + validation_report
                + "\n\nRewrite the commit message so it passes every rule. "
                + "Do not summarize the diff again. Do not add commentary.\n\n"
                + "Previous commit message:\n"
                + bad_message.strip()
                + "\n\n"
                + context_label
                + "\n\n"
                + change_context
            ),
        },
    ]


def ensure_valid_commit_message(
    commit_message: str,
    change_context: str,
    *,
    context_is_summary: bool,
) -> str:
    problems = validate_commit_message(commit_message)
    if not problems:
        print("Commit message passed deterministic format check.")
        return commit_message.strip()

    print("\nCommit message failed deterministic format check:")
    for problem in problems:
        print(f"  - {problem}")
    print("Asking Groq one final time to repair the commit message format...")

    repaired = groq_chat_try_all_keys(
        build_commit_repair_messages(
            bad_message=commit_message,
            problems=problems,
            change_context=change_context,
            context_is_summary=context_is_summary,
        ),
        max_tokens=900,
    ).strip()

    remaining = validate_commit_message(repaired)
    if remaining:
        print(
            "Groq repair pass still missed some deterministic format checks, "
            "but it produced a result. Using that repaired message without "
            "another Groq call:"
        )
        for problem in remaining:
            print(f"  - {problem}")
        return repaired

    print("Repaired commit message passed deterministic format check.")
    return repaired


def generate_commit_message(diff_text: str) -> str:
    if len(diff_text) <= MAX_DIRECT_DIFF_CHARS:
        messages = build_final_messages(diff_text, context_is_summary=False)
        commit_message = groq_chat_try_all_keys(messages).strip()
        return ensure_valid_commit_message(
            commit_message,
            diff_text,
            context_is_summary=False,
        )

    summary = summarize_large_diff(diff_text)
    save_text(OUTPUT_SUMMARY_FILE, summary)
    print(f"\nSaved staged diff summary to: {OUTPUT_SUMMARY_FILE}")

    messages = build_final_messages(summary, context_is_summary=True)

    try:
        commit_message = groq_chat_try_all_keys(messages).strip()
    except RequestTooLargeError:
        print("Final summary prompt was still too large. Truncating summary and retrying...")
        shorter_summary = summary[:MAX_SUMMARY_CHARS // 2]
        messages = build_final_messages(shorter_summary, context_is_summary=True)
        commit_message = groq_chat_try_all_keys(messages).strip()
        return ensure_valid_commit_message(
            commit_message,
            shorter_summary,
            context_is_summary=True,
        )

    return ensure_valid_commit_message(
        commit_message,
        summary,
        context_is_summary=True,
    )


def save_text(path: str | Path, text: str) -> None:
    (REPO_ROOT / path).write_text(text.rstrip() + "\n", encoding="utf-8")


def ask_yes_no(question: str, *, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(question + suffix).strip().lower()

    if not answer:
        return default

    return answer in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage ytb repo changes, ask Groq for a commit message, "
            "then optionally commit and push."
        )
    )
    parser.add_argument(
        "--stage",
        choices=["update", "all"],
        default=None,
        help="update = git add -u, all = git add .; default is all",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Run git commit -F after generating the message.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Run git push after committing. Implies --commit.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt before commit or push.",
    )
    args = parser.parse_args()

    validate_git_repo()
    print(f"Using ytb repo: {REPO_ROOT}")

    stage_mode = args.stage or "all"
    stage_changes(stage_mode)

    diff_text = get_staged_diff()
    if not diff_text.strip():
        raise SystemExit("No staged diff found after git add.")

    save_text(OUTPUT_DIFF_FILE, diff_text)
    print(f"\nSaved staged diff to: {OUTPUT_DIFF_FILE}")

    try:
        commit_message = generate_commit_message(diff_text).strip()
    except RequestTooLargeError as e:
        raise SystemExit(
            "The diff is still too large even after chunking. "
            "Commit fewer files at once or use --stage update.\n"
            f"{e}"
        )

    if not commit_message:
        raise SystemExit("Groq returned an empty commit message.")

    save_text(OUTPUT_MESSAGE_FILE, commit_message)
    copied = copy_to_clipboard(commit_message)

    print("\n" + "=" * 72)
    print("Generated commit message")
    print("=" * 72)
    print(commit_message)
    print("=" * 72)
    print(f"\nSaved to: {OUTPUT_MESSAGE_FILE}")

    if copied:
        print("Copied commit message to clipboard.")
    else:
        print("Could not copy to clipboard. Use the saved message file instead.")

    should_commit = args.commit or args.push
    if not should_commit and not args.yes:
        should_commit = ask_yes_no("Run git commit using this message now?", default=False)

    if should_commit:
        if args.yes or ask_yes_no("Confirm git commit -F now?", default=True):
            stream_cmd(["git", "commit", "-F", OUTPUT_MESSAGE_FILE])
        else:
            print("Commit skipped.")

    should_push = args.push
    if should_commit and not should_push and not args.yes:
        should_push = ask_yes_no("Run git push now?", default=False)

    if should_push:
        if args.yes or ask_yes_no("Confirm git push now?", default=True):
            stream_cmd(["git", "push"])
        else:
            print("Push skipped.")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
