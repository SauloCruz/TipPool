"""FastAPI app: auth, employees, daily review, periods, CSV export.

All money over the API is integer cents. Finalized days are immutable
snapshots; editing requires an admin reopen and re-finalizing writes the
next snapshot version (history retained, §2 rule 6).

NOTE: no `from __future__ import annotations` here — stringified annotations
break FastAPI's resolution of the closure-local Annotated dependency aliases
(DB/User/Admin) defined inside create_app()."""

import asyncio
import contextlib
import csv
import io
import json
import logging
import mimetypes
import sqlite3
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import engine

from . import auth as auth_mod
from . import settings_store, sync
from .compute import (EMPTY_INPUTS_BY_MODEL, DayValidationError,
                      compute_lf_outputs, compute_outputs,
                      compute_poq_outputs)
from .config import Settings
from .db import SCHEMA_VERSION, audit, connect, init_db, utcnow
from .periods import (VENUE_SCHEMES, next_period_scheme, period_days,
                      period_for_scheme, prev_period_scheme)
from .square import SquareClient, SquareError
from .square_extract import MUTABLE_WARNINGS
from engine import distribute_cents, round_hours_up

STATIC_DIR = Path(__file__).parent.parent / "static"
# informational flags: shown as reminders, never mark a day as flagged
INFO_FLAGS = {"no_host_resplit"}

# IRS 1099-NEC reporting threshold: report a contractor once the calendar-year
# total reaches this. Counted per venue (owner 2026-08-30).
CONTRACTOR_1099_CENTS = 60000


# ---------- request/response models ----------

log = logging.getLogger("tippool")


class LoginBody(BaseModel):
    email: str
    password: str


