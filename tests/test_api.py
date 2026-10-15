
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.main import app, get_db, compute_late_minutes, compute_overtime, compute_work_hours




@pytest.fixture
def client(mock_db):
    # Mock the database without starting the real MongoDB connection.
    with patch("app.main.get_db", return_value=mock_db):
        yield TestClient(app)



@pytest.fixture
def mock_db():
    db = MagicMock()
    db.employees.find_one.return_value = None
    db.employees.count_documents.return_value = 0
    db.attendance_logs.count_documents.return_value = 0
    db.attendance_logs.find_one.return_value = None
    db.attendance_logs.update_one.return_value.modified_count = 1
    return db


@pytest.fixture
def employee():
    return {
        "emp_code": "EMP1001",
        "name": "Test Employee",
        "email": "test.employee@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }


@pytest.fixture
def employee_doc():
    return {
        "emp_code": "EMP1001",
        "name": "Test Employee",
        "email": "test.employee@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }


@pytest.fixture
def attendance_doc():
    return {
        "_id": "attendance-1",
        "emp_code": "EMP1001",
        "date": "2026-06-15",
        "status": "PRESENT",
        "punch_in": datetime(2026, 6, 15, 4, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "message" in response.json()


def test_health_endpoint(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_db.command.assert_called_once_with("ping")


def test_health_database_unavailable(client, mock_db):
    mock_db.command.side_effect = PyMongoError("database unavailable")

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get("/health")

    assert response.status_code == 503


def test_create_employee_success(client, mock_db, employee):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.post("/employees", json=employee)

    assert response.status_code == 201
    assert response.json()["emp_code"] == employee["emp_code"]
    assert "_id" not in response.json()
    mock_db.employees.insert_one.assert_called_once()


def test_create_employee_duplicate_code(client, mock_db, employee):
    mock_db.employees.insert_one.side_effect = DuplicateKeyError("duplicate")

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post("/employees", json=employee)

    assert response.status_code == 409
    assert response.json()["detail"] == "emp_code already exists"


@pytest.mark.parametrize(
    "changes",
    [
        {"emp_code": "INVALID"},
        {"email": "not-an-email"},
        {"joined_on": "2026-02-30"},
        {"shift_start": "25:00"},
        {"name": ""},
    ],
)
def test_create_employee_invalid_data(client, mock_db, employee, changes):
    employee.update(changes)

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post("/employees", json=employee)

    assert response.status_code == 422
    mock_db.employees.insert_one.assert_not_called()


def test_list_employees(client, mock_db):
    mock_db.employees.count_documents.return_value = 1

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get("/employees")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["page"] == 1
    mock_db.employees.find.assert_called_once()


def test_list_employees_department_filter(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/employees",
            params={"department": "Engineering", "page": 2, "page_size": 5},
        )

    assert response.status_code == 200
    assert response.json()["page"] == 2
    mock_db.employees.count_documents.assert_called_once_with(
        {"department": "Engineering"}
    )


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page_size": 0}, {"page_size": 101}],
)
def test_list_employees_invalid_pagination(client, params):
    response = client.get("/employees", params=params)
    assert response.status_code == 422


def test_punch_in_success(client, mock_db, employee_doc):
    mock_db.employees.find_one.return_value = employee_doc

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP1001",
                "punched_at": 1781515800000,
                "status": "PRESENT",
            },
        )

    assert response.status_code == 201
    assert response.json()["emp_code"] == "EMP1001"
    assert response.json()["status"] == "PRESENT"
    mock_db.attendance_logs.insert_one.assert_called_once()


def test_punch_in_unknown_employee(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9999", "punched_at": 1781515800000},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown employee"


def test_punch_in_duplicate_record(client, mock_db, employee_doc):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.insert_one.side_effect = DuplicateKeyError("duplicate")

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP1001", "punched_at": 1781515800000},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Already punched in for this date"


def test_punch_in_invalid_status(client):
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1001", "status": "INVALID"},
    )
    assert response.status_code == 422


def test_punch_in_invalid_timestamp(client):
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1001", "punched_at": 123},
    )
    assert response.status_code == 422


def test_punch_out_unknown_employee(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9999", "punched_at": 1781519400000},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown employee"


def test_punch_out_without_punch_in(client, mock_db, employee_doc):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.find_one.return_value = None

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": 1781519400000},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "No punch-in found"


def test_punch_out_already_closed(client, mock_db, employee_doc):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.find_one.side_effect = [
        None,
        {
            "emp_code": "EMP1001",
            "punch_in": datetime(2026, 6, 15, 4, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 6, 15, 12, tzinfo=timezone.utc),
        },
    ]

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": 1781524800000},
        )

    assert response.status_code == 409


def test_punch_out_success(client, mock_db, employee_doc, attendance_doc):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.find_one.return_value = attendance_doc.copy()

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": 1781524800000},
        )

    assert response.status_code == 200
    assert response.json()["punch_out"] is not None
    assert response.json()["work_hours"] == 8.0
    mock_db.attendance_logs.update_one.assert_called_once()


