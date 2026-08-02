#!/usr/bin/env python3
"""Client for the local tasklist Codex API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:5000/api/codex"


class TasklistClientError(RuntimeError):
    pass


def api_request(method: str, path: str, payload: dict | None = None) -> dict:
    base_url = os.environ.get("TASKLIST_API_URL", DEFAULT_BASE_URL).rstrip("/")
    url = f"{base_url}/{path.lstrip('/')}"
    data = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    token = os.environ.get("CODEX_TASK_API_TOKEN", "").strip()
    if token:
        headers["X-Codex-Task-Token"] = token

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("error", raw)
        except json.JSONDecodeError:
            detail = raw or exc.reason
        raise TasklistClientError(f"Task API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TasklistClientError(
            "Task app is not reachable at "
            f"{base_url}. Start C:\\Users\\canmi\\Documents\\GitHub\\tasklist\\tasklist\\task.bat first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise TasklistClientError("Task API returned invalid JSON.") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate the local tasklist app for Codex.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("health", help="Check whether the task app API is available.")

    list_parser = commands.add_parser("list", help="List tasks.")
    list_parser.add_argument("--status", choices=("open", "completed", "all"), default="open")

    add_parser = commands.add_parser("add", help="Add a task.")
    add_parser.add_argument("title")
    add_parser.add_argument("--tag")
    add_parser.add_argument("--score", type=int, choices=(30, 40, 50, 60, 70, 80, 90, 100))
    add_parser.add_argument("--due", dest="due_date")
    add_parser.add_argument("--recur", choices=("none", "weekly", "monthly"))
    add_parser.add_argument("--parent-id", type=int)

    complete_parser = commands.add_parser("complete", help="Complete an open task by ID.")
    complete_parser.add_argument("task_id", type=int)

    reopen_parser = commands.add_parser("reopen", help="Reopen a task by ID.")
    reopen_parser.add_argument("task_id", type=int)

    return parser


def run_command(args: argparse.Namespace) -> dict:
    if args.command == "health":
        return api_request("GET", "health")

    if args.command == "list":
        query = urllib.parse.urlencode({"status": args.status})
        return api_request("GET", f"tasks?{query}")

    if args.command == "add":
        payload = {"title": args.title}
        for key in ("tag", "score", "due_date", "recur", "parent_id"):
            value = getattr(args, key)
            if value is not None:
                payload[key] = value
        return api_request("POST", "tasks", payload)

    if args.command == "complete":
        return api_request("POST", f"tasks/{args.task_id}/complete", {})

    if args.command == "reopen":
        return api_request("POST", f"tasks/{args.task_id}/reopen", {})

    raise TasklistClientError(f"Unsupported command: {args.command}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = run_command(args)
    except TasklistClientError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
