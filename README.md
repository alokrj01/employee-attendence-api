# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee records, attendance
punch-in/punch-out, attendance regularization, and monthly and
department-level analytics.

## Requirements

-   Python 3.11+
-   MongoDB 6.0+
-   Dependencies listed in `requirements.txt`

## Setup and run

From the repository root, create and activate a virtual environment,
then install dependencies:

``` bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Set the MongoDB connection settings in your environment. Do not commit
credentials:

``` bash
# macOS/Linux
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# Windows PowerShell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Start the API from the repository root:

``` bash
uvicorn app.main:app --port 8000
```

The application creates its indexes during startup. Check service health
at `GET http://localhost:8000/health`; interactive API documentation is
available at `http://localhost:8000/docs`.

## Endpoints

-   `POST /employees` and `GET /employees`
-   `POST /attendance/punch-in`, `POST /attendance/punch-out`, and
    `GET /attendance`
-   `PATCH /attendance/{emp_code}/{date_str}` for attendance
    regularization
-   `GET /analytics/employees/{emp_code}/monthly`
-   `GET /analytics/departments/summary`
-   `GET /analytics/leaderboard/late`
-   `GET /analytics/departments/{department}/trend`
-   `GET /admin/explain/{endpoint}` for query-plan inspection

Request fields, response formats, status codes, and attendance rules are
defined in `openapi.yaml`. MongoDB collections and field definitions are
documented in `DATA_MODEL.md`.

## Notes

-   Configure `MONGO_URI` and `MONGO_DB` through environment variables;
    do not hard-code secrets or commit a `.env` file.
-   Validate the implementation against the full contract and
    concurrent-request cases before submission. In particular, verify
    the department trend for a date range with no matching attendance
    records, and confirm that each explain request uses the same
    filters, sorting, and pagination as the endpoint it explains.
-   This README does not claim that automated tests against the full
    hidden-size dataset have passed.