class DayInputsBody(BaseModel):
    food_sales_cents: int = Field(default=0, ge=0)
    event_food_sales_cents: int = Field(default=0, ge=0)
    credit_tips_cents: int = 0
    cash_tips_cents: int = 0
    event_tips_cents: int = 0
    auto_gratuity_cents: int = 0
    boh_worked: list[int] = []
    foh_hours: dict[int, float] = {}
    # Contract labour hours — a separate field from foh_hours on purpose, so
    # typing them never marks the pulled hours map as an override (see
    # compute.EMPTY_INPUTS). Square never writes here.
    contractor_hours: dict[int, float] = {}
    # staff working the host/door that night — half tip credit per hour
    # (tl_door_weight). Per-day, not a fixed role: staff work dual roles.
    door_worked: list[int] = []
    # Derived by the pull from each shift's Square job, not manager-edited:
    # employee_id -> exact weight string ("1/2" for a door-only night, "5/6"
    # for five floor hours and one on the door). Round-tripped so a snapshot
    # explains its own numbers; a re-pull always replaces it.
    foh_role_weights: dict[int, str] = {}
    # event deposits attached to this day, "<order_id>:<line_uid>"
    event_deposit_ids: list[str] = []

    @field_validator("foh_role_weights")
    @classmethod
    def _weights_sane(cls, v):
        for eid, w in v.items():
            try:
                f = Fraction(str(w))
            except (ValueError, ZeroDivisionError) as exc:
                raise ValueError(f"bad tip-credit weight for employee {eid}") from exc
            if not 0 <= f <= 1:
                raise ValueError(f"tip-credit weight for employee {eid} must be 0-1")
        return v

    @field_validator("event_deposit_ids")
    @classmethod
    def _no_deposit_dupes(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("the same deposit is attached twice")
        return v

    @field_validator("foh_hours")
    @classmethod
    def _hours_sane(cls, v):
        for eid, h in v.items():
            if not 0 <= h <= 24:
                raise ValueError(f"hours for employee {eid} must be 0-24")
        return v

    @field_validator("boh_worked")
    @classmethod
    def _no_dupes(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("duplicate employee in BOH roster")
        return v

    @field_validator("door_worked")
    @classmethod
    def _no_door_dupes(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("duplicate employee in door roster")
        return v


class EventDepositsBody(BaseModel):
    """Deposit ids ("<order_id>:<line_uid>") attached to one event day."""
    deposit_ids: list[str] = []


class PoqShiftBody(BaseModel):
    """One timecard's worth of work. Role is the job chosen at clock-in."""
    employee_id: int
    role: str
    hours: float = 0

    @field_validator("hours")
    @classmethod
    def _sane(cls, v):
        if not 0 <= v <= 24:
            raise ValueError("hours must be 0-24")
        return v


class PoqDayInputsBody(BaseModel):
    """POINTS_HOURS day inputs (Poquitos). All money integer cents."""
    credit_tips_cents: int = 0
    cash_tips_cents: int = 0
    auto_gratuity_cents: int = 0
    shifts: list[PoqShiftBody] = []
    event_service_charge_cents: int = Field(default=0, ge=0)
    event_tips_cents: int = Field(default=0, ge=0)
    event_card_cents: int = Field(default=0, ge=0)
    event_start: str | None = None
    event_end: str | None = None
    event_bartender_employee_id: int | None = None
    event_bartender_hours: float = Field(default=0.0, ge=0)
    net_sales_cents: int = Field(default=0, ge=0)   # reporting only


class LFDayInputsBody(BaseModel):
    """PERCENT_TIPOUT day inputs (La Fontana). All money integer cents."""
    server_tips: dict[int, int] = {}
    server_cash_tips: dict[int, int] = {}
    auto_gratuity_cents: int = 0
    hours: dict[int, float] = {}
    unattributed_tips_cents: int = Field(default=0, ge=0)
    unattributed_assignments: dict[int, int] = {}
    unattributed_house_cents: int = Field(default=0, ge=0)

    @field_validator("server_tips", "server_cash_tips", "unattributed_assignments")
    @classmethod
    def _cents_non_negative(cls, v):
        for eid, cents in v.items():
            if cents < 0:
                raise ValueError(f"negative cents for employee {eid}")
        return v

    @field_validator("hours")
    @classmethod
    def _lf_hours_sane(cls, v):
        for eid, h in v.items():
            if not 0 <= h <= 24:
                raise ValueError(f"hours for employee {eid} must be 0-24")
        return v


class EmployeeBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    pool_role: str = Field(pattern="^(FOH|BOH|EXCLUDED|SERVER|BUSSER|HOST)$")
    square_team_member_id: str | None = None
    # contract labour: no Square account, paid directly against a W-9
    is_contractor: bool = False
    hourly_rate_cents: int | None = Field(default=None, ge=0)
    w9_received: bool = False


class EmployeePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    pool_role: str | None = Field(default=None, pattern="^(FOH|BOH|EXCLUDED|SERVER|BUSSER|HOST)$")
    active: bool | None = None
    square_team_member_id: str | None = None
    # LF: salaried kitchen staff never clock in but always share the monthly
    # BOH pool (pre-selected on the export roster)
    always_in_boh_pool: bool | None = None
    # not a payroll employee at all — an admin login, a contractor. Keeps
    # them off the payroll entry sheet without deactivating the record.
    in_payroll: bool | None = None
    # Contract labour (owner 2026-08-30): works shifts and shares the pool,
    # paid directly against a W-9. Setting this forces in_payroll off — a
    # 1099 worker must never be typed into the payroll form.
    is_contractor: bool | None = None
    hourly_rate_cents: int | None = Field(default=None, ge=0)
    w9_received: bool | None = None


class SettingsPatch(BaseModel):
    category_map: dict[str, dict] | None = None
    gratuity_service_charge: dict | None = None
    # service charges the house keeps: never staff money, whatever Square types
    # them (see settings_store.house_service_charges)
    house_service_charges: list[str] | None = None
    tippable_windows: dict[str, dict] | None = None
    rounding_increment: str | None = None
    # 0 (midnight) .. 360 (6 AM); how far past midnight the business day runs
    day_cutoff_minutes: int | None = Field(default=None, ge=0, le=360)
    muted_warnings: list[str] | None = None
    # PERCENT_TIPOUT venue settings (La Fontana)
    lf_percentages: dict | None = None
    lf_pool_split_mode: dict | None = None
    # flag no-host days only when bussers < N (0 = never flag)
    lf_no_host_min_bussers: int | None = Field(default=None, ge=0, le=20)
    # POOL_HOURS: tip credit per hour for a host/door shift ("0.5" = half, "1" = off)
    tl_door_weight: str | None = None
    # POOL_HOURS: Square job title -> FOH | DOOR | BOH | EXCLUDED. The SHIFT
    # decides the pool role, because staff hold two jobs (owner 2026-08-29).
    tl_job_roles: dict[str, str] | None = None
    tl_event_items: dict | None = None
    tl_deposit_lookback_days: int | None = Field(default=None, ge=1, le=730)
    # POINTS_HOURS: card processing fee withheld from credit tips before pooling
    poq_card_fee_pct: str | None = None
    poq_foh_pct: str | None = None
    # POINTS_HOURS: the Square logon private events are rung under. Its orders
    # become the event pool instead of the day's auto-gratuity; "" disables
    # detection and events are entered by hand.
    poq_event_logon_tmid: str | None = None
    # POINTS_HOURS overtime, REPORTING ONLY — mirrors the venue's payroll
    # settings so the period report reconciles; never enters a tip payout
    poq_workweek_start: str | None = None
    poq_overtime_after: str | None = None

    @field_validator("tl_job_roles")
    @classmethod
    def _job_roles_valid(cls, v):
        if v is None:
            return v
        allowed = {"FOH", "DOOR", "BOH", "EXCLUDED"}
        for title, role in v.items():
            if not str(title).strip():
                raise ValueError("a job title cannot be blank")
            if role not in allowed:
                raise ValueError(
                    f"{title!r}: role must be one of {sorted(allowed)}")
        return dict(v)

    @field_validator("poq_workweek_start")
    @classmethod
    def _weekday_valid(cls, v):
        if v is None:
            return v
        if str(v).upper() not in settings_store.WEEKDAYS:
            raise ValueError(f"must be one of {list(settings_store.WEEKDAYS)}")
        return str(v).upper()

    @field_validator("poq_overtime_after")
    @classmethod
    def _ot_threshold_valid(cls, v):
        if v is None:
            return v
        try:
            hours = float(str(v))
        except ValueError:
            raise ValueError("must be a number of hours, like 40")
        if not 0 < hours <= 168:
            raise ValueError("must be more than 0 and at most 168")
        return str(v)

    @field_validator("poq_card_fee_pct", "poq_foh_pct")
    @classmethod
    def _pct_valid(cls, v):
        if v is None:
            return v
        try:
            pct = Fraction(str(v))
        except (ValueError, ZeroDivisionError):
            raise ValueError("must be a number like 3 or 2.75")
        if not 0 <= pct <= 100:
            raise ValueError("must be between 0 and 100")
        return str(v)

    @field_validator("tl_door_weight")
    @classmethod
    def _door_weight_valid(cls, v):
        if v is None:
            return v
        try:
            w = Fraction(str(v))
        except (ValueError, ZeroDivisionError):
            raise ValueError("door weight must be a number like 0.5")
        if not 0 <= w <= 1:
            raise ValueError("door weight must be between 0 and 1")
        return str(v)

    @field_validator("lf_percentages")
    @classmethod
    def _lf_percentages_valid(cls, v):
        if v is None:
            return v
        from engine import validate_percentages
        validate_percentages(v)  # raises on bad/missing/≠100 totals
        return v

    @field_validator("lf_pool_split_mode")
    @classmethod
    def _lf_split_mode_valid(cls, v):
        if v is None:
            return v
        for bucket, mode in v.items():
            if bucket not in ("busser", "host", "boh"):
                raise ValueError(f"unknown pool {bucket!r}")
            if mode not in ("EVEN", "HOURS_PROPORTIONAL"):
                raise ValueError(f"bad split mode {mode!r} for {bucket}")
        return v

    @field_validator("muted_warnings")
    @classmethod
    def _only_mutable_warnings(cls, v):
        if v is None:
            return v
        bad = set(v) - set(MUTABLE_WARNINGS)
        if bad:
            raise ValueError(
                f"not mutable: {sorted(bad)} — blocking issues cannot be muted")
        return sorted(set(v))

    @field_validator("category_map")
    @classmethod
    def _groups_valid(cls, v):
        if v is None:
            return v
        for cid, entry in v.items():
            g = entry.get("group")
            if g is not None and g not in settings_store.CATEGORY_GROUPS:
                raise ValueError(f"bad group {g!r} for category {cid}")
        return v

    @field_validator("tippable_windows")
    @classmethod
    def _windows_valid(cls, v):
        if v is None:
            return v
        for wd, w in v.items():
            if int(wd) not in range(7):
                raise ValueError(f"bad weekday {wd}")
            if not 0 <= w["open_minutes"] < w["close_minutes"] <= 1440:
                raise ValueError(f"bad window for weekday {wd}")
        return v


class UserBody(BaseModel):
    # deliberately loose: LAN app, admin-entered; strict RFC validation rejects
    # perfectly usable internal addresses like name@host.local
    email: str = Field(pattern=r"^\S+@\S+\.\S+$")
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(manager|admin)$")
    venue_ids: list[int] | None = None
    super_admin: bool = False


class UserPatch(BaseModel):
    email: str | None = Field(default=None, pattern=r"^\S+@\S+\.\S+$")
    password: str | None = Field(default=None, min_length=8)
    role: str | None = Field(default=None, pattern="^(manager|admin)$")
    active: bool | None = None
    venue_ids: list[int] | None = None
    super_admin: bool | None = None


# ---------- app factory ----------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    init_db(settings.db_path, settings.venue_name, settings.timezone)

    boot = connect(settings.db_path)
    try:
        venue = boot.execute("SELECT * FROM venue LIMIT 1").fetchone()
        if auth_mod.bootstrap_admin(
            boot, venue["id"], settings.admin_email, settings.admin_password
        ):
            audit(boot, venue["id"], None, "bootstrap_admin", "user", settings.admin_email)
        auth_mod.prune_expired_sessions(boot)
        boot.commit()
    finally:
        boot.close()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.nightly_sync:
            task = asyncio.create_task(nightly_sync_loop())
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Tavern Law Tip Pool", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.settings = settings

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Container/platform health check: verifies the app can open SQLite."""
        conn = connect(settings.db_path)
        try:
            db_version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return {"ok": True, "schema_version": db_version or SCHEMA_VERSION}

    # overridable in tests: swaps the real Square client for a fake.
    # Per-venue credentials (M5): tokens are never mixed across venues.
    def _real_square_client(venue_slug: str) -> SquareClient:
        creds = settings.square_for(venue_slug)
        return SquareClient(creds["token"], creds["location_ids"], env=creds["env"])

    app.state.square_client_factory = _real_square_client

    # ---------- dependencies ----------

    def get_db():
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    DB = Annotated[sqlite3.Connection, Depends(get_db)]

    def current_user(
        conn: DB, session_token: Annotated[str | None, Cookie()] = None
    ) -> sqlite3.Row:
        user = auth_mod.get_session_user(conn, session_token or "")
        if user is None:
            raise HTTPException(401, "not signed in")
        return user

    User = Annotated[sqlite3.Row, Depends(current_user)]

    def is_super_admin(user: sqlite3.Row) -> bool:
        return bool(user["super_admin"])

    def effective_role(conn: sqlite3.Connection, user: sqlite3.Row,
                       venue_id: int) -> str | None:
        if is_super_admin(user):
            return "admin"
        row = conn.execute(
            "SELECT role FROM user_venue_access WHERE user_id = ? AND venue_id = ?",
            (user["id"], venue_id),
        ).fetchone()
        if row:
            return row["role"]
        # Backwards-compatible fallback for users created before explicit RBAC.
        if user["venue_id"] == venue_id:
            return user["role"]
        return None

    def accessible_venues(conn: sqlite3.Connection, user: sqlite3.Row) -> list[dict]:
        if is_super_admin(user):
            rows = conn.execute(
                "SELECT *, 'admin' AS access_role FROM venue ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        rows = conn.execute(
            """SELECT v.*, COALESCE(a.role, u.role) AS access_role
               FROM venue v
               JOIN user u ON u.id = ?
               LEFT JOIN user_venue_access a
                    ON a.venue_id = v.id AND a.user_id = u.id
               WHERE a.user_id IS NOT NULL OR v.id = u.venue_id
               ORDER BY v.id""",
            (user["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    def current_venue(
        conn: DB, user: User, x_venue_id: Annotated[str | None, Header()] = None
    ) -> sqlite3.Row:
        """Venue scope for the request. Non-super users are limited to venues
        explicitly assigned in user_venue_access, with their legacy home venue
        retained for compatibility."""
        if x_venue_id:
            row = conn.execute(
                "SELECT * FROM venue WHERE id = ?", (x_venue_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, f"unknown venue {x_venue_id!r}")
            if effective_role(conn, user, row["id"]) is None:
                raise HTTPException(403, "no access to this venue")
            return row
        venues = accessible_venues(conn, user)
        if not venues:
            raise HTTPException(403, "no venue access configured")
        return conn.execute(
            "SELECT * FROM venue WHERE id = ?", (venues[0]["id"],)
        ).fetchone()

    Venue = Annotated[sqlite3.Row, Depends(current_venue)]

    def require_admin(user: User, conn: DB, venue: Venue) -> sqlite3.Row:
        if effective_role(conn, user, venue["id"]) != "admin":
            raise HTTPException(403, "admin only")
        return user

    Admin = Annotated[sqlite3.Row, Depends(require_admin)]

    def require_super_admin(user: User) -> sqlite3.Row:
        if not is_super_admin(user):
            raise HTTPException(403, "super admin only")
        return user

    SuperAdmin = Annotated[sqlite3.Row, Depends(require_super_admin)]

    def parse_date(s: str) -> date:
        try:
            return date.fromisoformat(s)
        except ValueError:
            raise HTTPException(422, f"invalid date {s!r}")

    def employees_map(conn, venue_id: int) -> dict[int, dict]:
        rows = conn.execute(
            "SELECT * FROM employee WHERE venue_id = ?", (venue_id,)
        ).fetchall()
        return {
            r["id"]: {"display_name": r["display_name"], "pool_role": r["pool_role"],
                      "active": bool(r["active"]),
                      "always_in_boh_pool": bool(r["always_in_boh_pool"]),
                      "in_payroll": bool(r["in_payroll"]),
                      "is_contractor": bool(r["is_contractor"]),
                      "hourly_rate_cents": r["hourly_rate_cents"],
                      "w9_received": bool(r["w9_received"])}
            for r in rows
        }

    def compute_or_422(conn, venue, inputs: dict, emps: dict) -> dict:
        try:
            if venue["tip_model"] == "PERCENT_TIPOUT":
                return compute_lf_outputs(
                    inputs, emps,
                    settings_store.get_setting(conn, venue["id"], "lf_percentages"),
                    settings_store.get_setting(conn, venue["id"], "lf_pool_split_mode"),
                    settings_store.get_setting(conn, venue["id"], "lf_no_host_min_bussers"),
                )
            if venue["tip_model"] == "POINTS_HOURS":
                st = settings_store.all_settings(conn, venue["id"])
                return compute_poq_outputs(
                    inputs, emps, st["poq_roles"], st["poq_job_roles"],
                    st["poq_foh_pct"], st["poq_support_pct"],
                    st["poq_card_fee_pct"],
                )
            return compute_outputs(
                inputs, emps,
                settings_store.get_setting(conn, venue["id"], "tl_door_weight"),
                settings_store.rounding_increment(
                    settings_store.all_settings(conn, venue["id"])),
            )
        except DayValidationError as exc:
            raise HTTPException(422, str(exc))

    def day_row(conn, venue_id: int, d: date):
        return conn.execute(
            "SELECT * FROM day WHERE venue_id = ? AND date = ?", (venue_id, d.isoformat())
        ).fetchone()

    def snapshot_record(conn, day_id: int) -> tuple[dict, dict] | None:
        row = conn.execute(
            "SELECT inputs_json, outputs_json FROM day_snapshot WHERE day_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (day_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["inputs_json"]), json.loads(row["outputs_json"])

    def snapshot_outputs(conn, day_id: int) -> dict | None:
        row = conn.execute(
            "SELECT outputs_json FROM day_snapshot WHERE day_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (day_id,),
        ).fetchone()
        return json.loads(row["outputs_json"]) if row else None

    # ---------- auth ----------

    @app.post("/api/login")
    def login(body: LoginBody, conn: DB, response: Response):
        user = conn.execute(
            "SELECT * FROM user WHERE email = ? COLLATE NOCASE AND active = 1",
            (body.email.strip(),),
        ).fetchone()
        if user is None or not auth_mod.verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "invalid email or password")
        token = auth_mod.create_session(conn, user["id"], settings.session_days)
        conn.commit()
        response.set_cookie(
            "session_token", token, httponly=True, samesite="lax",
            max_age=settings.session_days * 86400,
        )
        return {"id": user["id"], "email": user["email"], "role": user["role"],
                "super_admin": bool(user["super_admin"])}

    @app.post("/api/logout")
    def logout(conn: DB, response: Response,
               session_token: Annotated[str | None, Cookie()] = None):
        if session_token:
            auth_mod.delete_session(conn, session_token)
            conn.commit()
        response.delete_cookie("session_token")
        return {"ok": True}

    DEFAULT_PREFS = {
        # Confirm prompts on finalize/reopen. On by default: a manager closing
        # one night wants the guardrail. An admin re-running a stretch of days
        # after an engine change finds it pure friction, and neither action is
        # destructive — a reopen keeps every snapshot, and re-finalizing writes
        # a new version rather than overwriting one.
        "skip_confirmations": False,
    }

    def user_prefs(user) -> dict:
        raw = user["prefs_json"] if "prefs_json" in user.keys() else None
        stored = json.loads(raw) if raw else {}
        return {**DEFAULT_PREFS, **{k: v for k, v in stored.items()
                                    if k in DEFAULT_PREFS}}

    class PrefsBody(BaseModel):
        skip_confirmations: bool | None = None

    @app.put("/api/me/prefs")
    def put_my_prefs(body: PrefsBody, user: User, conn: DB):
        """A user's own interface preferences — never anyone else's."""
        prefs = {**user_prefs(user),
                 **body.model_dump(exclude_none=True)}
        conn.execute("UPDATE user SET prefs_json = ? WHERE id = ?",
                     (json.dumps(prefs), user["id"]))
        conn.commit()
        return prefs

    @app.get("/api/me")
    def me(user: User, conn: DB, venue: Venue):
        today = datetime.now(ZoneInfo(venue["timezone"])).date()
        venues = accessible_venues(conn, user)
        role = effective_role(conn, user, venue["id"]) or user["role"]
        return {
            "id": user["id"], "email": user["email"], "role": role,
            "super_admin": bool(user["super_admin"]),
            "venue": {"id": venue["id"], "name": venue["name"],
                      "timezone": venue["timezone"], "slug": venue["slug"],
                      "tip_model": venue["tip_model"]},
            "venues": venues,
            "today": today.isoformat(),
            "prefs": user_prefs(user),
        }

    @app.get("/api/venues")
    def list_venues(user: User, conn: DB):
        rows = accessible_venues(conn, user)
        out = []
        for v in rows:
            entry = dict(v)
            entry["square_configured"] = settings.square_for(v["slug"])["configured"]
            out.append(entry)
        return out

    # ---------- users (admin) ----------

    def validate_venue_ids(conn: sqlite3.Connection, venue_ids: list[int]) -> None:
        if not venue_ids:
            raise HTTPException(422, "choose at least one venue")
        found = {r["id"] for r in conn.execute(
            f"SELECT id FROM venue WHERE id IN ({','.join('?' for _ in venue_ids)})",
            venue_ids,
        ).fetchall()}
        missing = sorted(set(venue_ids) - found)
        if missing:
            raise HTTPException(422, f"unknown venue ids: {missing}")

    def user_access_payload(conn: sqlite3.Connection, user_row: sqlite3.Row) -> dict:
        access = [dict(r) for r in conn.execute(
            """SELECT v.id AS venue_id, v.name, v.slug, a.role
               FROM user_venue_access a JOIN venue v ON v.id = a.venue_id
               WHERE a.user_id = ? ORDER BY v.id""",
            (user_row["id"],),
        ).fetchall()]
        if not access:
            home = conn.execute(
                "SELECT id AS venue_id, name, slug, ? AS role FROM venue WHERE id = ?",
                (user_row["role"], user_row["venue_id"]),
            ).fetchone()
            if home:
                access = [dict(home)]
        return {
            "id": user_row["id"], "email": user_row["email"],
            "role": user_row["role"], "active": bool(user_row["active"]),
            "created_at": user_row["created_at"],
            "super_admin": bool(user_row["super_admin"]),
            "venue_id": user_row["venue_id"],
            "access": access,
        }

    @app.post("/api/users", status_code=201)
    def create_user(body: UserBody, super_admin: SuperAdmin, conn: DB, venue: Venue):
        venue_ids = sorted(set(body.venue_ids or [venue["id"]]))
        validate_venue_ids(conn, venue_ids)
        role = "admin" if body.super_admin else body.role
        try:
            cur = conn.execute(
                "INSERT INTO user (venue_id, email, password_hash, role, super_admin, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (venue_ids[0], body.email.strip(), auth_mod.hash_password(body.password),
                 role, int(body.super_admin), utcnow()),
            )
            for vid in venue_ids:
                conn.execute(
                    "INSERT INTO user_venue_access (user_id, venue_id, role)"
                    " VALUES (?, ?, ?)",
                    (cur.lastrowid, vid, role),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a user with that email already exists")
        audit(conn, venue["id"], super_admin["id"], "user_created", "user", cur.lastrowid,
              json.dumps({"email": body.email, "role": role,
                          "venue_ids": venue_ids, "super_admin": body.super_admin}))
        conn.commit()
        row = conn.execute("SELECT * FROM user WHERE id = ?", (cur.lastrowid,)).fetchone()
        return user_access_payload(conn, row)

    @app.get("/api/users")
    def list_users(super_admin: SuperAdmin, conn: DB):
        rows = conn.execute(
            "SELECT * FROM user ORDER BY email"
        ).fetchall()
        return [user_access_payload(conn, r) for r in rows]

    @app.patch("/api/users/{user_id}")
    def update_user(user_id: int, body: UserPatch, super_admin: SuperAdmin,
                    conn: DB, venue: Venue):
        row = conn.execute("SELECT * FROM user WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "user not found")
        changes = body.model_dump(exclude_none=True)
        if user_id == super_admin["id"]:
            if changes.get("active") is False:
                raise HTTPException(422, "cannot deactivate your own user")
            if changes.get("super_admin") is False:
                raise HTTPException(422, "cannot remove your own Super Admin access")
        venue_ids = changes.pop("venue_ids", None)
        if venue_ids is not None:
            venue_ids = sorted(set(venue_ids))
            validate_venue_ids(conn, venue_ids)
        password = changes.pop("password", None)
        role = changes.get("role", row["role"])
        if changes.get("super_admin") is True:
            role = "admin"
            changes["role"] = "admin"
        if password:
            changes["password_hash"] = auth_mod.hash_password(password)
        if "super_admin" in changes:
            changes["super_admin"] = int(changes["super_admin"])
        if venue_ids is not None:
            changes["venue_id"] = venue_ids[0]
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            try:
                conn.execute(
                    f"UPDATE user SET {sets} WHERE id = ?",
                    (*changes.values(), user_id),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "a user with that email already exists")
        if venue_ids is not None:
            conn.execute("DELETE FROM user_venue_access WHERE user_id = ?", (user_id,))
            for vid in venue_ids:
                conn.execute(
                    "INSERT INTO user_venue_access (user_id, venue_id, role)"
                    " VALUES (?, ?, ?)",
                    (user_id, vid, role),
                )
        elif body.role is not None:
            conn.execute(
                "UPDATE user_venue_access SET role = ? WHERE user_id = ?",
                (role, user_id),
            )
        audit(conn, venue["id"], super_admin["id"], "user_updated", "user", user_id,
              json.dumps({k: ("***" if k == "password_hash" else v)
                          for k, v in changes.items()} |
                         ({"venue_ids": venue_ids} if venue_ids is not None else {})))
        conn.commit()
        row = conn.execute("SELECT * FROM user WHERE id = ?", (user_id,)).fetchone()
        return user_access_payload(conn, row)

    @app.get("/api/audit-log")
    def audit_log(user: User, conn: DB, venue: Venue,
                  limit: int = Query(default=200, ge=1, le=500),
                  all_venues: bool = False):
        if all_venues:
            if not is_super_admin(user):
                raise HTTPException(403, "super admin only")
            rows = conn.execute(
                """SELECT a.id, a.ts, a.action, a.entity, a.entity_id,
                          a.detail_json, u.email AS user_email,
                          v.name AS venue_name, v.slug AS venue_slug
                   FROM audit_log a
                   LEFT JOIN user u ON u.id = a.user_id
                   JOIN venue v ON v.id = a.venue_id
                   ORDER BY a.ts DESC, a.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            if effective_role(conn, user, venue["id"]) != "admin":
                raise HTTPException(403, "admin only")
            rows = conn.execute(
                """SELECT a.id, a.ts, a.action, a.entity, a.entity_id,
                          a.detail_json, u.email AS user_email,
                          v.name AS venue_name, v.slug AS venue_slug
                   FROM audit_log a
                   LEFT JOIN user u ON u.id = a.user_id
                   JOIN venue v ON v.id = a.venue_id
                   WHERE a.venue_id = ?
                   ORDER BY a.ts DESC, a.id DESC
                   LIMIT ?""",
                (venue["id"], limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- employees ----------

    # roles legal per tip model — enforced against the request's venue
    VALID_ROLES = {
        "POOL_HOURS": {"FOH", "BOH", "EXCLUDED"},
        "PERCENT_TIPOUT": {"SERVER", "BUSSER", "HOST", "BOH", "EXCLUDED"},
        # POINTS_HOURS reads the real role off each timecard, so the
        # person-level value is descriptive only — except EXCLUDED, which is
        # the manager hard-block safety net on top of the excluded JOBS.
        "POINTS_HOURS": {"FOH", "BOH", "EXCLUDED"},
    }

    def check_role(venue, role: str) -> None:
        allowed = VALID_ROLES[venue["tip_model"]]
        if role not in allowed:
            raise HTTPException(
                422, f"role {role!r} is not valid for {venue['name']}"
                     f" ({venue['tip_model']}); use one of {sorted(allowed)}")

    def employee_links(conn, venue_id: int) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for r in conn.execute(
                "SELECT employee_id, team_member_id FROM square_link"
                " WHERE venue_id = ? ORDER BY team_member_id", (venue_id,)):
            out.setdefault(r["employee_id"], []).append(r["team_member_id"])
        return out

    @app.get("/api/employees")
    def list_employees(user: User, conn: DB, venue: Venue):
        links = employee_links(conn, venue["id"])
        rows = conn.execute(
            "SELECT id, display_name, pool_role, active, always_in_boh_pool,"
            " in_payroll, is_contractor, hourly_rate_cents, w9_received"
            " FROM employee WHERE venue_id = ?"
            " ORDER BY pool_role, display_name",
            (venue["id"],),
        ).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            tmids = links.get(r["id"], [])
            e["square_team_member_ids"] = tmids
            e["square_team_member_id"] = tmids[0] if tmids else None
            out.append(e)
        return out

    def check_contractor(is_contractor, rate, tmid):
        """Contract labour rules (owner 2026-08-30).

        A rate is required because the point of recording a contractor is
        working out what to hand them; without one the $600 calendar-year
        total would silently undercount and the threshold would be crossed
        unnoticed. A Square account means they are on the till and, sooner or
        later, on payroll — the two states are exclusive, and mixing them is
        how someone ends up on both a 1099 and a W-2 for the same shifts.
        """
        if not is_contractor:
            return
        if rate is None:
            raise HTTPException(
                422, "contract labour needs an hourly rate — it is what the "
                     "$600 calendar-year total is built from")
        if tmid:
            raise HTTPException(
                422, "a contractor cannot also have a Square account: link "
                     "them to Square when you move them onto payroll, and "
                     "clear the contractor flag at the same time")

    @app.post("/api/employees", status_code=201)
    def create_employee(body: EmployeeBody, admin: Admin, conn: DB, venue: Venue):
        check_role(venue, body.pool_role)
        check_contractor(body.is_contractor, body.hourly_rate_cents,
                         body.square_team_member_id)
        try:
            cur = conn.execute(
                "INSERT INTO employee (venue_id, display_name, pool_role,"
                " created_at, is_contractor, hourly_rate_cents, w9_received,"
                " in_payroll) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (venue["id"], body.display_name.strip(), body.pool_role, utcnow(),
                 int(body.is_contractor), body.hourly_rate_cents,
                 int(body.w9_received), 0 if body.is_contractor else 1),
            )
            if body.square_team_member_id:
                conn.execute(
                    "INSERT INTO square_link (venue_id, team_member_id, employee_id)"
                    " VALUES (?, ?, ?)",
                    (venue["id"], body.square_team_member_id, cur.lastrowid),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(
                409, "employee name already exists (link the Square account to"
                     " them instead) or that Square account is already linked")
        audit(conn, venue["id"], admin["id"], "employee_created", "employee",
              cur.lastrowid, json.dumps(body.model_dump()))
        conn.commit()
        # read the row back rather than echoing the request: defaults set by
        # the schema (active, in_payroll) belong in the response, and a
        # hand-built dict silently drops every column added later
        out = dict(conn.execute(
            "SELECT * FROM employee WHERE id = ?", (cur.lastrowid,)).fetchone())
        out["square_team_member_ids"] = employee_links(
            conn, venue["id"]).get(cur.lastrowid, [])
        out["square_team_member_id"] = (out["square_team_member_ids"][0]
                                        if out["square_team_member_ids"] else None)
        return out

    @app.patch("/api/employees/{employee_id}")
    def update_employee(employee_id: int, body: EmployeePatch, admin: Admin, conn: DB, venue: Venue):
        row = conn.execute(
            "SELECT * FROM employee WHERE id = ? AND venue_id = ?",
            (employee_id, venue["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "employee not found in this venue")
        if body.pool_role is not None:
            check_role(venue, body.pool_role)
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        want_contractor = changes.get("is_contractor", bool(row["is_contractor"]))
        want_rate = changes.get("hourly_rate_cents", row["hourly_rate_cents"])
        existing_link = conn.execute(
            "SELECT 1 FROM square_link WHERE venue_id = ? AND employee_id = ?",
            (venue["id"], employee_id)).fetchone()
        want_link = changes.get("square_team_member_id",
                                "keep" if existing_link else None)
        check_contractor(want_contractor, want_rate,
                         None if want_link in ("", None) else want_link)
        # a 1099 worker must never reach the payroll entry sheet
        if want_contractor:
            changes["in_payroll"] = False
        # Square links live in square_link (one person, many accounts):
        # "" clears every link for this employee; a value ADDS a link.
        tmid = changes.pop("square_team_member_id", None)
        if tmid == "":
            conn.execute(
                "DELETE FROM square_link WHERE venue_id = ? AND employee_id = ?",
                (venue["id"], employee_id))
            audit(conn, venue["id"], admin["id"], "square_unlinked", "employee",
                  employee_id)
        elif tmid:
            try:
                conn.execute(
                    "INSERT INTO square_link (venue_id, team_member_id, employee_id)"
                    " VALUES (?, ?, ?)", (venue["id"], tmid, employee_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "that Square account is already linked"
                                         " to another employee")
            audit(conn, venue["id"], admin["id"], "square_linked", "employee",
                  employee_id, json.dumps({"team_member_id": tmid}))
        if not changes:
            conn.commit()
            out = dict(row)
            out["square_team_member_ids"] = employee_links(
                conn, venue["id"]).get(employee_id, [])
            return out
        sets = ", ".join(f"{k} = ?" for k in changes)
        try:
            conn.execute(
                f"UPDATE employee SET {sets} WHERE id = ?",
                (*changes.values(), employee_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "an employee with that name already exists")
        audit(conn, venue["id"], admin["id"], "employee_updated", "employee",
              employee_id, json.dumps({"old": {k: row[k] for k in changes}, "new": changes}))
        conn.commit()
        out = dict(conn.execute(
            "SELECT id, display_name, pool_role, active, always_in_boh_pool,"
            " in_payroll"
            " FROM employee WHERE id = ?", (employee_id,)).fetchone())
        out["square_team_member_ids"] = employee_links(conn, venue["id"]).get(employee_id, [])
        return out

    # ---------- days ----------

    def square_payload(conn, row) -> dict | None:
        """Client-facing slice of the stored pull: values + issues, raw
        extracts trimmed out (they stay in the DB for reconciliation).
        Muted warning codes are filtered here — display-level only; the
        stored record keeps every issue, and blocking issues always show."""
        if row is None or row["square_json"] is None:
            return None
        sq = json.loads(row["square_json"])
        muted = set(settings_store.get_setting(conn, row["venue_id"], "muted_warnings"))
        issues = [i for i in sq["issues"]
                  if i["severity"] != "warning" or i["code"] not in muted]
        # Who declared the cash. Square's own labor dashboard leaves manager
        # declarations out of its declared-cash tile while still counting the
        # manager's hours, so the totals disagree and there is no way to see
        # why from the outside. This is the one slice of `raw` the day screen
        # gets: enough to name the declarers, not the whole extract.
        raw = sq.get("raw") or {}
        declarations = [
            {"name": sh["name"], "job_title": sh.get("job_title"),
             "role": sh.get("role"), "cents": sh["declared_cents"]}
            # POINTS_HOURS stores them as "shifts", POOL_HOURS as "timecards"
            for sh in (raw.get("shifts") or raw.get("timecards") or [])
            if sh.get("declared_cents")
        ]
        out = {
            "pulled_at": sq["pulled_at"],
            "values": sq["values"],
            "issues": issues,
            "muted_count": len(sq["issues"]) - len(issues),
            "blocked_fields": sorted(sync.blocked_fields(sq)),
            "cash_declarations": sorted(
                declarations, key=lambda d: (-d["cents"], d["name"])),
        }
        # Tavern Law: what the shifts were, and where the event money came
        # from. Money held out of every pool (beverage packages, room fees)
        # is always shown — dropping it silently hides it just as badly as
        # pooling it would.
        if raw.get("timecards") is not None:
            out["shifts"] = [
                {k: sh.get(k) for k in
                 ("name", "job_title", "role", "raw_hours", "tippable_hours",
                  "credited_hours", "missing_clockout", "invalid_interval")}
                for sh in raw["timecards"]
            ]
        if any(k in raw for k in ("event_food_lines", "event_other_lines",
                                  "event_deposits_attached")):
            out["event"] = {
                "food_lines": raw.get("event_food_lines") or [],
                "other_lines": raw.get("event_other_lines") or [],
                "other_cents": raw.get("event_other_cents") or 0,
                "ticket_tips_cents": raw.get("event_ticket_tips_cents") or 0,
                "deposits": raw.get("event_deposits_attached") or [],
            }
        return out

    def acked_flags(row) -> list[str]:
        """Flags a manager has looked at and accepted for this day.

        A flag asks for a decision, it does not mean the day is wrong — the
        no-host event re-split is the routine case. Once reviewed, the day
        should stop nagging on the period screen. Names are stored, not a
        boolean, so a flag that appears LATER still raises the mark.
        """
        try:
            return list(json.loads(row["acked_flags_json"] or "[]"))
        except (KeyError, IndexError, TypeError, ValueError):
            return []

    def day_payload(conn, venue, d: date) -> dict:
        row = day_row(conn, venue["id"], d)
        emps = employees_map(conn, venue["id"])
        if row is None:
            return {
                "date": d.isoformat(), "status": "not_started",
                "inputs": dict(EMPTY_INPUTS_BY_MODEL[venue["tip_model"]]),
                "computed": compute_or_422(conn, venue, EMPTY_INPUTS_BY_MODEL[venue["tip_model"]], emps),
                "acked_flags": [], "snapshots": [], "square": None,
            }
        inputs = json.loads(row["inputs_json"])
        if row["status"] == "finalized":
            computed = snapshot_outputs(conn, row["id"])
        else:
            computed = compute_or_422(conn, venue, inputs, emps)
        snaps = conn.execute(
            "SELECT version, computed_at, engine_version FROM day_snapshot"
            " WHERE day_id = ? ORDER BY version",
            (row["id"],),
        ).fetchall()
        return {
            "date": d.isoformat(), "status": row["status"], "inputs": inputs,
            "computed": computed,
            "finalized_at": row["finalized_at"],
            "acked_flags": acked_flags(row),
            "snapshots": [dict(s) for s in snaps],
            "square": square_payload(conn, row),
        }

    @app.get("/api/days/{date_str}")
    def get_day(date_str: str, user: User, conn: DB, venue: Venue):
        return day_payload(conn, venue, parse_date(date_str))

    @app.post("/api/days/{date_str}/ack-flags")
    def ack_day_flags(date_str: str, body: dict, user: User, conn: DB,
                      venue: Venue):
        """Mark this day's flags as reviewed (or clear the acknowledgement).

        Deliberately records the flag NAMES rather than a "reviewed" bit: if
        the day changes and raises something new, that new flag is unreviewed
        and the period screen marks the day again.
        """
        d = parse_date(date_str)
        row = day_row(conn, venue["id"], d)
        if row is None:
            raise HTTPException(404, "no such day")
        flags = sorted({str(f) for f in (body.get("flags") or [])})
        conn.execute("UPDATE day SET acked_flags_json = ? WHERE id = ?",
                     (json.dumps(flags), row["id"]))
        audit(conn, venue["id"], user["id"], "day.ack_flags", "day", row["id"],
              json.dumps({"date": d.isoformat(), "flags": flags}))
        conn.commit()
        return day_payload(conn, venue, d)

    @app.put("/api/days/{date_str}")
    def put_day(date_str: str, body: dict, user: User, conn: DB, venue: Venue):
        d = parse_date(date_str)
        row = day_row(conn, venue["id"], d)
        if row is not None and row["status"] == "finalized":
            raise HTTPException(409, "day is finalized — an admin must reopen it first")
        model_cls = {"PERCENT_TIPOUT": LFDayInputsBody,
                     "POINTS_HOURS": PoqDayInputsBody}.get(
                         venue["tip_model"], DayInputsBody)
        try:
            parsed = model_cls(**body)
        except Exception as exc:
            raise HTTPException(422, f"invalid day inputs: {exc}")
        inputs = parsed.model_dump()
        # JSON object keys are strings; normalize all id-keyed maps for storage
        for key, value in list(inputs.items()):
            if isinstance(value, dict):
                inputs[key] = {str(k): v for k, v in value.items()}
        # Hours are credited in whole increments, rounded UP (owner 2026-07-29).
        # Enforced here so a hand-typed 0.78 is stored as 0.80 exactly like a
        # Square-pulled one — the pull rounds in clip_timecard, this covers
        # manual entry and overrides.
        if venue["tip_model"] == "POOL_HOURS":
            inc = settings_store.rounding_increment(
                settings_store.all_settings(conn, venue["id"]))
            inputs["foh_hours"] = {
                k: float(round_hours_up(Decimal(str(v)), inc))
                for k, v in inputs["foh_hours"].items()
            }
        emps = employees_map(conn, venue["id"])
        computed = compute_or_422(conn, venue, inputs, emps)  # validate before saving
        # override audit: log any Square-pulled field the manager changed away
        # from (or back to) the pulled value
        sq = square_payload(conn, row)
        if sq:
            old_inputs = json.loads(row["inputs_json"])
            for field in sync.SQUARE_FIELDS_BY_MODEL[venue["tip_model"]]:
                if field in sync.DERIVED_FIELDS:
                    continue  # machine-derived, never a manager decision
                if field not in sq["values"] or inputs.get(field) == old_inputs.get(field):
                    continue
                if inputs.get(field) != sq["values"][field]:
                    audit(conn, venue["id"], user["id"], "field_overridden", "day",
                          d.isoformat(), json.dumps({
                              "field": field, "square": sq["values"][field],
                              "old": old_inputs.get(field), "new": inputs.get(field)}))
                elif old_inputs.get(field) != sq["values"][field]:
                    audit(conn, venue["id"], user["id"], "override_reverted", "day",
                          d.isoformat(), json.dumps({"field": field}))
        now = utcnow()
        if row is None:
            conn.execute(
                "INSERT INTO day (venue_id, date, status, inputs_json, created_at,"
                " updated_at, updated_by) VALUES (?, ?, 'draft', ?, ?, ?, ?)",
                (venue["id"], d.isoformat(), json.dumps(inputs), now, now, user["id"]),
            )
        else:
            conn.execute(
                "UPDATE day SET inputs_json = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (json.dumps(inputs), now, user["id"], row["id"]),
            )
        audit(conn, venue["id"], user["id"], "day_inputs_saved", "day", d.isoformat())
        conn.commit()
        return day_payload(conn, venue, d)

    @app.get("/api/days/{date_str}/event-deposits")
    def list_event_deposits(date_str: str, user: User, conn: DB, venue: Venue):
        """Event deposits this venue has rung, and which day each is attached
        to.

        A deposit is the party's gratuity and is rung days or weeks before the
        night itself — 8/15 for the 8/22 event — so it can never be found by
        pulling the event's own day. Only some carry a note naming the date,
        so nothing here is parsed or guessed: the manager picks. `suggested`
        merely floats a deposit whose note mentions this day's date to the top
        of the list.
        """
        if venue["tip_model"] != "POOL_HOURS":
            raise HTTPException(422, "event deposits are a Tavern Law concept")
        d = parse_date(date_str)
        settings = settings_store.all_settings(conn, venue["id"])
        look = int(settings["tl_deposit_lookback_days"])
        ledger = sync.deposit_ledger(conn, venue["id"], d - timedelta(days=look))
        # "8/22" and "08/22" as a party would write it on the ticket note
        marks = (f"{d.month}/{d.day}", f"{d.month:02d}/{d.day:02d}")
        for dep in ledger:
            note = (dep.get("note") or "")
            dep["suggested"] = (dep["attached_to"] is None
                                and any(m in note for m in marks))
        return {"date": d.isoformat(), "lookback_days": look,
                "deposits": ledger}

    @app.put("/api/days/{date_str}/event-deposits")
    def set_event_deposits(date_str: str, body: EventDepositsBody, user: User,
                           conn: DB, venue: Venue):
        """Attach (or detach) event deposits, and re-derive the day's event
        tips from them.

        Event tips = whatever rode on the event's own tickets + every attached
        deposit. Recomputed here rather than typed, so the number can always
        be traced back to the lines that made it. A deposit already attached
        to another day is refused: that money would otherwise be paid twice.
        """
        if venue["tip_model"] != "POOL_HOURS":
            raise HTTPException(422, "event deposits are a Tavern Law concept")
        d = parse_date(date_str)
        row = day_row(conn, venue["id"], d)
        if row is None:
            raise HTTPException(422, "pull or enter this day first")
        if row["status"] == "finalized":
            raise HTTPException(409, "day is finalized — an admin must reopen it first")
        if row["square_json"] is None:
            raise HTTPException(422, "this day has no Square pull to attach to")

        settings = settings_store.all_settings(conn, venue["id"])
        look = int(settings["tl_deposit_lookback_days"])
        ledger = {dep["deposit_id"]: dep for dep in
                  sync.deposit_ledger(conn, venue["id"], d - timedelta(days=look))}
        wanted = list(dict.fromkeys(body.deposit_ids))
        for did in wanted:
            dep = ledger.get(did)
            if dep is None:
                raise HTTPException(422, f"no such event deposit: {did}")
            if dep["attached_to"] not in (None, d.isoformat()):
                raise HTTPException(
                    409, f"that deposit is already attached to {dep['attached_to']}")

        sq = json.loads(row["square_json"])
        attached = [ledger[did] for did in wanted]
        total = (sq.get("raw", {}).get("event_ticket_tips_cents") or 0) \
            + sum(dep["gross_cents"] for dep in attached)
        inputs = json.loads(row["inputs_json"])
        before = inputs.get("event_tips_cents", 0)
        inputs["event_deposit_ids"] = wanted
        inputs["event_tips_cents"] = total
        # keep the pull's own view in step, so the day reads "from Square"
        # rather than showing this as a manager override of itself
        sq["values"]["event_tips_cents"] = total
        sq.setdefault("raw", {})["event_deposits_attached"] = attached

        emps = employees_map(conn, venue["id"])
        computed = compute_or_422(conn, venue, inputs, emps)  # validate first
        now = utcnow()
        conn.execute(
            "UPDATE day SET inputs_json = ?, square_json = ?, updated_at = ?,"
            " updated_by = ? WHERE id = ?",
            (json.dumps(inputs), json.dumps(sq), now, user["id"], row["id"]),
        )
        audit(conn, venue["id"], user["id"], "event_deposits_set", "day",
              d.isoformat(), json.dumps({
                  "deposit_ids": wanted, "event_tips_cents": total,
                  "was": before}))
        conn.commit()
        return day_payload(conn, venue, d)

    @app.post("/api/days/{date_str}/finalize")
    def finalize_day(date_str: str, user: User, conn: DB, venue: Venue):
        d = parse_date(date_str)
        row = day_row(conn, venue["id"], d)
        if row is None:
            raise HTTPException(422, "nothing entered for this day yet")
        if row["status"] == "finalized":
            raise HTTPException(409, "day is already finalized")
        sq = square_payload(conn, row)
        if sq and sq["blocked_fields"]:
            raise HTTPException(
                422,
                "day has unresolved Square mapping issues "
                f"({', '.join(sq['blocked_fields'])}) — fix the mappings in "
                "Settings and pull again before finalizing",
            )
        inputs = json.loads(row["inputs_json"])
        outputs = compute_or_422(conn, venue, inputs, employees_map(conn, venue["id"]))
        if outputs["flags"].get("unattributed_tips_unresolved"):
            raise HTTPException(
                422, "unattributed tips remain — assign them to a server or mark"
                     " them house on the Daily screen before finalizing")
        if outputs["flags"].get("unattributed_tips_overresolved"):
            raise HTTPException(
                422, "unattributed-tip assignments exceed the pulled bucket —"
                     " reduce the assignments before finalizing")
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM day_snapshot WHERE day_id = ?",
            (row["id"],),
        ).fetchone()[0]
        now = utcnow()
        conn.execute(
            "INSERT INTO day_snapshot (day_id, version, inputs_json, outputs_json,"
            " engine_version, computed_at, computed_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["id"], version, json.dumps(inputs), json.dumps(outputs),
             outputs["engine_version"], now, user["id"]),
        )
        conn.execute(
            "UPDATE day SET status = 'finalized', finalized_at = ?, finalized_by = ?,"
            " updated_at = ? WHERE id = ?",
            (now, user["id"], now, row["id"]),
        )
        audit(conn, venue["id"], user["id"], "day_finalized", "day", d.isoformat(),
              json.dumps({"snapshot_version": version}))
        conn.commit()
        return day_payload(conn, venue, d)

    @app.post("/api/days/{date_str}/reopen")
    def reopen_day(date_str: str, admin: Admin, conn: DB, venue: Venue):
        d = parse_date(date_str)
        row = day_row(conn, venue["id"], d)
        if row is None or row["status"] != "finalized":
            raise HTTPException(409, "day is not finalized")
        conn.execute(
            "UPDATE day SET status = 'draft', updated_at = ?, updated_by = ? WHERE id = ?",
            (utcnow(), admin["id"], row["id"]),
        )
        audit(conn, venue["id"], admin["id"], "day_reopened", "day", d.isoformat())
        conn.commit()
        return day_payload(conn, venue, d)

    class BohRosterBody(BaseModel):
        employee_ids: list[int] = []

    @app.put("/api/periods/{anchor}/boh-roster")
    def put_boh_roster(anchor: str, body: BohRosterBody, user: User, conn: DB,
                       venue: Venue):
        """LF monthly kitchen roster: who shares the month's BOH pool."""
        if venue["tip_model"] == "POOL_HOURS":
            raise HTTPException(422, "monthly kitchen roster only applies to"
                                     " PERCENT_TIPOUT venues")
        start, _ = period_for_scheme(parse_date(anchor), "monthly")
        emps = employees_map(conn, venue["id"])
        bad = [i for i in body.employee_ids
               if emps.get(i, {}).get("pool_role") != "BOH"]
        if bad:
            raise HTTPException(422, f"not BOH employees of this venue: {bad}")
        if len(set(body.employee_ids)) != len(body.employee_ids):
            raise HTTPException(422, "duplicate employee in roster")
        settings_store.put_raw(
            conn, venue["id"], f"lf_boh_roster:{start.isoformat()}",
            {"employee_ids": sorted(set(body.employee_ids))}, user["id"])
        audit(conn, venue["id"], user["id"], "boh_roster_saved", "period",
              start.isoformat(), json.dumps({"employee_ids": body.employee_ids}))
        conn.commit()
        return period_summary(conn, venue, start, finalized_only=True,
                              scheme="monthly")

    class CashPayoutsBody(BaseModel):
        payouts: dict[int, int] = {}

        @field_validator("payouts")
        @classmethod
        def _non_negative(cls, v):
            for eid, cents in v.items():
                if cents < 0:
                    raise ValueError(f"negative payout for employee {eid}")
            return v

    @app.put("/api/periods/{anchor}/cash-payouts")
    def put_cash_payouts(anchor: str, body: CashPayoutsBody, user: User,
                         conn: DB, venue: Venue, scheme: str | None = None):
        """LF per-period cash payout overrides (weekly FOH / monthly kitchen).
        Values replace the ceil-to-$10 suggestion for the listed employees."""
        if venue["tip_model"] == "POOL_HOURS":
            raise HTTPException(422, "cash payouts only apply to PERCENT_TIPOUT venues")
        sch = resolve_scheme(venue, scheme)
        start, _ = period_for_scheme(parse_date(anchor), sch)
        emps = employees_map(conn, venue["id"])
        bad = [i for i in body.payouts if i not in emps]
        if bad:
            raise HTTPException(422, f"unknown employees: {bad}")
        key = f"lf_cash_payouts:{sch}:{start.isoformat()}"
        current = settings_store.get_raw(conn, venue["id"], key, {}) or {}
        current.update({str(k): v for k, v in body.payouts.items()})
        settings_store.put_raw(conn, venue["id"], key, current, user["id"])
        audit(conn, venue["id"], user["id"], "cash_payouts_saved", "period",
              start.isoformat(),
              json.dumps({"scheme": sch, "payouts": body.payouts}))
        conn.commit()
        return period_summary(conn, venue, start, finalized_only=True, scheme=sch)

    # ---------- Square sync (M3) ----------

    def get_square_client(venue: sqlite3.Row) -> SquareClient:
        if not settings.square_for(venue["slug"])["configured"]:
            sfx = "__" + venue["slug"].upper().replace("-", "_")
            hint = ("SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID"
                    if venue["slug"] == "tavern-law"
                    else f"SQUARE_ACCESS_TOKEN{sfx} and SQUARE_LOCATION_ID{sfx}")
            raise HTTPException(
                422, f"Square is not configured for {venue['name']} — set {hint} in .env")
        return app.state.square_client_factory(venue["slug"])

    def apply_pull(conn, venue, d: date, record: dict, user_id: int | None):
        """Merge a pull record into the day row (creating it if needed),
        preserving manager overrides. Idempotent."""
        row = day_row(conn, venue["id"], d)
        old_inputs = (json.loads(row["inputs_json"]) if row
                      else dict(EMPTY_INPUTS_BY_MODEL[venue["tip_model"]]))
        old_square = json.loads(row["square_json"]) if row and row["square_json"] else None
        new_inputs = sync.merge_pull_into_inputs(
            old_inputs, old_square, record,
            fields=sync.SQUARE_FIELDS_BY_MODEL[venue["tip_model"]])
        now = utcnow()
        if row is None:
            conn.execute(
                "INSERT INTO day (venue_id, date, status, inputs_json, square_json,"
                " created_at, updated_at, updated_by)"
                " VALUES (?, ?, 'draft', ?, ?, ?, ?, ?)",
                (venue["id"], d.isoformat(), json.dumps(new_inputs),
                 json.dumps(record), now, now, user_id),
            )
        else:
            conn.execute(
                "UPDATE day SET inputs_json = ?, square_json = ?, updated_at = ?,"
                " updated_by = ? WHERE id = ?",
                (json.dumps(new_inputs), json.dumps(record), now, user_id, row["id"]),
            )
        audit(conn, venue["id"], user_id, "day_pulled", "day", d.isoformat(),
              json.dumps({"issues": [i["code"] for i in record["issues"]]}))

    @app.post("/api/periods/{anchor}/refresh-labor")
    def refresh_period_labor(anchor: str, admin: Admin, conn: DB, venue: Venue,
                             scheme: str | None = None):
        """Backfill clock times and hourly rates onto days already pulled.

        Days finalized before those were stored carry no timecard detail, so
        the payroll sheet cannot report their hours. Re-pulling normally would
        mean reopening each day and recomputing its payouts from whatever
        Square says today — which can move a locked figure if a timecard was
        edited since. This writes ONLY the extracted shifts back onto the
        stored pull: inputs are untouched, snapshots are untouched, so no
        finalized payout can change. Safe on finalized days by construction.
        """
        if venue["tip_model"] != "POINTS_HOURS":
            raise HTTPException(
                422, "labor refresh is only available for points-and-hours venues")
        start, end = period_for_scheme(parse_date(anchor),
                                       resolve_scheme(venue, scheme))
        client = get_square_client(venue)
        updated, skipped, failed = [], [], []
        for d in period_days(start, end):
            row = day_row(conn, venue["id"], d)
            if row is None or row["square_json"] is None:
                skipped.append(d.isoformat())      # never pulled; nothing to enrich
                continue
            try:
                shifts = sync.refresh_labor_shifts(conn, client, venue, d)
            except SquareError as exc:
                raise HTTPException(502, f"{d}: {exc}")
            except Exception as exc:
                log.exception("labor refresh failed for %s on %s", venue["slug"], d)
                failed.append({"date": d.isoformat(),
                               "error": f"{type(exc).__name__}: {exc}"})
                continue
            record = json.loads(row["square_json"])
            raw = record.setdefault("raw", {})
            before = raw.get("shifts") or []
            raw["shifts"] = shifts
            record["labor_refreshed_at"] = utcnow()
            conn.execute(
                "UPDATE day SET square_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(record), utcnow(), row["id"]),
            )
            audit(conn, venue["id"], admin["id"], "labor_refreshed", "day",
                  d.isoformat(),
                  json.dumps({"shifts_before": len(before),
                              "shifts_after": len(shifts),
                              "status": row["status"]}))
            updated.append(d.isoformat())
        conn.commit()
        return {"updated": updated, "skipped": skipped, "failed": failed,
                "start": start.isoformat(), "end": end.isoformat()}

    @app.post("/api/days/{date_str}/pull")
    def pull_day_from_square(date_str: str, user: User, conn: DB, venue: Venue):
        d = parse_date(date_str)
        row = day_row(conn, venue["id"], d)
        if row is not None and row["status"] == "finalized":
            raise HTTPException(409, "day is finalized — reopen before re-pulling")
        client = get_square_client(venue)
        try:
            record = sync.pull_day(conn, client, venue, d, user["id"])
        except SquareError as exc:
            raise HTTPException(502, str(exc))
        except Exception as exc:
            # A pull touches whatever Square happens to return that day, so an
            # unexpected shape must not surface as a bare "Internal Server
            # Error" the manager can't act on. Log the traceback for us and
            # hand back the actual reason.
            log.exception("pull failed for %s on %s", venue["slug"], d)
            raise HTTPException(
                500,
                f"Square pull failed for {d}: {type(exc).__name__}: {exc}. "
                "The day was not changed — check the timecards for that date "
                "in Square, or send this message along for support.",
            )
        apply_pull(conn, venue, d, record, user["id"])
        conn.commit()
        return day_payload(conn, venue, d)

    def _payout_by_person(outputs: dict | None) -> dict[int, int]:
        """What each person was owed, whatever the venue's model calls it.

        Sums every `*_cents` field on a payout row rather than naming them, so
        this keeps working as models gain pools (the event pool did exactly
        that) instead of quietly comparing a subset.
        """
        out: dict[int, int] = {}
        for row in (outputs or {}).get("people", []) or ():
            eid = row.get("employee_id")
            if eid is None:
                continue
            out[eid] = sum(v for k, v in row.items()
                           if k.endswith("_cents") and isinstance(v, int))
        return out

    def _refresh_one_day(conn, venue, admin, client, d: date) -> dict:
        """Reopen, re-pull and re-finalize one finalized day.

        Shared by the single-day and whole-period actions so the two can never
        drift apart. Never raises: every outcome comes back as a status, which
        is what lets a period run report a bad day instead of aborting on it.

          refreshed  — relocked; `moved` says whose payout changed
          skipped    — not finalized, or never pulled; untouched
          failed     — the Square fetch failed BEFORE anything was written,
                       so the day is still finalized and unchanged
          left_open  — re-pulled, but something blocks finalizing. The day is
                       now a DRAFT holding the new data. This is the one
                       outcome that leaves state changed behind it, so it is
                       reported loudly rather than counted as a success.
        """
        iso = d.isoformat()
        row = day_row(conn, venue["id"], d)
        if row is None or row["status"] != "finalized":
            return {"date": iso, "status": "skipped", "reason": "not finalized"}
        if row["square_json"] is None:
            # never pulled: there is no Square baseline to refresh against, and
            # pulling now would recompute a hand-entered day from scratch
            return {"date": iso, "status": "skipped", "reason": "never pulled"}

        before = _payout_by_person(snapshot_outputs(conn, row["id"]))
        try:
            record = sync.pull_day(conn, client, venue, d, admin["id"])
        except SquareError as exc:
            # upstream said no
            return {"date": iso, "status": "failed", "error": str(exc), "code": 502}
        except Exception as exc:
            # our own extraction fell over on a payload shape — that is a bug
            # here, not at Square, and the status code should say so
            log.exception("refresh pull failed for %s on %s", venue["slug"], d)
            return {"date": iso, "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}", "code": 500}

        conn.execute(
            "UPDATE day SET status = 'draft', updated_at = ?, updated_by = ?"
            " WHERE id = ?", (utcnow(), admin["id"], row["id"]))
        audit(conn, venue["id"], admin["id"], "day_reopened", "day", iso,
              json.dumps({"reason": "refresh"}))
        apply_pull(conn, venue, d, record, admin["id"])
        conn.commit()

        try:
            payload = finalize_day(iso, admin, conn, venue)
        except HTTPException as exc:
            return {"date": iso, "status": "left_open", "error": exc.detail}

        after = _payout_by_person(payload.get("computed"))
        names = {eid: e["display_name"]
                 for eid, e in employees_map(conn, venue["id"]).items()}
        moved = []
        for eid in sorted(before.keys() | after.keys()):
            delta = after.get(eid, 0) - before.get(eid, 0)
            if delta:
                moved.append({"employee_id": eid,
                              "name": names.get(eid, f"#{eid}"),
                              "before_cents": before.get(eid, 0),
                              "after_cents": after.get(eid, 0),
                              "delta_cents": delta})
        audit(conn, venue["id"], admin["id"], "day_refreshed", "day", iso,
              json.dumps({"moved": moved}))
        conn.commit()
        return {"date": iso, "status": "refreshed", "moved": moved}

    @app.post("/api/days/{date_str}/refresh")
    def refresh_finalized_day(date_str: str, admin: Admin, conn: DB, venue: Venue):
        """Reopen, re-pull from Square and re-finalize, in one action.

        The manual path is reopen -> pull -> finalize, two confirm prompts
        deep, and it is what you do after an engine change lands. Collapsing
        it is not just fewer clicks: the manual path shows you NOTHING about
        whether a locked payout moved, while this reports the difference per
        person. Re-pulling a finalized day genuinely can move money — a
        timecard edited in Square since, a rate change — which is why this
        stays admin-only and says what happened.
        """
        d = parse_date(date_str)
        result = _refresh_one_day(conn, venue, admin, get_square_client(venue), d)
        if result["status"] == "skipped":
            raise HTTPException(409, f"day is {result['reason']} — pull it directly")
        if result["status"] == "failed":
            raise HTTPException(
                result.get("code", 502),
                f"Square pull failed for {d}: {result['error']}. "
                "The day is untouched and still finalized.")
        if result["status"] == "left_open":
            raise HTTPException(
                422, f"{result['error']} — the day has been re-pulled and left "
                     "OPEN; fix the above and finalize it.")
        payload = day_payload(conn, venue, d)
        payload["refresh"] = {
            "moved": result["moved"],
            "moved_total_cents": sum(m["delta_cents"] for m in result["moved"]),
        }
        return payload

    @app.post("/api/periods/{anchor}/refresh")
    def refresh_period(anchor: str, admin: Admin, conn: DB, venue: Venue,
                       scheme: str | None = None):
        """Re-pull and re-finalize every finalized day in a period.

        The same action as the single-day refresh, run across the period, for
        when an engine change lands and a whole stretch needs re-running. One
        bad day does NOT abort the rest — that would leave the run half done
        with no account of where it stopped — so every day comes back with its
        own outcome and the caller is told plainly which days moved money and
        which were left open.
        """
        start, end = period_for_scheme(parse_date(anchor),
                                       resolve_scheme(venue, scheme))
        client = get_square_client(venue)
        results = [_refresh_one_day(conn, venue, admin, client, d)
                   for d in period_days(start, end)]
        by = {k: [r for r in results if r["status"] == k]
              for k in ("refreshed", "skipped", "failed", "left_open")}
        moved_days = [r for r in by["refreshed"] if r["moved"]]
        audit(conn, venue["id"], admin["id"], "period_refreshed", "period",
              start.isoformat(),
              json.dumps({"refreshed": len(by["refreshed"]),
                          "moved": len(moved_days),
                          "failed": [r["date"] for r in by["failed"]],
                          "left_open": [r["date"] for r in by["left_open"]]}))
        conn.commit()
        return {
            "start": start.isoformat(), "end": end.isoformat(),
            "refreshed": len(by["refreshed"]),
            "skipped": len(by["skipped"]),
            "failed": by["failed"],
            "left_open": by["left_open"],
            "moved_days": [{"date": r["date"], "moved": r["moved"]}
                           for r in moved_days],
            "moved_total_cents": sum(m["delta_cents"] for r in moved_days
                                     for m in r["moved"]),
        }

    @app.get("/api/settings")
    def get_settings(user: User, conn: DB, venue: Venue):
        out = settings_store.all_settings(conn, venue["id"])
        square = settings.square_for(venue["slug"])
        out["square"] = {
            "configured": square["configured"],
            "env": square["env"],
            "location_ids": square["location_ids"],
            "nightly_sync": settings.nightly_sync,
            "nightly_sync_hour": settings.nightly_sync_hour,
        }
        out["category_groups"] = list(settings_store.CATEGORY_GROUPS)
        linked = conn.execute(
            "SELECT e.id, e.display_name, l.team_member_id AS square_team_member_id"
            " FROM employee e LEFT JOIN square_link l ON l.employee_id = e.id"
            " AND l.venue_id = e.venue_id"
            " WHERE e.venue_id = ?"
            " ORDER BY e.display_name, l.team_member_id",
            (venue["id"],),
        ).fetchall()
        out["employee_links"] = [dict(r) for r in linked]
        return out

    @app.put("/api/settings")
    def put_settings(body: SettingsPatch, admin: Admin, conn: DB, venue: Venue):
        for key, value in body.model_dump(exclude_none=True).items():
            if key == "tippable_windows":
                # partial update: merge into existing weekday map
                current = settings_store.get_setting(conn, venue["id"], key)
                current.update(value)
                value = current
            settings_store.put_setting(conn, venue["id"], key, value, admin["id"])
        conn.commit()
        return get_settings(admin, conn, venue)

    @app.post("/api/square/sync-catalog")
    def sync_catalog(admin: Admin, conn: DB, venue: Venue):
        client = get_square_client(venue)
        try:
            categories = client.list_categories()
        except SquareError as exc:
            raise HTTPException(502, str(exc))
        cmap = settings_store.get_setting(conn, venue["id"], "category_map")
        added = 0
        for cat in categories:
            cid = cat["id"]
            name = cat.get("category_data", {}).get("name", cid)
            if cid in cmap:
                cmap[cid]["name"] = name  # refresh display name, keep group
            else:
                cmap[cid] = {"name": name, "group": None}
                added += 1
        settings_store.put_setting(conn, venue["id"], "category_map", cmap, admin["id"])
        conn.commit()
        unmapped = sum(1 for e in cmap.values() if e["group"] is None)
        return {"total": len(cmap), "added": added, "unmapped": unmapped}

    @app.post("/api/square/sync-team")
    def sync_team(admin: Admin, conn: DB, venue: Venue):
        client = get_square_client(venue)
        try:
            members = client.search_team_members()
            inactive = client.search_team_members(status="INACTIVE")
        except SquareError as exc:
            raise HTTPException(502, str(exc))
        inactive_ids = {m["id"] for m in inactive}
        cache = [
            {"id": m["id"],
             "name": " ".join(filter(None, [m.get("given_name"), m.get("family_name")]))
                     or m["id"],
             "status": m.get("status", "ACTIVE")}
            for m in members
        ]
        settings_store.put_setting(conn, venue["id"], "square_team_cache", cache, admin["id"])
        # Pay type per member. Salaried staff never clock in, so without this
        # they are invisible to every timecard-driven report. One call each —
        # Square has no bulk wage-setting endpoint — and a failure on one
        # member must not lose the whole sync.
        wages = {}
        for m in cache:
            try:
                ws = client.retrieve_wage_setting(m["id"])
            except SquareError:
                continue
            job = next((j for j in (ws.get("job_assignments") or [])
                        if j.get("pay_type") == "SALARY"), None)
            if job is None:
                continue
            wages[m["id"]] = {
                "pay_type": "SALARY",
                "job_title": job.get("job_title"),
                "hourly_rate_cents": (job.get("hourly_rate") or {}).get("amount"),
                "annual_rate_cents": (job.get("annual_rate") or {}).get("amount"),
                "weekly_hours": job.get("weekly_hours"),
                "overtime_exempt": bool(ws.get("is_overtime_exempt")),
            }
        settings_store.put_setting(conn, venue["id"], "square_wage_settings",
                                   wages, admin["id"])
        conn.commit()
        links = conn.execute(
            "SELECT l.team_member_id AS tmid, e.id, e.display_name, e.active"
            " FROM square_link l JOIN employee e ON e.id = l.employee_id"
            " WHERE l.venue_id = ?", (venue["id"],)).fetchall()
        linked_ids = {r["tmid"] for r in links}

        # Square is the record of who still works here. Someone whose every
        # linked account has been deactivated there has left, so deactivate
        # them here too — otherwise they linger on the payroll sheet forever.
        # The reverse is NOT applied: an employee deactivated here while still
        # active in Square may have been switched off deliberately, and
        # silently undoing that would fight the manager.
        by_emp: dict[int, dict] = {}
        for r in links:
            e = by_emp.setdefault(r["id"], {"name": r["display_name"],
                                            "active": bool(r["active"]),
                                            "tmids": []})
            e["tmids"].append(r["tmid"])
        gone, reactivatable = [], []
        for eid, e in by_emp.items():
            all_inactive = all(t in inactive_ids for t in e["tmids"])
            if e["active"] and all_inactive:
                conn.execute("UPDATE employee SET active = 0 WHERE id = ?", (eid,))
                audit(conn, venue["id"], admin["id"], "employee_deactivated",
                      "employee", eid,
                      json.dumps({"reason": "inactive in Square"}))
                gone.append(e["name"])
            elif not e["active"] and not all_inactive:
                reactivatable.append(e["name"])
        conn.commit()

        return {"team": cache, "salaried": len(wages),
                "deactivated": sorted(gone),
                "active_in_square": sorted(reactivatable),
                "unlinked": [m for m in cache if m["id"] not in linked_ids]}

    # ---------- nightly sync ----------

    def run_nightly_sync():
        """Pull the prior day for every venue whose Square credentials are
        configured. Venues fail independently; failures land in that venue's
        audit trail."""
        conn = connect(settings.db_path)
        try:
            venues = conn.execute("SELECT * FROM venue ORDER BY id").fetchall()
            for venue in venues:
                if not settings.square_for(venue["slug"])["configured"]:
                    continue
                try:
                    now = datetime.now(ZoneInfo(venue["timezone"]))
                    target = sync.nightly_target_day(now)
                    row = day_row(conn, venue["id"], target)
                    if not sync.should_auto_sync(row):
                        continue
                    record = sync.pull_day(
                        conn, app.state.square_client_factory(venue["slug"]),
                        venue, target, None)
                    apply_pull(conn, venue, target, record, None)
                    conn.commit()
                except Exception as exc:  # never kill the loop; leave a trace
                    try:
                        audit(conn, venue["id"], None, "nightly_sync_failed", "day",
                              "", json.dumps({"error": str(exc)[:500]}))
                        conn.commit()
                    except Exception:
                        pass
        finally:
            conn.close()

    async def nightly_sync_loop():
        while True:
            now = datetime.now(ZoneInfo(settings.timezone))
            await asyncio.sleep(sync.seconds_until_hour(now, settings.nightly_sync_hour))
            await asyncio.to_thread(run_nightly_sync)

    # ---------- periods & export ----------

    def resolve_scheme(venue, scheme: str | None) -> str:
        allowed = VENUE_SCHEMES[venue["tip_model"]]
        if scheme is None:
            return allowed[0]
        if scheme not in allowed:
            raise HTTPException(
                422, f"scheme {scheme!r} is not valid for {venue['name']};"
                     f" use one of {list(allowed)}")
        return scheme

    def ceil_to_ten_dollars(cents: int) -> int:
        # "nearest round number (ending in zero)": 507.39 -> 510, 500 -> 500
        return -(-cents // 1000) * 1000

    def labor_hours_for(conn, venue, start: date, end: date):
        """Worked and overtime hours for a period, for RECONCILIATION ONLY.

        Reads the clock times off each day's stored Square pull rather than
        the tip-pool inputs, because the two attribute a shift differently:
        the pool credits a shift that ends at 00:06 to the night it belongs
        to, while a labor report puts those six minutes on the next calendar
        day. Only the latter reconciles against the point-of-sale.

        Overtime is weekly, so the week straddling `start` has to be loaded
        whole — hours worked before the period still count toward the
        threshold. Days with no stored pull are named rather than treated as
        zero: a short week would under-report overtime, and a plausible wrong
        number is worse than none.
        """
        settings = settings_store.all_settings(conn, venue["id"])
        tz = ZoneInfo(venue["timezone"])
        week_start = settings_store.poq_workweek_start(settings)
        threshold = float(settings.get("poq_overtime_after") or 40)

        # back to the first day of the week containing `start`
        lookback = start - timedelta(days=(start.weekday() - week_start) % 7)
        rows = conn.execute(
            "SELECT date, square_json FROM day"
            " WHERE venue_id = ? AND date BETWEEN ? AND ? ORDER BY date",
            (venue["id"], lookback.isoformat(), end.isoformat()),
        ).fetchall()

        day_hours, unknown = [], []
        for r in rows:
            shifts = ((json.loads(r["square_json"]).get("raw") or {}).get("shifts", [])
                      if r["square_json"] else [])
            timed = [sh for sh in shifts
                     if sh.get("start_at") and sh.get("end_at")]
            # A day that exists but carries no clock times cannot be reconciled:
            # either it was hand-entered, or it was pulled before clock times
            # were stored. A day with a pull and genuinely no timecards (venue
            # closed) is known to be zero, not unknown. A date with no row at
            # all never happened and is simply absent.
            if not timed and (r["square_json"] is None or shifts):
                if start.isoformat() <= r["date"] <= end.isoformat():
                    unknown.append(r["date"])
                continue
            for sh in timed:
                for d, h in engine.split_at_midnight(
                        datetime.fromisoformat(sh["start_at"]),
                        datetime.fromisoformat(sh["end_at"]), tz):
                    day_hours.append((sh["employee_id"], d, h,
                                      sh.get("rate_cents") or 0))
        worked = sum(h for _, d, h, _r in day_hours if start <= d <= end)
        overtime = engine.weekly_overtime(
            [(w, d, h) for w, d, h, _r in day_hours],
            start, end, week_start=week_start, threshold=threshold)
        per_employee = engine.period_labor(
            day_hours, start, end, week_start=week_start, threshold=threshold)
        return engine.LaborHours(worked, overtime, unknown), per_employee

    def period_summary(conn, venue, anchor: date, finalized_only: bool,
                       scheme: str) -> dict:
        start, end = period_for_scheme(anchor, scheme)
        emps = employees_map(conn, venue["id"])
        rows = conn.execute(
            "SELECT * FROM day WHERE venue_id = ? AND date BETWEEN ? AND ? ORDER BY date",
            (venue["id"], start.isoformat(), end.isoformat()),
        ).fetchall()
        by_date = {r["date"]: r for r in rows}

        is_lf = venue["tip_model"] == "PERCENT_TIPOUT"
        is_poq = venue["tip_model"] == "POINTS_HOURS"
        boh_monthly = None
        days_out = []
        if is_lf:
            totals = {"total_tips_cents": 0, "auto_gratuity_cents": 0,
                      "pool_busser_cents": 0, "pool_host_cents": 0,
                      "pool_boh_cents": 0}
        elif is_poq:
            totals = {"total_tips_cents": 0, "foh_pool_cents": 0,
                      "boh_pool_cents": 0, "auto_gratuity_cents": 0,
                      # so a period can be checked against Square's own
                      # card / cash / service-charge lines
                      "credit_tips_gross_cents": 0, "credit_tips_net_cents": 0,
                      "cash_tips_cents": 0, "card_fee_cents": 0,
                      "auto_gratuity_gross_cents": 0, "gratuity_fee_cents": 0,
                      "processing_fee_total_cents": 0, "net_sales_cents": 0,
                      "event_pool_cents": 0, "event_fee_cents": 0}
        else:
            totals = {"total_tips_cents": 0, "boh_allocation_cents": 0,
                      "foh_pool_cents": 0, "auto_gratuity_cents": 0}
        staff: dict[int, dict] = {}
        payroll: list[dict] = []
        draft_dates, flagged_dates = [], []
        days_missing_sales: list[str] = []
        # Hours on the clock, read off the day's stored shifts rather than the
        # payout rows: an EXCLUDED job earns nothing so it never reaches a
        # payout, but its hours are still hours and Square's labor dashboard
        # counts them. Reporting only — never part of the payout math.
        worked_hours = excluded_hours = 0.0
        poq_role_side = {}
        if is_poq:
            poq_role_side = {r: (cfg or {}).get("side") for r, cfg in
                             settings_store.get_setting(
                                 conn, venue["id"], "poq_roles").items()}

        for d in period_days(start, end):
            key = d.isoformat()
            row = by_date.get(key)
            if row is None:
                days_out.append({"date": key, "status": "not_started"})
                continue
            outputs = (
                snapshot_outputs(conn, row["id"])
                if row["status"] == "finalized"
                else compute_or_422(conn, venue, json.loads(row["inputs_json"]), emps)
            )
            acked = set(acked_flags(row))
            flags_on = [k for k, v in outputs["flags"].items()
                        if v and k not in INFO_FLAGS and k not in acked]
            if flags_on:
                flagged_dates.append(key)
            if row["status"] != "finalized":
                draft_dates.append(key)
            days_out.append({
                "date": key, "status": row["status"], "flags_on": flags_on,
                # whether a re-pull has anything to re-pull FROM: a day entered
                # by hand has no Square baseline, and refreshing it would mean
                # recomputing a manual day from scratch
                "pulled": row["square_json"] is not None,
                "total_tips_cents": outputs["totals"]["total_tips_cents"],
                "foh_pool_cents": outputs["totals"].get("foh_pool_cents"),
            })
            if finalized_only and row["status"] != "finalized":
                continue
            for k in totals:
                totals[k] += outputs["totals"].get(k, 0)
            if is_poq:
                # A day with tips but no sales figure (finalized before net
                # sales were captured) would silently shrink the tip-rate
                # denominator and overstate the rate. Track it so reports can
                # refuse to show a number rather than a wrong one.
                if (outputs["totals"].get("total_tips_cents")
                        and not outputs["totals"].get("net_sales_cents")):
                    days_missing_sales.append(key)
                for sh in json.loads(row["inputs_json"]).get("shifts", []):
                    h = float(sh.get("hours") or 0)
                    worked_hours += h
                    if poq_role_side.get(sh.get("role")) == "EXCLUDED":
                        excluded_hours += h
                for line in outputs["people"]:
                    s_ = staff.setdefault(line["employee_id"], {
                        "employee_id": line["employee_id"], "name": line["name"],
                        "tips_cents": 0, "gratuity_cents": 0, "event_cents": 0,
                        "days": 0, "hours": 0.0, "points": 0.0,
                    })
                    s_["tips_cents"] += line["tips_cents"]
                    s_["gratuity_cents"] += line["gratuity_cents"]
                    s_["event_cents"] += line.get("event_cents", 0)
                    s_["hours"] += line["hours"]
                    s_["points"] += line["points"]
                    if line["hours"] or line.get("event_cents"):
                        s_["days"] += 1
                continue
            if is_lf:
                for line in outputs["people"]:
                    s = staff.setdefault(line["employee_id"], {
                        "employee_id": line["employee_id"], "name": line["name"],
                        "role": line["role"], "keep_cents": 0, "returned_cents": 0,
                        "pool_share_cents": 0, "tips_cents": 0,
                        "gratuity_cents": 0, "days": 0, "hours": 0.0,
                    })
                    s["keep_cents"] += line["keep_cents"]
                    s["returned_cents"] += line["returned_cents"]
                    s["pool_share_cents"] += line["pool_share_cents"]
                    s["tips_cents"] += line["payout_cents"]
                    s["gratuity_cents"] += line["gratuity_cents"]
                    s["days"] += 1
                    s["hours"] += line["hours"]
                continue
            for line in outputs["foh"]:
                s = staff.setdefault(line["employee_id"], {
                    "employee_id": line["employee_id"], "name": line["name"],
                    "tips_cents": 0, "gratuity_cents": 0, "boh_cents": 0,
                    "days": 0, "hours": 0.0, "credited_hours": 0.0,
                })
                s["tips_cents"] += line["tips_cents"]
                s["gratuity_cents"] += line["gratuity_cents"]
                s["days"] += 1
                s["hours"] += line["hours"]
                # snapshots predating the 2026-07-29 door ruling have neither
                # key: credited hours then equal hours worked
                s["credited_hours"] += line.get("weighted_hours", line["hours"])
            for line in outputs["boh"]:
                s = staff.setdefault(line["employee_id"], {
                    "employee_id": line["employee_id"], "name": line["name"],
                    "tips_cents": 0, "gratuity_cents": 0, "boh_cents": 0,
                    "days": 0, "hours": 0.0, "credited_hours": 0.0,
                })
                s["boh_cents"] += line["share_cents"]
                s["days"] += 1

        # LF monthly payroll: the month's carried BOH pool is split evenly
        # among a kitchen roster decided on the export screen (pre-populated
        # from who worked during the month, persisted per month, editable).
        if is_lf and scheme == "monthly":
            boh_emps = {eid: e for eid, e in emps.items()
                        if e["pool_role"] == "BOH"}
            worked_days: dict[int, int] = {}
            for r in rows:
                for k, h in json.loads(r["inputs_json"]).get("hours", {}).items():
                    if int(k) in boh_emps and h and float(h) > 0:
                        worked_days[int(k)] = worked_days.get(int(k), 0) + 1
            stored = settings_store.get_raw(
                conn, venue["id"], f"lf_boh_roster:{start.isoformat()}")
            always = {eid for eid, e in boh_emps.items()
                      if e.get("always_in_boh_pool")}
            if stored is not None:
                selected = [i for i in stored.get("employee_ids", [])
                            if i in boh_emps]
            else:
                # who worked, plus salaried kitchen staff who never clock in
                selected = sorted(set(worked_days) | always)
            alloc = totals.get("pool_boh_cents", 0)
            shares = (distribute_cents(alloc, {str(i): 1 for i in selected})
                      if selected and alloc > 0 else {})
            # kitchen is paid in cash at payroll: per-person round-up decided
            # HERE, pre-filled to the next amount ending in zero
            stored_cash = settings_store.get_raw(
                conn, venue["id"],
                f"lf_cash_payouts:monthly:{start.isoformat()}", {}) or {}
            members = []
            k_roundup = 0
            k_cash = 0
            for eid, e in sorted(boh_emps.items(),
                                 key=lambda kv: kv[1]["display_name"]):
                m = {"employee_id": eid, "name": e["display_name"],
                     "selected": eid in selected,
                     "always": bool(e.get("always_in_boh_pool")),
                     "worked_days": worked_days.get(eid, 0)}
                share = shares.get(str(eid))
                if share is not None:
                    suggested = ceil_to_ten_dollars(share)
                    cash = stored_cash.get(str(eid), suggested)
                    m.update({"share_cents": share,
                              "suggested_cash_cents": suggested,
                              "cash_payout_cents": cash,
                              "roundup_cents": cash - share})
                    k_roundup += cash - share
                    k_cash += cash
                members.append(m)
            boh_monthly = {
                "allocation_cents": alloc,
                "stored": stored is not None,
                "members": members,
                "shares": shares,
                "unassigned": alloc > 0 and not selected,
                "total_cash_payout_cents": k_cash,
                "total_roundup_cents": k_roundup,
            }

        # LF weekly tip payout is paid in CASH: each employee's payout is
        # decided per period on the export screen, pre-filled to the next
        # amount ending in zero (507.39 -> 510). Monthly payroll stays exact.
        total_roundup = 0
        total_cash = 0
        if is_lf and scheme == "weekly":
            stored_cash = settings_store.get_raw(
                conn, venue["id"],
                f"lf_cash_payouts:weekly:{start.isoformat()}", {}) or {}
            for s in staff.values():
                tips = s["tips_cents"]
                suggested = ceil_to_ten_dollars(tips)
                cash = stored_cash.get(str(s["employee_id"]), suggested)
                s["suggested_cash_cents"] = suggested
                s["cash_payout_cents"] = cash
                s["roundup_cents"] = cash - tips
                total_roundup += cash - tips
                total_cash += cash
            totals["total_roundup_cents"] = total_roundup
            totals["total_cash_payout_cents"] = total_cash

        if is_poq:
            # from the tip-pool shifts: what earned a share, and what did not
            totals["excluded_hours"] = round(excluded_hours, 2)
            totals["credited_hours"] = round(
                sum(s_["hours"] for s_ in staff.values()), 2)
            # from the clock times: the point-of-sale's own labor figures
            labor, per_emp = labor_hours_for(conn, venue, start, end)
            totals.update(labor.as_dict())
            # per person, so the payroll form can be filled and cross-checked
            for s_ in staff.values():
                lab = per_emp.get(s_["employee_id"])
                s_.update(lab or {"paid_hours": None, "overtime_hours": None,
                                  "regular_hours": None, "wages_cents": None})
                s_["gross_pay_cents"] = (
                    None if lab is None else
                    lab["wages_cents"] + s_["tips_cents"]
                    + s_["event_cents"] + s_["gratuity_cents"])
            # Payroll covers everyone who worked, which is NOT everyone who
            # earned: a manager's shift takes no tip share but still draws
            # wages, and leaving them off the sheet would under-pay a real
            # person. Event money rides in the tips column — the venue pays
            # it as tips (owner 2026-08-16).
            # EVERY active employee gets a row, including people with nothing
            # this period and salaried staff who never clock in at all. The
            # sheet is read line by line against the payroll form, and a
            # missing name is how you type someone's pay onto the wrong
            # person (owner 2026-08-16).
            # Salaried staff never clock in, so no timecard-driven figure can
            # ever reach them. Square converts the salary to the period's
            # standard hours at the equivalent hourly rate — 40 h/wk over 24
            # semi-monthly periods is 86.67 h — and that is what its payroll
            # shows, so the sheet reproduces it rather than leaving a blank.
            per_year = {"semimonthly": 24, "monthly": 12, "weekly": 52}[scheme]
            wage_settings = settings_store.get_setting(
                conn, venue["id"], "square_wage_settings")
            tmids_by_emp: dict[int, list[str]] = {}
            for r in conn.execute(
                    "SELECT employee_id, team_member_id FROM square_link"
                    " WHERE venue_id = ?", (venue["id"],)):
                tmids_by_emp.setdefault(r["employee_id"], []).append(
                    r["team_member_id"])

            def salary_for(eid):
                for tmid in tmids_by_emp.get(eid, []):
                    ws = wage_settings.get(tmid)
                    if not ws or not ws.get("hourly_rate_cents"):
                        continue
                    hours = round(float(ws.get("weekly_hours") or 0)
                                  * 52 / per_year, 2)
                    rate = Decimal(ws["hourly_rate_cents"])
                    cents = int((Decimal(str(hours)) * rate).quantize(
                        Decimal("1"), rounding=ROUND_CEILING))
                    return hours, cents, ws
                return None

            payroll = []
            # Someone marked off payroll who nonetheless earned is worth
            # saying out loud — dropping their row silently would lose real
            # money without anyone noticing.
            off_payroll_with_pay = []
            for eid, emp in emps.items():
                if not emp["active"] and eid not in per_emp and eid not in staff:
                    continue
                if not emp.get("in_payroll", True):
                    s_ = staff.get(eid, {})
                    earned = (s_.get("tips_cents", 0) + s_.get("event_cents", 0)
                              + s_.get("gratuity_cents", 0))
                    if earned or eid in per_emp:
                        off_payroll_with_pay.append(emp["display_name"])
                    continue
                lab = per_emp.get(eid)
                s_ = staff.get(eid, {})
                tips = s_.get("tips_cents", 0) + s_.get("event_cents", 0)
                grat = s_.get("gratuity_cents", 0)
                wages = lab["wages_cents"] if lab else 0
                sal = None if lab else salary_for(eid)
                payroll.append({
                    "employee_id": eid,
                    "name": emp["display_name"],
                    "regular_hours": (lab["regular_hours"] if lab
                                      else (sal[0] if sal else 0.0)),
                    "overtime_hours": lab["overtime_hours"] if lab else 0.0,
                    "wages_cents": sal[1] if sal else wages,
                    "gratuity_cents": grat,
                    "tips_cents": tips,
                    "gross_pay_cents": (sal[1] if sal else wages) + grat + tips,
                    # overtime spanning two pay rates: the blended rate is the
                    # payroll engine's to decide, so flag the row rather than
                    # showing a figure that is quietly a few cents out
                    "blended_overtime": bool(lab and lab["blended_overtime"]),
                    # no timecards at all — salaried, or simply did not work.
                    # Either way we have no wages for them and must not imply
                    # their gross is zero.
                    "no_timecards": lab is None and sal is None,
                    # paid a salary, not from the clock: the hours shown are
                    # the period's standard hours, not hours worked
                    "salaried": bool(sal),
                })
            payroll.sort(key=lambda r: r["name"])
            totals["off_payroll_with_pay"] = sorted(off_payroll_with_pay)

        return {
            "start": start.isoformat(), "end": end.isoformat(),
            "prev_anchor": prev_period_scheme(start, scheme)[0].isoformat(),
            "next_anchor": next_period_scheme(end, scheme)[0].isoformat(),
            "scheme": scheme,
            "schemes": list(VENUE_SCHEMES[venue["tip_model"]]),
            "days": days_out,
            "totals": totals,
            # Contract labour is paid DIRECTLY, never through payroll, so it
            # must not reach the payroll export or its CSV — a 1099 worker
            # imported into Square Payroll gets paid a second time. Their
            # earnings appear on the contractor card instead, which is the
            # only place that figure belongs.
            "employees": sorted(
                (s for s in staff.values()
                 if not emps.get(s["employee_id"], {}).get("is_contractor")),
                key=lambda s: s["name"]),
            "contractor_employees": sorted(
                (s for s in staff.values()
                 if emps.get(s["employee_id"], {}).get("is_contractor")),
                key=lambda s: s["name"]),
            # everyone who worked, tips or not — the payroll entry sheet
            "payroll": payroll,
            "draft_dates": draft_dates,
            "flagged_dates": flagged_dates,
            "finalized_only": finalized_only,
            "model": venue["tip_model"],
            # the fee rate in force, so reports can label the deduction
            "card_fee_pct": (settings_store.get_setting(
                conn, venue["id"], "poq_card_fee_pct") if is_poq else None),
            # dates whose sales are unknown — the tip rate is not reportable
            # until these are re-pulled
            "days_missing_sales": days_missing_sales,
            "venue": {"id": venue["id"], "name": venue["name"],
                      "slug": venue["slug"]},
            "boh_monthly": boh_monthly,
        }

    def _contractor_earnings(conn, venue, emps, start: date, end: date) -> dict[int, dict]:
        """What each contractor earned between two dates, from finalized days.

        Reads the SNAPSHOT of every finalized day, not today's recomputation,
        so a figure that has been paid can never drift underneath the person
        it was paid to.
        """
        out: dict[int, dict] = {}
        rows = conn.execute(
            "SELECT * FROM day WHERE venue_id = ? AND date BETWEEN ? AND ?"
            " AND status = 'finalized' ORDER BY date",
            (venue["id"], start.isoformat(), end.isoformat()),
        ).fetchall()
        for row in rows:
            outputs = snapshot_outputs(conn, row["id"]) or {}
            inputs = json.loads(row["inputs_json"])
            hours_by = {int(k): float(v)
                        for k, v in (inputs.get("contractor_hours") or {}).items()}
            # every model names its payout rows differently; sum what is there
            payouts: dict[int, int] = {}
            for key in ("people", "foh", "boh"):
                for line in outputs.get(key) or ():
                    eid = line.get("employee_id")
                    if eid is None:
                        continue
                    payouts[eid] = payouts.get(eid, 0) + sum(
                        v for k, v in line.items()
                        if k.endswith("_cents") and isinstance(v, int))
            for eid, e in emps.items():
                if not e.get("is_contractor"):
                    continue
                hours = hours_by.get(eid, 0.0)
                tips = payouts.get(eid, 0)
                if not hours and not tips:
                    continue
                rate = e.get("hourly_rate_cents") or 0
                # hours priced the way the payroll sheet prices them: round the
                # reported hours first, then multiply, so the figure matches
                # what the manager can check by hand
                wages = int(Decimal(str(round(hours, 2))) * rate)
                rec = out.setdefault(eid, {
                    "employee_id": eid, "name": e["display_name"],
                    "hourly_rate_cents": rate,
                    "w9_received": bool(e.get("w9_received")),
                    "hours": 0.0, "wages_cents": 0, "tips_cents": 0,
                    "total_cents": 0, "days": 0, "dates": [],
                })
                rec["hours"] = round(rec["hours"] + hours, 2)
                rec["wages_cents"] += wages
                rec["tips_cents"] += tips
                rec["total_cents"] += wages + tips
                rec["days"] += 1
                rec["dates"].append(row["date"])
        return out

    @app.get("/api/periods/{anchor}/contractors")
    def get_contractor_pay(anchor: str, user: User, conn: DB, venue: Venue,
                           scheme: str | None = None):
        """Contract labour: what to hand each person, and the running
        calendar-year total against the $600 reporting threshold.

        The threshold is counted PER VENUE per calendar year (owner
        2026-08-30) — each venue pays its own contractors — and from
        finalized days only, because an unfinalized night is not yet money
        anyone is owed.
        """
        d = parse_date(anchor)
        start, end = period_for_scheme(d, resolve_scheme(venue, scheme))
        emps = employees_map(conn, venue["id"])
        period = _contractor_earnings(conn, venue, emps, start, end)
        ytd = _contractor_earnings(conn, venue, emps, date(d.year, 1, 1),
                                   date(d.year, 12, 31))
        rows = []
        for eid, rec in sorted(period.items(), key=lambda kv: kv[1]["name"]):
            y = ytd.get(eid, {})
            rec["ytd_total_cents"] = y.get("total_cents", 0)
            rec["ytd_hours"] = y.get("hours", 0.0)
            rec["crosses_threshold"] = rec["ytd_total_cents"] >= CONTRACTOR_1099_CENTS
            rows.append(rec)
        # someone with nothing this period may still have crossed earlier in
        # the year, and that is exactly when you need to know
        for eid, y in sorted(ytd.items(), key=lambda kv: kv[1]["name"]):
            if eid in period:
                continue
            rows.append({**y, "hours": 0.0, "wages_cents": 0, "tips_cents": 0,
                         "total_cents": 0, "days": 0, "dates": [],
                         "ytd_total_cents": y["total_cents"],
                         "ytd_hours": y["hours"],
                         "crosses_threshold": y["total_cents"] >= CONTRACTOR_1099_CENTS})
        return {
            "start": start.isoformat(), "end": end.isoformat(),
            "year": d.year,
            "threshold_cents": CONTRACTOR_1099_CENTS,
            "contractors": rows,
            "period_total_cents": sum(r["total_cents"] for r in rows),
        }

    @app.get("/api/periods/{anchor}")
    def get_period(anchor: str, user: User, conn: DB, venue: Venue,
                   scheme: str | None = None):
        return period_summary(conn, venue, parse_date(anchor),
                              finalized_only=False,
                              scheme=resolve_scheme(venue, scheme))

    @app.get("/api/periods/{anchor}/export")
    def get_export_preview(anchor: str, user: User, conn: DB, venue: Venue,
                           scheme: str | None = None):
        summary = period_summary(conn, venue, parse_date(anchor),
                                 finalized_only=True,
                                 scheme=resolve_scheme(venue, scheme))
        return summary

    @app.get("/api/periods/{anchor}/form4070")
    def form_4070(anchor: str, user: User, conn: DB, venue: Venue):
        """IRS Form 4070-style monthly data per employee (La Fontana only —
        the tip-out model tracks who received what; the pooled model
        deliberately doesn't). Finalized days only. Auto-gratuity excluded
        (service charges are wages, not tips). Amounts are exact tips before
        any cash round-up. SSN/address are intentionally never stored."""
        if venue["tip_model"] == "POOL_HOURS":
            raise HTTPException(
                422, "Form 4070 reports are only available for tip-out venues;"
                     " the pooled model does not track individual tip receipt")
        d = parse_date(anchor)
        start, end = period_for_scheme(d, "monthly")
        emps = employees_map(conn, venue["id"])
        rows = conn.execute(
            "SELECT * FROM day WHERE venue_id = ? AND date BETWEEN ? AND ?"
            " AND status = 'finalized' ORDER BY date",
            (venue["id"], start.isoformat(), end.isoformat()),
        ).fetchall()
        agg: dict[int, dict] = {}

        def entry(eid: int) -> dict:
            return agg.setdefault(eid, {
                "employee_id": eid,
                "name": emps[eid]["display_name"],
                "role": emps[eid]["pool_role"],
                "cash_tips_cents": 0, "card_tips_cents": 0,
                "paid_out_cents": 0,
            })

        finalized_dates = []
        for row in rows:
            rec = snapshot_record(conn, row["id"])
            if rec is None:
                continue
            inputs, outputs = rec
            if outputs.get("model") != "PERCENT_TIPOUT":
                continue
            finalized_dates.append(row["date"])
            cash_by = {int(k): v for k, v in
                       inputs.get("server_cash_tips", {}).items()}
            for p in outputs["people"]:
                eid = p["employee_id"]
                if eid not in emps:
                    continue
                e = entry(eid)
                if p["role"] == "SERVER":
                    cash = cash_by.get(eid, 0)
                    e["cash_tips_cents"] += cash
                    e["card_tips_cents"] += p["tips_cents"] - cash
                    e["paid_out_cents"] += (p["tips_cents"] - p["keep_cents"]
                                            - p["returned_cents"])
                else:
                    # busser/host pool shares are paid in cash weekly
                    e["cash_tips_cents"] += p["pool_share_cents"]
        # kitchen: the monthly pool split (paid in cash at payroll time)
        summary = period_summary(conn, venue, start, finalized_only=True,
                                 scheme="monthly")
        bm = summary.get("boh_monthly") or {}
        for eid_str, share in (bm.get("shares") or {}).items():
            eid = int(eid_str)
            if eid in emps:
                entry(eid)["cash_tips_cents"] += share

        forms = []
        for e in sorted(agg.values(), key=lambda x: (x["role"], x["name"])):
            net = e["cash_tips_cents"] + e["card_tips_cents"] - e["paid_out_cents"]
            if e["cash_tips_cents"] == 0 and e["card_tips_cents"] == 0:
                continue  # nothing to report
            forms.append({**e, "net_tips_cents": net})
        audit(conn, venue["id"], user["id"], "form4070_generated", "period",
              start.isoformat())
        conn.commit()
        return {
            "venue": {"name": venue["name"]},
            "month_label": start.strftime("%B %Y"),
            "start": start.isoformat(), "end": end.isoformat(),
            "finalized_days": len(finalized_dates),
            "draft_or_missing_days": (end - start).days + 1 - len(finalized_dates),
            "forms": forms,
        }

    @app.get("/api/periods/{anchor}/export.csv")
    def export_csv(anchor: str, user: User, conn: DB, venue: Venue,
                   scheme: str | None = None):
        sch = resolve_scheme(venue, scheme)
        s = period_summary(conn, venue, parse_date(anchor), finalized_only=True,
                           scheme=sch)
        buf = io.StringIO()
        w = csv.writer(buf)
        if venue["tip_model"] == "PERCENT_TIPOUT":
            # component columns so every number is traceable (M5 §5);
            # weekly = cash payout report, so it carries the round-up columns
            # no Hours column: LF tracks presence, not hours (2026-07-06)
            weekly = sch == "weekly"
            header = ["Employee", "Role", "Server Keep", "Pool Share", "Returned",
                      "Tips Total", "Auto Gratuity (wages)", "Days Worked",
                      "Cash Payout", "Round-up"]
            w.writerow(header)
            for e in s["employees"]:
                row = [
                    e["name"], e["role"],
                    f"{e['keep_cents'] / 100:.2f}",
                    f"{e['pool_share_cents'] / 100:.2f}",
                    f"{e['returned_cents'] / 100:.2f}",
                    f"{e['tips_cents'] / 100:.2f}",
                    f"{e['gratuity_cents'] / 100:.2f}",
                    e["days"],
                ]
                if weekly:
                    row += [f"{e['cash_payout_cents'] / 100:.2f}",
                            f"{e['roundup_cents'] / 100:.2f}"]
                else:
                    row += ["", ""]  # payroll rows stay exact — no cash rounding
                w.writerow(row)
            if s.get("boh_monthly"):
                bm = s["boh_monthly"]
                for m in bm["members"]:
                    if "share_cents" not in m:
                        continue
                    w.writerow([
                        m["name"], "BOH", "0.00",
                        f"{m['share_cents'] / 100:.2f}", "0.00",
                        f"{m['share_cents'] / 100:.2f}", "0.00",
                        m["worked_days"],
                        f"{m['cash_payout_cents'] / 100:.2f}",
                        f"{m['roundup_cents'] / 100:.2f}",
                    ])
        elif venue["tip_model"] == "POINTS_HOURS":
            # Points are the audit trail for the split: tips / points is the
            # value of one point, so any row can be checked by hand. Event
            # money is its own column — it comes from a different pool.
            # Tips and gratuity stay separate columns — payroll needs them on
            # different lines (tips vs wages) — but Take Home is what the
            # person is actually owed, so it is there to check against.
            w.writerow(["Employee", "Tips (daily pool)", "Event Payout",
                        "Tips Total", "Auto Gratuity (wages)", "Take Home",
                        "Days Worked", "Hours", "Points"])
            for e in s["employees"]:
                tips_total = e["tips_cents"] + e.get("event_cents", 0)
                take_home = tips_total + e["gratuity_cents"]
                w.writerow([
                    e["name"],
                    f"{e['tips_cents'] / 100:.2f}",
                    f"{e.get('event_cents', 0) / 100:.2f}",
                    f"{tips_total / 100:.2f}",
                    f"{e['gratuity_cents'] / 100:.2f}",
                    f"{take_home / 100:.2f}",
                    e["days"],
                    f"{e['hours']:.2f}",
                    f"{e['points']:.4f}".rstrip("0").rstrip("."),
                ])
            # Where the pooled money came from, so the CSV can be checked
            # line-by-line against Square's own card / cash / service-charge
            # figures without opening the app.
            t = s["totals"]
            w.writerow([])
            w.writerow(["Period totals", "", "", "", "", "", "", ""])
            for label, cents in [
                ("Card tips (gross)", t.get("credit_tips_gross_cents", 0)),
                ("Cash tips (declared)", t.get("cash_tips_cents", 0)),
                ("Auto-gratuity (gross)", t.get("auto_gratuity_gross_cents", 0)),
                (f"Card processing fee ({s.get('card_fee_pct') or '0'}% of card tips"
                 " + gratuity)", -t.get("processing_fee_total_cents", 0)),
                ("Pooled tips (net)", t.get("total_tips_cents", 0)),
                ("Front of house (80%)", t.get("foh_pool_cents", 0)),
                ("Kitchen (20%)", t.get("boh_pool_cents", 0)),
                ("Auto-gratuity paid out (net)", t.get("auto_gratuity_cents", 0)),
                ("Net sales (ex tax/tip/service charge)", t.get("net_sales_cents", 0)),
            ]:
                w.writerow([label, f"{cents / 100:.2f}"])
            sales = t.get("net_sales_cents", 0)
            if s.get("days_missing_sales"):
                w.writerow(["Average tip rate", "unavailable — no sales data for "
                            + ", ".join(s["days_missing_sales"]) + " (re-pull those days)"])
            elif sales:
                disc = (t.get("credit_tips_gross_cents", 0)
                        + t.get("cash_tips_cents", 0))
                w.writerow(["Average tip rate (card + cash / net sales)",
                            f"{disc / sales * 100:.2f}%"])
                w.writerow(["Average tip rate incl. auto-gratuity",
                            f"{(disc + t.get('auto_gratuity_gross_cents', 0))
                               / sales * 100:.2f}%"])
        else:
            # "FOH Hours" stays hours actually worked (what payroll needs);
            # "Credited Hours" is the tip-weighted figure the split used, so a
            # half-credit door shift explains its own smaller tip total.
            w.writerow(["Employee", "Pool Tips (FOH)", "Kitchen Share (BOH)", "Tips Total",
                        "Auto Gratuity (wages)", "Days Worked", "FOH Hours",
                        "Credited Hours"])
            for e in s["employees"]:
                tips_total = e["tips_cents"] + e["boh_cents"]
                w.writerow([
                    e["name"],
                    f"{e['tips_cents'] / 100:.2f}",
                    f"{e['boh_cents'] / 100:.2f}",
                    f"{tips_total / 100:.2f}",
                    f"{e['gratuity_cents'] / 100:.2f}",
                    e["days"],
                    f"{e['hours']:.2f}",
                    f"{e.get('credited_hours', e['hours']):.2f}",
                ])
        audit(conn, venue["id"], user["id"], "period_exported", "period", s["start"])
        conn.commit()
        filename = f"tips_{venue['slug']}_{s['start']}_{s['end']}.csv"
        return PlainTextResponse(
            buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ---------- static frontend ----------

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        """The SPA has no build step or cache-busting hashes, so browsers
        must revalidate app.js/styles.css on every load — otherwise a phone
        can pair a cached old stylesheet with new markup after an update.
        ETag/If-Modified-Since still make unchanged loads cheap 304s."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    # Ensure the PWA manifest is served with the correct media type
    # (Python's mimetypes doesn't know .webmanifest by default).
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
