# meteodatacollector

Sammelt automatisch Tageswerte der MeteoSchweiz-Station **Zürich-Kloten (KLO)**
sowie aktuelle Hydrologie-Messwerte des BAFU (Rhein, Bodensee, Glatt),
archiviert alles dauerhaft in einer Azure SQL Database und zeigt es auf
einer öffentlichen, statischen Startseite an.

**Live:** https://munotstadt.github.io/meteodatacollector/

---

## Architektur

```
GitHub Actions (ein Workflow, 4×/Tag, fixe UTC-Zeiten)
        │
        ├─► collector/collect.py        (MeteoSchweiz KLO)
        │     └─► Azure SQL "MeteoDB", Tabelle dbo.klo_daily
        │     └─► data/klo_daily.json   (Snapshot fuer die Website)
        │
        ├─► collector/collect_hydro.py  (BAFU / LINDAS)
        │     └─► Azure SQL "MeteoDB", Tabelle dbo.hydro_readings
        │     └─► data/hydro_latest.json (Snapshot fuer die Website)
        │
        └─► git commit + push beider JSON-Dateien
                        │
                        ▼
        GitHub Pages: index.html liest beide Dateien statisch
```

**Warum die Website nicht live aus der DB liest:** Azure SQL Database
läuft im Serverless-Tarif und pausiert bei Inaktivität — ein Live-Request
eines Besuchers könnte 30–60s hängen, bis die DB aufgewacht ist. Der
Collector (der ohnehin mehrmals täglich läuft und eine Retry-Logik fürs
Aufwachen hat) exportiert stattdessen nach jedem Lauf einen Snapshot als
JSON zurück ins Repo. Die Website lädt diese Dateien sofort, unabhängig
vom DB-Zustand.

**Wichtig zu GitHub Pages:** Pages baut die Seite automatisch bei jedem
Push zum Branch, ganz ohne eigenen Deploy-Workflow. Frühere Versuche mit
Azure Static Web Apps sind daran gescheitert, dass GitHub-interne Pushes
(via `GITHUB_TOKEN`) keine anderen Actions-Workflows triggern — bei Pages
ist das irrelevant, da kein Workflow dafür nötig ist. Details siehe
"Architektur-Historie" unten.

---

## Meteo — MeteoSchweiz KLO

### Erfasste Grössen (Tageswerte)

| Grösse | MeteoSchweiz-Parameter | Einheit | Spalte in DB/JSON |
|---|---|---|---|
| Temperatur, Tagesmittel | `tre200d0` | °C | `temp_mean_c` |
| Temperatur, Minimum | `tre200dn` | °C | `temp_min_c` |
| Temperatur, Maximum | `tre200dx` | °C | `temp_max_c` |
| Niederschlag, Tagessumme | `rre150d0` | mm | `precip_mm` |
| Sonnenscheindauer, Tagessumme | `sre000d0` | Minuten (Seite zeigt Stunden) | `sunshine_min` |
| Globalstrahlung, Tagesmittel | `gre000d0` | W/m² | `radiation_wm2` |
| Windgeschwindigkeit, Tagesmittel | `fu3010d0` | km/h | `wind_mean_kmh` |
| Böenspitze (1s), Tagesmaximum | `fu3010d1` | km/h | `wind_max_kmh` |
| Relative Luftfeuchtigkeit, Tagesmittel | `ure200d0` | % | `humidity_pct` |

