"""Google Health source, driven by a fake client that returns real API response shapes."""
from datetime import date

import pytest

from secondbrain import db as dbmod
from secondbrain.ingest import SafetyTripped, run
from secondbrain.search import search
from secondbrain.sources.google_health import METRICS, GoogleHealthMetric, HealthClient

from fakes import FakeEmbedder

TODAY = date(2026, 9, 26)
M = {m.name: m for m in METRICS}


def d(y, mo, dd):
    return {"year": y, "month": mo, "day": dd}


def steps_day(day, n):
    return {"civilStartTime": {"date": d(2026, 9, day), "time": {}},
            "civilEndTime": {"date": d(2026, 9, day + 1), "time": {}},
            "steps": {"countSum": str(n)}}


SLEEP = {"name": "users/1/dataTypes/sleep/dataPoints/abc", "sleep": {
    "interval": {"startTime": "2026-09-25T04:28:00Z", "startUtcOffset": "-14400s",
                 "endTime": "2026-09-25T11:40:00Z", "endUtcOffset": "-14400s"},
    "type": "STAGES", "metadata": {"stagesStatus": "SUCCEEDED", "processed": True, "mainSleep": True},
    "summary": {"minutesInSleepPeriod": "432", "minutesAsleep": "415", "minutesAwake": "17",
                "stagesSummary": [{"type": "AWAKE", "minutes": "17", "count": "3"},
                                  {"type": "LIGHT", "minutes": "219", "count": "13"},
                                  {"type": "DEEP", "minutes": "88", "count": "7"},
                                  {"type": "REM", "minutes": "108", "count": "5"}]},
    "updateTime": "2026-09-25T12:00:00Z"}}

EXERCISE = {"name": "users/1/dataTypes/exercise/dataPoints/868", "exercise": {
    "interval": {"startTime": "2026-09-25T23:27:07Z", "endTime": "2026-09-25T23:56:45Z"},
    "exerciseType": "WALKING", "displayName": "Walk", "activeDuration": "1778s",
    "metricsSummary": {"caloriesKcal": 171, "distanceMillimeters": 703800, "steps": "981",
                       "averageHeartRateBeatsPerMinute": "116", "activeZoneMinutes": "13"}}}

RHR = {"dailyRestingHeartRate": {"date": d(2026, 9, 25), "beatsPerMinute": "68",
                                 "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"}}}

WEIGHT = {"name": "users/1/dataTypes/weight/dataPoints/498", "weight": {
    "sampleTime": {"physicalTime": "2026-09-24T13:22:57.462302Z", "utcOffset": "-14400s"},
    "weightGrams": 80739}}


class FakeClient(HealthClient):
    def __init__(self, data):
        self.data, self.calls = data, []

    def list(self, data_type, flt, page_size):
        self.calls.append((data_type, flt))
        return self.data.get(data_type, [])

    def daily_rollup(self, data_type, start, end, max_days):
        self.calls.append((data_type, start, end, max_days))
        return self.data.get(data_type, [])


@pytest.fixture
def db(tmp_path):
    return dbmod.connect(tmp_path / "t.db")


def ingest(db, name, data, **kw):
    return run(db, GoogleHealthMetric(M[name], FakeClient(data), today=TODAY), FakeEmbedder(), **kw)


def rows(db, metric):
    return db.execute("SELECT * FROM health_metrics WHERE metric = ? AND stale_at IS NULL "
                      "ORDER BY start_at", (metric,)).fetchall()


def text(db, metric):
    return db.execute("SELECT c.text FROM chunks c JOIN health_metrics h ON h.id = c.source_row_id "
                      "WHERE c.source_type = 'health_metrics' AND h.metric = ?", (metric,)).fetchone()[0]


def test_daily_rollup_rows(db):
    s = ingest(db, "steps", {"steps": [steps_day(24, 8432), steps_day(25, 2034)]})
    assert (s["inserted"], s["chunks_added"]) == (2, 2)
    r = rows(db, "steps")
    assert [(x["start_at"], x["value"], x["occurred_at"]) for x in r] == [
        ("2026-09-24", 8432.0, "2026-09-24T00:00:00Z"), ("2026-09-25", 2034.0, "2026-09-25T00:00:00Z")]
    assert r[0]["external_id"] == "steps/2026-09-24"
    assert text(db, "steps").startswith("Health: Steps, Thu 2026-09-24\nSteps: 8,432 steps")


