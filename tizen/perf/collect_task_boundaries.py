#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _rsdb_common import (  # noqa: E402
    DEFAULT_OUT_ROOT,
    RsdbError,
    die,
    fetch_remote_tail,
    log,
    parse_iso8601,
    parse_jsonl,
    diff_millis,
    resolve_target,
    slugify_label,
    write_json,
    write_text,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch runtime trace and daemon log tails from a Tizen target, then summarize "
            "recent zeroclaw task/request boundaries from inbound message to completion. "
            "Also emits an OpenClaw-style markdown analysis for the reported completed tasks."
        )
    )
    parser.add_argument(
        "--target",
        help=(
            "Target selector. Accepts either a full rsdb target like 192.168.0.222:27101 "
            "or a bare IP/host like 192.168.0.222, which will be resolved through rsdb discover."
        ),
    )
    parser.add_argument("--probe-addr", help="Probe address to resolve through rsdb discover when --target is omitted.")
    parser.add_argument(
        "--capture-name",
        default="task-boundaries",
        help=(
            "Scenario label used for the output directory and artifact filenames, for example "
            "request-window. Default: task-boundaries."
        ),
    )
    parser.add_argument("--limit-tasks", type=int, default=5, help="Number of recent completed tasks to report. Default: 5.")
    parser.add_argument("--tail-lines", type=int, default=5000, help="Runtime trace tail lines to fetch.")
    parser.add_argument("--log-tail-lines", type=int, default=500, help="Daemon stdout/stderr tail lines to fetch.")
    parser.add_argument("--trace-path", default="/root/.zeroclaw/workspace/state/runtime-trace.jsonl")
    parser.add_argument("--stdout-log-path", default="/root/.zeroclaw/logs/daemon.stdout.log")
    parser.add_argument("--stderr-log-path", default="/root/.zeroclaw/logs/daemon.stderr.log")
    parser.add_argument(
        "--out-dir",
        help=(
            "Output directory. Default: tizen/perf/out/<capture-name>-tb. "
            "The default location is reused on each run."
        ),
    )
    parser.add_argument(
        "--out-dir-mode",
        choices=["overwrite", "fail"],
        default="overwrite",
        help=(
            "Behavior when the output directory already exists and contains files. "
            "Default: overwrite."
        ),
    )
    return parser.parse_args()


