#!/usr/bin/env python3
"""
test-openai-auth.py — Validate Codex/OpenAI auth connectivity

Usage:
  ./tizen/dev/test-openai-auth.py                            # prefers ~/.codex/auth.json
  ./tizen/dev/test-openai-auth.py --key <token>             # explicit token or API key
  ./tizen/dev/test-openai-auth.py --account-id <uuid>       # required with OAuth access token
  ./tizen/dev/test-openai-auth.py --config <path>           # optional config.toml for base URL only
  ./tizen/dev/test-openai-auth.py --models gpt-5,codex      # test subset of models
  ./tizen/dev/test-openai-auth.py --base-url <url>          # custom endpoint/base URL
  ./tizen/dev/test-openai-auth.py --codex-login             # auto-run `codex login --device-auth`
  ./tizen/dev/test-openai-auth.py --verbose                 # show full response bodies

Exit codes:
  0 — at least one model succeeded
  1 — all models failed
  2 — no usable credential found
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib import error, request

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_INSTRUCTIONS = "You are a concise and helpful coding assistant."
DEFAULT_MODELS = [
    "gpt-5.2",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5-codex",
]


@dataclass
class AuthMaterial:
    mode: str
    token: str
    account_id: str
    source: str


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


def eprint(*parts: str) -> None:
    print(*parts, file=sys.stderr)


def redact(value: str) -> str:
    if len(value) <= 16:
        return value
    return f"{value[:12]}...{value[-4:]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--key")
    parser.add_argument("--account-id")
    parser.add_argument("--config")
    parser.add_argument("--models")
    parser.add_argument("--base-url")
    parser.add_argument("--codex-login", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("-h", "--help", action="help")
    return parser.parse_args()


def load_top_level_config(path: Path) -> tuple[str, str]:
    provider = ""
    api_url = ""
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            if line.startswith("default_provider"):
                provider = strip_toml_string(line.split("=", 1)[1].strip())
            elif line.startswith("api_url"):
                api_url = strip_toml_string(line.split("=", 1)[1].strip())
    except OSError:
        return "", ""
    return provider, api_url


def strip_toml_string(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1]
    return value


def is_openai_provider(provider: str) -> bool:
    return provider in {"openai", "openai-codex", "openai_codex", "codex"} or provider.startswith(
        "custom:"
    )


def read_codex_auth() -> Optional[AuthMaterial]:
    auth_path = Path.home() / ".codex" / "auth.json"
    if not auth_path.exists():
        return None

    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    api_key = data.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        return AuthMaterial("api_key", api_key.strip(), "", str(auth_path))

    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if isinstance(access_token, str) and access_token.strip():
            return AuthMaterial(
                "oauth",
                access_token.strip(),
                account_id.strip() if isinstance(account_id, str) else "",
                str(auth_path),
            )

    return None


def prompt_or_run_codex_login(auto: bool) -> None:
    if shutil.which("codex") is None:
        print(f"{COLORS.red}codex CLI is not installed or not in PATH.{COLORS.reset}")
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            f"{COLORS.red}No Codex auth found, and interactive login is unavailable in this shell.{COLORS.reset}"
        )
        return

    if auto:
        print(f"{COLORS.yellow}No Codex auth found. Starting Codex device login...{COLORS.reset}")
        subprocess.run(["codex", "login", "--device-auth"], check=False)
        return

    answer = input("No Codex auth found. Run 'codex login --device-auth' now? [Y/n] ").strip().lower()
    if answer in {"", "y", "yes"}:
        subprocess.run(["codex", "login", "--device-auth"], check=False)


def resolve_auth(args: argparse.Namespace) -> tuple[Optional[AuthMaterial], str]:
    base_url = args.base_url or ""

    if args.key:
        mode = "oauth" if args.account_id else "api_key"
        return AuthMaterial(mode, args.key, args.account_id or "", "--key"), base_url

    if args.config:
        provider, api_url = load_top_level_config(Path(args.config))
        if not args.base_url and api_url and is_openai_provider(provider):
            base_url = api_url

    auth = read_codex_auth()
    if auth is not None:
        return auth, base_url

    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return AuthMaterial("api_key", env_key, "", "OPENAI_API_KEY"), base_url

    prompt_or_run_codex_login(args.codex_login)
    return read_codex_auth(), base_url


def build_endpoint(base_or_endpoint: str) -> str:
    raw = base_or_endpoint.rstrip("/")
    if raw.endswith(("backend-api/codex/responses", "/v1/responses", "/responses")):
        return raw
    if raw.endswith("backend-api/codex"):
        return f"{raw}/responses"
    for suffix in ("/chat/completions", "/audio/transcriptions", "/embeddings"):
        if raw.endswith(suffix):
            return f"{raw[: -len(suffix)]}/responses"
    if raw.endswith("/v1"):
        return f"{raw}/responses"
    return f"{raw}/v1/responses"


def select_models(filter_text: str) -> list[str]:
    if not filter_text:
        return list(DEFAULT_MODELS)

    tags = [tag.strip() for tag in filter_text.split(",") if tag.strip()]
    filtered = [model for model in DEFAULT_MODELS if any(tag in model for tag in tags)]
    return filtered or list(DEFAULT_MODELS)


def parse_error_body(body: str) -> tuple[str, str, str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "unknown", "", body.strip()[:120]

    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        return (
            str(error_obj.get("type") or "unknown"),
            str(error_obj.get("code") or ""),
            str(error_obj.get("message") or "")[:120],
        )

    detail = payload.get("detail")
    if detail is not None:
        return "detail", "", str(detail)[:120]

    return "unknown", "", str(payload)[:120]


def parse_oauth_success(body: str) -> tuple[str, str]:
    last_response = None
    for raw_line in body.splitlines():
        if not raw_line.startswith("data: "):
            continue
        data = raw_line[6:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response

    if not isinstance(last_response, dict):
        return "?", ""

    usage = last_response.get("usage") or {}
    output_tokens = str(usage.get("output_tokens", "?"))
    message_text = ""
    for item in last_response.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text":
                message_text = str(content.get("text") or "")
                break
        if message_text:
            break
    return output_tokens, message_text[:80]


def parse_api_success(body: str) -> tuple[str, str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "?", ""

    usage = payload.get("usage") or {}
    output_tokens = str(usage.get("output_tokens", "?"))
    text = str(payload.get("output_text") or "")
    if not text:
        for item in payload.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text":
                    text = str(content.get("text") or "")
                    break
            if text:
                break
    return output_tokens, text[:80]


def make_request(
    endpoint: str,
    auth: AuthMaterial,
    model: str,
) -> tuple[int, str, float]:
    if auth.mode == "oauth":
        payload = {
            "model": model,
            "instructions": DEFAULT_INSTRUCTIONS,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Say OK"}]}],
            "store": False,
            "stream": True,
            "text": {"verbosity": "medium"},
            "reasoning": {"effort": "medium", "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        headers = {
            "Authorization": f"Bearer {auth.token}",
            "chatgpt-account-id": auth.account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "pi",
            "accept": "text/event-stream",
            "Content-Type": "application/json",
        }
    else:
        payload = {"model": model, "input": "Say OK", "store": False}
        headers = {
            "Authorization": f"Bearer {auth.token}",
            "Content-Type": "application/json",
        }

    data = json.dumps(payload).encode("utf-8")
    req = request.Request(endpoint, data=data, headers=headers, method="POST")
    started = time.monotonic()

    try:
        with request.urlopen(req, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body, time.monotonic() - started
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body, time.monotonic() - started


def status_for_failure(http_code: int, err_code: str, err_type: str) -> tuple[str, str, bool]:
    if http_code == 400 and err_code == "model_not_found":
        return f"{COLORS.yellow}SKIP{COLORS.reset}", "400 model_not_found", False
    if http_code == 400:
        return f"{COLORS.red}FAIL{COLORS.reset}", "400 invalid_request", True
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


def test_model(endpoint: str, auth: AuthMaterial, model: str, verbose: bool) -> tuple[bool, bool]:
    if auth.mode == "oauth" and not auth.account_id:
        print(
            f"  {model:<38} [{COLORS.red}FAIL{COLORS.reset}] missing_account_id Codex OAuth requires account_id"
        )
        return False, True

    try:
        http_code, body, elapsed = make_request(endpoint, auth, model)
    except Exception as exc:  # pragma: no cover - terminal/network fallback
        print(f"  {model:<38} [{COLORS.red}FAIL{COLORS.reset}] network_error {exc}")
        return False, True

    if http_code == 200:
        if auth.mode == "oauth":
            output_tokens, sample_text = parse_oauth_success(body)
        else:
            output_tokens, sample_text = parse_api_success(body)
        extra = f"({output_tokens} output tokens, {elapsed:.6f}s"
        if sample_text:
            extra += f", text='{sample_text}'"
        extra += ")"
        print(f"  {model:<38} [{COLORS.green}PASS{COLORS.reset}] 200 OK {extra}")
        return True, False

    err_type, err_code, err_msg = parse_error_body(body)
    status_icon, status_msg, counts_as_fail = status_for_failure(http_code, err_code, err_type)
    if err_code:
        extra = f"[{err_type}/{err_code}] {err_msg}"
    else:
        extra = f"[{err_type}] {err_msg}"
    print(f"  {model:<38} [{status_icon}] {status_msg} {extra}")
    if verbose:
        print(f"    {COLORS.dim}{body}{COLORS.reset}")
    return False, counts_as_fail


def main() -> int:
    args = parse_args()
    auth, base_url = resolve_auth(args)

    if auth is None or not auth.token:
        print(f"{COLORS.red}No Codex/OpenAI auth found.{COLORS.reset}")
        print("Accepted sources:")
        print("  --key [--account-id UUID]")
        print("  ~/.codex/auth.json")
        print("  OPENAI_API_KEY")
        print("Run it interactively to be prompted for 'codex login --device-auth',")
        print("or rerun with --codex-login to auto-start that flow.")
        return 2

    if not base_url:
        base_url = DEFAULT_CODEX_RESPONSES_URL if auth.mode == "oauth" else DEFAULT_OPENAI_BASE_URL
    endpoint = build_endpoint(base_url)

    print(f"{COLORS.bold}OpenAI Auth Test{COLORS.reset}")
    print(f"  Mode       : {COLORS.cyan}{auth.mode}{COLORS.reset}")
    print(f"  Token      : {COLORS.dim}{redact(auth.token)}{COLORS.reset}")
    print(f"  Source     : {COLORS.dim}{auth.source}{COLORS.reset}")
    if auth.account_id:
        print(f"  Account ID : {COLORS.dim}{auth.account_id}{COLORS.reset}")
    print(f"  Endpoint   : {COLORS.dim}{endpoint}{COLORS.reset}")
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
    if auth.mode == "oauth":
        print()
        print(
            f"{COLORS.dim}Codex OAuth mode uses ~/.codex/auth.json tokens and the ChatGPT Codex backend.{COLORS.reset}"
        )

    return 0 if passed > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
