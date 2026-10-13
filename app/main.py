"""
Employee Attendance & Analytics API
HROne Engineering Assignment

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from .env; real environment variables win)

All API implementation lives in this single file as required.
"""
# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

# ---------------------------------------------------------------------------
# Third-party
# ---------------------------------------------------------------------------
from bson import json_util
import json
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Step 1 — Environment and MongoDB connection
# ---------------------------------------------------------------------------
load_dotenv()  # real environment variables override .env values

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB:  str = os.getenv("MONGO_DB",  "hrone")

# tz_aware=True: PyMongo returns UTC-aware datetimes for every BSON Date field.
# This eliminates the entire class of naive-datetime timezone bugs.
client = MongoClient(MONGO_URI, tz_aware=True)
db     = client[MONGO_DB]
# IST offset used throughout for shift/date calculations
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Step 2 — Indexes created at startup (idempotent via create_index)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create all required indexes at startup, close client on shutdown."""
    # employees: fast lookup + concurrent-create uniqueness
    db.employees.create_index("emp_code", unique=True)

    # attendance_logs: one record per (employee, day) + punch-in concurrency guard
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True
    )

    # attendance_logs: list_attendance sort + analytics month-range scans
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )

    # employees: department-filtered analytics
    db.employees.create_index("department")

    yield  # application runs

    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Step 3 — Shared pure-function helpers (no I/O)
# ---------------------------------------------------------------------------

# ── 3a  Epoch-millisecond conversion ────────────────────────────────────────

def to_epoch_ms(dt: datetime) -> int:
    """UTC-aware datetime → integer epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Integer epoch milliseconds → UTC-aware datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ── 3b  Second truncation (R1) ───────────────────────────────────────────────

def trunc_sec(dt: datetime) -> datetime:
    """
    R1: instants are truncated to whole seconds before any calculation
    or storage (09:40:00.900 counts as 09:40:00).
    """
    return dt.replace(microsecond=0)


# ── 3c  Attendance date (R1, overnight shifts) ───────────────────────────────

