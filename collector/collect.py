#!/usr/bin/env python3
"""
MeteoDataCollector - Sammelt Tageswerte der MeteoSchweiz-Station
Zürich-Kloten (KLO) und schreibt sie nach data/klo_daily.csv (Archiv)
und data/klo_daily.json (fuer die Webseite, letzte N Tage).

Quelle: MeteoSchweiz Open Government Data (OGD-SMN)
https://opendatadocs.meteoswiss.ch/a-data-groundbased/a1-automatic-weather-stations
Nutzungsbedingungen: Quellenangabe "Source: MeteoSchweiz" erforderlich.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import urllib.request

STATION = "klo"  # Zuerich-Kloten
BASE_URL = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{STATION}"
RECENT_URL = f"{BASE_URL}/ogd-smn_{STATION}_d_recent.csv"
HISTORICAL_URL = f"{BASE_URL}/ogd-smn_{STATION}_d_historical.csv"

# Parameter-Codes fuer Tageswerte (Quelle: ogd-smn_meta_parameters.csv).
# Falls MeteoSchweiz einen Code umbenennt, meldet der Check unten den Fehler
# mit einer Liste der tatsaechlich vorhandenen Spalten.
PARAMS = {
    "tre200d0": "temp_mean_c",       # Lufttemperatur 2m, Tagesmittel
    "tre200dn": "temp_min_c",        # Lufttemperatur 2m, Tagesminimum
    "tre200dx": "temp_max_c",        # Lufttemperatur 2m, Tagesmaximum
    "rre150d0": "precip_mm",         # Niederschlag, Tagessumme (05:40-05:40 Folgetag)
    "sre000d0": "sunshine_min",      # Sonnenscheindauer, Tagessumme (Minuten)
    "gre000d0": "radiation_wm2",     # Globalstrahlung, Tagesmittel (W/m^2)
    "fu3010d0": "wind_mean_kmh",     # Windgeschwindigkeit skalar, Tagesmittel (km/h)
    "ure200d0": "humidity_pct",      # Relative Luftfeuchtigkeit, Tagesmittel
}

TIMESTAMP_COL_CANDIDATES = ("reference_timestamp", "REFERENCE_TS")
STATION_COL_CANDIDATES = ("station_abbr", "STATION")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CSV_PATH = DATA_DIR / "klo_daily.csv"
JSON_PATH = DATA_DIR / "klo_daily.json"

# Wie viele Tage sollen im JSON fuer die Webseite enthalten sein.
JSON_WINDOW_DAYS = 3 * 365


def fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "meteodatacollector/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw), delimiter=";")
    rows = list(reader)
    if not rows:
        return []

    fieldnames = set(reader.fieldnames or [])
    missing = [p for p in PARAMS if p not in fieldnames]
    if missing:
        print(
            f"WARNUNG: Parameter nicht in {url} gefunden: {missing}\n"
            f"Vorhandene Spalten: {sorted(fieldnames)}",
            file=sys.stderr,
        )
    return rows


def parse_date(row: dict) -> str | None:
    for col in TIMESTAMP_COL_CANDIDATES:
        if col in row and row[col]:
            raw = row[col]
            # Formate: "01.01.2024 00:00" (historical) oder "2024-01-01T00:00" (recent)
            for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            print(f"WARNUNG: Konnte Datum nicht parsen: {raw!r}", file=sys.stderr)
    return None


def to_float(value: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def normalize_rows(raw_rows: list[dict]) -> dict[str, dict]:
    """Reduziert Rohzeilen auf {datum: {feld: wert}}."""
    out: dict[str, dict] = {}
    for row in raw_rows:
        date = parse_date(row)
        if not date:
            continue
        entry = {"date": date}
        for code, field in PARAMS.items():
            entry[field] = to_float(row.get(code, ""))
        out[date] = entry
    return out


def load_existing_csv() -> dict[str, dict]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = {}
        for row in reader:
            date = row["date"]
            rows[date] = {
                "date": date,
                **{
                    field: (float(row[field]) if row.get(field) not in (None, "") else None)
                    for field in PARAMS.values()
                },
            }
    return rows


def write_csv(all_rows: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["date", *PARAMS.values()]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for date in sorted(all_rows):
            writer.writerow(all_rows[date])


def write_json(all_rows: dict[str, dict]) -> None:
    dates_sorted = sorted(all_rows)[-JSON_WINDOW_DAYS:]
    payload = {
        "station": "KLO",
        "station_name": "Zürich-Kloten",
        "source": "MeteoSchweiz (opendata.swiss) - Source: MeteoSchweiz",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields": list(PARAMS.values()),
        "days": [all_rows[d] for d in dates_sorted],
    }
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    existing = load_existing_csv()
    print(f"Vorhandene Tage im Archiv: {len(existing)}")

    all_new: dict[str, dict] = {}
    for url in (HISTORICAL_URL, RECENT_URL):
        try:
            raw_rows = fetch_csv(url)
        except Exception as exc:  # noqa: BLE001
            print(f"FEHLER beim Laden von {url}: {exc}", file=sys.stderr)
            continue
        normalized = normalize_rows(raw_rows)
        print(f"{url}: {len(normalized)} Tage gelesen")
        all_new.update(normalized)

    if not all_new:
        print("Keine neuen Daten erhalten, breche ab ohne Aenderungen.", file=sys.stderr)
        sys.exit(1)

    merged = {**existing, **all_new}
    write_csv(merged)
    write_json(merged)
    print(f"Fertig. Archiv enthaelt jetzt {len(merged)} Tage ({CSV_PATH}).")


if __name__ == "__main__":
    main()
