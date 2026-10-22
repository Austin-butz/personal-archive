"""`log`: push meals (calories, protein) and water to Google Health.

The only code that writes to Google. Each entry is a `nutrition-log` or `hydration-log`
data point made from numbers you supply ("anonymous food": no food-database lookup).
Every push is recorded in the local `health_writes` table with the data point's name,
so `log undo` can delete it again. Google doesn't allow editing anonymous nutrition
logs, so a wrong meal is fixed by undoing it and logging it again.
"""
import json
import re
from datetime import datetime, timedelta

from .google_auth import NUTRITION_WRITE
from .hashing import utc_iso
from .sources.google_health import API

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")
MEAL_MINUTES = 30  # a nutrition log is a session; give it a nominal length

# unit spelling -> (VolumeUnit enum, millilitres per unit)
_VOLUME = {
    "ml": ("MILLILITER", 1.0),
    "l": ("LITER", 1000.0),
    "oz": ("FLUID_OUNCE_US", 29.5735),
    "floz": ("FLUID_OUNCE_US", 29.5735),
    "cup": ("CUP_US", 236.588),
    "cups": ("CUP_US", 236.588),
}

DDL = """
CREATE TABLE IF NOT EXISTS health_writes (
    id          INTEGER PRIMARY KEY,
    data_type   TEXT NOT NULL,     -- 'nutrition-log' | 'hydration-log'
    name        TEXT,              -- Google's DataPoint name; NULL if the create didn't finish
    logged_for  TEXT NOT NULL,     -- UTC ISO start of the entry's interval
    summary     TEXT NOT NULL,     -- one human-readable line
    request     TEXT NOT NULL,     -- JSON body that was sent
    created_at  TEXT NOT NULL,
    deleted_at  TEXT               -- set by `log undo`
)"""


# ---------------------------------------------------------------- parsing

