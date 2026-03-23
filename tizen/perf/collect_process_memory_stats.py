#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _rsdb_common import (  # noqa: E402
    DEFAULT_OUT_ROOT,
    RsdbError,
    detect_zeroclaw_pid,
    die,
    log,
    parse_iso8601,
    remote_shell,
    resolve_target,
    shell_quote,
    slugify_label,
    stream_remote_shell_to_path,
    write_json,
    write_text,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sample process-memory stats for a running zeroclaw daemon on a Tizen target "
            "using rsdb agent exec --stream."
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
    parser.add_argument("--service-name", default="zeroclaw", help="Systemd service name. Default: zeroclaw.")
    parser.add_argument(
        "--capture-name",
        default="process-memory",
        help=(
            "Scenario label used for the output directory and artifact filenames, for example idle "
            "or request-window. Default: process-memory."
        ),
    )
    parser.add_argument("--duration-secs", type=int, default=60, help="Total capture duration. Default: 60.")
    parser.add_argument("--interval-secs", type=int, default=1, help="Sampling interval in seconds. Default: 1.")
    parser.add_argument(
        "--out-dir",
        help=(
            "Output directory. Default: tizen/perf/out/<capture-name>-ps. "
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


def build_remote_sampler(service_name: str, samples: int, interval_secs: int) -> str:
    return textwrap.dedent(
        f"""
        service_name={shell_quote(service_name)}
        samples={samples}
        interval_secs={interval_secs}

        resolve_pid() {{
          main_pid=""
          if command -v systemctl >/dev/null 2>&1; then
            main_pid="$(systemctl show -p MainPID --value "${{service_name}}.service" 2>/dev/null || true)"
          fi
          if [ -n "$main_pid" ] && [ "$main_pid" != "0" ] && [ -d "/proc/$main_pid" ]; then
            printf '%s\\n' "$main_pid"
            return 0
          fi
          ps -eo pid=,args= | awk '
            $0 ~ /(^|[[:space:]])\\/usr\\/bin\\/zeroclaw([[:space:]]|$)/ &&
            $0 ~ / daemon([[:space:]]|$)/ {{
              print $1
              exit
            }}
          '
        }}

        printf 'timestamp,pid,rss_kb,pss_kb,swap_kb,anon_kb,threads,vmhwm_kb\\n'
        i=0
        while [ "$i" -lt "$samples" ]; do
          ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
          pid="$(resolve_pid)"
          if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
            status_path="/proc/$pid/status"
            rollup_path="/proc/$pid/smaps_rollup"
            rss="$(awk '/^VmRSS:/ {{ print $2 }}' "$status_path" 2>/dev/null)"
            swap="$(awk '/^VmSwap:/ {{ print $2 }}' "$status_path" 2>/dev/null)"
            threads="$(awk '/^Threads:/ {{ print $2 }}' "$status_path" 2>/dev/null)"
            vmhwm="$(awk '/^VmHWM:/ {{ print $2 }}' "$status_path" 2>/dev/null)"
            pss=""
            anon=""
            if [ -r "$rollup_path" ]; then
              pss="$(awk '/^Pss:/ {{ print $2 }}' "$rollup_path" 2>/dev/null)"
              anon="$(awk '/^Anonymous:/ {{ print $2 }}' "$rollup_path" 2>/dev/null)"
            fi
            printf '%s,%s,%s,%s,%s,%s,%s,%s\\n' \
              "$ts" "$pid" "${{rss:-}}" "${{pss:-}}" "${{swap:-}}" "${{anon:-}}" "${{threads:-}}" "${{vmhwm:-}}"
          else
            printf '%s,,,,,,,\\n' "$ts"
          fi
          i=$((i + 1))
          if [ "$i" -lt "$samples" ]; then
            sleep "$interval_secs"
          fi
        done
        """
    ).strip()


def summarize_samples(csv_path: Path) -> dict[str, object]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    numeric_fields = ["rss_kb", "pss_kb", "swap_kb", "anon_kb", "threads", "vmhwm_kb"]
    valid_rows = []
    pids: list[int] = []
    for row in rows:
        if not row.get("pid"):
            continue
        parsed: dict[str, int | str | None] = {"timestamp": row["timestamp"], "pid": int(row["pid"])}
        pids.append(int(row["pid"]))
        for field in numeric_fields:
            value = row.get(field, "").strip()
            parsed[field] = int(value) if value else None
        valid_rows.append(parsed)

    if not valid_rows:
        raise RsdbError("no valid samples were collected")

    summary: dict[str, object] = {
        "samples_total": len(rows),
        "samples_valid": len(valid_rows),
        "pids_seen": sorted(set(pids)),
        "first_timestamp": valid_rows[0]["timestamp"],
        "last_timestamp": valid_rows[-1]["timestamp"],
    }

    for field in numeric_fields:
        series = [int(row[field]) for row in valid_rows if row[field] is not None]
        last_value = next((row[field] for row in reversed(valid_rows) if row[field] is not None), None)
        summary[field] = {
            "samples_present": len(series),
            "min": min(series) if series else None,
            "avg": round(statistics.fmean(series), 2) if series else None,
            "max": max(series) if series else None,
            "last": int(last_value) if last_value is not None else None,
        }
    return summary


def format_mb(kb_value: float | int | None) -> str:
    if kb_value is None:
        return "-"
    return f"{float(kb_value) / 1024.0:.1f} MB"


def format_count(value: float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60.0:.1f}m"
    return f"{seconds / 3600.0:.1f}h"


def compute_window_duration_seconds(summary: dict[str, object]) -> float | None:
    start_ts = parse_iso8601(str(summary.get("first_timestamp") or ""))
    end_ts = parse_iso8601(str(summary.get("last_timestamp") or ""))
    if start_ts is None or end_ts is None:
        return None
    return max((end_ts - start_ts).total_seconds(), 0.0)


def build_executive_metrics(summary: dict[str, object]) -> dict[str, object]:
    footprint_field = "pss_kb" if summary["pss_kb"]["samples_present"] > 0 else "rss_kb"
    return {
        "footprint_field": footprint_field,
        "footprint_avg_mb": format_mb(summary[footprint_field]["avg"]),
        "footprint_peak_mb": format_mb(summary[footprint_field]["max"]),
        "footprint_last_mb": format_mb(summary[footprint_field]["last"]),
        "rss_avg_mb": format_mb(summary["rss_kb"]["avg"]),
        "rss_peak_mb": format_mb(summary["rss_kb"]["max"]),
        "rss_last_mb": format_mb(summary["rss_kb"]["last"]),
        "swap_avg_mb": format_mb(summary["swap_kb"]["avg"]),
        "swap_peak_mb": format_mb(summary["swap_kb"]["max"]),
        "swap_last_mb": format_mb(summary["swap_kb"]["last"]),
        "threads_avg_raw": summary["threads"]["avg"],
        "threads_peak_raw": summary["threads"]["max"],
        "threads_avg": format_count(summary["threads"]["avg"]),
        "threads_peak": format_count(summary["threads"]["max"]),
        "threads_last": format_count(summary["threads"]["last"]),
        "restarted": len(summary["pids_seen"]) > 1,
        "window_duration_seconds": compute_window_duration_seconds(summary),
    }


def render_summary(summary: dict[str, object]) -> str:
    executive = build_executive_metrics(summary)
    swap_max_kb = summary["swap_kb"]["max"] or 0
    footprint_note = (
        "Estimated footprint is based on shared-adjusted memory samples, which is the best single number "
        "for comparing idle versus busy captures."
        if executive["footprint_field"] == "pss_kb"
        else "Estimated footprint falls back to resident RAM because shared-adjusted memory samples were not available."
    )
    swap_note = (
        "The process had memory swapped out during this window, so resident RAM alone understates total memory impact."
        if swap_max_kb > 0
        else "No swap activity was observed during this window."
    )
    executive_sentence = (
        f"During this `{summary['capture_name']}` capture, ZeroClaw typically used about "
        f"{executive['footprint_avg_mb']} of effective memory footprint and peaked at "
        f"{executive['footprint_peak_mb']}. Physical RAM residency averaged {executive['rss_avg_mb']} "
        f"and peaked at {executive['rss_peak_mb']}. "
    )
    if swap_max_kb > 0:
        executive_sentence += (
            f"The OS had about {executive['swap_avg_mb']} swapped out on average, with a peak of "
            f"{executive['swap_peak_mb']}. "
        )
    else:
        executive_sentence += "No memory was swapped out during the capture. "
    thread_peak_suffix = ""
    if executive["threads_avg_raw"] != executive["threads_peak_raw"]:
        thread_peak_suffix = f" and peaked at {executive['threads_peak']}"
    executive_sentence += (
        f"Thread count stayed around {executive['threads_avg']}"
        f"{thread_peak_suffix}."
    )

    lines = [
        "# ZeroClaw Process Memory Report",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Capture | {summary['capture_name']} |",
        f"| Window | {summary['first_timestamp']} -> {summary['last_timestamp']} |",
        f"| Duration | {format_duration(executive['window_duration_seconds'])} |",
        f"| Samples | {summary['samples_valid']} valid out of {summary['samples_total']} |",
        f"| Process Restarts | {'Yes' if executive['restarted'] else 'No'} |",
        "",
        "## Executive Summary",
        "",
        executive_sentence,
        "",
        "## Memory Snapshot",
        "",
        "| Measure | Typical | Peak | Latest |",
        "|---------|---------|------|--------|",
        f"| Estimated footprint | {executive['footprint_avg_mb']} | {executive['footprint_peak_mb']} | {executive['footprint_last_mb']} |",
        f"| Resident RAM | {executive['rss_avg_mb']} | {executive['rss_peak_mb']} | {executive['rss_last_mb']} |",
        f"| Swapped out | {executive['swap_avg_mb']} | {executive['swap_peak_mb']} | {executive['swap_last_mb']} |",
        f"| Threads | {executive['threads_avg']} | {executive['threads_peak']} | {executive['threads_last']} |",
        "",
        "## Notes",
        "",
        f"- {footprint_note}",
        f"- {swap_note}",
        "- The CSV and JSON files in this folder still contain the raw engineering data if deeper debugging is needed.",
        "",
    ]
    return "\n".join(lines)


def render_terminal_summary(
    summary: dict[str, object],
    *,
    summary_path: Path,
    analysis_path: Path,
    out_dir: Path,
) -> str:
    executive = build_executive_metrics(summary)
    lines = [
        f"capture_name: {summary['capture_name']}",
        f"window: {summary['first_timestamp']} -> {summary['last_timestamp']}",
        f"samples: {summary['samples_valid']} valid out of {summary['samples_total']}",
        f"estimated_footprint_typical: {executive['footprint_avg_mb']}",
        f"estimated_footprint_peak: {executive['footprint_peak_mb']}",
        f"resident_ram_peak: {executive['rss_peak_mb']}",
        f"swapped_out_peak: {executive['swap_peak_mb']}",
        f"summary_txt: {summary_path}",
        f"analysis_md: {analysis_path}",
        f"artifacts: {out_dir}",
    ]
    return "\n".join(lines)


def resolve_out_dir(explicit: str | None, capture_slug: str, mode: str) -> Path:
    if explicit:
        out_dir = Path(explicit).expanduser().resolve()
    else:
        out_dir = (DEFAULT_OUT_ROOT / f"{capture_slug}-ps").resolve()
    existed = out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    if mode == "fail" and existed and any(out_dir.iterdir()):
        die(f"output directory already exists and is not empty: {out_dir}")
    return out_dir


def main() -> None:
    args = parse_args()
    if args.duration_secs <= 0:
        die("--duration-secs must be positive")
    if args.interval_secs <= 0:
        die("--interval-secs must be positive")

    try:
        target = resolve_target(args.target, args.probe_addr)
        capture_slug = slugify_label(args.capture_name, fallback="process-memory")
        out_dir = resolve_out_dir(args.out_dir, capture_slug, args.out_dir_mode)
        sample_count = args.duration_secs // args.interval_secs
        if sample_count <= 0:
            die("--duration-secs must be at least --interval-secs")

        initial_pid = detect_zeroclaw_pid(target, args.service_name)
        metadata = {
            "capture_name": capture_slug,
            "target": target,
            "service_name": args.service_name,
            "duration_secs": args.duration_secs,
            "interval_secs": args.interval_secs,
            "initial_pid": initial_pid,
        }
        write_json(out_dir / f"{capture_slug}-metadata.json", metadata)

        log(f"target: {target}")
        log(f"capture name: {capture_slug}")
        log(f"out dir mode: {args.out_dir_mode}")
        log(f"initial zeroclaw pid: {initial_pid}")
        log(f"capturing {sample_count} samples into {out_dir}")

        sample_file = out_dir / f"{capture_slug}-samples.csv"
        stream_timeout_secs = max(args.duration_secs + args.interval_secs + 120, 300)
        stream_remote_shell_to_path(
            target,
            build_remote_sampler(args.service_name, sample_count, args.interval_secs),
            sample_file,
            timeout_secs=stream_timeout_secs,
        )

        final_pid = detect_zeroclaw_pid(target, args.service_name)
        write_text(
            out_dir / f"{capture_slug}-pmap-start.txt",
            remote_shell(target, f"pmap -x {initial_pid} 2>/dev/null || true").stdout,
        )
        write_text(
            out_dir / f"{capture_slug}-pmap-end.txt",
            remote_shell(target, f"pmap -x {final_pid} 2>/dev/null || true").stdout,
        )

        summary = summarize_samples(sample_file)
        summary["capture_name"] = capture_slug
        summary["final_pid"] = final_pid
        summary["interval_secs"] = args.interval_secs
        summary["duration_secs_requested"] = args.duration_secs
        write_json(out_dir / f"{capture_slug}-summary.json", summary)
        report_text = render_summary(summary)
        summary_path = out_dir / f"{capture_slug}-summary.txt"
        analysis_path = out_dir / f"{capture_slug}-analysis.md"
        write_text(summary_path, report_text)
        write_text(analysis_path, report_text)
        print(
            render_terminal_summary(
                summary,
                summary_path=summary_path,
                analysis_path=analysis_path,
                out_dir=out_dir,
            )
        )
    except RsdbError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
