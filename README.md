# meteodatacollector

Sammelt automatisch Tageswerte der MeteoSchweiz-Station **Zürich-Kloten (KLO)**,
archiviert sie dauerhaft in einer Azure SQL Database und zeigt sie auf einer
öffentlichen, statischen Startseite an.

**Live:** https://munotstadt.github.io/meteodatacollector/

---

## Architektur

```
GitHub Actions (Collector, 4×/Tag)
        │
        ├─► Azure SQL Database "MeteoDB"      (Archiv, Quelle der Wahrheit)
        │
        └─► data/klo_daily.json im Repo       (Snapshot für die Website)
                        │
                        ▼
        GitHub Pages: index.html liest diese Datei statisch
```

**Warum so und nicht direkt live aus der DB?** Die Website liest bewusst
eine im Repo committete JSON-Datei statt live gegen die Datenbank zu fragen.
Grund: Azure SQL Database läuft im Serverless-Tarif und pausiert bei
Inaktivität — ein Live-Request von einem Website-Besucher könnte dadurch
30–60 Sekunden hängen, bis die DB aufgewacht ist. Der Collector (der ohnehin
alle paar Stunden läuft und eine Retry-Logik fürs Aufwachen hat) exportiert
stattdessen nach jedem Lauf einen Snapshot als JSON zurück ins Repo. Die
Website lädt diese Datei sofort, unabhängig vom DB-Zustand.

Eine frühere Version nutzte eine Azure Static Web App mit eigener API
(Azure Function). Das wurde wieder verworfen zugunsten von GitHub Pages,
u. a. weil GitHub Pages Änderungen am Branch ohne separaten
Actions-Workflow deployt (kein Token-Trigger-Problem, siehe unten).

## Erfasste Grössen (Tageswerte)

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
(`ch.meteoschweiz.ogd-smn`). Nutzung ohne Einschränkung, Quellenangabe
**„Source: MeteoSchweiz"** ist Pflicht.

**Bekannte Eigenheiten der Rohdaten:**
- **Niederschlag** wird von MeteoSchweiz offiziell **05:40–05:40 Uhr des
  Folgetags** summiert, nicht exakt Mitternacht bis Mitternacht.
- Für die **Windgeschwindigkeit führt MeteoSchweiz kein Tagesminimum**.
  Verfügbar sind nur der Tagesmittelwert und die Böenspitze (stärkste
  1-Sekunden-Böe des Tages) als Tagesmaximum.

## Aufbau des Repos

```
collector/collect.py            Laedt CSVs von MeteoSchweiz, schreibt DB + JSON
data/klo_daily.json             Snapshot fuer die Webseite (vom Collector erzeugt)
index.html                      Oeffentliche Startseite (Karten, Charts, Vergleichstabelle)
.github/workflows/collect.yml   Cronjob, 4x taeglich
```

## Azure-Ressourcen

| Ressource | Name |
|---|---|
| SQL-Server | `sql-munotstadt-meteo.database.windows.net` |
| Datenbank | `MeteoDB` |
| Tabelle | `dbo.klo_daily` (PK: `obs_date`) |
| Tarif | Azure SQL Database Free Offer (Serverless, Auto-Pause) |

Tabellenschema:

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

## GitHub Secrets (Repo → Settings → Secrets and variables → Actions)

| Name | Wert |
|---|---|
| `AZURE_SQL_SERVER` | `sql-munotstadt-meteo.database.windows.net` |
| `AZURE_SQL_DATABASE` | `MeteoDB` |
| `AZURE_SQL_USER` | SQL-Admin-Login |
| `AZURE_SQL_PASSWORD` | zugehöriges Passwort |

## Zeitplan

Der Collector läuft **4× täglich zur Schweizer Ortszeit 05:30 / 07:30 / 09:30 / 11:30**.
GitHub Actions Cron kennt keine Zeitzonen/DST, daher zwei Cron-Zeilen
(Sommer- und Winterzeit-Näherung, siehe Kommentare in `collect.yml`).

## Einrichtung / Deployment

1. **GitHub Pages aktivieren:** Settings → Pages → Source: „Deploy from a
   branch", Branch `main`, Ordner `/ (root)`.
2. Die vier Secrets oben hinterlegen.
3. Workflow einmal manuell starten: Actions → „Collect MeteoSwiss data (KLO)"
   → Run workflow.
4. Seite ist danach live unter `https://munotstadt.github.io/meteodatacollector/`.

**Wichtig:** Committed der Collector-Workflow eine neue `data/klo_daily.json`,
löst das bei GitHub Pages automatisch ein Rebuild aus — kein zusätzlicher
Deploy-Schritt nötig. (Das ist ein Unterschied zu Azure Static Web Apps: dort
hätte ein Push vom internen `GITHUB_TOKEN` keinen separaten Deploy-Workflow
ausgelöst, siehe Architektur-Historie unten.)

## Architektur-Historie (warum es so aussieht, wie es aussieht)

1. **v1:** Reines GitHub-Actions + GitHub-Pages-Setup, Daten als CSV/JSON
   direkt im Repo (kein Azure).
2. **v2:** Umzug der Datenhaltung nach Azure SQL Database (Free Tier),
   Frontend blieb auf GitHub Pages via Azure Function als API-Schicht
   (nötig, weil ein Static-Site-Browser nicht direkt mit SQL sprechen kann).
3. **v3:** Konsolidierung auf Azure Static Web Apps (Frontend + API in
   einer Ressource, kein CORS-Setup nötig).
4. **v4 (aktuell):** Zurück zu GitHub Pages + statischem JSON-Export vom
   Collector. Gründe: (a) Website-Ladezeit unabhängig vom
   Serverless-Aufwach-Zustand der DB machen, (b) ein GitHub-internes
   Token-Verhalten (Pushes von `GITHUB_TOKEN` triggern keine anderen
   Actions-Workflows) hätte bei Azure Static Web Apps einen Personal
   Access Token nötig gemacht — mit reinem GitHub Pages entfällt dieses
   Problem komplett. Azure SQL Database bleibt als dauerhaftes Archiv
   bestehen; nur der Live-API-Umweg über Azure Function/Static Web App
   wurde wieder entfernt.

## Lokal testen

```bash
export AZURE_SQL_SERVER=sql-munotstadt-meteo.database.windows.net
export AZURE_SQL_DATABASE=MeteoDB
export AZURE_SQL_USER=...
export AZURE_SQL_PASSWORD=...
pip install pymssql
python3 collector/collect.py

python3 -m http.server 8000   # dann im Browser: http://localhost:8000
```

## Parameter-Codes prüfen/anpassen

Falls sich MeteoSchweiz-Codes ändern, listet
[`ogd-smn_meta_parameters.csv`](https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ogd-smn_meta_parameters.csv)
alle aktuellen Parameter für die Station. Der Collector schreibt eine
Warnung auf stderr (sichtbar im Actions-Log), wenn ein erwarteter Code in
den Rohdaten fehlt — dort auch die tatsächlich vorhandene Spaltenliste zum
Abgleich.
