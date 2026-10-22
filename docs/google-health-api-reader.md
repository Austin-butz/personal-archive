# Google Health API reader: from a client-secret JSON to data

How `secondbrain` reads Fitbit/Google Health data using nothing but a Desktop OAuth
client JSON. Reference implementation: `secondbrain/google_auth.py` and
`secondbrain/sources/google_health.py`.

## 1. Inputs

- **One file:** an OAuth 2.0 client of type **Desktop app**, downloaded from Google Cloud
  Console (APIs & Services → Credentials). Shape:
  `{"installed": {"client_id": ..., "client_secret": ..., "auth_uri": ..., "token_uri": ...}}`.
- Store it **outside the repo**: `~/.config/secondbrain/google_client_secret.json`
  (override the dir with `SECONDBRAIN_CONFIG`). Never commit it.
- Project prerequisites (already done once, in Cloud Console): Google Health API enabled;
  OAuth consent screen set to **"In production" (unverified is fine)**. Do *not* leave it
  in "Testing": Testing-mode refresh tokens expire after 7 days, which silently breaks
  unattended jobs.
- Desktop clients use a loopback redirect, so **no redirect URI needs registering**.
- Only dependency: `google-auth-oauthlib` (pulls in `google-auth` and `requests`).

## 2. Scopes

```python
_BASE = "https://www.googleapis.com/auth/googlehealth."
NUTRITION_WRITE = _BASE + "nutrition.writeonly"
SCOPES = [_BASE + s for s in ("activity_and_fitness.readonly",
                              "health_metrics_and_measurements.readonly",
                              "sleep.readonly")] + [NUTRITION_WRITE]
```

`nutrition.writeonly` is only for `log` (section 6); ingest needs the three read scopes.
`session()` loads the token with the scopes it was granted rather than `SCOPES`, so adding
a scope doesn't break refresh for an older token; `session(require=...)` refuses with
"re-run `auth google`" when the token lacks a scope a caller needs.

## 3. Auth: one interactive consent, then refresh only

**Once, interactively** (`python -m secondbrain auth google`):

```python
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
# prompt=consent guarantees a refresh token even if the user consented before
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
# write creds.to_json() to ~/.config/secondbrain/google_token.json with mode 0600
```

`run_local_server(port=0)` opens the browser, listens on a random 127.0.0.1 port, and
does PKCE for you.

**Every other run, non-interactively:**

```python
creds = Credentials.from_authorized_user_info(json.load(TOKEN_FILE), SCOPES)
if not creds.valid:
    creds.refresh(Request())   # RefreshError -> tell the user to re-run auth; never open a browser
    save(creds)                # persist the new access token (0600)
session = AuthorizedSession(creds)   # a requests.Session that adds the bearer header
```

Rule: ingest code must never trigger a browser prompt. If refresh fails, fail loudly
with "re-run `auth google`".

## 4. API calls

Base: `https://health.googleapis.com/v4/users/me/dataTypes/{dataType}/dataPoints`

**a) Daily roll-ups** (steps, distance, floors, active-zone-minutes, total-calories,
heart-rate, active-minutes, active-energy-burned, sedentary-period, time-in-heart-rate-zone):

```
POST {base}/{dataType}/dataPoints:dailyRollUp
{"range": {"start": {"date": {"year":2026,"month":1,"day":1}},
           "end":   {"date": {"year":2026,"month":3,"day":31}}},
 "windowSizeDays": 1, "pageToken": "..."}
→ {"rollupDataPoints": [...], "nextPageToken": "..."}
```
- Range is civil dates, end exclusive. Cap each request at **90 days** (**14 for
  `total-calories`, `heart-rate`, `active-minutes` and `calories-in-heart-rate-zone`**,
  per the dailyRollUp reference); loop over windows. Days with no data are omitted.
- Stop at yesterday so only complete days are stored.
- Values, by data type (day from `p["civilStartTime"]["date"]`):

  | dataType | rollup field | value fields |
  |---|---|---|
  | `steps` | `steps` | `countSum` |
  | `distance` | `distance` | `millimetersSum` |
  | `floors` | `floors` | `countSum` |
  | `total-calories` | `totalCalories` | `kcalSum` |
  | `active-energy-burned` | `activeEnergyBurned` | `kcalSum` |
  | `active-zone-minutes` | `activeZoneMinutes` | `sumInFatBurnHeartZone`, `sumInCardioHeartZone`, `sumInPeakHeartZone` |
  | `heart-rate` | `heartRate` | `beatsPerMinuteAvg`, `beatsPerMinuteMin`, `beatsPerMinuteMax` |
  | `active-minutes` | `activeMinutes` | `activeMinutesRollupByActivityLevel[]` of `{activityLevel, activeMinutesSum}` |
  | `sedentary-period` | `sedentaryPeriod` | `durationSum` (Duration string, e.g. `"27000s"`) |
  | `time-in-heart-rate-zone` | `timeInHeartRateZone` | `timeInHeartRateZones[]` of `{heartRateZone: LIGHT/MODERATE/VIGOROUS/PEAK, duration}` |
  | `altitude` | `altitude` | `gainMillimetersSum` (not ingested; floors covers it) |
  | `body-fat`, `weight`, `run-vo2-max`, `core-body-temperature`, `blood-glucose` | same name | `*Avg`/`*Min`/`*Max` style aggregates (we list these instead) |

**b) List** (daily summaries, sessions, samples):

```
GET {base}/{dataType}/dataPoints?pageSize=N&filter=<field> >= "2026-01-01" AND <field> < "2026-09-27"&pageToken=...
→ {"dataPoints": [...], "nextPageToken": "..."}
```

