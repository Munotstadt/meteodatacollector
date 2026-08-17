#!/usr/bin/env python3
"""
Exportiert die Sommermonate (Juni-August) 2003 und 2026 der Station KLO
aus Turso nach data/sommer_vergleich.json - Datenbasis fuer sommer.html.

2003 ist historisch (aendert sich nie mehr), 2026 laeuft noch bis Ende
August - dieses Skript sollte daher bis Ende Sommer 2026 im normalen
Turso-Workflow mitlaufen, damit der Vergleich aktuell bleibt.

Benoetigte Umgebungsvariablen (dieselben wie collect_turso.py):
  TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import libsql

COLUMNS = [
    "temp_mean_c", "temp_min_c", "temp_max_c", "precip_mm",
    "sunshine_min", "radiation_wm2", "wind_mean_kmh", "wind_max_kmh", "humidity_pct",
]

YEARS = [2003, 2018, 2026]
SUMMER_START_MD = (6, 1)   # 1. Juni
SUMMER_END_MD = (8, 31)    # 31. August

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
JSON_PATH = DATA_DIR / "sommer_vergleich.json"


class ResilientConnection:
    """Siehe collect_turso.py fuer Details - identisches Reconnect-Verhalten
    bei Verbindungs-/Stream-Fehlern."""

    def __init__(self, max_query_attempts: int = 4):
        self.max_query_attempts = max_query_attempts
        self._conn = self._connect()

    def _connect(self, max_attempts: int = 5):
        url = os.environ["TURSO_DATABASE_URL"]
        token = os.environ["TURSO_AUTH_TOKEN"]
        delays = [3, 6, 12, 20, 30][: max_attempts - 1]
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                conn = libsql.connect(database=url, auth_token=token)
                conn.execute("SELECT 1")
                return conn
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == max_attempts:
                    break
                wait = delays[attempt - 1]
                print(f"WARNUNG: Turso-Verbindung fehlgeschlagen ({attempt}/{max_attempts}): {exc} -> warte {wait}s.", file=sys.stderr)
                time.sleep(wait)
        raise last_error  # noqa: RSE102

    def execute(self, sql, params=None):
        delays = [3, 6, 12]
        last_error: Exception | None = None
        for attempt in range(1, self.max_query_attempts + 1):
            try:
                return self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self.max_query_attempts:
                    break
                wait = delays[min(attempt - 1, len(delays) - 1)]
                print(f"WARNUNG: Turso-Abfrage fehlgeschlagen ({attempt}/{self.max_query_attempts}): {exc} -> neue Verbindung, warte {wait}s.", file=sys.stderr)
                time.sleep(wait)
                self._conn = self._connect()
        raise last_error  # noqa: RSE102


def fetch_summer(conn, year: int) -> list[dict]:
    start = f"{year}-{SUMMER_START_MD[0]:02d}-{SUMMER_START_MD[1]:02d}"
    end = f"{year}-{SUMMER_END_MD[0]:02d}-{SUMMER_END_MD[1]:02d}"
    col_list = ", ".join(["obs_date", *COLUMNS])
    result = conn.execute(
        f"SELECT {col_list} FROM klo_daily WHERE obs_date >= ? AND obs_date <= ? ORDER BY obs_date",
        (start, end),
    ).fetchall()

    days = []
    for row in result:
        entry = {"date": row[0]}
        for i, col in enumerate(COLUMNS, start=1):
            entry[col] = row[i]
        days.append(entry)
    return days


def main() -> None:
    conn = ResilientConnection()

    years_out = {}
    for year in YEARS:
        days = fetch_summer(conn, year)
        years_out[str(year)] = {"days": days}
        print(f"Sommer {year}: {len(days)} Tage gefunden.")

    payload = {
        "station": "KLO",
        "station_name": "Zürich-Kloten",
        "source": "MeteoSchweiz (opendata.swiss) - Source: MeteoSchweiz",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summer_range": f"{SUMMER_START_MD[1]:02d}.{SUMMER_START_MD[0]:02d}. - {SUMMER_END_MD[1]:02d}.{SUMMER_END_MD[0]:02d}.",
        "years": years_out,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Export nach {JSON_PATH} geschrieben.")


if __name__ == "__main__":
    main()
