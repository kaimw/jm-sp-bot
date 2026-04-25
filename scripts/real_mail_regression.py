from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys
import time

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run low-frequency real Tencent Exmail regression through the local admin API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Running jm-sp-bot base URL.")
    parser.add_argument("--username", default="admin", help="Admin username.")
    parser.add_argument("--password", default="admin", help="Admin password.")
    parser.add_argument("--runs", type=int, default=1, help="Number of E2E runs.")
    parser.add_argument("--interval-seconds", type=int, default=60, help="Minimum interval between runs.")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="HTTP timeout per E2E run.")
    parser.add_argument(
        "--report-dir",
        default="data/test-reports/real-mail-regression",
        help="Directory for archived JSON and Markdown reports.",
    )
    return parser.parse_args()


def safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("|", "\\|")


def report_timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def write_reports(report_dir: str, summary: dict) -> tuple[Path, Path]:
    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_timestamp()
    json_path = output_dir / f"real-mail-regression-{stamp}.json"
    md_path = output_dir / f"real-mail-regression-{stamp}.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(summary), encoding="utf-8")
    return json_path, md_path


def render_markdown_report(summary: dict) -> str:
    status = "通过" if summary.get("ok") else "未通过"
    lines = [
        "# 真实企业邮箱回归测试报告",
        "",
        f"- 测试结论：{status}",
        f"- 请求轮次：{summary.get('runs_requested')}",
        f"- 完成轮次：{summary.get('runs_completed')}",
        f"- 发信间隔：{summary.get('interval_seconds')} 秒",
        f"- 目标系统：{summary.get('base_url')}",
        "",
        "## 明细",
        "",
        "| 轮次 | 开始时间 | 结果 | 任务号 | 来源邮件 | 错误 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary.get("results", []):
        result = "通过" if item.get("ok") else "失败"
        lines.append(
            "| "
            + " | ".join(
                [
                    safe_text(item.get("run_index")),
                    safe_text(item.get("started_at")),
                    result,
                    safe_text(item.get("task_no") or item.get("task", {}).get("task_no")),
                    safe_text(item.get("source_message_id") or item.get("mail_message_id")),
                    safe_text(item.get("error") or item.get("response_text")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 原始结果",
            "",
            "```json",
            json.dumps(summary, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    interval = max(60, args.interval_seconds)
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")

    results: list[dict] = []
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=args.timeout_seconds) as client:
        login = client.post("/api/auth/login", json={"username": args.username, "password": args.password})
        login.raise_for_status()

        for index in range(args.runs):
            if index > 0:
                time.sleep(interval)
            started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            try:
                response = client.post("/api/e2e/tencent-mail/run")
                response.raise_for_status()
                payload = response.json()
                payload["started_at"] = started_at
                payload["run_index"] = index + 1
                results.append(payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            except Exception as exc:
                failure = {"ok": False, "run_index": index + 1, "started_at": started_at, "error": str(exc)}
                if isinstance(exc, httpx.HTTPStatusError):
                    failure["response_text"] = exc.response.text[:2000]
                results.append(failure)
                print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
                break

    summary = {
        "ok": all(item.get("ok") for item in results),
        "base_url": args.base_url.rstrip("/"),
        "runs_requested": args.runs,
        "runs_completed": len(results),
        "interval_seconds": interval,
        "results": results,
    }
    json_path, md_path = write_reports(args.report_dir, summary)
    summary["report_files"] = {"json": str(json_path), "markdown": str(md_path)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
