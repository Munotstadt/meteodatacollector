#!/usr/bin/env python3
"""
Haengt einen Eintrag an logs/turso-run-log.md an: welcher Turso-Workflow
wann gelaufen ist, wie ausgeloest (manuell/Zeitplan) und mit klickbarem
Link zum konkreten Actions-Lauf. Wird als letzter Schritt in jedem
Turso-Workflow aufgerufen (analog zum Run-Log bei sql-munotstadt-securities).

Nutzt von GitHub Actions automatisch gesetzte Umgebungsvariablen:
  GITHUB_WORKFLOW, GITHUB_EVENT_NAME, GITHUB_RUN_ID, GITHUB_RUN_NUMBER,
  GITHUB_SERVER_URL, GITHUB_REPOSITORY
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

MAX_ROWS = 200  # verhindert unbegrenztes Wachstum der Log-Datei

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "turso-run-log.md"

HEADER = [
    "| Workflow | Datum/Zeit (UTC) | Trigger | Run |\n",
    "|---|---|---|---|\n",
]

TRIGGER_LABELS = {
    "workflow_dispatch": "Manual",
    "schedule": "Scheduled",
}


def trigger_label(event_name: str) -> str:
    return TRIGGER_LABELS.get(event_name, event_name)


def run_link(server_url: str, repository: str, run_id: str, run_number: str) -> str:
    url = f"{server_url}/{repository}/actions/runs/{run_id}"
    return f"[#{run_number} ↗]({url})"


def load_existing():
    if not LOG_PATH.exists():
        return HEADER, []
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) >= 2 and lines[0].lstrip().startswith("|") and "---" in lines[1]:
        return lines[:2], lines[2:]
    return HEADER, []


def main() -> None:
    workflow_name = os.environ.get("GITHUB_WORKFLOW", "unbekannt")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "unbekannt")
    run_id = os.environ.get("GITHUB_RUN_ID", "0")
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "0")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")

    timestamp = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")
    trigger = trigger_label(event_name)
    link = run_link(server_url, repository, run_id, run_number)

    new_row = f"| {workflow_name} | {timestamp} | {trigger} | {link} |\n"

    header, data_rows = load_existing()
    data_rows = [new_row] + data_rows
    data_rows = data_rows[:MAX_ROWS]

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("".join(header + data_rows), encoding="utf-8")
    print(f"Log-Eintrag geschrieben: {new_row.strip()}")


if __name__ == "__main__":
    main()
