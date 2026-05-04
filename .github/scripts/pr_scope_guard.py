#!/usr/bin/env python3
"""Fail external PRs that touch files outside the miner harness."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GITHUB_API = "https://api.github.com"
MARKER = "<!-- ninja-pr-scope-guard -->"
DEFAULT_TRUSTED_AUTHORS = ("unarbos",)
DEFAULT_EXTERNAL_ALLOWED_FILES = ("agent.py",)


def main() -> int:
    try:
        event = _load_event()
        repo = _required_env("GITHUB_REPOSITORY")
        token = _required_env("GITHUB_TOKEN")

        pr = event["pull_request"]
        pr_number = int(pr["number"])
        author = str((pr.get("user") or {}).get("login") or "")
        trusted_authors = _csv_env("TRUSTED_PR_AUTHORS", DEFAULT_TRUSTED_AUTHORS)
        allowed_files = _csv_env("EXTERNAL_PR_ALLOWED_FILES", DEFAULT_EXTERNAL_ALLOWED_FILES)

        files = _fetch_pr_files(token, repo, pr_number)
        changed_files = [str(item.get("filename") or "") for item in files]
        violations = _scope_violations(changed_files, author, trusted_authors, allowed_files)

        if author in trusted_authors:
            body = _render_comment("pass", author, changed_files, allowed_files, [])
            _update_existing_comment(token, repo, pr_number, body)
            _write_step_summary(body)
            print(f"Trusted PR author {author}; external file-scope guard bypassed.")
            return 0

        if violations:
            body = _render_comment("fail", author, changed_files, allowed_files, violations)
            _upsert_comment(token, repo, pr_number, body)
            _write_step_summary(body)
            print("External PR changed files outside the allowed surface:")
            for filename in violations:
                print(f"- {filename}")
            return 1

        body = _render_comment("pass", author, changed_files, allowed_files, [])
        _update_existing_comment(token, repo, pr_number, body)
        _write_step_summary(body)
        print("External PR file scope is valid.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"scope guard error: {exc}", file=sys.stderr)
        return 1


def _load_event() -> dict[str, Any]:
    path = Path(_required_env("GITHUB_EVENT_PATH"))
    return json.loads(path.read_text(encoding="utf-8"))


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _csv_env(name: str, default: tuple[str, ...]) -> set[str]:
    raw = os.environ.get(name)
    values = default if not raw else tuple(raw.split(","))
    parsed = {value.strip() for value in values if value.strip()}
    if not parsed:
        raise RuntimeError(f"{name} must contain at least one value")
    return parsed


def _scope_violations(
    changed_files: list[str],
    author: str,
    trusted_authors: set[str],
    allowed_files: set[str],
) -> list[str]:
    if author in trusted_authors:
        return []
    return [filename for filename in changed_files if filename not in allowed_files]


def _fetch_pr_files(token: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _github_json(token, repo, f"/pulls/{pr_number}/files?per_page=100&page={page}")
        if not batch:
            return files
        files.extend(batch)
        if len(batch) < 100:
            return files
        page += 1


def _github_json(token: str, repo: str, path: str, method: str = "GET", payload: Any | None = None) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    data = _github_request(token, f"/repos/{repo}{path}", method, body)
    return json.loads(data.decode("utf-8"))


def _github_request(token: str, path: str, method: str, body: bytes | None) -> bytes:
    req = urllib.request.Request(
        url=f"{GITHUB_API}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ninja-pr-scope-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed with HTTP {exc.code}: {error_body}") from exc


def _render_comment(
    verdict: str,
    author: str,
    changed_files: list[str],
    allowed_files: set[str],
    violations: list[str],
) -> str:
    title_verdict = verdict.upper()
    lines = [
        MARKER,
        "## Ninja PR Scope Guard",
        "",
        f"Verdict: **{title_verdict}**",
        f"Author: `{author}`",
        "External contributor file allowlist: "
        + ", ".join(f"`{filename}`" for filename in sorted(allowed_files)),
        "",
    ]
    if violations:
        lines.extend(
            [
                "External PRs may only change the miner harness file.",
                "",
                "### Files Outside Scope",
            ]
        )
        lines.extend(f"- `{filename}`" for filename in violations)
    else:
        lines.append("This PR satisfies the external contributor file-scope rule.")

    lines.extend(["", "### Changed Files"])
    lines.extend(f"- `{filename}`" for filename in changed_files or ["No files returned by GitHub."])
    return "\n".join(lines) + "\n"


def _upsert_comment(token: str, repo: str, pr_number: int, body: str) -> None:
    if _update_existing_comment(token, repo, pr_number, body):
        return
    _github_json(token, repo, f"/issues/{pr_number}/comments", method="POST", payload={"body": body})


def _update_existing_comment(token: str, repo: str, pr_number: int, body: str) -> bool:
    comments = _github_json(token, repo, f"/issues/{pr_number}/comments?per_page=100")
    for comment in comments:
        if MARKER in str(comment.get("body", "")):
            _github_json(token, repo, f"/issues/comments/{comment['id']}", method="PATCH", payload={"body": body})
            return True
    return False


def _write_step_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        Path(path).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