def test_rollups_stop_before_today_sessions_include_it(db):
    c = FakeClient({})
    GoogleHealthMetric(M["steps"], c, today=TODAY)._points()
    GoogleHealthMetric(M["exercise"], c, today=TODAY)._points()
    assert c.calls[0] == ("steps", date(2026, 1, 1), date(2026, 9, 26), 90)
    assert c.calls[1] == ("exercise", 'exercise.interval.civil_start_time >= "2026-01-01" '
                                      'AND exercise.interval.civil_start_time < "2026-09-27"')


def test_calories_use_14_day_requests():
    assert M["calories"].max_days == 14


def test_sleep_session(db):
    ingest(db, "sleep", {"sleep": [SLEEP]})
    r = rows(db, "sleep")[0]
    assert (r["value"], r["unit"], r["start_at"], r["end_at"]) == (
        415.0, "min", "2026-09-25T04:28:00Z", "2026-09-25T11:40:00Z")
    assert r["external_id"] == "users/1/dataTypes/sleep/dataPoints/abc"
    t = text(db, "sleep")
    assert "Asleep 6h 55m" in t and "deep 1h 28m" in t and "rem 1h 48m" in t


def test_sync_metadata_does_not_change_hash(db):
    ingest(db, "sleep", {"sleep": [SLEEP]})
    resynced = {**SLEEP, "sleep": {**SLEEP["sleep"], "updateTime": "2026-09-26T09:00:00Z"}}
    s = ingest(db, "sleep", {"sleep": [resynced]})
    assert (s["inserted"], s["marked_stale"]) == (0, 0)


def test_exercise(db):
    ingest(db, "exercise", {"exercise": [EXERCISE]})
    r = rows(db, "exercise")[0]
    assert (r["value"], r["unit"]) == (29.6, "min")
    t = text(db, "exercise")
    assert "Walk" in t and "0.7 km" in t and "171 kcal" in t and "avg heart rate 116 bpm" in t


def test_daily_summary_and_weight(db):
    ingest(db, "resting_hr", {"daily-resting-heart-rate": [RHR]})
    ingest(db, "weight", {"weight": [WEIGHT]})
    assert (rows(db, "resting_hr")[0]["value"], rows(db, "resting_hr")[0]["start_at"]) == (68.0, "2026-09-25")
    w = rows(db, "weight")[0]
    assert (w["value"], w["unit"], w["start_at"]) == (80.74, "kg", "2026-09-24T13:22:57Z")


def test_revised_day_replaces_row(db):
    ingest(db, "steps", {"steps": [steps_day(24, 8000)]})
    s = ingest(db, "steps", {"steps": [steps_day(24, 8432)]})  # late sync
    assert (s["inserted"], s["marked_stale"]) == (1, 1)
    assert [r["value"] for r in rows(db, "steps")] == [8432.0]


def test_empty_sleep_response_does_not_stale_sleep(db):
    ingest(db, "sleep", {"sleep": [SLEEP]})
    ingest(db, "steps", {"steps": [steps_day(24, 8432)]})
    with pytest.raises(SafetyTripped):
        ingest(db, "sleep", {"sleep": []})  # the flaky empty response
    assert len(rows(db, "sleep")) == 1
    # and other metrics are unaffected by sleep's scope
    s = ingest(db, "steps", {"steps": [steps_day(24, 8432)]})
    assert (s["inserted"], s["marked_stale"]) == (0, 0)


def test_run_log_names_the_metric(db):
    ingest(db, "steps", {"steps": [steps_day(24, 8432)]})
    assert db.execute("SELECT source FROM ingest_runs").fetchone()[0] == "google_health:steps"


def test_search_finds_sleep_by_date(db):
    ingest(db, "sleep", {"sleep": [SLEEP]})
    ingest(db, "steps", {"steps": [steps_day(24, 8432)]})
    hits = search(db, FakeEmbedder(), "sleep asleep stages", limit=5, since="2026-09-25")
    assert hits and hits[0]["text"].startswith("Health: Sleep")


# ---------------------------------------------------------------- metrics added from the v4 reference

def rollup_day(day, **fields):
    return {"civilStartTime": {"date": d(2026, 9, day), "time": {}},
            "civilEndTime": {"date": d(2026, 9, day + 1), "time": {}}, **fields}


