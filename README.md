# Employee Attendance & Analytics API

HROne Software Engineer Trainee technical assignment.  
FastAPI + PyMongo + MongoDB Atlas (or local MongoDB 6.0+).

---

## Quick start

### 1. Clone and create a virtual environment (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root (never commit it — it is already in `.gitignore`):

```
MONGO_URI=<your MongoDB connection string>
MONGO_DB=<your database name>
```

- `MONGO_URI` — MongoDB connection string, configured through an environment variable. Use your private Atlas connection string or a local MongoDB URI; never commit credentials.
 - `MONGO_DB` — the database name to use (e.g. `hrone`).  
- Real environment variables always override the `.env` file.  
- **Never commit credentials.** Do not include `.env` in the repository.

MongoDB Atlas free tier (M0) works. Local Docker:

```bat
docker run -p 27017:27017 mongo:7
```

**MongoDB 6.0 or newer is required** (`$densify` was added in 5.1; the grader uses 7.0).

### 3. Seed sample data (optional)

```bat
python sample_seed.py
```

Loads the nine sample documents from `sample_data/` into the configured database. The grader seeds its own larger dataset separately.

### 4. Start the server

```bat
uvicorn app.main:app --port 8000 --reload
```

The app creates all required MongoDB indexes at startup (idempotent).

### 5. Browse the API

- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)  
- ReDoc:       [http://localhost:8000/redoc](http://localhost:8000/redoc)  
- OpenAPI JSON: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

---

## Implemented endpoints

### System

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Returns `{"status":"ok"}` when MongoDB answers a ping, `503` otherwise. The grader polls this before running any test. |

### Employees

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/employees` | Create an employee. `emp_code` must match `^EMP\d{4,6}$` and be unique. Concurrent duplicate creates produce one 201 and the rest 409 (enforced by a unique DB index, not application logic). |
| `GET` | `/employees` | List employees sorted by `emp_code` ascending. Optional `?department=` filter (exact, case-sensitive). Paginated via `page` (default 1) and `page_size` (default 20, max 100). `total` reflects the filtered set. |

### Attendance

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/attendance/punch-in` | Record a punch-in. `status` must be `PRESENT`, `WFH`, or `ON_DUTY`. The attendance date is computed in IST (including overnight-shift exception). `late_minutes` is computed per R2. Concurrent same-day punch-ins yield one 201 and the rest 409. |
| `POST` | `/attendance/punch-out` | Close the most recent open record whose `punch_in ≤ punched_at`. Computes `work_hours` (R4), `overtime_minutes` (R3), `half_day` (R5). 422 if `punched_at ≤ punch_in` or more than 24 h after it. Concurrent punch-outs yield one 200 and the rest 409. |
| `GET` | `/attendance` | List records sorted by `date DESC, emp_code ASC`. Filters: `emp_code`, `date_from`, `date_to` (inclusive), `status`. 422 if `date_from > date_to`. Paginated. Sort and pagination are pushed to MongoDB. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manually correct a record. Recomputes derived fields after applying changes. Appends one history entry containing only the fields that actually changed. 422 if no fields changed, if ABSENT/LEAVE supplied with punch times, or if new `punch_in` falls on a different IST date. Concurrent edits produce one 200 and the rest 409 (optimistic lock on history length, covering legacy records). |

### Analytics

> Analytics read stored derived values (`late_minutes`, `work_hours`, etc.) from the database. They do not recompute from punch timestamps.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee. `working_days` counts Mon–Fri from `joined_on` (inclusive). `present_days` counts Mon–Fri presence records (half-day = 0.5). `attendance_pct` is null when `working_days = 0`. |
| `GET` | `/analytics/departments/summary` | Department-level monthly summary. `headcount` includes employees with no logs (R9). `present_days` is Mon–Fri only. `avg_work_hours` is null when no qualifying records exist. Optional `?department=` filter. |
| `GET` | `/analytics/leaderboard/late` | Late-comers leaderboard. Standard competition ranking (1,2,2,4). `limit` applies after ranking so ties at the cutoff can produce more rows than `limit`. Optional `?department=` scopes ranking to that department. |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend. Returns exactly one row per calendar day (`from` to `to` inclusive) using MongoDB `$densify` for gap-filling. `attendance_rate` is null on non-working days or when headcount is 0. `moving_avg_7d` averages non-null rates over a 7-calendar-day window. 422 if range exceeds 92 days. 404 if department has no employees. |

### Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/explain/{endpoint}` | Returns MongoDB `executionStats` explain for the named endpoint's main query or pipeline, built with the same filters and pagination as the real endpoint. Accepted names: `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`. Missing required parameters return 422. |

---

## Business rules summary

| Rule | Summary |
|------|---------|
| R1 | Attendance dates are IST. Overnight shifts (`shift_end ≤ shift_start`): a punch-in whose IST time is **strictly before** `shift_end` belongs to the previous day. All instants truncated to whole seconds before calculation. |
| R2 | Late only if punch-in is **strictly more than** 600 s after shift start. `late_minutes = floor(elapsed_seconds / 60)`. |
| R3 | `overtime_minutes = floor(minutes after shift_end)`, only when ≥ 30 min. |
| R4 | `work_hours = (punch_out − punch_in) / 3600`, rounded to 2 decimals **half-up**. |
| R5 | `half_day = true` when rounded `work_hours < 4.50`. Counts as 0.5 present day. |
| R6 | PRESENT, WFH, ON_DUTY count as present. ABSENT and LEAVE do not. |
| R7 | Working days = Mon–Fri from `joined_on` (inclusive). Weekend records still contribute to late and overtime totals. |
| R8 | Analytics figures: 2 decimal places half-up (rates: 4 decimal places half-up). |
| R9 | Headcount = employees with `joined_on ≤ end of period`, whether or not they have logs. |
| R10 | `page` starts at 1; `page_size` max 100, default 20. `total` reflects the applied filters. |

---

## MongoDB indexes

Created automatically at startup (idempotent):

| Collection | Index | Type | Purpose |
|------------|-------|------|---------|
| `employees` | `emp_code` | unique | Fast lookup; enforces uniqueness |
| `attendance_logs` | `(emp_code ASC, date ASC)` | unique compound | One record per employee-day; punch-in concurrency |
| `attendance_logs` | `(date DESC, emp_code ASC)` | compound | `list_attendance` sort; analytics scans |
| `employees` | `department` | single-field | Department-filtered analytics |

---

## Timestamp convention

Every point-in-time value in requests and responses is an **integer count of milliseconds since 1970-01-01T00:00:00Z** (epoch milliseconds). Values that look like seconds (below 100 000 000 000), floats, and strings are rejected with 422.

---

## Known limitations

- **Runtime testing has not yet been performed.** The implementation was validated by Python syntax checks and static content checks only. No endpoint has been called against a live MongoDB instance.
- The `/admin/explain` endpoint for `department_summary` explains the `attendance_logs` aggregation only. The separate `employees` headcount query (small collection, indexed) is not explained.
- The `$densify` stage in the trend endpoint requires MongoDB 5.1+. MongoDB 6.0+ is required to run this project.

---

## Project structure

```
app/
  main.py          — all API implementation (FastAPI app, helpers, endpoints)
  __init__.py
sample_data/
  employees.json   — 6 sample employee documents (MongoDB Extended JSON)
  attendance_logs.json — 9 sample attendance log documents
sample_seed.py     — loads sample_data/ into the configured database
requirements.txt   — fastapi, uvicorn, pymongo[srv], pydantic, python-dotenv
openapi.yaml       — API contract and business rules R1–R10 (source of truth)
DATA_MODEL.md      — MongoDB collections, field types, sample documents
REVIEW.md          — starter-code defect log
DECISIONS.md       — design decision notes
```

---

## Submit

A public Git repository containing `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this `README.md`.  
Do **not** include `.env`, secrets, `database.py`, or a Dockerfile.
