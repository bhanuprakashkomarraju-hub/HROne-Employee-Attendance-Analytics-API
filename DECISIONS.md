# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Four indexes are created at startup via the `lifespan` function. A unique index on `employees.emp_code` enforces uniqueness and makes employee lookups fast. A unique compound index on `attendance_logs.(emp_code, date)` enforces one record per employee per day and makes the punch-in race condition safe at the database level — no application lock is needed. A compound index on `attendance_logs.(date DESC, emp_code ASC)` covers the `list_attendance` sort and analytics month-range scans without a collection scan. A single-field index on `employees.department` speeds up department-filtered analytics. I considered a separate single-field `emp_code` index on `attendance_logs` and rejected it because the compound index already covers `emp_code`-only queries as a left-prefix scan.

2. **Punch-in race.** Both requests pass Pydantic validation and look up the employee. Both compute the same attendance date. Both call `insert_one`. The unique `(emp_code, date)` index allows only one insert to succeed. The other raises `DuplicateKeyError`, which the handler catches and returns as 409. No check-then-insert, no locks, no transactions needed.

3. **Ties.** The leaderboard aggregates and sorts in MongoDB, then ranking is assigned in Python using standard competition ranking: `rank = i + 1` only when `total_late_minutes` drops from the previous row. The `limit` filter keeps every row whose `rank <= limit`, so if two employees tie at rank 2 and `limit` is 2, all three rows (rank 1, rank 2, rank 2) are returned. The response can exceed `limit` rows when there is a tie at the cutoff — this is required by the spec.

4. **Headcount.** The department summary runs two separate queries. The first fetches all employees whose `joined_on` is on or before the last day of the month, grouped by department. Headcount comes from this employee query, not from the attendance logs. The second query aggregates attendance logs for those employees. The two results are merged in Python. Employees with no logs still appear in the headcount because they were counted in the first query.

5. **One thing I would change at 100x scale.** Pre-aggregate daily summaries into a `daily_stats` collection at punch-out time. Monthly analytics currently scan every attendance log in the month; at 100x data that becomes millions of documents per aggregation. A `daily_stats` document per `(emp_code, date)` containing `present_days`, `late_minutes`, and `work_hours` would reduce analytics queries to a much smaller indexed scan, with no change to the API contract.

---

*Runtime testing against a live MongoDB instance has not yet been performed. All statements above describe the code as written and the design intentions behind it.*
