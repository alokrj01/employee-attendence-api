"""
Employee Attendance & Analytics API.

Run with:  uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables.
"""

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from bson import json_util
import json

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError


load_dotenv() #Real environment variables take precedence over values in .env


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH_MIN_MS = 100_000_000_000
EPOCH_MAX_MS = 4_102_444_800_000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}



# --------------------------------------------------------------------------- #
# Startup: Idempotent Index Creation
# --------------------------------------------------------------------------- #
def create_indexes(database: Any) -> None:
    """Create indexes idempotently; unique indexes enforce business invariants."""
    database.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_1")
    database.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code"
    )
    database.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")

    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_1_date_1"
    )
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_-1_emp_code_1"
    )
    database.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_code"
    )



@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect and index at startup, then close the client on shutdown."""
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGO_DB", "attendance_db")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, tzinfo=UTC)
    try:
        client.admin.command("ping")
        database = client[mongo_db]
        create_indexes(database)
        app.state.mongo_client = client
        app.state.db = database
        yield
    finally:
        client.close()



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def get_db():
    """Return the database initialized by the FastAPI lifespan handler."""
    database = getattr(app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is not available")
    return database


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_epoch_ms(value: int | None) -> datetime:
    """Convert validated epoch milliseconds to a UTC datetime truncated to seconds."""
    if value is None:
        return utc_now()
    if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN_MS <= value <= EPOCH_MAX_MS:
        raise HTTPException(status_code=422, detail="Timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def to_epoch_ms(value: datetime | None) -> int | None:
    """Serialize BSON datetimes as integer epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def api_employee(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["created_at"] = to_epoch_ms(result.get("created_at"))
    return result


def api_attendance(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["punch_in"] = to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = to_epoch_ms(result.get("punch_out"))
    result["history"] = result.get("history", [])
    for entry in result["history"]:
        entry["at"] = to_epoch_ms(entry.get("at"))
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                changes[field]["from"] = to_epoch_ms(changes[field].get("from"))
                changes[field]["to"] = to_epoch_ms(changes[field].get("to"))
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("work_hours", None)
    return result


def validate_date_string(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    return parsed


def validate_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return first, last



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    """Apply R2 using the correct shift-start date, including overnight shifts."""
    local = punch_in.astimezone(IST)
    start = tuple(map(int, shift_start.split(":")))
    end = tuple(map(int, shift_end.split(":")))
    shift_start_dt = local.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    # For an overnight shift, a punch before shift_end belongs to yesterday's shift.
    if end <= start and (local.hour, local.minute) < end:
        shift_start_dt -= timedelta(days=1)
    elapsed_seconds = (local - shift_start_dt).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)
    return elapsed_minutes if elapsed_seconds > 600 else 0


def round_half_up(value: float, places: int = 2) -> float:
    """Round non-negative reported values using decimal half-up rounding."""
    from decimal import Decimal, ROUND_HALF_UP
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """Apply R3, placing overnight shift end on the following calendar day."""
    local_out = punch_out.astimezone(IST)
    hour, minute = map(int, shift_end.split(":"))
    shift_end_dt = datetime.combine(date.fromisoformat(date_str), datetime.min.time(), tzinfo=IST).replace(
        hour=hour, minute=minute
    )
    start_hour, start_minute = map(int, shift_start.split(":"))
    if (hour, minute) <= (start_hour, start_minute):
        shift_end_dt += timedelta(days=1)
    elapsed_seconds = (local_out - shift_end_dt).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)
    return elapsed_minutes if elapsed_minutes >= 30 else 0


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    start = tuple(map(int, shift_start.split(":")))
    end = tuple(map(int, shift_end.split(":")))
    day = local.date()
    if end <= start and (local.hour, local.minute) < end:
        day -= timedelta(days=1)
    return day.isoformat()



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        validate_date_string(value, "joined_on")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: int | None = Field(default=None, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS, strict=True)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: int | None = Field(default=None, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS, strict=True)



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/")
async def root():
    return {"message": "Employee Attendence API is running!"}


@app.get("/health")
def health():
    """Report healthy only when the configurd database responds."""
    try:
        get_db().command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    database = get_db()
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = get_db()
    query = {"department": department} if department is not None else {}
    total = database.employees.count_documents(query)
    cursor = database.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    items = [api_employee(item) for item in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    database = get_db()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    punched_at = from_epoch_ms(body.punched_at)
    day = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    return api_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = get_db()
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds: dict[str, str] = {}
        if date_from is not None:
            validate_date_string(date_from, "date_from")
            bounds["$gte"] = date_from
        if date_to is not None:
            validate_date_string(date_to, "date_to")
            bounds["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query["date"] = bounds
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status
    total = database.attendance_logs.count_documents(query)
    cursor = database.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
    items = [api_attendance(item) for item in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}



class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str | None = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: int = Field(default=None, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS, strict=True)
    punch_out: int = Field(default=None, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    database = get_db()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    punched_at = from_epoch_ms(body.punched_at)
    # Find the latest open shift started no later than the requested punch-out instant.
    record = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        # Distinguish a previously closed latest record (409) from no punch-in (404).
        closed = database.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
            sort=[("punch_in", DESCENDING)],
        )
        if closed and closed.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="Attendance record is already punched out")
        raise HTTPException(status_code=404, detail="No punch-in found")
    seconds = (punched_at - record["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(record["punch_in"], punched_at)
    overtime = compute_overtime(punched_at, employee["shift_end"], record["date"], employee["shift_start"])
    update = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": work_hours < 4.50,
    }
    # Conditional update is the concurrency guard: only one caller can close this record.
    result = database.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None}, {"$set": update}
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")
    record.update(update)
    return api_attendance(record)


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    database = get_db()
    validate_date_string(date_str, "date")
    employee = database.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    record = database.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    requested = body.model_dump(exclude_unset=True)
    requested.pop("reason", None)
    regularized_by = requested.pop("regularized_by")
    reason = body.reason
    old_values = {key: record.get(key) for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    final_status = requested.get("status", record["status"])
    if final_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in requested or "punch_out" in requested:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot include punch times")
        final_in = final_out = None
    else:
        final_in = from_epoch_ms(requested["punch_in"]) if "punch_in" in requested and requested["punch_in"] is not None else record.get("punch_in")
        final_out = from_epoch_ms(requested["punch_out"]) if "punch_out" in requested and requested["punch_out"] is not None else record.get("punch_out")
        if final_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        if attendance_date(final_in, employee["shift_start"], employee["shift_end"]) != date_str:
            raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")
        if final_out is not None:
            elapsed = (final_out - final_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if final_status in {"ABSENT", "LEAVE"}:
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        derived = {
            "late_minutes": compute_late_minutes(final_in, employee["shift_start"], employee["shift_end"]),
            "work_hours": compute_work_hours(final_in, final_out) if final_out else None,
            "overtime_minutes": compute_overtime(final_out, employee["shift_end"], date_str, employee["shift_start"]) if final_out else 0,
            "half_day": compute_work_hours(final_in, final_out) < 4.50 if final_out else False,
        }
    new_values = {"status": final_status, "punch_in": final_in, "punch_out": final_out, **derived}
    changes = {}
    for key, new_value in new_values.items():
        old_value = old_values.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="Regularization does not change the record")

    entry = {"at": utc_now(), "by": regularized_by, "reason": reason, "changes": changes}
    # Compare-and-swap protects against lost concurrent edits and lost history entries.
    expected_history = record.get("history", [])
    # Legacy records may not contain a history field at all; treat that as an empty list.
    history_filter = {"history": expected_history}
    if "history" not in record:
        history_filter = {"$or": [{"history": []}, {"history": {"$exists": False}}]}
    result = database.attendance_logs.update_one(
        {"_id": record["_id"], **history_filter},
        {"$set": new_values, "$push": {"history": entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry regularization")
    record.update(new_values)
    record.setdefault("history", []).append(entry)
    return api_attendance(record)


def month_filter(first: date, last: date, **extra) -> dict:
    return {**extra, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}


def round_decimal_expression(expression: Any, places: int) -> dict:
    """MongoDB aggregation expression using $round; final reporting is normalized below."""
    return {"$round": [expression, places]}



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    database = get_db()
    employee = database.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1, "emp_code": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    first, last = validate_month(month)
    working_start = max(first, date.fromisoformat(employee["joined_on"]))
    working_days = sum(1 for offset in range(max(0, (last - working_start).days + 1)) if (working_start + timedelta(days=offset)).weekday() < 5)
    pipeline = [
        {"$match": month_filter(first, last, emp_code=emp_code)},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(database.attendance_logs.aggregate(pipeline))
    totals = rows[0] if rows else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    present_days = round_half_up(totals["present_days"], 2)
    pct = round_half_up(present_days / working_days * 100, 2) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present_days, "leave_days": totals["leave_days"], "late_count": totals["late_count"], "total_late_minutes": totals["total_late_minutes"], "total_overtime_minutes": totals["total_overtime_minutes"], "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    database = get_db()
    first, last = validate_month(month)
    employee_match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}, {"$gte": ["$date", "$$joined"]}]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 1]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$avg": "$hours"}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": ASCENDING}},
    ]
    items = list(database.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2) if item.get("avg_work_hours") is not None else None
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: str | None = None):
    database = get_db()
    first, last = validate_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]
    return {"month": month, "items": list(database.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    database = get_db()
    first = validate_date_string(from_date, "from")
    last = validate_date_string(to_date, "to")
    if last < first or (last - first).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range must be ordered and no longer than 92 days")
    if database.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Unknown department")
    # Aggregate logs by day, then densify the date series and calculate a rolling mean in MongoDB.
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(first, datetime.min.time()), datetime.combine(last + timedelta(days=1), datetime.min.time())]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}}, {"$count": "count"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}, "weekday": {"$dayOfWeek": "$day"}}},
        {"$set": {"is_working_day": {"$and": [{"$ne": ["$weekday", 1]}, {"$ne": ["$weekday", 7]}]}, "attendance_rate": {"$cond": [{"$and": [{"$ne": ["$weekday", 1]}, {"$ne": ["$weekday", 7]}, {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": ASCENDING}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": ASCENDING}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]
    items = list(database.attendance_logs.aggregate(pipeline))
    for item in items:
        item["attendance_rate"] = round_half_up(item["attendance_rate"], 4) if item.get("attendance_rate") is not None else None
        item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4) if item.get("moving_avg_7d") is not None else None
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = get_db()
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None: query["emp_code"] = emp_code
        if status is not None: query["status"] = status
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None: query["date"]["$gte"] = date_from
            if date_to is not None: query["date"]["$lte"] = date_to
        operation = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
    elif endpoint in {"employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        if endpoint == "employee_monthly" and (not emp_code or not month):
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        if endpoint in {"department_summary", "late_leaderboard"} and not month:
            raise HTTPException(status_code=422, detail="month is required")
        if endpoint == "department_trend" and (not department or not from_date or not to_date):
            raise HTTPException(status_code=422, detail="department, from and to are required")
        if month: first, last = validate_month(month)
        if endpoint == "employee_monthly":
            pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}]
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            match = {"joined_on": {"$lte": last.isoformat()}}
            if department is not None: match["department"] = department
            pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}]
            collection = "employees"
        elif endpoint == "late_leaderboard":
            pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
            collection = "attendance_logs"
        else:
            first = validate_date_string(from_date, "from"); last = validate_date_string(to_date, "to")
            pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}}, {"$match": {"employee.department": department}}]
            collection = "attendance_logs"
        operation = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    else:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")
    try:
        explain = database.command({"explain": operation, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Explain failed: {exc.__class__.__name__}")
    safe_explain = json.loads(json_util.dumps(explain))

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": safe_explain,
    }

