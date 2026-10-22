"""Google Health API -> health_metrics (DESIGN.md §6).

Daily granularity only: one row per day for daily totals and daily summaries, and one
row per sleep session, exercise, weigh-in and body-fat reading. Intraday samples (heart
rate every few seconds, per-minute steps) are out of scope; Google's dailyRollUp gives
the per-day aggregate (e.g. heart rate avg/min/max) instead.

Each metric is ingested as its own run with its own scope, so the safety check is per
metric: the sleep endpoint occasionally returns nothing, and that must not stale the
existing sleep rows just because the other metrics look fine.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from ..hashing import utc_iso

API = "https://health.googleapis.com/v4/users/me/dataTypes"
WINDOW_START = "2026-01-01"  # the account has nothing earlier


# ---------------------------------------------------------------- API client

class HealthClient:
    def __init__(self, session):
        self.s = session

    def list(self, data_type: str, flt: str, page_size: int) -> list[dict]:
        out, token = [], None
        while True:
            params = {"pageSize": page_size, "filter": flt, **({"pageToken": token} if token else {})}
            r = self.s.get(f"{API}/{data_type}/dataPoints", params=params, timeout=60)
            r.raise_for_status()
            body = r.json()
            out += body.get("dataPoints", [])
            token = body.get("nextPageToken")
            if not token:
                return out

    def daily_rollup(self, data_type: str, start: date, end: date, max_days: int) -> list[dict]:
        """One point per civil day in [start, end); days without data are omitted."""
        out, cur = [], start
        while cur < end:
            nxt = min(cur + timedelta(days=max_days), end)
            token = None
            while True:
                body = {"range": {"start": _civil(cur), "end": _civil(nxt)}, "windowSizeDays": 1,
                        **({"pageToken": token} if token else {})}
                r = self.s.post(f"{API}/{data_type}/dataPoints:dailyRollUp", json=body, timeout=60)
                r.raise_for_status()
                page = r.json()
                out += page.get("rollupDataPoints", [])
                token = page.get("nextPageToken")
                if not token:
                    break
            cur = nxt
        return out


def _civil(d: date) -> dict:
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


def _ymd(d: dict) -> str:
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def _utc(ts: str) -> str:
    return utc_iso(datetime.fromisoformat(ts.replace("Z", "+00:00")))


def _num(x) -> float:
    return float(x) if x not in (None, "") else 0.0


# ---------------------------------------------------------------- metrics
# Each parser turns one API point into (value, unit, start_at, end_at, external_id, details)
# or None to skip it. Values are rounded so float noise can't change the hash.

@dataclass
class Metric:
    name: str                    # metric column value, e.g. "steps"
    label: str                   # for chunk text
    data_type: str               # API data type, e.g. "daily-resting-heart-rate"
    kind: str                    # "rollup" | "daily" | "session" | "sample"
    parse: Callable[[dict], tuple | None]
    filter_field: str = ""       # list filter field, for non-rollup kinds
    page_size: int = 10000
    max_days: int = 90           # rollup range cap per request


def _rollup(field, key, scale=1.0, digits=0, unit=""):
    def parse(p):
        v = p.get(field, {}).get(key)
        if v is None:
            return None
        day = _ymd(p["civilStartTime"]["date"])
        return round(_num(v) * scale, digits), unit, day, None, None, None
    return parse


def _azm(p):
    v = p.get("activeZoneMinutes")
    if not v:
        return None
    zones = {"fat_burn": int(_num(v.get("sumInFatBurnHeartZone"))),
             "cardio": int(_num(v.get("sumInCardioHeartZone"))),
             "peak": int(_num(v.get("sumInPeakHeartZone")))}
    return float(sum(zones.values())), "min", _ymd(p["civilStartTime"]["date"]), None, None, zones


def _daily(field, key, unit, digits=1, extra=()):
    def parse(p):
        v = p.get(field)
        if not v or v.get(key) is None:
            return None
        details = {k: v[k] for k in extra if k in v} or None
        return round(_num(v[key]), digits), unit, _ymd(v["date"]), None, None, details
    return parse


def _sleep(p):
    s = p["sleep"]
    summary = s.get("summary") or {}
    if "minutesAsleep" not in summary:
        return None  # not processed yet; it'll come through on a later run
    details = {
        "minutes_awake": int(_num(summary.get("minutesAwake"))),
        "minutes_in_bed": int(_num(summary.get("minutesInSleepPeriod"))),
        "stages": {st["type"].lower(): int(_num(st.get("minutes")))
                   for st in summary.get("stagesSummary", [])},
        "main_sleep": bool((s.get("metadata") or {}).get("mainSleep")),
    }
    iv = s["interval"]
    return (float(_num(summary["minutesAsleep"])), "min", _utc(iv["startTime"]),
            _utc(iv["endTime"]), p["name"], details)


def _exercise(p):
    e = p["exercise"]
    m = e.get("metricsSummary") or {}
    iv = e["interval"]
    secs = _num(e.get("activeDuration", "0s").rstrip("s")) or (
        datetime.fromisoformat(iv["endTime"].replace("Z", "+00:00"))
        - datetime.fromisoformat(iv["startTime"].replace("Z", "+00:00"))).total_seconds()
    details = {"type": e.get("displayName") or e.get("exerciseType", "").title()}
    if m.get("distanceMillimeters"):
        details["distance_km"] = round(_num(m["distanceMillimeters"]) / 1e6, 2)
    if m.get("caloriesKcal"):
        details["calories_kcal"] = round(_num(m["caloriesKcal"]))
    if m.get("averageHeartRateBeatsPerMinute"):
        details["avg_hr_bpm"] = int(_num(m["averageHeartRateBeatsPerMinute"]))
    if m.get("steps"):
        details["steps"] = int(_num(m["steps"]))
    if m.get("activeZoneMinutes"):
        details["active_zone_minutes"] = int(_num(m["activeZoneMinutes"]))
    if e.get("notes"):
        details["notes"] = e["notes"]
    return (round(secs / 60, 1), "min", _utc(iv["startTime"]), _utc(iv["endTime"]),
            p["name"], details)


def _weight(p):
    w = p["weight"]
    return (round(_num(w["weightGrams"]) / 1000, 2), "kg", _utc(w["sampleTime"]["physicalTime"]),
            None, p["name"], {"notes": w["notes"]} if w.get("notes") else None)


def _duration_s(x) -> float:
    """Protobuf Duration string ("3600s") -> seconds."""
    return _num(str(x).rstrip("s")) if x not in (None, "") else 0.0


def _heart_rate(p):
    v = p.get("heartRate")
    if not v or v.get("beatsPerMinuteAvg") is None:
        return None
    details = {"min_bpm": int(_num(v.get("beatsPerMinuteMin"))),
               "max_bpm": int(_num(v.get("beatsPerMinuteMax")))}
    return (float(round(_num(v["beatsPerMinuteAvg"]))), "bpm", _ymd(p["civilStartTime"]["date"]),
            None, None, details)


def _active_minutes(p):
    v = p.get("activeMinutes")
    levels = {e["activityLevel"].lower(): int(_num(e.get("activeMinutesSum")))
              for e in (v or {}).get("activeMinutesRollupByActivityLevel", [])
              if e.get("activityLevel")}
    if not levels:
        return None
    return float(sum(levels.values())), "min", _ymd(p["civilStartTime"]["date"]), None, None, levels


def _sedentary(p):
    v = p.get("sedentaryPeriod")
    if not v or v.get("durationSum") is None:
        return None
    return (float(round(_duration_s(v["durationSum"]) / 60)), "min",
            _ymd(p["civilStartTime"]["date"]), None, None, None)


def _hr_zones(p):
    v = p.get("timeInHeartRateZone")
    zones = {z["heartRateZone"].lower(): int(round(_duration_s(z.get("duration")) / 60))
             for z in (v or {}).get("timeInHeartRateZones", []) if z.get("heartRateZone")}
    if not zones:
        return None
    # The value is minutes at moderate intensity or above; "light" stays in details.
    value = float(sum(m for k, m in zones.items() if k != "light"))
    return value, "min", _ymd(p["civilStartTime"]["date"]), None, None, zones


def _vo2max(p):
    v = p.get("dailyVo2Max")
    if not v or v.get("vo2Max") is None:
        return None
    details = {}
    if v.get("cardioFitnessLevel"):
        details["fitness_level"] = v["cardioFitnessLevel"].lower().replace("_", " ")
    if "estimated" in v:
        details["estimated"] = bool(v["estimated"])
    return round(_num(v["vo2Max"]), 1), "ml/kg/min", _ymd(v["date"]), None, None, details or None


def _sleep_temperature(p):
    v = p.get("dailySleepTemperatureDerivations")
    if not v or v.get("nightlyTemperatureCelsius") is None:
        return None
    nightly = round(_num(v["nightlyTemperatureCelsius"]), 2)
    details = None
    if v.get("baselineTemperatureCelsius") is not None:
        base = round(_num(v["baselineTemperatureCelsius"]), 2)
        details = {"baseline_c": base, "vs_baseline_c": round(nightly - base, 2)}
    return nightly, "°C", _ymd(v["date"]), None, None, details


def _body_fat(p):
    b = p["bodyFat"]
    return (round(_num(b["percentage"]), 1), "%", _utc(b["sampleTime"]["physicalTime"]),
            None, p["name"], None)


METRICS = [
    Metric("steps", "Steps", "steps", "rollup", _rollup("steps", "countSum", unit="steps")),
    Metric("distance", "Distance", "distance", "rollup",
           _rollup("distance", "millimetersSum", 1e-6, 2, "km")),
    Metric("floors", "Floors climbed", "floors", "rollup", _rollup("floors", "countSum", unit="floors")),
    Metric("active_zone_minutes", "Active zone minutes", "active-zone-minutes", "rollup", _azm),
    Metric("calories", "Calories burned", "total-calories", "rollup",
           _rollup("totalCalories", "kcalSum", unit="kcal"), max_days=14),
    Metric("resting_hr", "Resting heart rate", "daily-resting-heart-rate", "daily",
           _daily("dailyRestingHeartRate", "beatsPerMinute", "bpm", 0),
           filter_field="daily_resting_heart_rate.date"),
    Metric("hrv", "Heart rate variability", "daily-heart-rate-variability", "daily",
           _daily("dailyHeartRateVariability", "averageHeartRateVariabilityMilliseconds", "ms",
                  extra=("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",)),
           filter_field="daily_heart_rate_variability.date"),
    Metric("spo2", "Blood oxygen (SpO2)", "daily-oxygen-saturation", "daily",
           _daily("dailyOxygenSaturation", "averagePercentage", "%",
                  extra=("lowerBoundPercentage", "upperBoundPercentage")),
           filter_field="daily_oxygen_saturation.date"),
    Metric("respiratory_rate", "Respiratory rate", "daily-respiratory-rate", "daily",
           _daily("dailyRespiratoryRate", "breathsPerMinute", "breaths/min"),
           filter_field="daily_respiratory_rate.date"),
    Metric("sleep", "Sleep", "sleep", "session", _sleep,
           filter_field="sleep.interval.civil_end_time", page_size=25),
    Metric("exercise", "Exercise", "exercise", "session", _exercise,
           filter_field="exercise.interval.civil_start_time", page_size=25),
    Metric("weight", "Weight", "weight", "sample", _weight,
           filter_field="weight.sample_time.civil_time"),
    # Added after reading the v4 data-types reference (docs/google-health-api-reader.md §4).
    Metric("heart_rate", "Heart rate", "heart-rate", "rollup", _heart_rate, max_days=14),
    Metric("active_minutes", "Active minutes", "active-minutes", "rollup", _active_minutes,
           max_days=14),
    Metric("active_calories", "Active calories", "active-energy-burned", "rollup",
           _rollup("activeEnergyBurned", "kcalSum", unit="kcal")),
    Metric("sedentary", "Sedentary time", "sedentary-period", "rollup", _sedentary),
    Metric("hr_zones", "Time in heart rate zones", "time-in-heart-rate-zone", "rollup", _hr_zones),
    Metric("vo2max", "Cardio fitness (VO2 max)", "daily-vo2-max", "daily", _vo2max,
           filter_field="daily_vo2_max.date"),
    Metric("sleep_temperature", "Sleep skin temperature", "daily-sleep-temperature-derivations",
           "daily", _sleep_temperature, filter_field="daily_sleep_temperature_derivations.date"),
    Metric("body_fat", "Body fat", "body-fat", "sample", _body_fat,
           filter_field="body_fat.sample_time.civil_time"),
]


# ---------------------------------------------------------------- source

class GoogleHealthMetric:
    """One metric as an ingest source. Daily totals stop at yesterday (only complete
    days); sessions and samples run through today."""
    source = "google_health"
    table = "health_metrics"
    columns = ["source", "external_id", "metric", "value", "unit", "start_at", "end_at",
               "details", "occurred_at"]

    def __init__(self, metric: Metric, client: HealthClient, today: date | None = None):
        self.m, self.client = metric, client
        self.run_name = f"{self.source}:{metric.name}"
        today = today or date.today()
        self.start = date.fromisoformat(WINDOW_START)
        self.end = today if metric.kind in ("rollup", "daily") else today + timedelta(days=1)

    def _points(self):
        if self.m.kind == "rollup":
            return self.client.daily_rollup(self.m.data_type, self.start, self.end, self.m.max_days)
        f = self.m.filter_field
        flt = f'{f} >= "{self.start.isoformat()}" AND {f} < "{self.end.isoformat()}"'
        return self.client.list(self.m.data_type, flt, self.m.page_size)

    def fetch(self):
        items = []
        for p in self._points():
            parsed = self.m.parse(p)
            if parsed is None:
                continue
            value, unit, start_at, end_at, ext, details = parsed
            items.append({
                "source": self.source,
                "external_id": ext or f"{self.m.name}/{start_at}",
                "metric": self.m.name,
                "value": value,
                "unit": unit,
                "start_at": start_at,
                "end_at": end_at,
                "details": json.dumps(details, sort_keys=True) if details else None,
                "occurred_at": f"{start_at}T00:00:00Z" if len(start_at) == 10 else start_at,
            })
        scope = ("source = ? AND metric = ? AND start_at >= ? AND start_at < ?",
                 (self.source, self.m.name, self.start.isoformat(), self.end.isoformat()))
        return items, scope

    def hashed_fields(self, item):
        return {k: item[k] for k in ("external_id", "metric", "value", "unit", "start_at",
                                     "end_at", "details")}

    def chunk_texts(self, row):
        return [_render(self.m, row)]


def _fmt_min(m: float) -> str:
    h, mm = divmod(int(round(m)), 60)
    return f"{h}h {mm:02d}m" if h else f"{mm}m"


def _local(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()


def _num_str(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,}"


def _render(m: Metric, row) -> str:
    d = json.loads(row["details"]) if row["details"] else {}
    if m.name == "sleep":
        s, e = _local(row["start_at"]), _local(row["end_at"])
        stages = ", ".join(f"{k} {_fmt_min(v)}" for k, v in d.get("stages", {}).items())
        lines = [f"Health: Sleep, night ending {e:%a %Y-%m-%d}",
                 f"Asleep {_fmt_min(row['value'])}, in bed {s:%H:%M} to {e:%H:%M}"
                 f"{'' if d.get('main_sleep', True) else ' (nap)'}"]
        if stages:
            lines.append(f"Stages: {stages}")
        return "\n".join(lines)
    if m.name == "exercise":
        s = _local(row["start_at"])
        bits = [_fmt_min(row["value"])]
        if "distance_km" in d:
            bits.append(f"{d['distance_km']} km")
        if "calories_kcal" in d:
            bits.append(f"{d['calories_kcal']} kcal")
        if "avg_hr_bpm" in d:
            bits.append(f"avg heart rate {d['avg_hr_bpm']} bpm")
        if "steps" in d:
            bits.append(f"{d['steps']:,} steps")
        text = f"Health: Exercise, {d.get('type', 'workout')} on {s:%a %Y-%m-%d %H:%M}\n" + ", ".join(bits)
        return text + (f"\nNotes: {d['notes']}" if d.get("notes") else "")
    when = (f"{date.fromisoformat(row['start_at']):%a %Y-%m-%d}" if len(row["start_at"]) == 10
            else f"{_local(row['start_at']):%a %Y-%m-%d %H:%M}")
    text = f"Health: {m.label}, {when}\n{m.label}: {_num_str(row['value'])} {row['unit']}"
    suffix = _DETAIL_SUFFIX.get(m.name)
    return text + (suffix(d) if suffix and d else "")


def _kv(d: dict) -> str:
    return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in d.items())


_DETAIL_SUFFIX = {
    "active_zone_minutes": lambda d: f" (fat burn {d['fat_burn']}, cardio {d['cardio']}, peak {d['peak']})",
    "heart_rate": lambda d: f" average (min {d['min_bpm']}, max {d['max_bpm']})",
    "active_minutes": lambda d: f" ({_kv(d)})",
    "hr_zones": lambda d: f" at moderate or above ({_kv(d)})",
    "vo2max": lambda d: f" ({d['fitness_level']})" if d.get("fitness_level") else "",
    "sleep_temperature": lambda d: f" ({d['vs_baseline_c']:+.2f} vs baseline {d['baseline_c']})",
}
