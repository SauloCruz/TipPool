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
import mimetypes
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth as auth_mod
from . import settings_store, sync
from .compute import (EMPTY_INPUTS_BY_MODEL, DayValidationError,
                      compute_lf_outputs, compute_outputs)
from .config import Settings
from .db import SCHEMA_VERSION, audit, connect, init_db, utcnow
from .periods import (VENUE_SCHEMES, next_period_scheme, period_days,
                      period_for_scheme, prev_period_scheme)
from .square import SquareClient, SquareError
from .square_extract import MUTABLE_WARNINGS
from engine import distribute_cents

STATIC_DIR = Path(__file__).parent.parent / "static"
# informational flags: shown as reminders, never mark a day as flagged
INFO_FLAGS = {"no_host_resplit"}


# ---------- request/response models ----------

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


class EmployeePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    pool_role: str | None = Field(default=None, pattern="^(FOH|BOH|EXCLUDED|SERVER|BUSSER|HOST)$")
    active: bool | None = None
    square_team_member_id: str | None = None
    # LF: salaried kitchen staff never clock in but always share the monthly
    # BOH pool (pre-selected on the export roster)
    always_in_boh_pool: bool | None = None


class SettingsPatch(BaseModel):
    category_map: dict[str, dict] | None = None
    gratuity_service_charge: dict | None = None
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
                      "always_in_boh_pool": bool(r["always_in_boh_pool"])}
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
            return compute_outputs(inputs, emps)
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
            "SELECT id, display_name, pool_role, active, always_in_boh_pool"
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

    @app.post("/api/employees", status_code=201)
    def create_employee(body: EmployeeBody, admin: Admin, conn: DB, venue: Venue):
        check_role(venue, body.pool_role)
        try:
            cur = conn.execute(
                "INSERT INTO employee (venue_id, display_name, pool_role,"
                " created_at) VALUES (?, ?, ?, ?)",
                (venue["id"], body.display_name.strip(), body.pool_role, utcnow()),
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
        return {"id": cur.lastrowid, **body.model_dump(), "active": True}

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
            "SELECT id, display_name, pool_role, active, always_in_boh_pool"
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
        return {
            "pulled_at": sq["pulled_at"],
            "values": sq["values"],
            "issues": issues,
            "muted_count": len(sq["issues"]) - len(issues),
            "blocked_fields": sorted(sync.blocked_fields(sq)),
        }

    def day_payload(conn, venue, d: date) -> dict:
        row = day_row(conn, venue["id"], d)
        emps = employees_map(conn, venue["id"])
        if row is None:
            return {
                "date": d.isoformat(), "status": "not_started",
                "inputs": dict(EMPTY_INPUTS_BY_MODEL[venue["tip_model"]]),
                "computed": compute_or_422(conn, venue, EMPTY_INPUTS_BY_MODEL[venue["tip_model"]], emps),
                "snapshots": [], "square": None,
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
            "snapshots": [dict(s) for s in snaps],
            "square": square_payload(conn, row),
        }

    @app.get("/api/days/{date_str}")
    def get_day(date_str: str, user: User, conn: DB, venue: Venue):
        return day_payload(conn, venue, parse_date(date_str))

    @app.put("/api/days/{date_str}")
    def put_day(date_str: str, body: dict, user: User, conn: DB, venue: Venue):
        d = parse_date(date_str)
        row = day_row(conn, venue["id"], d)
        if row is not None and row["status"] == "finalized":
            raise HTTPException(409, "day is finalized — an admin must reopen it first")
        model_cls = (LFDayInputsBody if venue["tip_model"] == "PERCENT_TIPOUT"
                     else DayInputsBody)
        try:
            parsed = model_cls(**body)
        except Exception as exc:
            raise HTTPException(422, f"invalid day inputs: {exc}")
        inputs = parsed.model_dump()
        # JSON object keys are strings; normalize all id-keyed maps for storage
        for key, value in list(inputs.items()):
            if isinstance(value, dict):
                inputs[key] = {str(k): v for k, v in value.items()}
        emps = employees_map(conn, venue["id"])
        computed = compute_or_422(conn, venue, inputs, emps)  # validate before saving
        # override audit: log any Square-pulled field the manager changed away
        # from (or back to) the pulled value
        sq = square_payload(conn, row)
        if sq:
            old_inputs = json.loads(row["inputs_json"])
            for field in sync.SQUARE_FIELDS_BY_MODEL[venue["tip_model"]]:
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
        if venue["tip_model"] != "PERCENT_TIPOUT":
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
        if venue["tip_model"] != "PERCENT_TIPOUT":
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
        apply_pull(conn, venue, d, record, user["id"])
        conn.commit()
        return day_payload(conn, venue, d)

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
        except SquareError as exc:
            raise HTTPException(502, str(exc))
        cache = [
            {"id": m["id"],
             "name": " ".join(filter(None, [m.get("given_name"), m.get("family_name")]))
                     or m["id"],
             "status": m.get("status", "ACTIVE")}
            for m in members
        ]
        settings_store.put_setting(conn, venue["id"], "square_team_cache", cache, admin["id"])
        conn.commit()
        linked_ids = {r["team_member_id"] for r in conn.execute(
            "SELECT team_member_id FROM square_link WHERE venue_id = ?",
            (venue["id"],)).fetchall()}
        return {"team": cache,
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
        boh_monthly = None
        days_out = []
        totals = ({"total_tips_cents": 0, "auto_gratuity_cents": 0,
                   "pool_busser_cents": 0, "pool_host_cents": 0,
                   "pool_boh_cents": 0} if is_lf else
                  {"total_tips_cents": 0, "boh_allocation_cents": 0,
                   "foh_pool_cents": 0, "auto_gratuity_cents": 0})
        staff: dict[int, dict] = {}
        draft_dates, flagged_dates = [], []

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
            flags_on = [k for k, v in outputs["flags"].items()
                        if v and k not in INFO_FLAGS]
            if flags_on:
                flagged_dates.append(key)
            if row["status"] != "finalized":
                draft_dates.append(key)
            days_out.append({
                "date": key, "status": row["status"], "flags_on": flags_on,
                "total_tips_cents": outputs["totals"]["total_tips_cents"],
                "foh_pool_cents": outputs["totals"].get("foh_pool_cents"),
            })
            if finalized_only and row["status"] != "finalized":
                continue
            for k in totals:
                totals[k] += outputs["totals"].get(k, 0)
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
                    "days": 0, "hours": 0.0,
                })
                s["tips_cents"] += line["tips_cents"]
                s["gratuity_cents"] += line["gratuity_cents"]
                s["days"] += 1
                s["hours"] += line["hours"]
            for line in outputs["boh"]:
                s = staff.setdefault(line["employee_id"], {
                    "employee_id": line["employee_id"], "name": line["name"],
                    "tips_cents": 0, "gratuity_cents": 0, "boh_cents": 0,
                    "days": 0, "hours": 0.0,
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

        return {
            "start": start.isoformat(), "end": end.isoformat(),
            "prev_anchor": prev_period_scheme(start, scheme)[0].isoformat(),
            "next_anchor": next_period_scheme(end, scheme)[0].isoformat(),
            "scheme": scheme,
            "schemes": list(VENUE_SCHEMES[venue["tip_model"]]),
            "days": days_out,
            "totals": totals,
            "employees": sorted(staff.values(), key=lambda s: s["name"]),
            "draft_dates": draft_dates,
            "flagged_dates": flagged_dates,
            "finalized_only": finalized_only,
            "model": venue["tip_model"],
            "venue": {"id": venue["id"], "name": venue["name"],
                      "slug": venue["slug"]},
            "boh_monthly": boh_monthly,
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
        if venue["tip_model"] != "PERCENT_TIPOUT":
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
        else:
            w.writerow(["Employee", "Pool Tips (FOH)", "Kitchen Share (BOH)", "Tips Total",
                        "Auto Gratuity (wages)", "Days Worked", "FOH Hours"])
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