def test_heart_rate_daily_rollup(db):
    p = rollup_day(24, heartRate={"beatsPerMinuteAvg": 71.4, "beatsPerMinuteMin": 52, "beatsPerMinuteMax": 143})
    ingest(db, "heart_rate", {"heart-rate": [p]})
    r = rows(db, "heart_rate")[0]
    assert (r["value"], r["unit"], r["start_at"]) == (71.0, "bpm", "2026-09-24")
    assert text(db, "heart_rate").endswith("Heart rate: 71 bpm average (min 52, max 143)")


def test_heart_rate_and_active_minutes_use_14_day_requests():
    assert (M["heart_rate"].max_days, M["active_minutes"].max_days) == (14, 14)
    assert M["active_calories"].max_days == 90


def test_active_minutes_by_level(db):
    p = rollup_day(24, activeMinutes={"activeMinutesRollupByActivityLevel": [
        {"activityLevel": "LIGHTLY_ACTIVE", "activeMinutesSum": "210"},
        {"activityLevel": "FAIRLY_ACTIVE", "activeMinutesSum": "25"},
        {"activityLevel": "VERY_ACTIVE", "activeMinutesSum": "40"}]})
    ingest(db, "active_minutes", {"active-minutes": [p]})
    r = rows(db, "active_minutes")[0]
    assert (r["value"], r["unit"]) == (275.0, "min")
    assert "fairly active 25" in text(db, "active_minutes")


def test_sedentary_and_active_calories(db):
    ingest(db, "sedentary", {"sedentary-period": [rollup_day(24, sedentaryPeriod={"durationSum": "27000s"})]})
    ingest(db, "active_calories", {"active-energy-burned": [rollup_day(24, activeEnergyBurned={"kcalSum": 612.7})]})
    assert rows(db, "sedentary")[0]["value"] == 450.0
    assert (rows(db, "active_calories")[0]["value"], rows(db, "active_calories")[0]["unit"]) == (613.0, "kcal")


def test_hr_zones_value_excludes_light(db):
    p = rollup_day(24, timeInHeartRateZone={"timeInHeartRateZones": [
        {"heartRateZone": "LIGHT", "duration": "72000s"},
        {"heartRateZone": "MODERATE", "duration": "1800s"},
        {"heartRateZone": "VIGOROUS", "duration": "600s"},
        {"heartRateZone": "PEAK", "duration": "0s"}]})
    ingest(db, "hr_zones", {"time-in-heart-rate-zone": [p]})
    r = rows(db, "hr_zones")[0]
    assert r["value"] == 40.0
    assert "light 1200" in text(db, "hr_zones") and "at moderate or above" in text(db, "hr_zones")


def test_vo2max_and_sleep_temperature(db):
    ingest(db, "vo2max", {"daily-vo2-max": [{"dailyVo2Max": {
        "date": d(2026, 9, 24), "vo2Max": 41.23, "cardioFitnessLevel": "GOOD", "estimated": True}}]})
    ingest(db, "sleep_temperature", {"daily-sleep-temperature-derivations": [
        {"dailySleepTemperatureDerivations": {"date": d(2026, 9, 24), "nightlyTemperatureCelsius": 33.61,
                                              "baselineTemperatureCelsius": 33.2}}]})
    v = rows(db, "vo2max")[0]
    assert (v["value"], v["unit"]) == (41.2, "ml/kg/min") and "(good)" in text(db, "vo2max")
    t = rows(db, "sleep_temperature")[0]
    assert (t["value"], t["unit"]) == (33.61, "°C")
    assert "+0.41 vs baseline 33.2" in text(db, "sleep_temperature")


def test_body_fat_sample(db):
    ingest(db, "body_fat", {"body-fat": [{"name": "users/1/dataTypes/body-fat/dataPoints/7", "bodyFat": {
        "sampleTime": {"physicalTime": "2026-09-24T13:22:57Z"}, "percentage": 21.46}}]})
    r = rows(db, "body_fat")[0]
    assert (r["value"], r["unit"], r["start_at"], r["external_id"]) == (
        21.5, "%", "2026-09-24T13:22:57Z", "users/1/dataTypes/body-fat/dataPoints/7")


def test_empty_rollup_points_are_skipped(db):
    s = ingest(db, "heart_rate", {"heart-rate": [rollup_day(24), rollup_day(25, heartRate={})]})
    assert s["fetched"] == 0


def test_every_metric_has_a_distinct_name_and_data_type():
    assert len({m.name for m in METRICS}) == len(METRICS) == 20
    assert len({m.data_type for m in METRICS}) == len(METRICS)
    for m in METRICS:
        assert m.kind == "rollup" or m.filter_field, m.name
