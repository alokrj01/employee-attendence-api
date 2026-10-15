# DECISIONS.md

1. **Indexes.** Unique employees.emp_code enforces employee identity. (department, emp_code) supports filtered employee lists; (joined_on, department) supports join-date queries. In attendance_logs, unique (emp_code, date) prevents duplicate daily records; (date DESC, emp_code ASC) supports list ordering; (date, status, emp_code) helps date/status filters. I would confirm index use with execution plans on the full dataset.

2. **Simultaneous punch-ins.** Both requests may insert, but unique (emp_code, date) permits only one record per employee and attendance date. One succeeds with 201; the duplicate raises DuplicateKeyError, converted to 409 Conflict. The database constraint resolves the race, not a check-then-insert.

3. **Leaderboard cutoff ties.** MongoDB $rank gives equal totals the same rank and skips the next rank. rank <= limit includes everyone tied at the cutoff. Sorting by total late minutes and emp_code makes output deterministic.

4. **Employees with no logs.** The summary starts from employees who joined by month-end and looks up monthly attendance. preserveNullAndEmptyArrays retains employees without logs, so they count toward headcount.

5. **100× data.** I would benchmark latency and query plans, then consider pre-aggregated daily/monthly summaries with a refresh strategy for regularizations. I would test analytics on empty and sparse date ranges.


 