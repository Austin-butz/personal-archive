"""`log`: request bodies, the local log, and undo, against a fake Google session."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from secondbrain import db as dbmod
from secondbrain import health_log as hl
from secondbrain.sources.google_health import API

EDT = timezone(timedelta(hours=-4))
AT = datetime(2026, 10, 22, 19, 0, tzinfo=EDT)


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Answers create and batchDelete the way the docs show: a done Operation."""
    def __init__(self, status=200, done=True):
        self.calls, self.status, self.done, self.n = [], status, done, 0

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        if url.endswith(":batchDelete"):
            return Resp({"done": True, "response": {}}, self.status)
        self.n += 1
        dtype = url.split("/dataTypes/")[1].split("/")[0]
        op = {"done": self.done}
        if self.done:
            op["response"] = {"name": f"users/1/dataTypes/{dtype}/dataPoints/{self.n}"}
        return Resp(op, self.status)


@pytest.fixture
def db(tmp_path):
    return dbmod.connect(tmp_path / "t.db")


@pytest.mark.parametrize("text,ml,unit", [("16oz", 473.2, "FLUID_OUNCE_US"),
                                          ("16 fl oz", 473.2, "FLUID_OUNCE_US"),
                                          ("500ml", 500.0, "MILLILITER"),
                                          ("1.5 L", 1500.0, "LITER"),
                                          ("2 cups", 473.2, "CUP_US")])
def test_parse_volume(text, ml, unit):
    assert hl.parse_volume(text) == (ml, unit)


@pytest.mark.parametrize("text", ["16", "lots", "16 gallons", ""])
def test_parse_volume_needs_a_known_unit(text):
    with pytest.raises(ValueError):
        hl.parse_volume(text)


def test_parse_time_hhmm_is_today_local():
    now = datetime(2026, 10, 22, 8, 15, 30, tzinfo=EDT)
    assert hl.parse_time("13:30", now) == datetime(2026, 10, 22, 13, 30, tzinfo=EDT)
    assert hl.parse_time(None, now) == now
    assert hl.parse_time("2026-10-21T07:00:00-04:00", now) == datetime(2026, 10, 21, 7, tzinfo=EDT)


def test_meal_body_matches_the_api_shape():
    b = hl.meal_body("chicken and rice", 650, 45, "dinner", AT)["nutritionLog"]
    assert b["interval"] == {"startTime": "2026-10-22T23:00:00Z", "startUtcOffset": "-14400s",
                             "endTime": "2026-10-22T23:30:00Z", "endUtcOffset": "-14400s"}
    assert b["energy"] == {"kcal": 650}
    assert b["nutrients"] == [{"nutrient": "PROTEIN", "quantity": {"grams": 45}}]
    assert (b["foodDisplayName"], b["mealType"]) == ("chicken and rice", "DINNER")
    assert "mealType" not in hl.meal_body("x", 1, 1, None, AT)["nutritionLog"]
    with pytest.raises(ValueError):
        hl.meal_body("x", -1, 0, None, AT)


def test_hydration_body_matches_the_api_shape():
    b = hl.hydration_body(473.2, "FLUID_OUNCE_US", AT)["hydrationLog"]
    assert b["amountConsumed"] == {"milliliters": 473.2, "userProvidedUnit": "FLUID_OUNCE_US"}
    assert b["interval"]["endTime"] == "2026-10-22T23:00:01Z"


def test_push_posts_and_records(db):
    s = FakeSession()
    r = hl.push(db, hl.HealthWriter(s), hl.meal_body("eggs", 300, 20, "breakfast", AT))
    assert s.calls[0][0] == f"{API}/nutrition-log/dataPoints"
    assert r["name"] == "users/1/dataTypes/nutrition-log/dataPoints/1"
    [e] = hl.entries(db)
    assert (e["data_type"], e["logged_for"], e["deleted_at"]) == ("nutrition-log", "2026-10-22T23:00:00Z", None)
    assert "300 kcal, 20 g protein" in e["summary"]


def test_undo_deletes_the_latest_then_the_one_before(db):
    s = FakeSession()
    w = hl.HealthWriter(s)
    hl.push(db, w, hl.meal_body("eggs", 300, 20, None, AT))
    hl.push(db, w, hl.hydration_body(500, "MILLILITER", AT))
    row = hl.find_undoable(db)
    assert row["data_type"] == "hydration-log"
    hl.undo(db, w, row)
    assert s.calls[-1] == (f"{API}/hydration-log/dataPoints:batchDelete",
                           {"names": ["users/1/dataTypes/hydration-log/dataPoints/2"]})
    assert hl.find_undoable(db)["data_type"] == "nutrition-log"
    with pytest.raises(LookupError, match="already undone"):
        hl.find_undoable(db, row["id"])


def test_nothing_to_undo(db):
    with pytest.raises(LookupError):
        hl.find_undoable(db)


def test_unfinished_create_is_recorded_but_not_undoable(db):
    r = hl.push(db, hl.HealthWriter(FakeSession(done=False)), hl.hydration_body(250, "MILLILITER", AT))
    assert r["name"] is None
    with pytest.raises(LookupError, match="no Google data point name"):
        hl.find_undoable(db)


def test_403_points_at_reauth_and_records_nothing(db):
    with pytest.raises(RuntimeError, match="auth google"):
        hl.push(db, hl.HealthWriter(FakeSession(status=403)), hl.hydration_body(250, "MILLILITER", AT))
    assert hl.entries(db) == []
