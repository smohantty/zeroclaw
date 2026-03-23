from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
PERF_ROOT = Path(__file__).resolve().parent
DEFAULT_OUT_ROOT = PERF_ROOT / "out"


class RsdbError(RuntimeError):
    pass


@dataclass
class ExecResult:
    status: int
    stdout: str
    stderr: str
    timed_out: bool = False


def log(message: str) -> None:
    print(f"==> {message}", file=sys.stderr)


def die(message: str, exit_code: int = 1) -> None:
    raise SystemExit(f"error: {message}") from None


def ensure_out_dir(kind: str, explicit: str | None = None) -> Path:
    if explicit:
        out_dir = Path(explicit).expanduser().resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = (DEFAULT_OUT_ROOT / kind / stamp).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def slugify_label(value: str, *, fallback: str = "capture") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-").lower()
    return slug or fallback


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RsdbError(
            f"command failed ({proc.returncode}): {' '.join(args)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def run_agent_json(args: list[str]) -> dict[str, Any]:
    proc = run_cmd(["rsdb", "agent", *args])
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RsdbError(f"rsdb returned non-JSON output for: {' '.join(args)}") from exc
    if not payload.get("ok", False):
        error = payload.get("error") or {}
        message = error.get("message") or "unknown rsdb error"
        raise RsdbError(message)
    return payload


def resolve_target(explicit_target: str | None, probe_addr: str | None = None) -> str:
    if explicit_target:
        if ":" in explicit_target:
            return explicit_target
        probe_addr = explicit_target

    args = ["discover"]
    if probe_addr:
        args.extend(["--probe-addr", probe_addr])
    payload = run_agent_json(args)
    targets = [
        target
        for target in payload.get("data", {}).get("targets", [])
        if target.get("compatible", False)
    ]
    if not targets:
        if probe_addr:
            raise RsdbError(f"no compatible rsdb targets found for {probe_addr}")
        raise RsdbError("no compatible rsdb targets found")
    if len(targets) > 1:
        available = ", ".join(target.get("target", "<unknown>") for target in targets)
        raise RsdbError(f"multiple compatible targets found; pass --target explicitly: {available}")
    return str(targets[0]["target"])


def remote_exec(target: str, remote_args: list[str]) -> ExecResult:
    payload = run_agent_json(["exec", "--target", target, "--", *remote_args])
    data = payload.get("data", {})
    return ExecResult(
        status=int(data.get("status", 0)),
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
        timed_out=bool(data.get("timed_out", False)),
    )


def remote_shell(target: str, script: str) -> ExecResult:
    return remote_exec(target, ["sh", "-lc", script])


def stream_remote_shell_to_path(
    target: str,
    script: str,
    destination: Path,
    *,
    timeout_secs: int | None = None,
) -> int:
    cmd = ["rsdb", "agent", "exec", "--target", target]
    if timeout_secs is not None:
        cmd.extend(["--timeout-secs", str(timeout_secs)])
    cmd.extend(["--stream", "--", "sh", "-lc", script])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    completed_status: int | None = None
    try:
        assert proc.stdout is not None
        with destination.open("w", encoding="utf-8") as handle:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RsdbError(f"failed to parse rsdb stream event: {line}") from exc
                kind = event.get("event")
                data = event.get("data", {})
                if kind == "stdout":
                    chunk = data.get("chunk", "")
                    handle.write(chunk)
                elif kind == "stderr":
                    chunk = data.get("chunk", "")
                    if chunk:
                        sys.stderr.write(chunk)
                elif kind == "completed":
                    completed_status = int(data.get("status", 0))
                elif kind == "failed":
                    error = data.get("error", {})
                    message = error.get("message") or str(error) or "unknown rsdb stream failure"
                    raise RsdbError(message)
    finally:
        stderr_output = proc.stderr.read() if proc.stderr is not None else ""
        returncode = proc.wait()
        if returncode != 0:
            raise RsdbError(
                f"rsdb stream exited with code {returncode}\n"
                f"stderr:\n{stderr_output}"
            )
    if completed_status is None:
        raise RsdbError("rsdb stream ended without a completed event")
    if completed_status != 0:
        raise RsdbError(f"remote command exited with status {completed_status}")
    return completed_status


def detect_zeroclaw_pid(target: str, service_name: str) -> int:
    script = textwrap.dedent(
        f"""
        service_name={shell_quote(service_name)}
        main_pid=""
        if command -v systemctl >/dev/null 2>&1; then
          main_pid="$(systemctl show -p MainPID --value "${{service_name}}.service" 2>/dev/null || true)"
        fi
        if [ -n "$main_pid" ] && [ "$main_pid" != "0" ] && [ -d "/proc/$main_pid" ]; then
          printf '%s\\n' "$main_pid"
          exit 0
        fi
        ps -eo pid=,args= | awk '
          $0 ~ /(^|[[:space:]])\\/usr\\/bin\\/zeroclaw([[:space:]]|$)/ &&
          $0 ~ / daemon([[:space:]]|$)/ {{
            print $1
            exit
          }}
        '
        """
    ).strip()
    result = remote_shell(target, script)
    if result.status != 0:
        raise RsdbError(result.stderr.strip() or "failed to detect zeroclaw PID")
    text = result.stdout.strip()
    if not text:
        raise RsdbError("could not find a running zeroclaw daemon PID on the target")
    try:
        return int(text.splitlines()[-1].strip())
    except ValueError as exc:
        raise RsdbError(f"unexpected PID output: {text}") from exc


def detect_gateway_port(target: str, config_path: str = "/root/.zeroclaw/config.toml") -> int:
    script = textwrap.dedent(
        f"""
        config_path={shell_quote(config_path)}
        if [ ! -f "$config_path" ]; then
          printf '42617\\n'
          exit 0
        fi
        port="$(awk '
          BEGIN {{ in_gateway = 0 }}
          /^\\[gateway\\]/ {{ in_gateway = 1; next }}
          /^\\[/ {{ in_gateway = 0 }}
          in_gateway && $1 == "port" {{
            gsub(/"/, "", $3)
            print $3
            exit
          }}
        ' "$config_path")"
        if [ -n "$port" ]; then
          printf '%s\\n' "$port"
        else
          printf '42617\\n'
        fi
        """
    ).strip()
    result = remote_shell(target, script)
    if result.status != 0:
        raise RsdbError(result.stderr.strip() or "failed to detect gateway port")
    try:
        return int(result.stdout.strip() or "42617")
    except ValueError as exc:
        raise RsdbError(f"unexpected gateway port output: {result.stdout}") from exc


def fetch_remote_tail(target: str, path: str, lines: int) -> str:
    script = textwrap.dedent(
        f"""
        path={shell_quote(path)}
        lines={int(lines)}
        if [ -f "$path" ]; then
          tail -n "$lines" "$path"
        fi
        """
    ).strip()
    return remote_shell(target, script).stdout


def fetch_remote_text(target: str, path: str) -> str:
    script = textwrap.dedent(
        f"""
        path={shell_quote(path)}
        if [ -f "$path" ]; then
          cat "$path"
        fi
        """
    ).strip()
    return remote_shell(target, script).stdout


def fetch_metrics_snapshot(target: str, port: int) -> str:
    script = textwrap.dedent(
        f"""
        port={int(port)}
        if command -v curl >/dev/null 2>&1; then
          curl -fsS "http://127.0.0.1:${{port}}/metrics" 2>/dev/null || true
        elif command -v wget >/dev/null 2>&1; then
          wget -qO- "http://127.0.0.1:${{port}}/metrics" 2>/dev/null || true
        fi
        """
    ).strip()
    return remote_shell(target, script).stdout


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_jsonl(lines: Iterable[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"(\.\d{6})\d+([+-]\d\d:\d\d)$", r"\1\2", normalized)
    normalized = re.sub(r"(\.\d{6})\d+$", r"\1", normalized)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def diff_millis(start: str | None, end: str | None) -> int | None:
    start_ts = parse_iso8601(start)
    end_ts = parse_iso8601(end)
    if start_ts is None or end_ts is None:
        return None
    return int((end_ts - start_ts).total_seconds() * 1000)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