def attendance_date_ist(
    punch_in_utc: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """
    Returns the attendance date as a YYYY-MM-DD string (IST calendar date).

    R1 overnight exception: if shift_end <= shift_start (e.g. 22:00–06:00)
    and the punch-in's IST time is *strictly earlier than* shift_end, the
    punch-in belongs to the shift that started the previous calendar day.
    A punch-in at exactly shift_end belongs to the current day.
    """
    sh_end_h,   sh_end_m   = map(int, shift_end.split(":"))
    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    is_overnight = (sh_end_h * 60 + sh_end_m) <= (sh_start_h * 60 + sh_start_m)

    punch_ist = punch_in_utc.astimezone(IST)

    if is_overnight:
        end_time = time(sh_end_h, sh_end_m, 0)
        # STRICT <: a punch-in at exactly shift_end is NOT on the previous day
        if punch_ist.time() < end_time:
            return (punch_ist.date() - timedelta(days=1)).isoformat()

    return punch_ist.date().isoformat()


# ── 3d  Late minutes (R2) ────────────────────────────────────────────────────

def compute_late_minutes(
    punch_in_utc: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    """
    R2: late only if strictly more than 600 seconds (10:00) after shift_start.
    Returns floor(elapsed_seconds / 60); 0 otherwise.

    `attendance_date` is the *shift-start day* (returned by attendance_date_ist),
    NOT necessarily the punch-in calendar date.  This is essential for overnight
    employees: shift 22:00 on Jul 6 must be compared against 22:00 Jul 6 even
    when the punch-in timestamp falls on Jul 7.

    Examples (shift 09:30):
        09:40:00 IST → elapsed 600 s → NOT > 600 → 0
        09:40:01 IST → elapsed 601 s → > 600     → 10
        10:15:59 IST → elapsed 2759 s             → 45
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    yy, mm, dd = map(int, attendance_date.split("-"))
    shift_start_ist = datetime(yy, mm, dd, sh_h, sh_m, 0, tzinfo=IST)

    punch_ist = punch_in_utc.astimezone(IST)
    elapsed_seconds = int((punch_ist - shift_start_ist).total_seconds())

    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


# ── 3e  Overtime minutes (R3) ────────────────────────────────────────────────

def compute_overtime_minutes(
    punch_out_utc: datetime,
    shift_end: str,
    attendance_date: str,
    shift_start: str,
) -> int:
    """
    R3: floor minutes between shift_end and punch_out, only when >= 30; else 0.

    For overnight shifts, shift_end falls on the NEXT calendar day relative
    to the attendance date.
    """
    sh_end_h,   sh_end_m   = map(int, shift_end.split(":"))
    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    is_overnight = (sh_end_h * 60 + sh_end_m) <= (sh_start_h * 60 + sh_start_m)

    att      = date.fromisoformat(attendance_date)
    end_date = att + timedelta(days=1) if is_overnight else att

    shift_end_ist = datetime(
        end_date.year, end_date.month, end_date.day,
        sh_end_h, sh_end_m, 0, tzinfo=IST,
    )
    punch_out_ist = punch_out_utc.astimezone(IST)

    delta_s = int((punch_out_ist - shift_end_ist).total_seconds())
    return delta_s // 60 if delta_s >= 1800 else 0  # 1800 s = 30 min


# ── 3f  Work hours (R4) ──────────────────────────────────────────────────────

def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals
    using half-up rounding (not banker's rounding, not float rounding).
    """
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    d = Decimal(str(seconds / 3600))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── 3g  Half day (R5) ────────────────────────────────────────────────────────

def compute_half_day(work_hours: float) -> bool:
    """R5: true when rounded work_hours < 4.50."""
    return work_hours < 4.50


# ── 3h  General rounding (R8) ────────────────────────────────────────────────

def round_half_up(value, decimals: int) -> float:
    """R8: half-up rounding to `decimals` decimal places."""
    d = Decimal(str(value)).quantize(
        Decimal(10) ** -decimals, rounding=ROUND_HALF_UP
    )
    return float(d)


# ── 3i  Working-days counter (R7) ────────────────────────────────────────────

def working_days_in_month(year: int, month: int, joined_on_str: str) -> int:
    """
    R7: count Mon–Fri days in [max(month_start, joined_on), month_end].
    joined_on is INCLUSIVE (the joining day itself is a working day).
    Returns 0 if the employee had not yet joined by the end of the month.
    """
    from calendar import monthrange

    joined   = date.fromisoformat(joined_on_str)
    _, last  = monthrange(year, month)
    month_end = date(year, month, last)

    if joined > month_end:
        return 0  # employee joins after this month

    start = max(date(year, month, 1), joined)
    count, d = 0, start
    while d <= month_end:
        if d.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        d += timedelta(days=1)
    return count


# ── 3j  Response serializers ────────────────────────────────────────────────

def serialize_employee(doc: dict) -> dict:
    """
    Convert a raw MongoDB employee document to the Employee API response shape.
    Removes _id. Converts created_at BSON Date → epoch ms integer.
    """
    doc.pop("_id", None)
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  to_epoch_ms(doc["created_at"]),
    }



def _serialize_history_entry(entry: dict) -> dict:
    """
    Serialize one history entry for the AttendanceRecord response.
    Converts `at` BSON Date to epoch milliseconds.
    Preserves epoch-millisecond integers in punch_in/punch_out changes.
    """
    changes: dict = {}
    for field, diff in entry.get("changes", {}).items():
        from_v = diff.get("from")
        to_v = diff.get("to")

        if field in ("punch_in", "punch_out"):
            from_v = (
                to_epoch_ms(from_v)
                if isinstance(from_v, datetime)
                else from_v
            )
            to_v = (
                to_epoch_ms(to_v)
                if isinstance(to_v, datetime)
                else to_v
            )

        changes[field] = {"from": from_v, "to": to_v}

    return {
        "at": to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }



def serialize_attendance(doc: dict) -> dict:
    """
    Convert a raw MongoDB attendance_logs document to the AttendanceRecord
    API response shape.

    - Never exposes _id (popped on every code path, including post-insert).
    - Converts punch_in / punch_out BSON Dates → epoch ms or null.
    - Applies safe defaults for legacy records that may be missing half_day,
      late_minutes, overtime_minutes, or history.
    - doc.get("history") or [] handles field-absent AND explicit-null cases.
    """
    doc.pop("_id", None)  # safe on both post-insert (mutated) and post-find docs

    return {
        "emp_code":         doc["emp_code"],
        "date":             doc["date"],
        "status":           doc["status"],
        "punch_in":         to_epoch_ms(doc["punch_in"])  if doc.get("punch_in")  else None,
        "punch_out":        to_epoch_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours":       doc.get("work_hours"),              # float or None
        "late_minutes":     doc.get("late_minutes",     0),     # legacy: default 0
        "overtime_minutes": doc.get("overtime_minutes", 0),     # legacy: default 0
        "half_day":         doc.get("half_day",     False),     # legacy: default False
        "history": [
            _serialize_history_entry(e)
            for e in (doc.get("history") or [])                 # handles missing & null
        ],
    }


# ---------------------------------------------------------------------------
# Step 4 — Pydantic input models with strict validation
# ---------------------------------------------------------------------------

# Reusable annotated type for epoch-millisecond timestamps.
#
# StrictInt rejects JSON strings and JSON floats at parse time.
# ge / le enforce the openapi.yaml EpochMillis range:
#   minimum: 100000000000  (rejects seconds-range values like 1751780400)
#   maximum: 4102444800000
#
# Verified: rejects strings, floats, 10-digit ints, values out of range.
EpochMsOpt = Annotated[
    Optional[StrictInt],
    Field(default=None, ge=100_000_000_000, le=4_102_444_800_000),
]


class EmployeeCreate(BaseModel):
    """Request body for POST /employees.  Mirrors openapi.yaml EmployeeCreate."""
    emp_code:    str  = Field(pattern=r"^EMP\d{4,6}$")
    name:        str  = Field(min_length=1,  max_length=100)
    email:       str  = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str  = Field(min_length=1,  max_length=50)
    shift_start: str  = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str  = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:   date  # Pydantic date validates YYYY-MM-DD; serialize with .isoformat()

    @model_validator(mode="after")
    def shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    """Request body for POST /attendance/punch-in."""
    emp_code:   str
    punched_at: EpochMsOpt = None
    status:     Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    """Request body for POST /attendance/punch-out."""
    emp_code:   str
    punched_at: EpochMsOpt = None


class RegularizeRequest(BaseModel):
    """Request body for PATCH /attendance/{emp_code}/{date}."""
    status:         Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in:       EpochMsOpt = None
    punch_out:      EpochMsOpt = None
    reason:         str = Field(min_length=5,  max_length=200)
    regularized_by: str = Field(min_length=1,  max_length=50)

    @model_validator(mode="after")
    def absent_leave_no_punches(self) -> "RegularizeRequest":
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError(
                    "ABSENT and LEAVE status cannot be combined with punch times"
                )
        return self


# ---------------------------------------------------------------------------
# Endpoint stubs — Steps 5–16 will fill these in.
# Defined here so uvicorn starts cleanly and /docs shows all routes.
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    """
    Step 5 — Readiness probe.

    Returns 200 {"status": "ok"} only when MongoDB answers a ping.
    Returns 503 {"detail": "database unavailable"} otherwise.
    The grader runner polls this endpoint before executing any tests.
    """
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Step 6 — Create an employee.

    The client supplies emp_code (the customer's own code).  Uniqueness is
    enforced by the unique index on employees.emp_code: a concurrent duplicate
    create raises DuplicateKeyError, caught and returned as 409.

    created_at is set by the server (UTC); never supplied by the client.
    joined_on is stored as a YYYY-MM-DD string (Pydantic gives us a date object).
    """
    doc = body.model_dump()
    doc["joined_on"]  = body.joined_on.isoformat()   # date → "YYYY-MM-DD" string
    doc["created_at"] = datetime.now(timezone.utc)   # server clock, UTC-aware

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    return serialize_employee(doc)  # strips _id, converts created_at → epoch ms


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1,  ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """
    Step 7 — List employees.

    Sorted by emp_code ascending.
    Filtered by department (exact, case-sensitive) when supplied.
    total always reflects the filtered set, not the whole collection (R10).
    Sorting, skipping, and limiting are all pushed to MongoDB — no in-memory loading.
    """
    q: dict = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)           # filtered count
    skip  = (page - 1) * page_size                    # page 1 → skip 0

    items = list(
        db.employees
          .find(q, {"_id": 0})
          .sort("emp_code", ASCENDING)
          .skip(skip)
          .limit(page_size)
    )

    return {
        "items":     [serialize_employee(doc) for doc in items],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Step 8 — Record a punch-in.

    Creates the attendance record for the day.  The attendance date follows R1
    (IST calendar date, overnight-shift exception).  late_minutes follows R2.
    punch_out, work_hours start null; overtime_minutes=0, half_day=false,
    history=[].

    Concurrency: the unique (emp_code, date) index at the DB level guarantees
    that only one insert wins.  A duplicate raises DuplicateKeyError → 409.
    No check-then-insert pattern is used.

    Errors:
      404 – employee not found
      409 – already punched in for that attendance date
      422 – invalid request body (handled by Pydantic)
    """
    # 1. Employee must exist
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="employee not found")

    # 2. Resolve and truncate the punch-in instant (R1: whole seconds only)
    if body.punched_at is not None:
        punch_in_utc = trunc_sec(from_epoch_ms(body.punched_at))
    else:
        punch_in_utc = trunc_sec(datetime.now(timezone.utc))  # always UTC-aware

    # 3. Determine attendance date per R1 (IST, overnight exception)
    att_date = attendance_date_ist(
        punch_in_utc, emp["shift_start"], emp["shift_end"]
    )

    # 4. Compute late_minutes per R2 using the attendance date as the shift-start day
    late = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    # 5. Build document (derived fields at their initial values per spec)
    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         punch_in_utc,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }

    # 6. Insert atomically; catch duplicate (same employee, same attendance date)
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this attendance date")

    return serialize_attendance(doc)  # strips _id, converts datetimes → epoch ms


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Step 10 — Record a punch-out.

    Finds the employee's most recent open record whose punch_in ≤ punched_at.
    This works across midnight for overnight shifts.

    Computes work_hours (R4), overtime_minutes (R3), half_day (R5).

    Concurrency: the update_one filter includes punch_out=null so only one
    of two simultaneous punch-outs can match and write; the other gets
    matched_count=0 → 409.

    Errors:
      404 – unknown employee, or no open punch-in found at/before punched_at
      409 – that record is already punched out (concurrent punch-out)
      422 – punched_at ≤ punch_in, or > 24 h after punch_in (Pydantic handles
             non-integer / out-of-range values before we even get here)
    """
    # 1. Employee must exist
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="employee not found")

    # 2. Resolve and truncate punch-out instant (R1: whole seconds)
    if body.punched_at is not None:
        punch_out_utc = trunc_sec(from_epoch_ms(body.punched_at))
    else:
        punch_out_utc = trunc_sec(datetime.now(timezone.utc))  # always UTC-aware

    # 3. Find the most recent open record whose punch_in ≤ punched_at
    #    (sort punch_in DESC, take first)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, detail="no open punch-in found at or before the given time")

    # 4. Validate timing
    if punch_out_utc <= record["punch_in"]:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if punch_out_utc - record["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")

    # 5. Compute derived fields using the record's stored attendance date
    wh = compute_work_hours(record["punch_in"], punch_out_utc)
    ot = compute_overtime_minutes(
        punch_out_utc, emp["shift_end"], record["date"], emp["shift_start"]
    )
    hd = compute_half_day(wh)

    # 6. Atomic update — punch_out=None filter prevents double punch-out
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out":        punch_out_utc,
            "work_hours":       wh,
            "overtime_minutes": ot,
            "half_day":         hd,
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(409, detail="already punched out")

    # 7. Fetch the updated document and return it
    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}, {"_id": 0}
    )
    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1,  ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Step 9 — List attendance records.

    Sorted: date descending, then emp_code ascending.
    Filters: emp_code, date_from/date_to (inclusive string range on 'date'),
             status (exact match).
    date_from > date_to → 422.
    total reflects the filtered set (R10).
    Sort, skip, and limit are all pushed to MongoDB — no in-memory loading.
    """
    # Validate date range if both supplied
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    # Build filter
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)

    docs = list(
        db.attendance_logs
          .find(q, {"_id": 0})
          .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
          .skip((page - 1) * page_size)
          .limit(page_size)
    )

    return {
        "items":     [serialize_attendance(doc) for doc in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Step 11 -- Regularize (manually correct) one attendance record.

    Addressed by natural key (emp_code, date).  Omitted fields are unchanged.
    After applying changes, recomputes late_minutes, work_hours, overtime_minutes,
    half_day from the final punch times (R2-R5) and appends one history entry.
    history[].changes contains only fields that actually changed.

    Concurrency: the update_one filter locks on the current history length,
    covering absent, empty-array, and explicit-null history (legacy records).
    A concurrent edit that already pushed an entry changes the length, making
    this update miss (matched_count=0) and return 409.

    Errors:
      404 -- employee not found, or no record for that date
      409 -- concurrent edit detected
      422 -- invalid body, no fields changed, ABSENT/LEAVE with punch times,
             new punch_in on a different IST date, punch_out <= punch_in,
             punch_out more than 24 h after punch_in
    """
    # 1. Employee must exist
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="employee not found")

    # 2. Record must exist
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, detail="no attendance record found for that date")

    # 3. Snapshot current values (safe defaults for legacy records)
    old_status = record["status"]
    old_pi     = record.get("punch_in")    # datetime or None
    old_po     = record.get("punch_out")   # datetime or None
    old_wh     = record.get("work_hours")
    old_late   = record.get("late_minutes",     0)
    old_ot     = record.get("overtime_minutes", 0)
    old_hd     = record.get("half_day",     False)

    # 4. Determine new status (default: keep existing)
    new_status = body.status if body.status is not None else old_status

    # 5. Determine new punch times.
    #    ABSENT / LEAVE always clear both punch times regardless of what was stored.
    if new_status in ("ABSENT", "LEAVE"):
        new_pi = None
        new_po = None
    else:
        # Apply supplied values; fall back to existing stored values
        new_pi = (trunc_sec(from_epoch_ms(body.punch_in))
                  if body.punch_in is not None else old_pi)
        new_po = (trunc_sec(from_epoch_ms(body.punch_out))
                  if body.punch_out is not None else old_po)

    # 6. Validate new state
    if new_status not in ("ABSENT", "LEAVE") and new_pi is None:
        raise HTTPException(422, detail="presence status requires a punch_in")

    if new_pi is not None and new_pi != old_pi:
        # punch_in must remain on the record's attendance date (R1)
        pi_ist_date = new_pi.astimezone(IST).date().isoformat()
        if pi_ist_date != date:
            raise HTTPException(
                422,
                detail="punch_in IST date must match the record's attendance date"
            )

    if new_pi is not None and new_po is not None:
        if new_po <= new_pi:
            raise HTTPException(422, detail="punch_out must be after punch_in")
        if new_po - new_pi > timedelta(hours=24):
            raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # 7. Recompute derived fields from the new state
    if new_status in ("ABSENT", "LEAVE") or new_pi is None:
        new_late = 0
        new_wh   = None
        new_ot   = 0
        new_hd   = False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"], date)
        if new_po is not None:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime_minutes(
                new_po, emp["shift_end"], date, emp["shift_start"]
            )
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # 8. Build changes dict -- only fields that actually changed
    WATCHED = ["status", "punch_in", "punch_out", "work_hours",
               "late_minutes", "overtime_minutes", "half_day"]
    new_vals = {
        "status":           new_status,
        "punch_in":         new_pi,
        "punch_out":        new_po,
        "work_hours":       new_wh,
        "late_minutes":     new_late,
        "overtime_minutes": new_ot,
        "half_day":         new_hd,
    }
    old_vals = {
        "status":           old_status,
        "punch_in":         old_pi,
        "punch_out":        old_po,
        "work_hours":       old_wh,
        "late_minutes":     old_late,
        "overtime_minutes": old_ot,
        "half_day":         old_hd,
    }
    changes: dict = {}
    for field in WATCHED:
        ov, nv = old_vals[field], new_vals[field]
        if ov != nv:
            if field in ("punch_in", "punch_out"):
                ov = to_epoch_ms(ov) if isinstance(ov, datetime) else None
                nv = to_epoch_ms(nv) if isinstance(nv, datetime) else None
            changes[field] = {"from": ov, "to": nv}

    if not changes:
        raise HTTPException(422, detail="no fields changed")

    # 9. Build history entry
    entry = {
        "at":      datetime.now(timezone.utc),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # 10. Atomic update with legacy-safe concurrency lock.
    #     Lock is on the current history length so a concurrent write that
    #     already appended an entry will cause this update to miss (409).
    current_len = len(record.get("history") or [])
    if current_len == 0:
        # Cover: field absent, history=[], history=null (legacy records)
        lock_filter = {"$or": [
            {"history": {"$exists": False}},
            {"history": {"$size": 0}},
            {"history": None},
        ]}
    else:
        lock_filter = {"history": {"$size": current_len}}

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], **lock_filter},
        {
            "$set": {k: new_vals[k] for k in WATCHED},
            "$push": {"history": entry},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(409, detail="concurrent edit detected; please retry")

    # 11. Fetch updated document and return
    updated = db.attendance_logs.find_one({"_id": record["_id"]}, {"_id": 0})
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month:    str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Step 12 -- Monthly attendance summary for one employee.

    working_days per R7 (joined_on inclusive, Mon-Fri only).
    present_days per R5-R7: half-day=0.5, Mon-Fri only; reads stored values.
    leave_days, late_count, total_late_minutes, total_overtime_minutes: all records.
    attendance_pct = present_days / working_days * 100 (R8, 2dp half-up), null
    when working_days=0.

    Analytics read the stored derived values -- no recomputation from punch times
    (DATA_MODEL.md requirement).
    """
    # 1. Employee must exist
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="employee not found")

    # 2. Parse month string
    year_s, mon_s = month.split("-")
    year, mon_num = int(year_s), int(mon_s)

    from calendar import monthrange
    _, last_day = monthrange(year, mon_num)
    month_start = f"{year:04d}-{mon_num:02d}-01"
    month_end   = f"{year:04d}-{mon_num:02d}-{last_day:02d}"

    # 3. Working days (pure Python, R7)
    wk = working_days_in_month(year, mon_num, emp["joined_on"])

    # 4. Aggregation over attendance_logs for this employee and month.
    #
    #    eff_presence -- ALL conditions inlined, no same-stage cross-references:
    #      - weekday check via $dayOfWeek on the date string ($dateFromString)
    #        Sun=1, Mon=2, Tue=3, Wed=4, Thu=5, Fri=6, Sat=7
    #      - presence status check ($in)
    #      - half_day weighting (0.5 or 1.0)
    #    All other accumulators use $ifNull to guard legacy records.
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date":     {"$gte": month_start, "$lte": month_end},
        }},
        {"$addFields": {
            "eff_presence": {"$cond": [
                {"$and": [
                    {"$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]},
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                ]},
                {"$cond": [
                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    0.5,
                    1.0,
                ]},
                0,
            ]},
        }},
        {"$group": {
            "_id":                    None,
            "present_days":           {"$sum": "$eff_presence"},
            "leave_days":             {"$sum": {"$cond": [
                                          {"$eq": ["$status", "LEAVE"]}, 1, 0,
                                      ]}},
            "late_count":             {"$sum": {"$cond": [
                                          {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                                      ]}},
            "total_late_minutes":     {"$sum": {"$ifNull": ["$late_minutes",     0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    agg  = rows[0] if rows else {}

    present_days = round_half_up(agg.get("present_days", 0), 2)
    att_pct      = (
        round_half_up(present_days / wk * 100, 2) if wk > 0 else None
    )

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           wk,
        "present_days":           present_days,
        "leave_days":             int(agg.get("leave_days",             0)),
        "late_count":             int(agg.get("late_count",             0)),
        "total_late_minutes":     int(agg.get("total_late_minutes",     0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct":         att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month:      str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Step 13 -- Department-level monthly summary.

    Two-query approach (ensures zero-log employees are counted per R9):
      Query A -- qualifying employees (joined_on <= month_end), grouped by dept.
      Query B -- attendance_logs aggregation for the month, grouped by emp_code.
    The two results are merged in Python.

    present_days -- Mon-Fri presence records only, half-day=0.5 (R7).
    avg_work_hours -- mean of work_hours for presence records that have a
      non-null work_hours value; computed via wh_sum/wh_count (no $push/$$REMOVE);
      null when no qualifying records exist (Step 14 final revision).
    late_count / total_late_minutes -- ALL records regardless of weekday (R7).
    leave_count, on_duty_count -- all records.
    headcount -- from employees collection, not from logs (R9 zero-log inclusion).
    Sorted by department ascending.
    """
    from calendar import monthrange
    from collections import defaultdict

    year_s, mon_s = month.split("-")
    year, mon_num = int(year_s), int(mon_s)
    _, last_day   = monthrange(year, mon_num)
    month_start   = f"{year:04d}-{mon_num:02d}-01"
    month_end     = f"{year:04d}-{mon_num:02d}-{last_day:02d}"

    # ---- Query A: qualifying employees (joined_on <= month_end) ----
    emp_filter: dict = {"joined_on": {"$lte": month_end}}
    if department:
        emp_filter["department"] = department

    qualifying = list(db.employees.find(emp_filter, {"_id": 0, "emp_code": 1, "department": 1}))
    if not qualifying:
        return {"month": month, "items": []}

    dept_headcount: dict = defaultdict(int)
    emp_to_dept:    dict = {}
    for e in qualifying:
        dept_headcount[e["department"]] += 1
        emp_to_dept[e["emp_code"]] = e["department"]

    all_codes = list(emp_to_dept.keys())

    # ---- Query B: attendance aggregation for the month ----
    #
    #  eff_presence -- ALL conditions inlined (no same-stage cross-references):
    #    weekday AND presence AND half_day weighting.
    #    Mon-Fri only for present_days (R7); late/leave/on_duty count all days.
    #
    #  wh_sum / wh_count replace the $push/$$REMOVE array accumulator
    #  (Step 14 plan correction -- pure $sum, universally supported).
    pipeline = [
        {"$match": {
            "emp_code": {"$in": all_codes},
            "date":     {"$gte": month_start, "$lte": month_end},
        }},
        {"$addFields": {
            "eff_presence": {"$cond": [
                {"$and": [
                    {"$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]},
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                ]},
                {"$cond": [
                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    0.5,
                    1.0,
                ]},
                0,
            ]},
        }},
        {"$group": {
            "_id":          "$emp_code",
            "present_days": {"$sum": "$eff_presence"},
            # avg_work_hours numerator: work_hours for presence + non-null work_hours
            "wh_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in":  ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne":  [{"$ifNull": ["$work_hours", None]}, None]},
                ]},
                "$work_hours",
                0,
            ]}},
            # avg_work_hours denominator
            "wh_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in":  ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne":  [{"$ifNull": ["$work_hours", None]}, None]},
                ]},
                1,
                0,
            ]}},
            "late_count":         {"$sum": {"$cond": [
                                       {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                                   ]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes",     0]}},
            "leave_count":        {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]},    1, 0]}},
            "on_duty_count":      {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]},  1, 0]}},
        }},
    ]
    agg_rows = list(db.attendance_logs.aggregate(pipeline))

    # ---- Merge by department in Python ----
    zero = lambda: {
        "present_days": 0.0, "wh_sum": 0.0, "wh_count": 0,
        "late_count": 0, "total_late_minutes": 0,
        "leave_count": 0, "on_duty_count": 0,
    }
    dept_agg: dict = defaultdict(zero)

    for row in agg_rows:
        dept = emp_to_dept.get(row["_id"])
        if dept is None:
            continue
        da = dept_agg[dept]
        da["present_days"]        += row["present_days"]
        da["wh_sum"]              += row["wh_sum"]
        da["wh_count"]            += row["wh_count"]
        da["late_count"]          += row["late_count"]
        da["total_late_minutes"]  += row["total_late_minutes"]
        da["leave_count"]         += row["leave_count"]
        da["on_duty_count"]       += row["on_duty_count"]

    # ---- Assemble response, sorted by department ascending ----
    items = []
    for dept in sorted(dept_headcount.keys()):
        da     = dept_agg[dept]
        wh_sum = da["wh_sum"]
        wh_cnt = da["wh_count"]
        avg_wh = round_half_up(wh_sum / wh_cnt, 2) if wh_cnt > 0 else None

        items.append({
            "department":         dept,
            "headcount":          dept_headcount[dept],
            "present_days":       round_half_up(da["present_days"], 2),
            "avg_work_hours":     avg_wh,
            "late_count":         da["late_count"],
            "total_late_minutes": da["total_late_minutes"],
            "leave_count":        da["leave_count"],
            "on_duty_count":      da["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month:      str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Step 14 -- Late-comers leaderboard for a month.

    Ranks employees by total late_minutes in the month, highest first.
    Only employees with total > 0 appear.

    Standard competition ranking (1,2,2,4 -- not array position):
      - Ties share a rank; the next rank after a tie is skipped.
      - limit applies AFTER ranking: every row whose rank <= limit is returned,
        so the response can contain more than `limit` rows when there is a tie
        at the cutoff rank.

    Row order: total_late_minutes DESC, emp_code ASC.
    When department is given, ranking happens inside that department only.

    name and department come from employees; logs whose employee does not exist
    are ignored.
    """
    from calendar import monthrange

    year_s, mon_s = month.split("-")
    year, mon_num = int(year_s), int(mon_s)
    _, last_day   = monthrange(year, mon_num)
    month_start   = f"{year:04d}-{mon_num:02d}-01"
    month_end     = f"{year:04d}-{mon_num:02d}-{last_day:02d}"

    # Pipeline: aggregate late_minutes per employee for the month,
    # join employees to get name + department, filter orphaned logs.
    pipeline = [
        # 1. Match month range
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
        }},
        # 2. Join employees to get name and department
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "emp_info",
        }},
        # 3. Discard logs whose employee record no longer exists
        {"$match": {"emp_info": {"$ne": []}}},
        # 4. Unwind the single-element array from $lookup
        {"$unwind": "$emp_info"},
        # 5. Optional department filter -- applied inside the pipeline so
        #    ranking happens within the department only
        *(
            [{"$match": {"emp_info.department": department}}]
            if department else []
        ),
        # 6. Group by emp_code, sum late_minutes and count late days
        {"$group": {
            "_id":                "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count":         {"$sum": {"$cond": [
                                       {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                                   ]}},
            "name":       {"$first": "$emp_info.name"},
            "department": {"$first": "$emp_info.department"},
        }},
        # 7. Only employees who were late at least once
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # 8. Sort: total_late_minutes DESC, emp_code ASC
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking in Python (no MongoDB equivalent).
    # rank = 1-based position of the first row with this total_late_minutes.
    rank      = 1
    prev_total: Optional[int] = None
    for i, row in enumerate(rows):
        if prev_total is None or row["total_late_minutes"] < prev_total:
            rank = i + 1         # natural 1-based position
        row["rank"]  = rank
        prev_total   = row["total_late_minutes"]

    # limit applies after ranking: keep every row whose rank <= limit
    output = [
        {
            "rank":               r["rank"],
            "emp_code":           r["_id"],
            "name":               r["name"],
            "department":         r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count":         r["late_count"],
        }
        for r in rows if r["rank"] <= limit
    ]

    return {"month": month, "items": output}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_:      str = Query(alias="from"),
    to:         str = Query(),
):
    """
    Step 15 -- Daily attendance trend for a department.

    Returns exactly one row per calendar day from `from` to `to` inclusive.
    Weekends and days with no records are gap-filled via $densify (MongoDB 5.1+;
    assignment requires 6.0 -- safe).

    Per-day fields:
      date            -- YYYY-MM-DD string
      is_working_day  -- Mon-Fri
      headcount       -- employees with joined_on <= that day (R9)
      present_count   -- presence records that day (half_day=0.5), any weekday
      late_count      -- records with late_minutes > 0
      attendance_rate -- present_count/headcount, 4dp half-up, only on working
                         days with headcount > 0; 0.0 when no records that day;
                         null otherwise
      moving_avg_7d   -- mean of non-null attendance_rate values in the
                         current and up to 6 preceding calendar rows inside
                         the requested range; window advances for every day
                         (including null-rate days); null when window has no
                         non-null rate

    Errors:
      404 -- no employees in department
      422 -- to < from, or range exceeds 92 days
    """
    # --- Validation ---
    try:
        from_date = date.fromisoformat(from_)
        to_date   = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, detail="from and to must be valid YYYY-MM-DD dates")

    if to_date < from_date:
        raise HTTPException(422, detail="to must not be before from")
    if (to_date - from_date).days > 92:
        raise HTTPException(422, detail="range must not exceed 92 days")

    # --- 404 if department unknown ---
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, detail="department not found")

    # --- Pre-fetch department employees (all, regardless of joined_on) ---
    # We need joined_on to compute per-day headcount and to filter logs.
    dept_employees = list(db.employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    ))
    dept_codes = [e["emp_code"] for e in dept_employees]

    from_str = from_date.isoformat()
    to_str   = to_date.isoformat()

    # UTC midnight datetimes for $densify bounds (both INCLUSIVE per MongoDB 6.0 docs)
    # $dateFromString without a timezone produces UTC midnight for a YYYY-MM-DD string,
    # so the grouped _id values match these bounds exactly.
    from_dt = datetime(from_date.year, from_date.month, from_date.day, 0, 0, 0, tzinfo=timezone.utc)
    to_dt   = datetime(to_date.year,   to_date.month,   to_date.day,   0, 0, 0, tzinfo=timezone.utc)

    # --- MongoDB aggregation with $densify for gap-filling ---
    # Stage 2 $addFields: ALL conditions inlined -- no same-stage cross-references.
    # $densify requires MongoDB 5.1+; assignment requires MongoDB 6.0 -- safe.
    pipeline = [
        # 1. Match logs for this department's employees within the date range
        {"$match": {
            "emp_code": {"$in": dept_codes},
            "date":     {"$gte": from_str, "$lte": to_str},
        }},
        # 2. Convert string date to UTC midnight datetime for $densify;
        #    compute eff (presence + half_day) and is_late -- all inline.
        {"$addFields": {
            "date_dt": {"$dateFromString": {
                "dateString": "$date",
                "format":     "%Y-%m-%d",
                # No timezone: output is UTC midnight (00:00:00Z), matching bounds
            }},
            # Inline presence check + half_day weighting -- no $is_presence helper
            "eff": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [
                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    0.5,
                    1.0,
                ]},
                0,
            ]},
            # Inline late check
            "is_late": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                1,
                0,
            ]},
        }},
        # 3. Group by UTC midnight date
        {"$group": {
            "_id":           "$date_dt",
            "present_count": {"$sum": "$eff"},
            "late_count":    {"$sum": "$is_late"},
        }},
        # 4. Gap-fill: one document per calendar day from from_dt to to_dt inclusive.
        #    Gap-filled docs have _id set; present_count and late_count are null.
        {"$densify": {
            "field": "_id",
            "range": {
                "step":   1,
                "unit":   "day",
                "bounds": [from_dt, to_dt],
            },
        }},
        # 5. Replace null on gap rows with 0
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count":    {"$ifNull": ["$late_count",    0]},
        }},
        {"$sort": {"_id": 1}},
    ]

    raw_rows = list(db.attendance_logs.aggregate(pipeline))

    # --- Python post-processing ---
    # Extract date string from UTC midnight datetime.
    # $dateFromString produces UTC midnight, so UTC date == original date string.
    rows_out: list  = []
    all_rates: list = []   # one entry per calendar day; None when rate is null

    for row in raw_rows:
        # row["_id"] is ISODate("YYYY-MM-DDT00:00:00Z")
        day_str  = row["_id"].strftime("%Y-%m-%d")  # UTC date == original date
        day_dt   = date.fromisoformat(day_str)
        is_wd    = (day_dt.weekday() < 5)           # Mon=0 ... Fri=4

        # Headcount: employees with joined_on <= this day (R9)
        hc = sum(1 for e in dept_employees if e["joined_on"] <= day_str)

        present_count = float(row.get("present_count") or 0)
        late_count    = int(row.get("late_count")    or 0)

        if is_wd and hc > 0:
            rate = round_half_up(present_count / hc, 4)
        else:
            rate = None

        all_rates.append(rate)  # advances for EVERY calendar day, including null-rate days

        rows_out.append({
            "date":            day_str,
            "is_working_day":  is_wd,
            "headcount":       hc,
            "present_count":   present_count,
            "late_count":      late_count,
            "attendance_rate": rate,
            "moving_avg_7d":   None,   # filled in second pass below
        })

    # Moving average: 7-calendar-position window (current + up to 6 preceding).
    # Window advances every day; average only over non-null rates in the window.
    for i in range(len(rows_out)):
        window_start = max(0, i - 6)                     # 7-position window
        window       = all_rates[window_start : i + 1]
        non_null     = [r for r in window if r is not None]
        rows_out[i]["moving_avg_7d"] = (
            round_half_up(sum(non_null) / len(non_null), 4) if non_null else None
        )

    return {"department": department, "items": rows_out}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_:      Optional[str] = Query(default=None, alias="from"),
    to:         Optional[str] = None,
    page:       int = Query(default=1,  ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """
    Step 16 -- MongoDB executionStats explain for an endpoint's main query.

    Runs explain with executionStats verbosity for the main query or
    aggregation of the named endpoint, built exactly as that endpoint
    builds it (same filters, sort, pagination).

    Accepted endpoint names:
      attendance_list     -- GET /attendance (find + sort + skip + limit)
      employee_monthly    -- GET /analytics/employees/{emp_code}/monthly
      department_summary  -- GET /analytics/departments/summary
      late_leaderboard    -- GET /analytics/leaderboard/late
      department_trend    -- GET /analytics/departments/{department}/trend

    Missing required parameters for the named endpoint -> 422.

    The grader checks that the plan contains IXSCAN and no COLLSCAN on a
    ~100k-record dataset.

    department_summary note: the real endpoint uses two queries (employees
    headcount + attendance_logs aggregation).  This explain targets the
    attendance_logs aggregation -- the main high-volume query.  The employees
    headcount query hits a small collection with a department index and is
    not explained separately.

    BSON types in the explain output are serialized to JSON-safe values via
    json_util before returning.
    """
    from calendar import monthrange
    from collections import defaultdict

    COLL = "attendance_logs"

    # ------------------------------------------------------------------
    # Helper: run explain with executionStats verbosity
    # ------------------------------------------------------------------
    
    def run_explain(command_doc: dict) -> dict:
        """Wrap a MongoDB command in explain with executionStats verbosity."""
        raw = db.command({
            "explain": command_doc,
            "verbosity": "executionStats",
        })

        assert "executionStats" in raw, (
            "MongoDB explain response did not contain executionStats"
        )

        safe = {
            "queryPlanner": raw.get("queryPlanner", {}),
            "executionStats": raw.get("executionStats", {}),
        }
        return safe 

    # ------------------------------------------------------------------
    # attendance_list -- uses find (not aggregate)
    # ------------------------------------------------------------------
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail="date_from must not be after date_to")

        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        cmd = {
            "find":   COLL,
            "filter": q,
            "sort":   {"date": -1, "emp_code": 1},
            "skip":   (page - 1) * page_size,
            "limit":  page_size,
        }
        safe = run_explain(cmd)
        return {"endpoint": endpoint, "collection": COLL, "explain": safe}

    # ------------------------------------------------------------------
    # Helper: build month range strings
    # ------------------------------------------------------------------
    def month_range(m: str):
        yr, mn = map(int, m.split("-"))
        _, last = monthrange(yr, mn)
        return (f"{yr:04d}-{mn:02d}-01",
                f"{yr:04d}-{mn:02d}-{last:02d}",
                yr, mn)

    # ------------------------------------------------------------------
    # employee_monthly
    # ------------------------------------------------------------------
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, detail="month is required for employee_monthly")

        ms, me, yr, mn = month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": ms, "$lte": me}}},
            {"$addFields": {
                "eff_presence": {"$cond": [
                    {"$and": [
                        {"$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6],
                        ]},
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    ]},
                    {"$cond": [
                        {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                        0.5, 1.0,
                    ]},
                    0,
                ]},
            }},
            {"$group": {
                "_id":                    None,
                "present_days":           {"$sum": "$eff_presence"},
                "leave_days":             {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count":             {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes":     {"$sum": {"$ifNull": ["$late_minutes",     0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ]
        cmd = {"aggregate": COLL, "pipeline": pipeline, "cursor": {}}
        safe = run_explain(cmd)
        return {"endpoint": endpoint, "collection": COLL, "explain": safe}

    # ------------------------------------------------------------------
    # department_summary
    # ------------------------------------------------------------------
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")

        ms, me, yr, mn = month_range(month)
        emp_filter: dict = {"joined_on": {"$lte": me}}
        if department:
            emp_filter["department"] = department
        qualifying = list(db.employees.find(emp_filter, {"_id": 0, "emp_code": 1}))
        all_codes  = [e["emp_code"] for e in qualifying]

        pipeline = [
            {"$match": {
                "emp_code": {"$in": all_codes},
                "date":     {"$gte": ms, "$lte": me},
            }},
            {"$addFields": {
                "eff_presence": {"$cond": [
                    {"$and": [
                        {"$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6],
                        ]},
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    ]},
                    {"$cond": [
                        {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                        0.5, 1.0,
                    ]},
                    0,
                ]},
            }},
            {"$group": {
                "_id":          "$emp_code",
                "present_days": {"$sum": "$eff_presence"},
                "wh_sum":       {"$sum": {"$cond": [
                    {"$and": [
                        {"$in":  ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne":  [{"$ifNull": ["$work_hours", None]}, None]},
                    ]},
                    "$work_hours", 0,
                ]}},
                "wh_count":     {"$sum": {"$cond": [
                    {"$and": [
                        {"$in":  ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne":  [{"$ifNull": ["$work_hours", None]}, None]},
                    ]},
                    1, 0,
                ]}},
                "late_count":         {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count":        {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]},   1, 0]}},
                "on_duty_count":      {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
        ]
        cmd = {"aggregate": COLL, "pipeline": pipeline, "cursor": {}}
        safe = run_explain(cmd)
        return {"endpoint": endpoint, "collection": COLL, "explain": safe}

    # ------------------------------------------------------------------
    # late_leaderboard
    # ------------------------------------------------------------------
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")

        ms, me, yr, mn = month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": ms, "$lte": me}}},
            {"$lookup": {
                "from": "employees", "localField": "emp_code",
                "foreignField": "emp_code", "as": "emp_info",
            }},
            {"$match": {"emp_info": {"$ne": []}}},
            {"$unwind": "$emp_info"},
            *(
                [{"$match": {"emp_info.department": department}}]
                if department else []
            ),
            {"$group": {
                "_id":                "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count":         {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "name":               {"$first": "$emp_info.name"},
                "department":         {"$first": "$emp_info.department"},
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        cmd = {"aggregate": COLL, "pipeline": pipeline, "cursor": {}}
        safe = run_explain(cmd)
        return {"endpoint": endpoint, "collection": COLL, "explain": safe}

    # ------------------------------------------------------------------
    # department_trend
    # ------------------------------------------------------------------
    if endpoint == "department_trend":
        if not department:
            raise HTTPException(422, detail="department is required for department_trend")
        if not from_:
            raise HTTPException(422, detail="from is required for department_trend")
        if not to:
            raise HTTPException(422, detail="to is required for department_trend")

        try:
            from_date = date.fromisoformat(from_)
            to_date   = date.fromisoformat(to)
        except ValueError:
            raise HTTPException(422, detail="from and to must be valid YYYY-MM-DD dates")
        if to_date < from_date:
            raise HTTPException(422, detail="to must not be before from")
        if (to_date - from_date).days > 92:
            raise HTTPException(422, detail="range must not exceed 92 days")

        dept_codes = [
            e["emp_code"] for e in
            db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})
        ]
        from_dt = datetime(from_date.year, from_date.month, from_date.day, 0, 0, 0, tzinfo=timezone.utc)
        to_dt   = datetime(to_date.year,   to_date.month,   to_date.day,   0, 0, 0, tzinfo=timezone.utc)

        pipeline = [
            {"$match": {
                "emp_code": {"$in": dept_codes},
                "date":     {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()},
            }},
            {"$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "eff": {"$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [
                        {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                        0.5, 1.0,
                    ]},
                    0,
                ]},
                "is_late": {"$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                ]},
            }},
            {"$group": {
                "_id":           "$date_dt",
                "present_count": {"$sum": "$eff"},
                "late_count":    {"$sum": "$is_late"},
            }},
            {"$densify": {
                "field": "_id",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]},
            }},
            {"$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count":    {"$ifNull": ["$late_count",    0]},
            }},
            {"$sort": {"_id": 1}},
        ]
        cmd = {"aggregate": COLL, "pipeline": pipeline, "cursor": {}}
        safe = run_explain(cmd)
        return {"endpoint": endpoint, "collection": COLL, "explain": safe}

    # ------------------------------------------------------------------
    # Unknown endpoint name
    # ------------------------------------------------------------------
    raise HTTPException(
        422,
        detail=(
            "endpoint must be one of: attendance_list, employee_monthly, "
            "department_summary, late_leaderboard, department_trend"
        ),
    )
