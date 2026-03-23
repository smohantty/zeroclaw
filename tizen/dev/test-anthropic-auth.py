#!/usr/bin/env python3
"""
test-anthropic-auth.py — Validate Anthropic Claude auth connectivity

Usage:
  ./tizen/dev/test-anthropic-auth.py                       # prefers ~/.claude/.credentials.json
  ./tizen/dev/test-anthropic-auth.py --token <token>      # explicit Claude CLI OAuth token
  ./tizen/dev/test-anthropic-auth.py --models sonnet      # test subset of models
  ./tizen/dev/test-anthropic-auth.py --base-url <url>     # custom endpoint/base URL
  ./tizen/dev/test-anthropic-auth.py --verbose            # show full response bodies

Exit codes:
  0 — at least one model succeeded
  1 — all models failed
  2 — no usable credential found
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib import error, request

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]
DEFAULT_PROMPT = "Say OK"
CLAUDE_CODE_SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."
CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


@dataclass
class AuthMaterial:
    token: str
    source: str
    expires_at_ms: Optional[int] = None


class Colors:
    def __init__(self) -> None:
        enabled = sys.stdout.isatty()
        self.green = "\033[0;32m" if enabled else ""
        self.red = "\033[0;31m" if enabled else ""
        self.yellow = "\033[0;33m" if enabled else ""
        self.cyan = "\033[0;36m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.reset = "\033[0m" if enabled else ""


COLORS = Colors()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--token")
    parser.add_argument("--models")
    parser.add_argument("--base-url")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("-h", "--help", action="help")
    return parser.parse_args()


def redact(value: str) -> str:
    if len(value) <= 16:
        return value
    return f"{value[:12]}...{value[-4:]}"


def build_endpoint(base_or_endpoint: str) -> str:
    raw = base_or_endpoint.rstrip("/")
    if raw.endswith("/v1/messages"):
        return raw
    if raw.endswith("/v1"):
        return f"{raw}/messages"
    return f"{raw}/v1/messages"


def select_models(filter_text: str) -> list[str]:
    if not filter_text:
        return list(DEFAULT_MODELS)

    tags = [tag.strip() for tag in filter_text.split(",") if tag.strip()]
    filtered = [model for model in DEFAULT_MODELS if any(tag in model for tag in tags)]
    return filtered or list(DEFAULT_MODELS)


def read_claude_credentials() -> Optional[AuthMaterial]:
    if not CLAUDE_CREDENTIALS_PATH.exists():
        return None

    try:
        payload = json.loads(CLAUDE_CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None

    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    if not access_token.startswith("sk-ant-oat01-"):
        return None

    expires_at = oauth.get("expiresAt")
    return AuthMaterial(
        token=access_token.strip(),
        source=str(CLAUDE_CREDENTIALS_PATH),
        expires_at_ms=expires_at if isinstance(expires_at, int) else None,
    )


def resolve_auth(args: argparse.Namespace) -> Optional[AuthMaterial]:
    if args.token:
        token = args.token.strip()
        if not token:
            return None
        if not token.startswith("sk-ant-oat01-"):
            return None
        return AuthMaterial(token=token, source="--token")
    return read_claude_credentials()


def parse_error_body(body: str) -> tuple[str, str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "unknown", body.strip()[:120]

    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        err_type = str(error_obj.get("type") or "unknown")
        message = str(error_obj.get("message") or "")[:120]
        return err_type, message

    return "unknown", str(payload)[:120]


def parse_success(body: str) -> tuple[str, str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "?", ""

    usage = payload.get("usage") or {}
    output_tokens = str(usage.get("output_tokens", "?"))
    message_text = ""
    for item in payload.get("content", []) or []:
        if item.get("type") == "text":
            message_text = str(item.get("text") or "")
            break
    return output_tokens, message_text[:80]


def status_for_failure(http_code: int, err_type: str) -> tuple[str, str, bool]:
    if http_code == 401:
        return f"{COLORS.red}FAIL{COLORS.reset}", "401 auth_error", True
    if http_code == 403:
        return f"{COLORS.red}FAIL{COLORS.reset}", "403 forbidden", True
    if http_code == 404:
        return f"{COLORS.yellow}SKIP{COLORS.reset}", "404 not_found", False
    if http_code == 429:
        return f"{COLORS.yellow}RATE{COLORS.reset}", "429 rate_limited", True
    if http_code in {500, 502, 503}:
        return f"{COLORS.red}FAIL{COLORS.reset}", f"{http_code} server_error", True
    return f"{COLORS.red}FAIL{COLORS.reset}", f"{http_code} {err_type}", True


def make_request(endpoint: str, auth: AuthMaterial, model: str) -> tuple[int, str, float]:
    payload = {
        "model": model,
        "max_tokens": 64,
        "system": [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": DEFAULT_PROMPT}],
    }
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "Authorization": f"Bearer {auth.token}",
        "anthropic-beta": "oauth-2025-04-20",
    }

    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()

    try:
        with request.urlopen(req, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body, time.monotonic() - started
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body, time.monotonic() - started


def test_model(endpoint: str, auth: AuthMaterial, model: str, verbose: bool) -> tuple[bool, bool]:
    try:
        http_code, body, elapsed = make_request(endpoint, auth, model)
    except Exception as exc:  # pragma: no cover - terminal/network fallback
        print(f"  {model:<38} [{COLORS.red}FAIL{COLORS.reset}] network_error {exc}")
        return False, True

    if http_code == 200:
        output_tokens, sample_text = parse_success(body)
        extra = f"({output_tokens} output tokens, {elapsed:.6f}s"
        if sample_text:
            extra += f", text='{sample_text}'"
        extra += ")"
        print(f"  {model:<38} [{COLORS.green}PASS{COLORS.reset}] 200 OK {extra}")
        return True, False

    err_type, err_msg = parse_error_body(body)
    status_icon, status_msg, counts_as_fail = status_for_failure(http_code, err_type)
    extra = f"[{err_type}] {err_msg}"
    print(f"  {model:<38} [{status_icon}] {status_msg} {extra}")
    if verbose:
        print(f"    {COLORS.dim}{body}{COLORS.reset}")
    return False, counts_as_fail


def print_expiry(auth: AuthMaterial) -> None:
    if auth.expires_at_ms is None:
        return
    expires_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(auth.expires_at_ms / 1000))
    print(f"  Expires    : {COLORS.dim}{expires_at}{COLORS.reset}")


def main() -> int:
    args = parse_args()
    auth = resolve_auth(args)
    if auth is None or not auth.token:
        print(f"{COLORS.red}No Anthropic auth found.{COLORS.reset}")
        print("Accepted sources:")
        print("  --token (Claude OAuth setup token)")
        print("  ~/.claude/.credentials.json")
        return 2

    endpoint = build_endpoint(args.base_url or DEFAULT_BASE_URL)

    print(f"{COLORS.bold}Anthropic Auth Test{COLORS.reset}")
    print(f"  Mode       : {COLORS.cyan}oauth{COLORS.reset}")
    print(f"  Token      : {COLORS.dim}{redact(auth.token)}{COLORS.reset}")
    print(f"  Source     : {COLORS.dim}{auth.source}{COLORS.reset}")
    print_expiry(auth)
    print(f"  Endpoint   : {COLORS.dim}{endpoint}{COLORS.reset}")
    print(f"  System     : {COLORS.dim}prepend Claude Code system prompt{COLORS.reset}")
    print(f"  Headers    : {COLORS.dim}minimal OAuth header set{COLORS.reset}")
    print()

    models = select_models(args.models or "")
    print(f"{COLORS.bold}Models{COLORS.reset}")

    passed = 0
    failed = 0
    for model in models:
        ok, counts_as_fail = test_model(endpoint, auth, model, args.verbose)
        passed += int(ok)
        failed += int(counts_as_fail)

    print()
    print(
        f"{COLORS.bold}Summary{COLORS.reset}: {COLORS.green}{passed} passed{COLORS.reset}, "
        f"{COLORS.red}{failed} failed{COLORS.reset} out of {len(models)} tested"
    )

    print()
    print(
        f"{COLORS.dim}Claude OAuth mode uses ~/.claude/.credentials.json accessToken with the Anthropic Messages API, prepends the Claude Code system prompt, and keeps the header set minimal.{COLORS.reset}"
    )

    return 0 if passed > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