Quelle: [MeteoSchweiz Open Government Data](https://opendatadocs.meteoswiss.ch/a-data-groundbased/a1-automatic-weather-stations)
(`ch.meteoschweiz.ogd-smn`). Quellenangabe **„Source: MeteoSchweiz"** ist Pflicht.

**Bekannte Eigenheiten der Rohdaten:**
- **Niederschlag** wird 05:40–05:40 Uhr des Folgetags summiert, nicht
  exakt Mitternacht bis Mitternacht.
- MeteoSchweiz führt **kein Tagesminimum der Windgeschwindigkeit**.
  Verfügbar sind nur der Tagesmittelwert und die Böenspitze (stärkste
  1-Sekunden-Böe) als Tagesmaximum — `fu3010dn`/`fu3010dx` (analog zur
  Temperatur) existieren für diese Station nicht, auch wenn das
  zunächst naheliegend wirkte.

### Tabellenschema `dbo.klo_daily`

```sql
CREATE TABLE dbo.klo_daily (
  obs_date DATE NOT NULL PRIMARY KEY,
  temp_mean_c DECIMAL(4,1) NULL,
  temp_min_c DECIMAL(4,1) NULL,
  temp_max_c DECIMAL(4,1) NULL,
  precip_mm DECIMAL(5,1) NULL,
  sunshine_min DECIMAL(5,1) NULL,
  radiation_wm2 DECIMAL(5,1) NULL,
  wind_mean_kmh DECIMAL(4,1) NULL,
  wind_max_kmh DECIMAL(4,1) NULL,
  humidity_pct DECIMAL(4,1) NULL,
  updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
```

(Historische Spalte `wind_min_kmh` existiert in der DB evtl. noch, wird
aber nicht mehr befüllt — kann bei Gelegenheit gelöscht werden.)

---

## Hydrologie — BAFU (Rhein / Bodensee / Glatt)

### Stationen

| Anzeige | BAFU-Station | Nr. | Grösse |
|---|---|---|---|
| Bodensee Berlingen | Bodensee (Untersee) – Berlingen | 2043 | Pegel (m ü. M.) |
| Rhein Neuhausen | Rhein – Neuhausen, Flurlingerbrücke | 2288 | Abfluss (m³/s) |
| Glatt Rheinsfelden | Glatt – Rheinsfelden | 2415 | Abfluss (m³/s) |

Quelle: Bundesamt für Umwelt BAFU, [LINDAS Linked Data Service](https://www.hydrodaten.admin.ch/)
(`ld.admin.ch`, SPARQL-Endpoint, kein API-Key nötig). Quellenangabe
**„Source: BAFU"**.

**Wichtige Einschränkung:** LINDAS liefert nur den jeweils **aktuellsten
Messwert** pro Station (Update alle ~10 Min.), **keine mehrjährige
Historie** wie MeteoSchweiz. Eine Bestellung historischer BAFU-Daten
erfolgt manuell über den "Datenservice Hydrologie" des Bundes, nicht über
eine offene API. Unsere Zeitreihe baut sich daher organisch **ab dem
ersten Collector-Lauf** auf — Min/Max in den Range-Bar-Karten auf der
Website beziehen sich entsprechend auf "seit Beginn unserer Aufzeichnung",
nicht auf historische Rekordwerte.

### Tabellenschema `dbo.hydro_readings`

```sql
CREATE TABLE dbo.hydro_readings (
  station_id VARCHAR(10) NOT NULL,
  reading_time DATETIME2 NOT NULL,
  discharge_m3s DECIMAL(10,2) NULL,
  water_level_m DECIMAL(10,3) NULL,
  water_temp_c DECIMAL(4,1) NULL,
  inserted_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
  PRIMARY KEY (station_id, reading_time)
);
```

---

## Aufbau des Repos

```
collector/collect.py            MeteoSchweiz KLO -> Azure SQL + data/klo_daily.json
collector/collect_hydro.py      BAFU/LINDAS -> Azure SQL + data/hydro_latest.json
data/klo_daily.json             Meteo-Snapshot fuer die Webseite
data/hydro_latest.json          Hydro-Snapshot fuer die Webseite (~90 Tage Fenster)
index.html                      Oeffentliche Startseite
.github/workflows/collect.yml   Ein Workflow, ruft beide Collector-Skripte auf
```

## Azure-Ressourcen

| Ressource | Name |
|---|---|
| SQL-Server | `sql-munotstadt-meteo.database.windows.net` |
| Datenbank | `MeteoDB` |
| Tabellen | `dbo.klo_daily`, `dbo.hydro_readings` |
| Tarif | Azure SQL Database Free Offer (Serverless, Auto-Pause) |

## GitHub Secrets (Repo → Settings → Secrets and variables → Actions)

Von beiden Collectoren gemeinsam genutzt:

| Name | Wert |
|---|---|
| `AZURE_SQL_SERVER` | `sql-munotstadt-meteo.database.windows.net` |
| `AZURE_SQL_DATABASE` | `MeteoDB` |
| `AZURE_SQL_USER` | SQL-Admin-Login |
| `AZURE_SQL_PASSWORD` | zugehöriges Passwort |

## Zeitplan

Der Workflow läuft **4× täglich zu festen UTC-Zeiten: 02:00 / 08:00 / 14:00 / 20:00**.
Keine DST-Umrechnung nötig (frühere Versionen versuchten Schweizer
Ortszeit über zwei Sommer-/Winter-Cron-Zeilen nachzubilden — wurde zugunsten
fixer UTC-Zeiten vereinfacht). In Schweizer Ortszeit entspricht das
03:00/09:00/15:00/21:00 (Winter, CET) bzw. 04:00/10:00/16:00/22:00 (Sommer, CEST).

Innerhalb eines Laufs: erst `collect.py` (Meteo), dann `collect_hydro.py`
(Hydro, mit `continue-on-error: true` — ein BAFU-Ausfall verhindert nicht
den Meteo-Teil), danach ein gemeinsamer Commit beider JSON-Dateien.

## Frontend-Features (`index.html`)

- Kennzahl-Karten für KLO (Temp Ø/Min/Max, Niederschlag, Sonnenstunden,
  Globalstrahlung, Wind Ø, Böenspitze, Luftfeuchtigkeit) mit Delta zum Vortag
- Zeitraum-Auswahl (30 Tage / 90 Tage / 1 Jahr / 3 Jahre / Alle) für alle Meteo-Charts
- Charts: Balkendiagramme für Niederschlag/Sonnenstunden/Globalstrahlung,
  Liniendiagramm mit Min/Max-Band für Temperatur, einfache Linien für
  Wind/Feuchtigkeit
- **Alle Charts sind anklickbar/antippbar** — zeigt Datum (+ bei Hydro
  Uhrzeit) und Wert des gewählten Punkts in einer Zeile unter dem Chart
- Vergleichstabelle: Tagesdurchschnitte letzte 7 Tage vs. Vorwoche,
  aktueller Monat vs. Vorjahr (tagesgenau), YTD vs. Vorjahr
- "Letzte Messwerte"-Tabelle ist standardmässig eingeklappt (▶-Toggle)
- Hydrodaten-Sektion: Range-Bar-Karten (Bodensee, Rhein, Glatt) mit
  aktuellem Wert, Position zwischen Min/Max seit Aufzeichnungsbeginn,
  Delta vs. Vortag zur ähnlichen Uhrzeit (±3h Toleranz), plus Charts

## Einrichtung / Deployment

1. **GitHub Pages aktivieren:** Settings → Pages → Source: „Deploy from a
   branch", Branch `main`, Ordner `/ (root)`.
2. Die vier Secrets oben hinterlegen.
3. Beide Tabellen (`dbo.klo_daily`, `dbo.hydro_readings`) in Azure SQL anlegen.
4. Workflow einmal manuell starten: Actions → „Collect MeteoSwiss + BAFU data"
   → Run workflow.
5. Seite ist danach live unter `https://munotstadt.github.io/meteodatacollector/`.

## Lokal testen

```bash
export AZURE_SQL_SERVER=sql-munotstadt-meteo.database.windows.net
export AZURE_SQL_DATABASE=MeteoDB
export AZURE_SQL_USER=...
export AZURE_SQL_PASSWORD=...
pip install pymssql
python3 collector/collect.py
python3 collector/collect_hydro.py

python3 -m http.server 8000   # dann im Browser: http://localhost:8000
```

## Troubleshooting

**"No matching Static Web App was found" im Actions-Log:** Überbleibsel
aus der (verworfenen) Azure-Static-Web-Apps-Phase. Die Datei
`.github/workflows/azure-static-web-apps-....yml` versucht bei jedem Push
weiter zu deployen, obwohl die Azure-Ressource gelöscht wurde. Fix: diese
Workflow-Datei im Repo löschen (siehe Architektur-Historie).

**Seite lädt, aber Charts/Karten bleiben leer, HTTP 404 auf die
JSON-Dateien:** Der Collector-Workflow muss mindestens einmal
durchgelaufen sein, damit `data/klo_daily.json` bzw. `data/hydro_latest.json`
im Repo existieren. Manuell auslösen: Actions → Run workflow.

**Collector hängt/bricht mit Fehler 40613 ab:** Azure SQL Serverless war
pausiert und wacht gerade auf — der Collector hat dafür eine
Retry-Logik mit Backoff (bis zu 6 Versuche), sollte sich also selbst lösen.

## Architektur-Historie

1. **v1:** GitHub Actions + GitHub Pages, Daten als CSV/JSON direkt im Repo.
2. **v2:** Datenhaltung nach Azure SQL Database verschoben, Frontend
   blieb auf GitHub Pages via Azure Function als API-Schicht.
3. **v3:** Konsolidierung auf Azure Static Web Apps (Frontend + API in
   einer Ressource).
4. **v4:** Zurück zu GitHub Pages + statischem JSON-Export vom Collector.
   Gründe: (a) Website-Ladezeit unabhängig vom Serverless-Aufwach-Zustand
   der DB machen, (b) Pushes von `GITHUB_TOKEN` triggern keine anderen
   Actions-Workflows — bei Azure Static Web Apps wäre dafür ein Personal
   Access Token nötig gewesen, bei GitHub Pages entfällt das Problem
   komplett, da kein Deploy-Workflow existiert.
5. **v5 (aktuell):** BAFU-Hydrodaten (Rhein/Bodensee/Glatt) über LINDAS
   ergänzt, eigener Collector + eigene Tabelle, gleicher Rhythmus wie
   Meteo. Frontend um Hydrodaten-Sektion mit Range-Bar-Karten sowie
   klickbare Chart-Punkte erweitert. Zeitplan von Schweizer-Ortszeit-
   Näherung (zwei DST-Cron-Zeilen) auf feste UTC-Zeiten vereinfacht.