def parse_volume(text: str) -> tuple[float, str]:
    """'16oz', '500 ml', '1.5l', '2 cups' -> (millilitres, VolumeUnit). A unit is required."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(fl\s*oz|[a-z]+)\s*", text.lower())
    unit = m and _VOLUME.get(m.group(2).replace(" ", ""))
    if not unit:
        raise ValueError(f"can't read {text!r}: use a number and a unit "
                         f"({', '.join(_VOLUME)}), e.g. 16oz or 500ml")
    return round(float(m.group(1)) * unit[1], 1), unit[0]


def parse_time(text: str | None, now: datetime | None = None) -> datetime:
    """None -> now; 'HH:MM' -> today at that local time; otherwise ISO 8601.
    Times without an offset are local. Returns an aware datetime."""
    now = (now or datetime.now()).astimezone()
    if not text:
        return now
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        h, mi = map(int, text.split(":"))
        return now.replace(hour=h, minute=mi, second=0, microsecond=0)
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.astimezone()


# ---------------------------------------------------------------- request bodies

def _interval(start: datetime, end: datetime) -> dict:
    def off(t):
        return f"{int(t.utcoffset().total_seconds())}s"
    return {"startTime": utc_iso(start), "startUtcOffset": off(start),
            "endTime": utc_iso(end), "endUtcOffset": off(end)}


def hydration_body(ml: float, unit: str, at: datetime) -> dict:
    return {"hydrationLog": {"interval": _interval(at, at + timedelta(seconds=1)),
                             "amountConsumed": {"milliliters": ml, "userProvidedUnit": unit}}}


def meal_body(name: str, kcal: float, protein_g: float, meal: str | None, at: datetime) -> dict:
    if kcal < 0 or protein_g < 0:
        raise ValueError("calories and protein can't be negative")
    log = {"interval": _interval(at, at + timedelta(minutes=MEAL_MINUTES)),
           "foodDisplayName": name,
           "energy": {"kcal": kcal},
           "nutrients": [{"nutrient": "PROTEIN", "quantity": {"grams": protein_g}}]}
    if meal:
        log["mealType"] = meal.upper()
    return {"nutritionLog": log}


def describe(body: dict) -> str:
    """One line for previews and the local log."""
    if "hydrationLog" in body:
        h = body["hydrationLog"]
        return f"water {h['amountConsumed']['milliliters']:g} ml at {_local(h['interval'])}"
    n = body["nutritionLog"]
    protein = n["nutrients"][0]["quantity"]["grams"]
    meal = f" ({n['mealType'].lower()})" if "mealType" in n else ""
    return (f"meal{meal} {n['foodDisplayName']!r}: {n['energy']['kcal']:g} kcal, "
            f"{protein:g} g protein at {_local(n['interval'])}")


def _local(iv: dict) -> str:
    t = datetime.fromisoformat(iv["startTime"].replace("Z", "+00:00"))
    return t.astimezone().strftime("%Y-%m-%d %H:%M")


def data_type(body: dict) -> str:
    return "hydration-log" if "hydrationLog" in body else "nutrition-log"


# ---------------------------------------------------------------- API + local log

class HealthWriter:
    def __init__(self, session):
        self.s = session

    def create(self, dtype: str, body: dict) -> str | None:
        """Returns the new DataPoint's name, or None if Google hasn't finished the create."""
        r = self.s.post(f"{API}/{dtype}/dataPoints", json=body, timeout=60)
        _raise(r)
        op = r.json()
        if op.get("error"):
            raise RuntimeError(f"Google rejected the entry: {op['error']}")
        return op.get("response", {}).get("name") if op.get("done") else None

    def delete(self, dtype: str, names: list[str]):
        r = self.s.post(f"{API}/{dtype}/dataPoints:batchDelete", json={"names": names}, timeout=60)
        _raise(r)
        op = r.json()
        if op.get("error"):
            raise RuntimeError(f"Google refused the delete: {op['error']}")


def _raise(r):
    if r.status_code == 403:
        raise RuntimeError(f"Google said 403 ({r.text[:200]}). The token may lack "
                           f"{NUTRITION_WRITE}: re-run `secondbrain auth google`.")
    r.raise_for_status()


def push(db, writer: HealthWriter, body: dict, now: datetime | None = None) -> dict:
    db.execute(DDL)
    dtype = data_type(body)
    name = writer.create(dtype, body)
    start = body[next(iter(body))]["interval"]["startTime"]
    cur = db.execute(
        "INSERT INTO health_writes (data_type, name, logged_for, summary, request, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dtype, name, start, describe(body), json.dumps(body), utc_iso(now or datetime.now().astimezone())))
    db.commit()
    return {"id": cur.lastrowid, "name": name, "summary": describe(body)}


def entries(db, limit: int = 20) -> list[dict]:
    db.execute(DDL)
    return [dict(r) for r in db.execute(
        "SELECT id, data_type, name, logged_for, summary, created_at, deleted_at "
        "FROM health_writes ORDER BY id DESC LIMIT ?", (limit,))]


def find_undoable(db, entry_id: int | None = None) -> dict:
    """The given entry, or the most recent one not yet undone."""
    db.execute(DDL)
    if entry_id is None:
        row = db.execute("SELECT * FROM health_writes WHERE deleted_at IS NULL "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            raise LookupError("nothing to undo")
    else:
        row = db.execute("SELECT * FROM health_writes WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            raise LookupError(f"no entry #{entry_id}")
        if row["deleted_at"]:
            raise LookupError(f"entry #{entry_id} was already undone at {row['deleted_at']}")
    if not row["name"]:
        raise LookupError(f"entry #{row['id']} has no Google data point name (its create "
                          "didn't finish); delete it in the Fitbit app instead")
    return dict(row)


def undo(db, writer: HealthWriter, row: dict, now: datetime | None = None):
    writer.delete(row["data_type"], [row["name"]])
    db.execute("UPDATE health_writes SET deleted_at = ? WHERE id = ?",
               (utc_iso(now or datetime.now().astimezone()), row["id"]))
    db.commit()