def test_punch_out_concurrent_update(client, mock_db, employee_doc, attendance_doc):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.find_one.return_value = attendance_doc.copy()
    mock_db.attendance_logs.update_one.return_value.modified_count = 0

    with patch("app.main.get_db", return_value=mock_db):
        response = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": 1781524800000},
        )

    assert response.status_code == 409


def test_list_attendance_filters_and_pagination(client, mock_db):
    params = {
        "emp_code": "EMP1001",
        "status": "PRESENT",
        "page": 2,
        "page_size": 5,
    }

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get("/attendance", params=params)

    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 2
    assert data["page_size"] == 5
    assert data["items"] == []
    expected = {"emp_code": "EMP1001", "status": "PRESENT"}
    mock_db.attendance_logs.count_documents.assert_called_once_with(expected)


@pytest.mark.parametrize(
    "params",
    [
        {"status": "INVALID"},
        {"date_from": "2026-02-30"},
        {"date_to": "not-a-date"},
        {"date_from": "2026-06-20", "date_to": "2026-06-10"},
        {"page": 0},
        {"page_size": 101},
    ],
)
def test_list_attendance_invalid_parameters(client, params):
    response = client.get("/attendance", params=params)
    assert response.status_code == 422


def test_regularize_unknown_employee(client, mock_db):
    body = {
        "status": "ABSENT",
        "reason": "Approved correction",
        "regularized_by": "admin",
    }

    with patch("app.main.get_db", return_value=mock_db):
        response = client.patch(
            "/attendance/EMP9999/2026-06-15", json=body
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown employee"


def test_regularize_missing_record(client, mock_db, employee_doc):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.find_one.return_value = None
    body = {
        "status": "ABSENT",
        "reason": "Approved correction",
        "regularized_by": "admin",
    }

    with patch("app.main.get_db", return_value=mock_db):
        response = client.patch(
            "/attendance/EMP1001/2026-06-15", json=body
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Attendance record not found"


def test_regularize_invalid_date(client):
    response = client.patch(
        "/attendance/EMP1001/not-a-date",
        json={
            "status": "ABSENT",
            "reason": "Approved correction",
            "regularized_by": "admin",
        },
    )
    assert response.status_code == 422


def test_regularize_absent_cannot_have_punch_times(
    client, mock_db, employee_doc, attendance_doc
):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.find_one.return_value = attendance_doc.copy()
    body = {
        "status": "ABSENT",
        "punch_in": 1781515800000,
        "reason": "Approved correction",
        "regularized_by": "admin",
    }

    with patch("app.main.get_db", return_value=mock_db):
        response = client.patch(
            "/attendance/EMP1001/2026-06-15", json=body
        )

    assert response.status_code == 422
    assert "cannot include punch times" in response.json()["detail"]


def test_regularize_no_changes(client, mock_db, employee_doc, attendance_doc):
    mock_db.employees.find_one.return_value = employee_doc
    mock_db.attendance_logs.find_one.return_value = attendance_doc.copy()
    body = {
        "status": "PRESENT",
        "reason": "Review existing record",
        "regularized_by": "admin",
    }

    with patch("app.main.get_db", return_value=mock_db):
        response = client.patch(
            "/attendance/EMP1001/2026-06-15", json=body
        )

    assert response.status_code == 422
    assert "does not change" in response.json()["detail"]


def test_monthly_analytics_unknown_employee(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/analytics/employees/EMP9999/monthly",
            params={"month": "2026-06"},
        )

    assert response.status_code == 404


def test_monthly_analytics_invalid_month(client, mock_db):
    mock_db.employees.find_one.return_value = {"joined_on": "2026-01-01"}

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/analytics/employees/EMP1001/monthly",
            params={"month": "2026-13"},
        )

    assert response.status_code == 422


def test_monthly_analytics_success(client, mock_db):
    mock_db.employees.find_one.return_value = {
        "emp_code": "EMP1001",
        "joined_on": "2026-06-01",
    }

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/analytics/employees/EMP1001/monthly",
            params={"month": "2026-06"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["emp_code"] == "EMP1001"
    assert data["month"] == "2026-06"
    assert data["present_days"] == 0
    assert data["attendance_pct"] == 0.0


def test_department_summary_success(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/analytics/departments/summary",
            params={"month": "2026-06", "department": "Engineering"},
        )

    assert response.status_code == 200
    assert response.json() == {"month": "2026-06", "items": []}


def test_late_leaderboard_success(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-06", "limit": 5},
        )

    assert response.status_code == 200
    assert response.json() == {"month": "2026-06", "items": []}


def test_late_leaderboard_invalid_limit(client):
    response = client.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-06", "limit": 100},
    )
    assert response.status_code == 422


def test_department_trend_unknown_department(client, mock_db):
    mock_db.employees.count_documents.return_value = 0

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-06-01", "to": "2026-06-10"},
        )

    assert response.status_code == 404