| dataType | filter field | value |
|---|---|---|
| `daily-resting-heart-rate` | `daily_resting_heart_rate.date` | `dailyRestingHeartRate.beatsPerMinute` |
| `daily-heart-rate-variability` | `daily_heart_rate_variability.date` | `dailyHeartRateVariability.averageHeartRateVariabilityMilliseconds` |
| `daily-oxygen-saturation` | `daily_oxygen_saturation.date` | `dailyOxygenSaturation.averagePercentage` |
| `daily-respiratory-rate` | `daily_respiratory_rate.date` | `dailyRespiratoryRate.breathsPerMinute` |
| `sleep` (pageSize 25) | `sleep.interval.civil_end_time` | `sleep.summary.minutesAsleep`, `stagesSummary`, `interval` |
| `exercise` (pageSize 25) | `exercise.interval.civil_start_time` | `exercise.activeDuration`, `metricsSummary`, `interval` |
| `weight` | `weight.sample_time.civil_time` | `weight.weightGrams`, `sampleTime.physicalTime` |
| `body-fat` | `body_fat.sample_time.civil_time` | `bodyFat.percentage`, `sampleTime.physicalTime` |
| `daily-vo2-max` | `daily_vo2_max.date` | `dailyVo2Max.vo2Max`, `cardioFitnessLevel`, `estimated` |
| `daily-sleep-temperature-derivations` | `daily_sleep_temperature_derivations.date` | `nightlyTemperatureCelsius`, `baselineTemperatureCelsius` |

Filter rules (from the Filters guide): only `>=` and `<`, joined by `AND`; civil times are
`YYYY-MM-DD[THH:mm:ss]`, physical times RFC 3339 with `Z`; the filter field prefix is the
snake_case data type (`body_fat`), while the URL path uses kebab-case (`body-fat`).

**Not ingested, and why.** `heart-rate-variability`, `oxygen-saturation`, `activity-level`
are intraday samples (the daily-* types cover them). `respiratory-rate-sleep-summary` and
`daily-heart-rate-zones` duplicate `daily-respiratory-rate` / `time-in-heart-rate-zone`.
`nutrition-log`, `hydration-log`, `food` need the `nutrition.readonly` scope (adding it
means re-running `auth google`). `electrocardiogram` and `irregular-rhythm-notification`
need their own scopes. `height`, `blood-glucose`, `core-body-temperature`, `swim-lengths-data`
need devices this account doesn't have. (`daily-vo2-max` and `body-fat` are ingested but
returned nothing on the first run; they cost one empty request each.)
Reference: <https://developers.google.com/health/data-types>.

Page with `nextPageToken` until absent. Numbers often arrive as strings; coerce them.
Use `p["name"]` as the stable ID for sleep/exercise/weight points.

## 5. Gotchas learned the hard way

- **Testing-mode consent screen ⇒ 7-day token death.** Publish to production.
- **Always pass `prompt="consent"`**, or a re-auth may return no refresh token.
- **Sleep may lack `summary.minutesAsleep`** until Google finishes processing; skip and
  pick it up next run.
- **Sleep sometimes returns nothing at all.** Fetch each metric as its own unit with its
  own failure/safety handling, so one empty endpoint can't invalidate the others.
- **Calories roll-up rejects ranges > 14 days**; others allow 90.
- Round floats before hashing/deduping so noise doesn't look like a change.
- Keep the client JSON and token at mode 0600, outside version control.

## 6. Minimal checklist for another project

1. Put the Desktop client JSON somewhere outside the repo.
2. `pip install google-auth-oauthlib`.
3. Run the `InstalledAppFlow` snippet once with the scopes above; save the token (0600).
4. On each run: load token → refresh if needed → `AuthorizedSession`.
5. Hit `dailyRollUp` / `dataPoints` as in §4, paginate, parse.

## 6. Writing meals and water (`log`)

Reference implementation: `secondbrain/health_log.py`. Scope: `nutrition.writeonly`.

```
POST {base}/nutrition-log/dataPoints
{"nutritionLog": {"interval": {"startTime": "2026-10-22T23:00:00Z", "startUtcOffset": "-14400s",
                               "endTime": "2026-10-22T23:30:00Z", "endUtcOffset": "-14400s"},
                  "foodDisplayName": "chicken and rice", "mealType": "DINNER",
                  "energy": {"kcal": 650},
                  "nutrients": [{"nutrient": "PROTEIN", "quantity": {"grams": 45}}]}}

POST {base}/hydration-log/dataPoints
{"hydrationLog": {"interval": {... one second long ...},
                  "amountConsumed": {"milliliters": 473.2, "userProvidedUnit": "FLUID_OUNCE_US"}}}

→ {"done": true, "response": {"name": "users/.../dataTypes/nutrition-log/dataPoints/ID", ...}}
```

- Create returns an `Operation`. Keep `response.name`; it is the only handle for deleting.
- Intervals need both UTC times and the local `*UtcOffset` (seconds, `"-14400s"`), so the
  entry lands on the right civil day in daily rollups.
- `mealType`: `BREAKFAST`, `LUNCH`, `DINNER`, `SNACK` (also `BEFORE_BREAKFAST`,
  `BEFORE_LUNCH`, `BEFORE_DINNER`, `AFTER_DINNER`, `ANYTIME`). `energy.kcal` and
  nutrient `grams` must be in [0, 100000].
- A log made from `foodDisplayName` plus manual numbers ("anonymous food") can't be
  edited. Hydration logs can be PATCHed, but `log` treats both the same: undo and re-log.
- Delete with `POST {base}/{dataType}/dataPoints:batchDelete {"names": [...]}`; a plain
  HTTP DELETE on a data point isn't supported.