def truncate_text(value: str | None, limit: int = 60) -> str:
    text = (value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def escape_md_cell(value: str | None) -> str:
    text = (value or "-").replace("\n", " ").strip()
    if not text:
        return "-"
    return text.replace("|", "\\|")


def format_time(timestamp: str | None) -> str:
    parsed = parse_iso8601(timestamp)
    if parsed is None:
        return "-"
    return parsed.astimezone(timezone.utc).strftime("%H:%M:%S")


def format_duration_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 0.01:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 1:
        return f"{seconds:.2f}s"
    return f"{seconds:.1f}s"


def build_text_detail(raw_text: str | None, *, limit: int = 50) -> str | None:
    preview = truncate_text(raw_text, limit)
    if not preview:
        return None
    return f'text: "{preview}"'


def build_tool_result_detail(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    tool_name = str(payload.get("tool") or "?")
    status = "completed" if event.get("success") is not False else "failed"
    raw_output = payload.get("output") or event.get("message") or ""
    preview = truncate_text(str(raw_output), 40)
    if preview:
        return f'{tool_name} ({status}): "{preview}"'
    return f"{tool_name} ({status})"


def format_tokens_cell(usage: dict[str, int] | None) -> str:
    if not usage:
        return "-"
    return f'{usage["input"]}/{usage["output"]}'


def build_turn_timelines(
    events: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    turn_lookup = {str(turn["turn_id"]): turn for turn in turns}
    turn_events: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for event in events:
        turn_id = event.get("turn_id")
        if not turn_id:
            continue
        if str(turn_id) not in turn_lookup:
            continue
        event_type = event.get("event_type", "")
        if event_type in {"llm_response", "tool_call_start", "tool_call_result"}:
            turn_events[str(turn_id)].append(event)

    timelines: dict[str, list[dict[str, Any]]] = {}
    for turn in turns:
        turn_id = str(turn["turn_id"])
        steps: list[dict[str, Any]] = []
        if turn.get("request_start_timestamp"):
            steps.append(
                {
                    "type": "User Prompt",
                    "timestamp": turn["request_start_timestamp"],
                    "detail": f'"{truncate_text(str(turn.get("inbound_preview") or "unknown"), 50)}"',
                    "usage": None,
                    "cost": None,
                }
            )

        ordered_events = sorted(
            turn_events.get(turn_id, []),
            key=lambda item: parse_iso8601(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        llm_response_positions = [
            index for index, item in enumerate(ordered_events) if item.get("event_type") == "llm_response"
        ]

        for index, event in enumerate(ordered_events):
            event_type = event.get("event_type", "")
            payload = event.get("payload") or {}
            if event_type == "tool_call_result":
                steps.append(
                    {
                        "type": "Tool Result",
                        "timestamp": event.get("timestamp"),
                        "detail": build_tool_result_detail(event),
                        "usage": None,
                        "cost": None,
                    }
                )
                continue

            if event_type != "llm_response":
                continue

            next_llm_index = None
            for candidate in llm_response_positions:
                if candidate > index:
                    next_llm_index = candidate
                    break
            slice_end = next_llm_index if next_llm_index is not None else len(ordered_events)
            tool_names: list[str] = []
            for future_event in ordered_events[index + 1 : slice_end]:
                if future_event.get("event_type") != "tool_call_start":
                    continue
                future_payload = future_event.get("payload") or {}
                tool_name = str(future_payload.get("tool") or "").strip()
                if tool_name and tool_name not in tool_names:
                    tool_names.append(tool_name)

            text_detail = build_text_detail(str(payload.get("raw_response") or ""), limit=40)
            detail_parts: list[str] = []
            if text_detail:
                detail_parts.append(text_detail)
            for tool_name in tool_names:
                detail_parts.append(f"toolCall: {tool_name}")
            if not detail_parts:
                detail_parts.append("inference")

            is_last_llm = index == llm_response_positions[-1] if llm_response_positions else False
            step_type = "Response to User" if is_last_llm and not tool_names else "Inference"
            if step_type == "Response to User" and not text_detail:
                response_text = turn.get("response_preview") or turn.get("final_response_text") or ""
                detail_parts = [build_text_detail(str(response_text), limit=40) or "text: \"-\""]

            steps.append(
                {
                    "type": step_type,
                    "timestamp": event.get("timestamp"),
                    "detail": " + ".join(detail_parts),
                    "usage": {
                        "input": int(payload.get("input_tokens") or 0),
                        "output": int(payload.get("output_tokens") or 0),
                    },
                    "cost": None,
                }
            )

        ordered_steps = sorted(
            steps,
            key=lambda item: parse_iso8601(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        for position, step in enumerate(ordered_steps):
            if position == len(ordered_steps) - 1:
                step["duration_seconds"] = None
                continue
            current_ts = parse_iso8601(step.get("timestamp"))
            next_ts = parse_iso8601(ordered_steps[position + 1].get("timestamp"))
            if current_ts is None or next_ts is None:
                step["duration_seconds"] = None
            else:
                step["duration_seconds"] = (next_ts - current_ts).total_seconds()
        timelines[turn_id] = ordered_steps

    return timelines


def compute_markdown_summary(turn: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    inference_count = 0
    tool_count = 0
    total_input = 0
    total_output = 0

    for step in steps:
        if step["type"] in {"Inference", "Response to User"}:
            inference_count += 1
            usage = step.get("usage") or {}
            total_input += int(usage.get("input") or 0)
            total_output += int(usage.get("output") or 0)
        elif step["type"] == "Tool Result":
            tool_count += 1

    total_tokens = total_input + total_output
    total_time_seconds = None
    if steps:
        start_ts = parse_iso8601(steps[0].get("timestamp"))
        end_ts = parse_iso8601(steps[-1].get("timestamp"))
        if start_ts is not None and end_ts is not None:
            total_time_seconds = (end_ts - start_ts).total_seconds()
    if total_time_seconds is None and turn.get("wall_time_ms") is not None:
        total_time_seconds = float(turn["wall_time_ms"]) / 1000.0

    return {
        "turn_id": turn["turn_id"],
        "model": f'{turn.get("provider") or "-"} / {turn.get("model") or "-"}',
        "user_prompt": f'"{truncate_text(str(turn.get("inbound_preview") or "unknown"), 70)}"',
        "total_time_seconds": total_time_seconds,
        "inference_count": inference_count,
        "tool_count": tool_count,
        "total_tokens": total_tokens,
        "total_input": total_input,
        "total_output": total_output,
        "window": f'{turn.get("request_start_timestamp") or "-"} -> {turn.get("request_end_timestamp") or "-"}',
    }


def render_markdown_analysis_section(
    turn: dict[str, Any],
    steps: list[dict[str, Any]],
    *,
    heading: str,
) -> list[str]:
    summary = compute_markdown_summary(turn, steps)
    lines = [
        heading,
        "",
        "### Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| User Prompt | {escape_md_cell(summary['user_prompt'])} |",
        f"| Turn ID | {escape_md_cell(summary['turn_id'])} |",
        f"| Model | {escape_md_cell(summary['model'])} |",
        f"| Window | {escape_md_cell(summary['window'])} |",
        f"| Total Time | {format_duration_seconds(summary['total_time_seconds'])} |",
        f"| Inference Calls | {summary['inference_count']} |",
        f"| Tool Calls | {summary['tool_count']} |",
        (
            f"| Total Tokens | {summary['total_tokens']:,} "
            f"(input: {summary['total_input']:,}, output: {summary['total_output']:,}) |"
        ),
        "",
        "### Step-by-Step Timeline",
        "",
        "| # | Type | Timestamp | Duration | Detail | Tokens (in/out) |",
        "|---|------|-----------|----------|--------|-----------------|",
    ]

    for index, step in enumerate(steps, 1):
        lines.append(
            f"| {index} | {escape_md_cell(step['type'])} | {format_time(step.get('timestamp'))} "
            f"| {format_duration_seconds(step.get('duration_seconds'))} "
            f"| {escape_md_cell(step.get('detail'))} | {format_tokens_cell(step.get('usage'))} |"
        )
    return lines


def render_markdown_analysis(
    capture_name: str,
    turns: list[dict[str, Any]],
    timelines: dict[str, list[dict[str, Any]]],
) -> str:
    lines = [
        "# ZeroClaw Task Analysis",
        "",
        f"capture_name: `{capture_name}`",
        "",
        f"reported_tasks: {len(turns)}",
        "",
    ]

    for index, turn in enumerate(turns, 1):
        if index > 1:
            lines.append("")
        lines.extend(
            render_markdown_analysis_section(
                turn,
                timelines.get(str(turn["turn_id"]), []),
                heading=f"## Task {index}",
            )
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This report is derived from ZeroClaw `runtime-trace.jsonl` plus daemon log tails.",
            "- `input_tokens` and `output_tokens` come from `llm_response` events in the trace.",
            "- Timeline durations are deltas between consecutive visible steps.",
            "",
        ]
    )
    return "\n".join(lines)


def summarize_turns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending_inbound: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    pending_outbound: dict[tuple[str, str, str], deque[str]] = defaultdict(deque)
    turns: dict[str, dict[str, Any]] = {}

    for event in events:
        event_type = event.get("event_type", "")
        channel = str(event.get("channel") or "")
        provider = str(event.get("provider") or "")
        model = str(event.get("model") or "")
        payload = event.get("payload") or {}
        timestamp = event.get("timestamp")

        if event_type == "channel_message_inbound":
            pending_inbound[channel].append(
                {
                    "timestamp": timestamp,
                    "sender": payload.get("sender"),
                    "message_id": payload.get("message_id"),
                    "content_preview": payload.get("content_preview"),
                }
            )
            continue

        if event_type == "channel_message_outbound":
            key = (channel, provider, model)
            if pending_outbound[key]:
                turn_id = pending_outbound[key].popleft()
                turn = turns[turn_id]
                turn["outbound_timestamp"] = timestamp
                elapsed_ms = payload.get("elapsed_ms")
                if isinstance(elapsed_ms, int):
                    turn["channel_elapsed_ms"] = elapsed_ms
                turn["response_preview"] = payload.get("response")
            continue

        turn_id = event.get("turn_id")
        if not turn_id:
            continue

        turn = turns.setdefault(
            str(turn_id),
            {
                "turn_id": str(turn_id),
                "channel": channel,
                "provider": provider,
                "model": model,
                "first_event_timestamp": timestamp,
                "last_event_timestamp": timestamp,
                "first_llm_request_timestamp": None,
                "final_response_timestamp": None,
                "inbound_timestamp": None,
                "channel_elapsed_ms": None,
                "llm_duration_total_ms": 0,
                "tool_duration_total_ms": 0,
                "llm_requests": 0,
                "tool_calls": 0,
                "tool_failures": 0,
                "iterations_max": 0,
                "input_tokens_total": 0,
                "output_tokens_total": 0,
                "tool_names": [],
                "success": None,
                "inbound_preview": None,
                "response_preview": None,
                "final_response_text": None,
            },
        )

        turn["last_event_timestamp"] = timestamp
        if not turn["first_event_timestamp"]:
            turn["first_event_timestamp"] = timestamp

        iteration = payload.get("iteration")
        if isinstance(iteration, int):
            turn["iterations_max"] = max(turn["iterations_max"], iteration)

        if event_type == "llm_request":
            turn["llm_requests"] += 1
            if not turn["first_llm_request_timestamp"]:
                turn["first_llm_request_timestamp"] = timestamp
                if channel and pending_inbound[channel]:
                    inbound = pending_inbound[channel].popleft()
                    turn["inbound_timestamp"] = inbound.get("timestamp")
                    turn["inbound_preview"] = inbound.get("content_preview")

        elif event_type == "llm_response":
            duration_ms = payload.get("duration_ms")
            if isinstance(duration_ms, int):
                turn["llm_duration_total_ms"] += duration_ms
            input_tokens = payload.get("input_tokens")
            output_tokens = payload.get("output_tokens")
            if isinstance(input_tokens, int):
                turn["input_tokens_total"] += input_tokens
            if isinstance(output_tokens, int):
                turn["output_tokens_total"] += output_tokens

        elif event_type == "tool_call_start":
            turn["tool_calls"] += 1
            tool_name = payload.get("tool")
            if isinstance(tool_name, str) and tool_name and tool_name not in turn["tool_names"]:
                turn["tool_names"].append(tool_name)

        elif event_type == "tool_call_result":
            duration_ms = payload.get("duration_ms")
            if isinstance(duration_ms, int):
                turn["tool_duration_total_ms"] += duration_ms
            if event.get("success") is False:
                turn["tool_failures"] += 1
            tool_name = payload.get("tool")
            if isinstance(tool_name, str) and tool_name and tool_name not in turn["tool_names"]:
                turn["tool_names"].append(tool_name)

        elif event_type == "turn_final_response":
            turn["final_response_timestamp"] = timestamp
            if isinstance(event.get("success"), bool):
                turn["success"] = bool(event["success"])
            final_text = payload.get("text")
            if isinstance(final_text, str) and final_text:
                turn["final_response_text"] = final_text
            pending_outbound[(channel, provider, model)].append(str(turn_id))

    summaries = []
    for turn in turns.values():
        request_start = turn["inbound_timestamp"] or turn["first_llm_request_timestamp"] or turn["first_event_timestamp"]
        request_end = turn["outbound_timestamp"] if turn.get("outbound_timestamp") else (
            turn["final_response_timestamp"] or turn["last_event_timestamp"]
        )
        wall_time_ms = turn["channel_elapsed_ms"]
        if wall_time_ms is None:
            wall_time_ms = diff_millis(request_start, request_end)
        turn["request_start_timestamp"] = request_start
        turn["request_end_timestamp"] = request_end
        turn["wall_time_ms"] = wall_time_ms
        summaries.append(turn)

    summaries.sort(
        key=lambda item: (
            parse_iso8601(item.get("request_end_timestamp"))
            or parse_iso8601(item.get("last_event_timestamp"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
    )
    return summaries


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "turn_id",
        "channel",
        "provider",
        "model",
        "request_start_timestamp",
        "request_end_timestamp",
        "wall_time_ms",
        "llm_duration_total_ms",
        "tool_duration_total_ms",
        "tool_calls",
        "tool_failures",
        "iterations_max",
        "input_tokens_total",
        "output_tokens_total",
        "success",
        "tool_names",
        "inbound_preview",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            serializable = dict(row)
            serializable["tool_names"] = ",".join(row.get("tool_names", []))
            writer.writerow({name: serializable.get(name) for name in columns})


def render_report(capture_name: str, turns: list[dict[str, Any]]) -> str:
    lines = [
        f"capture_name: {capture_name}",
        f"completed_tasks: {len(turns)}",
        "boundary_source: runtime_trace.jsonl",
        "daemon_logs_collected: yes",
        "",
        "field_glossary:",
        "  wall_time_ms: end-to-end turn time from inbound message timestamp to outbound completion when available.",
        "  llm_duration_total_ms: sum of all runtime-trace llm_response durations inside the turn.",
        "  tool_duration_total_ms: sum of all runtime-trace tool_call_result durations inside the turn.",
        "  tool_calls: number of tool_call_start events seen for the turn.",
        "  tool_failures: number of tool_call_result events marked unsuccessful.",
        "  iterations_max: highest tool-loop iteration observed for the turn.",
        "  input_tokens_total: sum of input_tokens reported by llm_response events in the turn.",
        "  output_tokens_total: sum of output_tokens reported by llm_response events in the turn.",
        "",
    ]
    for turn in turns:
        lines.extend(
            [
                f"turn_id: {turn['turn_id']}",
                f"  channel: {turn['channel']}",
                f"  model: {turn['provider']} / {turn['model']}",
                f"  window: {turn['request_start_timestamp']} -> {turn['request_end_timestamp']}",
                f"  wall_time_ms: {turn['wall_time_ms']}",
                f"  llm_duration_total_ms: {turn['llm_duration_total_ms']}",
                f"  tool_duration_total_ms: {turn['tool_duration_total_ms']}",
                f"  tool_calls: {turn['tool_calls']}",
                f"  tool_failures: {turn['tool_failures']}",
                f"  iterations_max: {turn['iterations_max']}",
                f"  input_tokens_total: {turn['input_tokens_total']}",
                f"  output_tokens_total: {turn['output_tokens_total']}",
                f"  success: {turn['success']}",
                f"  tools: {', '.join(turn.get('tool_names', [])) or '-'}",
                f"  inbound_preview: {turn.get('inbound_preview') or '-'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_terminal_summary(
    capture_name: str,
    turns: list[dict[str, Any]],
    *,
    analysis_path: Path | None,
    summary_path: Path,
    out_dir: Path,
) -> str:
    lines = [
        f"capture_name: {capture_name}",
        f"reported_tasks: {len(turns)}",
    ]
    if turns:
        latest = turns[-1]
        lines.extend(
            [
                f"latest_turn_id: {latest['turn_id']}",
                f"latest_prompt: {latest.get('inbound_preview') or '-'}",
                f"latest_model: {latest.get('provider') or '-'} / {latest.get('model') or '-'}",
                f"latest_window: {latest.get('request_start_timestamp') or '-'} -> {latest.get('request_end_timestamp') or '-'}",
                f"latest_wall_time_ms: {latest.get('wall_time_ms')}",
                f"latest_tool_calls: {latest.get('tool_calls')}",
            ]
        )
    else:
        lines.append("latest_turn_id: -")
    lines.append(f"summary_txt: {summary_path}")
    if analysis_path is not None:
        lines.append(f"analysis_md: {analysis_path}")
    lines.append(f"artifacts: {out_dir}")
    return "\n".join(lines)


def resolve_out_dir(explicit: str | None, capture_slug: str, mode: str) -> Path:
    if explicit:
        out_dir = Path(explicit).expanduser().resolve()
    else:
        out_dir = (DEFAULT_OUT_ROOT / f"{capture_slug}-tb").resolve()
    existed = out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    if mode == "fail" and existed and any(out_dir.iterdir()):
        die(f"output directory already exists and is not empty: {out_dir}")
    return out_dir


def main() -> None:
    args = parse_args()
    if args.limit_tasks <= 0:
        raise SystemExit("error: --limit-tasks must be positive")
    if args.tail_lines <= 0:
        raise SystemExit("error: --tail-lines must be positive")
    if args.log_tail_lines <= 0:
        raise SystemExit("error: --log-tail-lines must be positive")

    try:
        target = resolve_target(args.target, args.probe_addr)
        capture_slug = slugify_label(args.capture_name, fallback="task-boundaries")
        out_dir = resolve_out_dir(args.out_dir, capture_slug, args.out_dir_mode)

        log(f"target: {target}")
        log(f"capture name: {capture_slug}")
        log(f"out dir mode: {args.out_dir_mode}")
        log(f"writing artifacts into {out_dir}")

        runtime_trace_tail = fetch_remote_tail(target, args.trace_path, args.tail_lines)
        if not runtime_trace_tail.strip():
            raise RsdbError(f"no runtime trace data returned from {args.trace_path}")
        stdout_tail = fetch_remote_tail(target, args.stdout_log_path, args.log_tail_lines)
        stderr_tail = fetch_remote_tail(target, args.stderr_log_path, args.log_tail_lines)

        write_text(out_dir / f"{capture_slug}-runtime-trace.tail.jsonl", runtime_trace_tail)
        write_text(out_dir / f"{capture_slug}-daemon.stdout.tail.log", stdout_tail)
        write_text(out_dir / f"{capture_slug}-daemon.stderr.tail.log", stderr_tail)

        events = parse_jsonl(runtime_trace_tail.splitlines())
        turns = summarize_turns(events)
        completed_turns = [turn for turn in turns if turn.get("request_end_timestamp")]
        recent_turns = completed_turns[-args.limit_tasks :]
        timelines = build_turn_timelines(events, recent_turns)

        write_json(
            out_dir / f"{capture_slug}-task-boundaries.json",
            {
                "capture_name": capture_slug,
                "target": target,
                "boundary_source": "runtime_trace.jsonl",
                "daemon_logs_collected": True,
                "turns": recent_turns,
            },
        )
        write_csv(out_dir / f"{capture_slug}-task-boundaries.csv", recent_turns)
        summary_path = out_dir / f"{capture_slug}-task-boundaries.txt"
        report_text = render_report(capture_slug, recent_turns)
        write_text(summary_path, report_text)
        analysis_path: Path | None = None
        if recent_turns:
            markdown_text = render_markdown_analysis(capture_slug, recent_turns, timelines)
            analysis_path = out_dir / f"{capture_slug}-analysis.md"
            write_text(analysis_path, markdown_text)
        print(
            render_terminal_summary(
                capture_slug,
                recent_turns,
                analysis_path=analysis_path,
                summary_path=summary_path,
                out_dir=out_dir,
            )
        )
    except RsdbError as exc:
        raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    main()