def test_department_trend_success(client, mock_db):
    mock_db.employees.count_documents.return_value = 1

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-06-01", "to": "2026-06-10"},
        )

    assert response.status_code == 200
    assert response.json() == {"department": "Engineering", "items": []}


@pytest.mark.parametrize(
    "date_from,date_to",
    [
        ("2026-06-10", "2026-06-01"),
        ("2026-01-01", "2026-04-10"),
        ("invalid", "2026-06-10"),
    ],
)
def test_department_trend_invalid_range(client, date_from, date_to):
    response = client.get(
        "/analytics/departments/Engineering/trend",
        params={"from": date_from, "to": date_to},
    )
    assert response.status_code == 422


def test_explain_attendance_list_success(client, mock_db):
    mock_db.command.return_value = {"ok": 1, "queryPlanner": {}}

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/admin/explain/attendance_list",
            params={"emp_code": "EMP1001", "page": 1, "page_size": 10},
        )

    assert response.status_code == 200
    assert response.json()["endpoint"] == "attendance_list"
    assert response.json()["collection"] == "attendance_logs"


def test_explain_unsupported_endpoint(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.get("/admin/explain/not_supported")

    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported endpoint"


def test_explain_missing_required_parameters(client, mock_db):
    with patch("app.main.get_db", return_value=mock_db):
        response = client.get("/admin/explain/employee_monthly")

    assert response.status_code == 422
    assert "required" in response.json()["detail"]


def test_explain_database_error(client, mock_db):
    mock_db.command.side_effect = PyMongoError("explain failed")

    with patch("app.main.get_db", return_value=mock_db):
        response = client.get(
            "/admin/explain/attendance_list"
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "Explain failed: PyMongoError"


def test_compute_work_hours():
    start = datetime(2026, 6, 15, 4, tzinfo=timezone.utc)
    end = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
    assert compute_work_hours(start, end) == 8.0


def test_compute_late_minutes():
    # 10:00 IST is 30 minutes after the 09:30 shift start.
    punch_in = datetime(2026, 6, 15, 4, 30, tzinfo=timezone.utc)
    assert compute_late_minutes(punch_in, "09:30", "18:30") == 30



def test_compute_overtime_below_threshold():
    # Shift ends at 18:30 IST; this punch-out is 20 minutes later.
    punch_out = datetime(2026, 6, 15, 13, 20, tzinfo=timezone.utc)
    assert compute_overtime(
        punch_out, "18:30", "2026-06-15", "09:30"
    ) == 0

