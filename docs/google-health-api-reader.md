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

## 2. Scopes (read-only)

```python
_BASE = "https://www.googleapis.com/auth/googlehealth."
SCOPES = [_BASE + s for s in ("activity_and_fitness.readonly",
                              "health_metrics_and_measurements.readonly",
                              "sleep.readonly")]
```

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

**a) Daily roll-ups** (steps, distance, floors, active-zone-minutes, total-calories):

```
POST {base}/{dataType}/dataPoints:dailyRollUp
{"range": {"start": {"date": {"year":2026,"month":1,"day":1}},
           "end":   {"date": {"year":2026,"month":3,"day":31}}},
 "windowSizeDays": 1, "pageToken": "..."}
→ {"rollupDataPoints": [...], "nextPageToken": "..."}
```
- Range is civil dates, end exclusive. Cap each request at **90 days** (**14 for
  `total-calories`**); loop over windows. Days with no data are omitted.
- Stop at yesterday so only complete days are stored.
- Values: `p["steps"]["countSum"]`, `p["distance"]["millimetersSum"]`,
  `p["floors"]["countSum"]`, `p["totalCalories"]["kcalSum"]`,
  `p["activeZoneMinutes"]["sumIn{FatBurn,Cardio,Peak}HeartZone"]`; day from
  `p["civilStartTime"]["date"]`.

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
