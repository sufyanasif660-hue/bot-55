#!/usr/bin/env python3
"""
Goethe Booking Bot - API Backend
=================================
Flask-based API for the standalone frontend.

Deploy backend anywhere (Railway, Render, Fly.io, VPS).
Frontend (frontend/index.html) deploys on Vercel.

Usage:
  pip install flask
  python webapp.py

Frontend connects via Backend URL input.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from werkzeug.exceptions import NotFound

from collections import defaultdict

import crypto_utils

from pydantic import BaseModel, Field, ValidationError

class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=255)

class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)

class StudentItem(BaseModel):
    student_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)

class StartRequest(BaseModel):
    students: list[StudentItem] = Field(default_factory=list)
    headless: bool = True
    immediate: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    level: str = ""

class ScheduleStartRequest(BaseModel):
    datetime: str = Field(min_length=1)
    students: list[StudentItem] = Field(default_factory=list)

class QueueEnqueueRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = ""
    level: str = ""
    city: str = ""
    priority: int = 0

class QueueEnqueueManyRequest(BaseModel):
    students: list[StudentItem] = Field(min_length=1)

class QueueCompleteRequest(BaseModel):
    id: str | int
    result: dict = {}  # type: ignore

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list = Field(default_factory=list)

def validate(model_cls):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            data = flask.request.get_json(silent=True) or {}
            try:
                validated = model_cls(**data)
            except ValidationError as e:
                return jsonify({"ok": False, "error": "Validation failed", "details": e.errors(include_input=False)}), 400
            return fn(validated, *args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import flask
from flask import Blueprint, Flask, Response, jsonify, request
from flasgger import Swagger, swag_from

PROJECT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_DIR))

import booking_helper as bot
_db_url = os.environ.get("DATABASE_URL", "").strip()
if _db_url.startswith("postgresql://") or _db_url.startswith("postgres://"):
    import database as db
else:
    import db
import alexa
import goethe_scraper
import student_queue as sqmod
from deadman import DeadManSwitch
from telegram_commander import TelegramCommander
import signal

# WebSocket broadcaster (stub-safe — no-op if flask-sock missing)
from websocket_handler import broadcaster as ws_broadcaster

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB limit for uploads

# ── WebSocket setup ──
from websocket_handler import setup_websocket
sock = setup_websocket(app)
ws_broadcaster.start()

class WebSocketLogHandler(logging.Handler):
    """Logging handler that pushes all log records to WebSocket clients."""
    def emit(self, record):
        try:
            if record.name.startswith(("werkzeug", "flask", "urllib", "gspread")):
                return
            ws_broadcaster.push({
                "type": "log",
                "time": self.formatter.formatTime(record) if self.formatter else record.created,
                "data": self.format(record),
            })
        except Exception:
            pass

# Attach WebSocket handler to root, but filter out noise
logging.getLogger().setLevel(logging.INFO)
_ws_handler = WebSocketLogHandler()
_ws_handler.setFormatter(logging.Formatter("%(message)s"))
_ws_handler.addFilter(lambda r: not r.name.startswith(("werkzeug", "flask", "urllib", "gspread")))
logging.getLogger().addHandler(_ws_handler)

# ── Graceful shutdown ──
def _handle_shutdown(signum, frame):
    import logging as _lg
    _lg.getLogger("shutdown").warning("Signal %s received, checkpointing...", signum)
    try:
        count = bot.checkpoint_all_running_students()
        _lg.getLogger("shutdown").info("Checkpointed %d running student(s)", count)
    except Exception as exc:
        _lg.getLogger("shutdown").error("Checkpoint failed: %s", exc)

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)

bp = Blueprint("api", __name__)

# ── Swagger / OpenAPI ──
app.config["SWAGGER"] = {
    "title": "Goethe Booking Bot API",
    "version": "2.0.0",
    "description": "Automated exam booking backend for Goethe-Institut Pakistan",
    "termsOfService": "",
    "specs_route": "/api/docs/",
}
swagger = Swagger(app, template={
    "info": {
        "contact": {"email": "hamzarafiq655@gmail.com"},
    },
    "securityDefinitions": {
        "Bearer": {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "description": "Enter: Bearer &lt;token&gt;",
        }
    },
})

# ── Sentry error tracking ──
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.5,
        send_default_pii=False,
    )

# ── Allowed origins for CORS ──
_ALLOWED_ORIGINS = {
    "https://goethe-frontend-v3.vercel.app",
    "https://goethe-booking-bot-production-21af.up.railway.app",
    "http://localhost:3000",
    "http://localhost:5000",
    "http://127.0.0.1:5000",
}


@app.before_request
def enforce_https():
    if not flask.request.is_secure and os.environ.get("ENFORCE_HTTPS", "").lower() in ("1", "true", "yes"):
        url = flask.request.url.replace("http://", "https://", 1)
        return flask.redirect(url, 301)


# ── Auth config ──
AUTH_EMAIL = os.environ.get("AUTH_EMAIL", "admin@example.com")
_raw_password = os.environ.get("AUTH_PASSWORD", "change-me-in-production")
AUTH_PASSWORD_HASHED = crypto_utils.hash_password(_raw_password)
FERNET_KEY = os.environ.get("FERNET_KEY", "")
if not FERNET_KEY:
    # No env key — fall back to a key persisted in the DB so it survives restarts
    # (an ephemeral per-boot key would make stored student passwords undecryptable).
    try:
        FERNET_KEY = db.get_state("fernet_key", "")
    except Exception:
        FERNET_KEY = ""
    if FERNET_KEY:
        print("INFO: FERNET_KEY not set in env; reusing the key persisted in the DB.")
    else:
        FERNET_KEY = crypto_utils.generate_fernet_key()
        try:
            db.set_state("fernet_key", FERNET_KEY)
            print("WARNING: No FERNET_KEY env var set. Generated a key and persisted it in the DB. "
                  "Set FERNET_KEY explicitly for portability across environments.")
        except Exception:
            print("WARNING: No FERNET_KEY env var set and DB persist failed. Using an ephemeral key "
                  "— student passwords will be lost on restart. Set FERNET_KEY.")
AUTH_SALT = os.environ.get("AUTH_SALT", "goethe-bot-salt-2026")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "admin@example.com")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PROCESS_START_TIME = time.time()

def _make_token(email: str) -> str:
    return db.create_session(email, expiry_hours=24)

def validate_token(token: str) -> bool:
    if not token:
        return False
    if token == _raw_password:
        return True
    email = db.validate_session(token)
    return email == AUTH_EMAIL if email else False

def _check_auth():
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return validate_token(token)

# ── Global state ──
bot_stop_event = threading.Event()
bot_thread: Optional[threading.Thread] = None
bot_running = False
log_queue: queue.Queue = queue.Queue()
student_queue = sqmod.StudentQueue()
deadman = DeadManSwitch()
student_status: Dict[str, Dict] = {}  # name -> {status, color, details}
student_results: List[Dict] = []
config_path: str = ""
telegram_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
telegram_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")
telegram_commander: Optional[TelegramCommander] = None

# ── Scheduled mode ──
scheduled_start: Optional[str] = None  # ISO datetime string
scheduler_thread: Optional[threading.Thread] = None
scheduler_stop = threading.Event()


class WebLogHandler(logging.Handler):
    def __init__(self, lq: queue.Queue):
        super().__init__()
        self.lq = lq

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        self.lq.put({"time": record.asctime if hasattr(record, 'asctime') else '',
                     "level": record.levelname,
                     "message": msg,
                     "name": record.name})


class DbLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            db.add_log(record.name, record.levelname, msg)
        except Exception:
            pass


class JsonFileHandler(logging.Handler):
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            entry = json.dumps({
                "time": datetime.now().isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
            with self._lock:
                with open(self.path, "a") as f:
                    f.write(entry + "\n")
        except Exception:
            pass


class JsonStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord):
        try:
            entry = json.dumps({
                "time": datetime.now().isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
            print(entry, flush=True)
        except Exception:
            pass


def setup_web_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = WebLogHandler(log_queue)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.addHandler(DbLogHandler())
    json_handler = JsonFileHandler(str(PROJECT_DIR / "bot_logs.ndjson"))
    logger.addHandler(json_handler)
    logger.addHandler(JsonStreamHandler())
    return logger


def _strip_sensitive(students: List[Dict]) -> List[Dict]:
    return [{k: v for k, v in s.items() if k != "password"} for s in students]


def _student_key(s: Dict) -> str:
    return f"{s.get('name', '?')}|{s.get('level', s.get('exam_level', '?'))}|{s.get('city', '?')}"


def deadman_alert():
    master_logger = setup_web_logger("deadman")
    master_logger.critical("DEAD MAN SWITCH TRIGGERED — no heartbeat detected")
    bot.notify("Dead Man Switch", "Bot heartbeat stopped. Process may be hung or crashed.", master_logger)

REQUEUE_MAX_RETRIES = int(os.environ.get("REQUEUE_MAX_RETRIES", "3"))
REQUEUE_DELAY_SECONDS = int(os.environ.get("REQUEUE_DELAY_SECONDS", "300"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))  # 5 min


def run_students_web(students: List[Dict], headless: bool, immediate: bool = False):
    global bot_stop_event, bot_running, student_status, student_results
    bot_stop_event.clear()
    bot_running = True
    deadman.start_monitor(interval=120, on_death=lambda: deadman_alert())

    db.save_students(students)
    master_logger = setup_web_logger("web_bot")
    master_logger.info("Bot started with %d student(s)", len(students))

    threads = []
    results_lock = threading.Lock()
    concurrency_sem = threading.Semaphore(MAX_CONCURRENT)
    requeue_counts: Dict[str, int] = {}
    for s in students:
        requeue_counts[_student_key(s)] = 0

    def run_one(s: Dict):
        key = _student_key(s)
        name = s.get("name", "Unknown")
        level = s.get("level", s.get("exam_level", "?"))
        student_logger = setup_web_logger(f"bot_{name}_{level}")

        concurrency_sem.acquire()
        try:
            while True:
                if bot_stop_event.is_set():
                    student_status[key] = {"status": "Stopped", "color": "danger", "details": "Cancelled by user"}
                    return

                student_status[key] = {"status": "Waiting...", "color": "warning", "details": "Polling for slot"}
                result = bot.smart_retry(
                    s, use_headless=headless,
                    logger=student_logger,
                    stop_event=bot_stop_event,
                    immediate=immediate,
                )

                status = result.get("status", "failed")
                if status == "stopped":
                    student_status[key] = {"status": "Stopped", "color": "danger", "details": "Cancelled by user"}
                    return

                if status not in ("failed", "error"):
                    with results_lock:
                        student_results.append(result)
                    db.update_student_status(key, status, result)
                    try:
                        ws_broadcaster.push({"type": "status", "name": name, "status": status, "time": time.time()})
                    except Exception:
                        pass

                    if status == "confirmed":
                        student_status[key] = {"status": "Confirmed!", "color": "success", "details": f"Ref: {result.get('reference', 'N/A')}"}
                    elif status == "submitted":
                        student_status[key] = {"status": "Submitted", "color": "success", "details": "Form submitted"}
                    elif status == "verified":
                        student_status[key] = {"status": "Verified", "color": "success", "details": f"Ref: {result.get('reference', 'N/A')}"}
                    else:
                        student_status[key] = {"status": status, "color": "success", "details": ""}
                    return

                requeue_counts[key] = requeue_counts.get(key, 0) + 1
                if requeue_counts[key] > REQUEUE_MAX_RETRIES:
                    master_logger.warning("Re-queue limit reached for %s (%d attempts) — giving up", name, requeue_counts[key])
                    student_status[key] = {"status": "Failed", "color": "danger", "details": f"Failed after {requeue_counts[key]} attempts"}
                    with results_lock:
                        student_results.append(result)
                    db.update_student_status(key, "failed", result)
                    return

                master_logger.warning("Re-queuing %s (attempt %d/%d) — waiting %ds",
                                      name, requeue_counts[key], REQUEUE_MAX_RETRIES, REQUEUE_DELAY_SECONDS)
                student_status[key] = {"status": "Re-queued", "color": "warning", "details": f"Retry {requeue_counts[key]}/{REQUEUE_MAX_RETRIES} in {REQUEUE_DELAY_SECONDS}s"}
                try:
                    ws_broadcaster.push({"type": "status", "name": name, "status": "requeued", "time": time.time(),
                                         "attempt": requeue_counts[key]})
                except Exception:
                    pass

                for _ in range(REQUEUE_DELAY_SECONDS):
                    if bot_stop_event.is_set():
                        student_status[key] = {"status": "Stopped", "color": "danger", "details": "Cancelled by user"}
                        return
                    time.sleep(1)
        finally:
            concurrency_sem.release()

    for s in students:
        key = _student_key(s)
        student_status[key] = {"status": "Starting...", "color": "info", "details": "Launching browser"}
        t = threading.Thread(target=run_one, args=(s,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    bot_running = False
    deadman.stop_monitor()
    master_logger.info("All students finished")
    if telegram_commander:
        summary_lines = ["Bot finished. Results:"]
        for r in student_results[-10:]:
            summary_lines.append(f"  {r.get('name')}: {r.get('status')} ref={r.get('reference', 'N/A')}")
        telegram_commander.send("\n".join(summary_lines))
    log_queue.put(None)  # Signal SSE stream to end


# ── Security headers + CORS ──
@app.after_request
def add_headers(resp):
    origin = flask.request.headers.get("Origin", "")
    if origin in _ALLOWED_ORIGINS or not origin:
        resp.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
    else:
        resp.headers["Access-Control-Allow-Origin"] = ""
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    resp.headers["Access-Control-Expose-Headers"] = "X-RateLimit-Remaining, Retry-After"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://goethe-frontend-v3.vercel.app; "
        "img-src 'self' data:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "frame-ancestors 'none';"
    )
    if flask.request.method == "OPTIONS":
        resp.status_code = 200
    return resp


# ── Rate limiter ──
_login_attempts = defaultdict(list)
RATE_LIMIT = 5  # max attempts per IP
RATE_WINDOW = 300  # seconds (5 min)
_global_failures = 0
_global_lockout_until: float = 0
ACCOUNT_LOCKOUT_THRESHOLD = 30  # total failed attempts across all IPs
ACCOUNT_LOCKOUT_DURATION = 900  # 15 min


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < RATE_WINDOW]
    if len(_login_attempts[ip]) >= RATE_LIMIT:
        return False
    _login_attempts[ip].append(now)
    return True


# ── Auth routes ──

@bp.route("/login", methods=["POST"])
@swag_from({
    "tags": ["Auth"],
    "summary": "Admin login",
    "parameters": [{
        "in": "body",
        "name": "body",
        "schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "example": "admin@example.com"},
                "password": {"type": "string", "example": "your-password"},
            },
            "required": ["email", "password"],
        },
    }],
    "responses": {"200": {"description": "Login successful, returns token"}},
})
@validate(LoginRequest)
def api_login(data: LoginRequest):
    global _global_failures, _global_lockout_until
    ip = request.remote_addr or "unknown"
    if time.monotonic() < _global_lockout_until:
        remaining = round(_global_lockout_until - time.monotonic())
        db.add_audit_log("login_blocked", "global", "Account locked due to brute force", ip)
        resp = jsonify({"ok": False, "error": f"Account locked. Try again in {remaining}s."})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(remaining)
        return resp
    if not _check_rate_limit(ip):
        resp = jsonify({"ok": False, "error": "Too many attempts. Try again in 5 minutes."})
        resp.status_code = 429
        resp.headers["Retry-After"] = "300"
        resp.headers["X-RateLimit-Remaining"] = "0"
        return resp
    email = data.email.strip().lower()
    password = data.password
    if email == AUTH_EMAIL.lower() and crypto_utils.check_password(password, AUTH_PASSWORD_HASHED):
        _global_failures = 0
        token = _make_token(AUTH_EMAIL)
        db.add_audit_log("login", email, "Successful login", ip)
        return jsonify({"ok": True, "token": token, "email": AUTH_EMAIL})
    _global_failures += 1
    db.add_audit_log("login_failed", email, f"Failed login attempt (global: {_global_failures})", ip)
    if _global_failures >= ACCOUNT_LOCKOUT_THRESHOLD:
        _global_lockout_until = time.monotonic() + ACCOUNT_LOCKOUT_DURATION
        db.add_audit_log("login_lockout", "global", f"Account locked for {ACCOUNT_LOCKOUT_DURATION}s", ip)
    return jsonify({"ok": False, "error": "Invalid email or password"}), 401


@bp.route("/forgot-password", methods=["POST"])
@validate(ForgotPasswordRequest)
def api_forgot_password(data: ForgotPasswordRequest):
    email = data.email.strip().lower()
    if email == AUTH_EMAIL.lower():
        return jsonify({"ok": True, "message": f"Reset link sent to {SUPPORT_EMAIL}"})
    return jsonify({"ok": True, "message": "If that email is registered, a reset link has been sent"})


def require_auth(f):
    """Decorator to require valid auth token."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if _check_auth():
            return f(*args, **kwargs)
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    return decorated


@bp.route("/logout", methods=["POST"])
@require_auth
def api_logout():
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token:
        db.delete_session(token)
        db.add_audit_log("logout", AUTH_EMAIL, "User logged out", request.remote_addr or "")
    return jsonify({"ok": True})


@bp.route("/refresh", methods=["POST"])
@require_auth
def api_refresh():
    auth = request.headers.get("Authorization", "")
    old_token = auth.removeprefix("Bearer ").strip()
    if old_token:
        db.delete_session(old_token)
    new_token = _make_token(AUTH_EMAIL)
    db.add_audit_log("token_refresh", AUTH_EMAIL, "Session token rotated", request.remote_addr or "")
    return jsonify({"ok": True, "token": new_token})


# ── Routes ──

@app.route("/")
@app.route("/health")
def health():
    uptime_secs = int(time.time() - PROCESS_START_TIME)
    return jsonify({
        "status": "ok",
        "running": bot_running,
        "uptime_seconds": uptime_secs,
        "uptime_human": f"{uptime_secs // 3600}h {(uptime_secs % 3600) // 60}m",
    })


_student_list_cache: list = []
_student_list_ts: float = 0
STUDENT_LIST_TTL = 30

@bp.route("/status")
@require_auth
def api_status():
    global _student_list_cache, _student_list_ts
    now = time.time()
    if not _student_list_cache or (now - _student_list_ts) > STUDENT_LIST_TTL:
        _student_list_cache = _get_loaded_students()
        _student_list_ts = now
    return jsonify({
        "running": bot_running,
        "students": [
            {
                "name": s.get("name", "?"),
                "level": s.get("level", s.get("exam_level", "?")),
                "city": s.get("city", "?"),
                "booking_time": s.get("booking_datetime", "?"),
                "status": student_status.get(_student_key(s), {}).get("status", "Not started"),
                "color": student_status.get(_student_key(s), {}).get("color", "secondary"),
                "details": student_status.get(_student_key(s), {}).get("details", ""),
            }
            for s in _student_list_cache
        ],
        "config_loaded": len(_student_list_cache) > 0,
    })


@bp.route("/config", methods=["GET"])
@require_auth
def api_get_config():
    students = _get_loaded_students()
    return jsonify({
        "path": config_path,
        "count": len(students),
        "students": _strip_sensitive(students),
    })


@bp.route("/config/load", methods=["POST"])
@require_auth
def api_load_config():
    global config_path
    data = request.get_json(silent=True) or {}
    path = data.get("path", str(PROJECT_DIR / "config.csv"))
    if not Path(path).exists():
        return jsonify({"ok": False, "error": "File not found"}), 400
    try:
        bot.load_all_students(path)
        config_path = path
        return jsonify({"ok": True, "path": path, "count": len(bot.load_all_students(path))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.route("/config/upload", methods=["POST"])
@require_auth
def api_config_upload():
    global config_path
    csv_content = request.get_data(as_text=True)
    if not csv_content.strip():
        return jsonify({"ok": False, "error": "Empty CSV data"}), 400
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as tmp:
            tmp.write(csv_content)
            tmp_path = tmp.name
        students = bot.load_all_students(tmp_path)
        config_path = tmp_path
        return jsonify({"ok": True, "count": len(students), "students": _strip_sensitive(students)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(404)
def api_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Not found"}), 404


@app.errorhandler(405)
def api_405(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Method not allowed"}), 405


@app.errorhandler(500)
def api_500(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Internal server error"}), 500


@bp.route("/students", methods=["GET"])
@require_auth
def api_get_students():
    try:
        all_students = _get_loaded_students()
        return jsonify({"ok": True, "students": _strip_sensitive(all_students)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/students", methods=["POST"])
@require_auth
def api_add_student():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400
    password_plain = data.get("password", "")
    password_enc = crypto_utils.encrypt_password(password_plain, FERNET_KEY)
    student = {
        "name": name,
        "email": (data.get("email") or "").strip(),
        "password": password_enc,
        "level": (data.get("level") or "").strip().upper(),
        "city": (data.get("city") or "").strip(),
        "booking_datetime": (data.get("booking_datetime") or "").strip(),
        "first_name": (data.get("first_name") or "").strip(),
        "surname": (data.get("surname") or "").strip(),
        "dob": (data.get("dob") or "").strip(),
        "contact_number": (data.get("contact_number") or "").strip(),
        "country": (data.get("country") or "").strip(),
        "postal_code": (data.get("postal_code") or "").strip(),
        "street": (data.get("street") or "").strip(),
        "house_number": (data.get("house_number") or "").strip(),
        "additional_address": (data.get("additional_address") or "").strip(),
        "location_city": (data.get("location_city") or "").strip(),
        "phone_prefix": (data.get("phone_prefix") or "").strip(),
        "phone": (data.get("phone") or "").strip(),
        "place_of_birth": (data.get("place_of_birth") or "").strip(),
        "motivation": (data.get("motivation") or "").strip(),
        "promo_code": (data.get("promo_code") or "").strip(),
        "modules": (data.get("modules") or "").strip(),
    }
    try:
        sid = db.add_student(student)
        try:
            import google_sheets
            sheet_student = dict(student)
            sheet_student["password"] = password_plain
            google_sheets.append_student(sheet_student)
        except Exception:
            pass
        return jsonify({"ok": True, "id": sid})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/students/status", methods=["POST"])
@require_auth
def api_set_student_status():
    """Manually set a student's booking status — e.g. to record a booking that was
    made by hand, so it shows in history. Body: {"key": "Name|Level|City",
    "status": "confirmed", "result": {...optional}}."""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    status = (data.get("status") or "").strip()
    if not key or not status:
        return jsonify({"ok": False, "error": "key and status required"}), 400
    try:
        db.update_student_status(key, status, data.get("result"))
        return jsonify({"ok": True, "key": key, "status": status})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/students/<int(signed=True):student_id>", methods=["DELETE"])
@require_auth
def api_delete_student(student_id: int):
    try:
        if student_id < 0:
            # Negative id = sheet/config-backed student (assigned in _get_loaded_students).
            # Resolve it back to its name/level/city and delete the matching Google Sheet row.
            match = next((s for s in _get_loaded_students() if s.get("id") == student_id), None)
            if not match:
                return jsonify({"ok": False, "error": "Student not found"}), 404
            try:
                import google_sheets
                res = google_sheets.delete_student(
                    match.get("name", ""), match.get("level", match.get("exam_level", "")), match.get("city", "")
                )
            except Exception as exc:
                return jsonify({"ok": False, "error": f"Google Sheet delete failed: {exc}"}), 500
            if res.get("ok"):
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": res.get("error", "Could not delete from Google Sheet")}), 400
        ok = db.delete_student(student_id)
        if not ok:
            return jsonify({"ok": False, "error": "Student not found"}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/exams", methods=["GET"])
@require_auth
def api_get_exams():
    level = request.args.get("level", "B1").strip().upper()
    try:
        result = bot.check_slot_via_api(level, flask.current_app.logger)
        if not result.get("api_ok"):
            return jsonify({"ok": False, "error": result.get("message", "API unavailable"), "exams": []})
        exams = result.get("exams", [])
        dates = []
        for ex in exams:
            date_str = ex.get("startDate", "")
            loc = ex.get("locationName", "") or ""
            txt = ex.get("availabilityText", "") or ""
            dates.append({
                "date": date_str,
                "location": loc,
                "available": txt,
                "level": ex.get("courselevelShortcut", level),
                "label": f"{date_str} — {loc} ({txt})" if loc else f"{date_str} ({txt})",
            })
        if not dates:
            msg = result.get("message", "No dates available")
            return jsonify({"ok": True, "exams": [], "message": msg})
        return jsonify({"ok": True, "exams": dates, "message": f"{len(dates)} date(s) found"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "exams": []}), 500


@bp.route("/start", methods=["POST"])
@require_auth
@swag_from({
    "tags": ["Bot"],
    "summary": "Start booking for all loaded students",
    "parameters": [{
        "in": "body",
        "name": "body",
        "schema": {
            "type": "object",
            "properties": {
                "headless": {"type": "boolean", "default": True},
                "immediate": {"type": "boolean", "default": False},
            },
        },
    }],
    "responses": {"200": {"description": "Bot started"}},
})
@validate(StartRequest)
def api_start(data: StartRequest):
    global bot_thread

    if bot_running:
        return jsonify({"ok": False, "error": "Bot is already running"}), 400

    students = _get_loaded_students()
    if not students:
        return jsonify({"ok": False, "error": "No students loaded. Load a config.csv first"}), 400

    level_filter = data.level.strip().upper()
    if level_filter:
        students = [s for s in students if s.get("level", "").upper() == level_filter]
        if not students:
            return jsonify({"ok": False, "error": f"No students found for level {level_filter}"}), 400

    students = sorted(students, key=lambda s: s.get("booking_datetime", "") or "9999-12-31T23:59")

    headless = data.headless
    # Only force headless on Linux with no X display. On Windows/macOS a headful
    # window works without DISPLAY — and headful is REQUIRED to pass Goethe's
    # reCAPTCHA v3 (headless gets silently blocked). Don't force it there.
    if not headless and os.name != "nt" and sys.platform != "darwin" and not os.environ.get("DISPLAY"):
        headless = True
    immediate = data.immediate

    global telegram_token, telegram_chat_id
    telegram_token = data.telegram_token or telegram_token
    telegram_chat_id = data.telegram_chat_id or telegram_chat_id

    bot.TELEGRAM_BOT_TOKEN = telegram_token
    bot.TELEGRAM_CHAT_ID = telegram_chat_id

    global student_status, student_results
    student_status.clear()
    student_results.clear()

    # Clear log queue
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break

    db.add_audit_log("bot_start", AUTH_EMAIL, f"Started bot for {len(students)} student(s), headless={headless}, immediate={immediate}", request.remote_addr or "")

    bot_thread = threading.Thread(target=run_students_web, args=(students, headless, immediate), daemon=True)
    bot_thread.start()

    return jsonify({"ok": True, "message": f"Started bot for {len(students)} student(s)"})


@bp.route("/stop", methods=["POST"])
@require_auth
def api_stop():
    if not bot_running:
        return jsonify({"ok": False, "error": "Bot is not running"}), 400
    bot_stop_event.set()
    db.add_audit_log("bot_stop", AUTH_EMAIL, "Stop signal sent by user", request.remote_addr or "")
    return jsonify({"ok": True, "message": "Stop signal sent"})


@bp.route("/logs")
def api_logs():
    # SSE needs token via query param (EventSource doesn't support custom headers)
    query_token = request.args.get("token", "")
    if query_token:
        email = db.validate_session(query_token)
        if not email or email != AUTH_EMAIL:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
    elif not _check_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    def generate():
        while True:
            try:
                record = log_queue.get(timeout=30)
                if record is None:
                    break
                yield f"data: {json.dumps(record)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/results")
@require_auth
def api_results():
    return jsonify(student_results)


@bp.route("/live-status")
@require_auth
def api_live_status():
    date_filter = request.args.get("date", "").strip()
    students = db.get_students()
    summary = {"total": 0, "booked": 0, "failed": 0, "pending": 0}
    per_student = []
    for s in students:
        status = s.get("status", "pending")
        if status in ("confirmed", "submitted"):
            summary["booked"] += 1
        elif status == "failed":
            summary["failed"] += 1
        else:
            summary["pending"] += 1
        summary["total"] += 1
        result = s.get("result", {})
        per_student.append({
            "name": s.get("name", "?"),
            "level": s.get("level", "?"),
            "city": s.get("city", "?"),
            "status": status,
            "reference": result.get("reference", ""),
            "exam_date": result.get("exam_date", ""),
            "exam_time": result.get("exam_time", ""),
            "error": result.get("error", ""),
            "updated_at": s.get("updated_at", ""),
        })
    logs = db.get_logs(limit=500, date_filter=date_filter or None)
    return jsonify({
        "summary": summary,
        "students": per_student,
        "logs": logs,
        "results": student_results,
    })


@bp.route("/goethe-schedule")
def api_goethe_schedule():
    force = request.args.get("refresh", "").lower() == "1"
    level = request.args.get("level", "").strip().upper()
    section = request.args.get("section", "all").strip().lower()
    entries = goethe_scraper.get_schedule(force_refresh=force)

    if level and level in ("A1", "A2", "B1"):
        entries = [e for e in entries if e.level == level]

    classified = goethe_scraper.classify_dates(entries)

    if section == "past":
        data = goethe_scraper.to_dict(classified["past"])
    elif section == "coming":
        data = goethe_scraper.to_dict(classified["coming"])
    else:
        data = goethe_scraper.to_dict(entries)

    return jsonify({
        "ok": True,
        "data": data,
        "cached": not force,
        "summary": {
            "past": len(classified["past"]),
            "coming": len(classified["coming"]),
            "total": len(classified["all"]),
        },
    })


@bp.route("/sheets/test", methods=["GET"])
def api_sheets_test():
    try:
        import google_sheets
        result = google_sheets.test_connection()
        return jsonify({"ok": True, "message": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/sheets/auto-fill", methods=["POST"])
@require_auth
def api_sheets_auto_fill():
    try:
        import google_sheets
        result = google_sheets.auto_fill_booking_datetimes()
        return jsonify({"ok": True, "message": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/sheets/update-schedule", methods=["POST"])
@require_auth
def api_sheets_update_schedule():
    try:
        import google_sheets
        msg = google_sheets.update_schedule_tab()
        msg2 = google_sheets.setup_dropdown()
        return jsonify({"ok": True, "schedule": msg, "dropdown": msg2})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/heartbeat")
def api_heartbeat():
    deadman.ping()
    return jsonify({
        "status": "ok",
        "seconds_since_ping": deadman.seconds_since_ping,
        "alive": deadman.is_alive,
    })

@bp.route("/health")
@swag_from({
    "tags": ["System"],
    "summary": "Health check — DB, uptime, Chrome status",
    "responses": {"200": {"description": "Health status"}},
})
def api_health():
    db_ok = False
    try:
        db.get_state("health_check")
        db_ok = True
    except Exception:
        pass
    chrome_ok = bot._find_chrome() is not None if hasattr(bot, "_find_chrome") else True
    uptime = round(time.time() - PROCESS_START_TIME)
    cb = bot.CIRCUIT_BREAKER

    from selector_fallbacks import ELEMENT_SELECTORS
    total_selectors = len(ELEMENT_SELECTORS)
    empty_selectors = sum(1 for v in ELEMENT_SELECTORS.values() if not v)
    selectors_ok = empty_selectors == 0

    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": uptime,
        "version": "2.0.0",
        "checks": {
            "database": "ok" if db_ok else "fail",
            "chrome": "ok" if chrome_ok else "fail",
            "selectors": {
                "status": "ok" if selectors_ok else "degraded",
                "total": total_selectors,
                "empty": empty_selectors,
            },
        },
        "circuit_breaker": {
            "state": cb.state,
            "consecutive_failures": cb.consecutive_failures,
        },
    })

@bp.route("/metrics")
@require_auth
def api_metrics():
    students = db.get_students()
    all_logs = db.get_logs(limit=10000)
    total_bookings = sum(1 for s in students if s.get("status") == "booked")
    failed_bookings = sum(1 for s in students if s.get("status") == "failed")
    pending_bookings = sum(1 for s in students if s.get("status") in ("pending", ""))
    error_logs = sum(1 for l in all_logs if l.get("level") == "ERROR")
    cb = bot.CIRCUIT_BREAKER
    return jsonify({
        "students_total": len(students),
        "students_booked": total_bookings,
        "students_failed": failed_bookings,
        "students_pending": pending_bookings,
        "success_rate": round(total_bookings / len(students) * 100, 1) if students else 0,
        "errors_total": error_logs,
        "circuit_breaker": {
            "state": cb.state,
            "consecutive_failures": cb.consecutive_failures,
        },
        "uptime_seconds": round(time.time() - PROCESS_START_TIME),
    })

@bp.route("/audit-log")
@require_auth
def api_audit_log():
    limit = request.args.get("limit", 100, type=int)
    return jsonify(db.get_audit_logs(limit=limit))


@bp.route("/schedule")
@require_auth
def api_schedule():
    return jsonify(bot.get_schedule())


# ── Scheduled mode ──

def scheduler_loop(target_iso: str):
    global bot_running, scheduled_start, scheduler_thread
    try:
        target = datetime.fromisoformat(target_iso)
    except Exception:
        return
    while not scheduler_stop.is_set():
        now = datetime.now()
        if now >= target:
            break
        remaining = (target - now).total_seconds()
        gap = min(15, remaining)
        scheduler_stop.wait(gap)
    if not scheduler_stop.is_set():
        scheduled_start = None
        # Auto-start the bot
        with app.app_context():
            students = _get_loaded_students()
            if students:
                bot_stop_event.clear()
                bot_thread = threading.Thread(target=run_students_web, args=(students, True), daemon=True)
                bot_thread.start()


@bp.route("/schedule-start", methods=["POST"])
@validate(ScheduleStartRequest)
def api_schedule_start(data: ScheduleStartRequest):
    global scheduled_start, scheduler_thread, scheduler_stop
    dt_str = data.datetime
    try:
        datetime.fromisoformat(dt_str)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid datetime format"}), 400

    scheduler_stop.clear()
    scheduled_start = dt_str
    scheduler_thread = threading.Thread(target=scheduler_loop, args=(dt_str,), daemon=True)
    scheduler_thread.start()
    return jsonify({"ok": True, "message": f"Scheduled for {dt_str}"})


@bp.route("/schedule-status")
@require_auth
def api_schedule_status():
    return jsonify({
        "scheduled": scheduled_start is not None,
        "datetime": scheduled_start,
    })


@bp.route("/schedule-cancel", methods=["POST"])
@require_auth
def api_schedule_cancel():
    global scheduled_start
    scheduler_stop.set()
    scheduled_start = None
    return jsonify({"ok": True, "message": "Schedule cancelled"})


# ── Queue endpoints ──

@bp.route("/queue/enqueue", methods=["POST"])
@validate(QueueEnqueueRequest)
def api_queue_enqueue(data: QueueEnqueueRequest):
    item_id = student_queue.enqueue(
        name=data.name,
        email=data.email,
        level=data.level,
        city=data.city,
        priority=data.priority,
    )
    return jsonify({"ok": True, "id": item_id})


@bp.route("/queue/enqueue-many", methods=["POST"])
@require_auth
def api_queue_enqueue_many():
    data = request.get_json(silent=True) or {}
    students = data.get("students", [])
    if not students:
        return jsonify({"ok": False, "error": "Empty student list"}), 400
    priority = data.get("priority", 0)
    try:
        validated = QueueEnqueueManyRequest(students=students)
    except ValidationError as e:
        return jsonify({"ok": False, "error": "Validation failed", "details": e.errors(include_input=False)}), 400
    student_queue.enqueue_many([s.model_dump() for s in validated.students], priority)
    return jsonify({"ok": True, "count": len(validated.students)})


@bp.route("/queue/dequeue", methods=["POST"])
@require_auth
def api_queue_dequeue():
    item = student_queue.dequeue()
    if not item:
        return jsonify({"ok": False, "error": "Queue is empty"}), 404
    return jsonify({"ok": True, "item": dict(item)})


@bp.route("/queue/complete", methods=["POST"])
@require_auth
def api_queue_complete():
    data = request.get_json(silent=True) or {}
    item_id = data.get("id")
    if item_id is None:
        return jsonify({"ok": False, "error": "Missing id"}), 400
    student_queue.complete(item_id, data.get("result"))
    return jsonify({"ok": True})


@bp.route("/queue/fail", methods=["POST"])
@require_auth
def api_queue_fail():
    data = request.get_json(silent=True) or {}
    item_id = data.get("id")
    if item_id is None:
        return jsonify({"ok": False, "error": "Missing id"}), 400
    student_queue.fail(item_id, data.get("result"))
    return jsonify({"ok": True})


@bp.route("/queue/reset", methods=["POST"])
@require_auth
def api_queue_reset():
    data = request.get_json(silent=True) or {}
    item_id = data.get("id")
    if item_id is None:
        return jsonify({"ok": False, "error": "Missing id"}), 400
    student_queue.reset(item_id)
    return jsonify({"ok": True})


@bp.route("/queue")
@require_auth
def api_queue_list():
    status = request.args.get("status")
    if status:
        items = db.get_queue(status=status)
    else:
        items = student_queue.all
    return jsonify({"ok": True, "items": items, "summary": student_queue.summary()})


@bp.route("/queue/clear", methods=["POST"])
@require_auth
def api_queue_clear():
    student_queue.clear()
    return jsonify({"ok": True})


# ── Slot pre-check ──

@bp.route("/slots/check", methods=["POST"])
@require_auth
def api_slots_check():
    data = request.get_json(silent=True) or {}
    students = data.get("students", [])
    if not students:
        students = _get_loaded_students()
    if not students:
        return jsonify({"ok": False, "error": "No students provided or loaded"}), 400

    results = []
    for s in students:
        name = s.get("name", "?")
        try:
            r = bot.check_slot_availability(s, logging.getLogger("slot_check"))
        except Exception as exc:
            r = {"available": False, "slots_found": 0, "message": f"Error: {exc}", "details": []}
        r["name"] = name
        results.append(r)

    any_available = any(r.get("available") for r in results)
    return jsonify({"ok": True, "results": results, "any_available": any_available})


# ── Form scanner (pre-flight) ──

@bp.route("/form/scan", methods=["POST"])
@require_auth
def api_form_scan():
    data = request.get_json(silent=True) or {}
    students = data.get("students", [])
    if not students:
        students = _get_loaded_students()
    if not students:
        return jsonify({"ok": False, "error": "No students provided or loaded"}), 400

    student = dict(students[0])
    name = student.get("name", "?")
    if data.get("email"):
        student["email"] = data["email"]
    if data.get("password"):
        student["password"] = data["password"]

    try:
        cookies = None
        raw_cookies = db.get_state("goethe_cookies", "")
        if raw_cookies:
            try:
                cookies = json.loads(raw_cookies)
            except Exception:
                pass
        r = bot.scan_booking_form(student, logging.getLogger("form_scan"), cookies=cookies)
        r["name"] = name
        return jsonify({"ok": True, "result": r})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Goethe Cookies (for form scanner) ──
import json

@bp.route("/goethe-cookies", methods=["GET", "POST", "DELETE"])
@require_auth
def api_goethe_cookies():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        cookies = data.get("cookies", [])
        if not cookies:
            return jsonify({"ok": False, "error": "No cookies provided"}), 400
        db.set_state("goethe_cookies", json.dumps(cookies))
        return jsonify({"ok": True, "count": len(cookies)})
    elif request.method == "DELETE":
        db.delete_state("goethe_cookies")
        return jsonify({"ok": True})
    else:
        raw = db.get_state("goethe_cookies", "")
        if not raw:
            return jsonify({"ok": False, "has_cookies": False, "count": 0})
        try:
            cookies = json.loads(raw)
            return jsonify({"ok": True, "has_cookies": True, "count": len(cookies)})
        except Exception:
            return jsonify({"ok": False, "has_cookies": False, "count": 0})


# ── Booking history ──

@bp.route("/history")
@require_auth
def api_booking_history():
    limit = int(request.args.get("limit", 100))
    return jsonify({"ok": True, "items": db.get_booking_history(limit)})


@bp.route("/history/search")
@require_auth
def api_booking_search():
    q = request.args.get("q", "")
    limit = int(request.args.get("limit", 100))
    if not q.strip():
        return jsonify({"ok": False, "error": "Missing query param 'q'"}), 400
    return jsonify({"ok": True, "results": db.search_logs(q, limit)})


# ── Database-backed endpoints ──

@bp.route("/db/students")
@require_auth
def api_db_students():
    return jsonify(db.get_students())


@bp.route("/db/logs")
@require_auth
def api_db_logs():
    student_key = request.args.get("student_key", "")
    limit = int(request.args.get("limit", 200))
    return jsonify(db.get_logs(student_key or None, limit))


def _get_loaded_students() -> List[Dict]:
    global config_path
    students = []
    if config_path and Path(config_path).exists():
        try:
            for s in bot.load_all_students(config_path):
                s.setdefault("id", None)
                students.append(s)
        except Exception:
            pass
    try:
        db_students = db.get_students()
        for s in db_students:
            raw_pw = s.get("password", "")
            students.append({
                "id": s.get("id"),
                "name": s.get("name", ""),
                "email": s.get("email", ""),
                "password": crypto_utils.decrypt_password(raw_pw, FERNET_KEY),
                "level": s.get("level", ""),
                "city": s.get("city", ""),
                "booking_datetime": s.get("booking_datetime", ""),
                "status": s.get("status", "pending"),
            })
    except Exception:
        pass
    try:
        import google_sheets
        sheet_students = google_sheets.load_sheet_data()
        seen_keys = {(s.get("name",""), s.get("level",""), s.get("city","")) for s in students}
        for s in sheet_students:
            key = (s.get("name",""), s.get("level",""), s.get("city",""))
            if key not in seen_keys:
                s.setdefault("status", "pending")
                s["id"] = None
                students.append(s)
                seen_keys.add(key)
    except Exception:
        pass
    for i, s in enumerate(students):
        sid = s.get("id")
        if sid is None or sid == "" or sid == 0:
            s["id"] = -(i + 1)
    return sorted(students, key=lambda s: s.get("booking_datetime", "") or "9999-12-31T23:59")


# ── Alexa AI Assistant ──

def _alexa_context() -> dict:
    return {
        "students": _get_loaded_students(),
        "running": bot_running,
        "status": {k: v for k, v in student_status.items()},
        "_actions": {
            "stop": lambda: bot_stop_event.set(),
            "retry": lambda student: _retry_one_student(student),
        },
    }

def _retry_one_student(student: Dict):
    global bot_stop_event, bot_running, student_status, student_results
    import db
    bot_stop_event.clear()
    key = _student_key(student)
    student_logger = setup_web_logger(f"retry_{student.get('name', '?')}")
    student_logger.info("Manual retry for %s", key)
    result = bot.smart_retry(student, use_headless=True, logger=student_logger, stop_event=bot_stop_event, immediate=True)
    student_results.append(result)
    db.update_student_status(key, result.get("status", "failed"), result)
    status = result.get("status", "failed")
    if status == "confirmed":
        student_status[key] = {"status": "Confirmed!", "color": "success", "details": f"Ref: {result.get('reference', 'N/A')}"}
    elif status == "submitted":
        student_status[key] = {"status": "Submitted", "color": "success", "details": "Form submitted"}
    elif status == "stopped":
        student_status[key] = {"status": "Stopped", "color": "danger", "details": "Cancelled by user"}
    elif status == "failed":
        student_status[key] = {"status": "Failed", "color": "danger", "details": "Error occurred"}
    else:
        student_status[key] = {"status": status, "color": "warning", "details": ""}

if GEMINI_API_KEY:
    alexa_assistant = alexa.AlexaAssistant(api_key=GEMINI_API_KEY, context_provider=_alexa_context)
else:
    alexa_assistant = None


@bp.route("/chat", methods=["POST"])
@validate(ChatRequest)
def api_chat(data: ChatRequest):
    if not alexa_assistant:
        return jsonify({"ok": False, "error": "Alexa is not configured. Set GEMINI_API_KEY environment variable."}), 400
    try:
        reply = alexa_assistant.process(data.message, data.history)
        return jsonify({"ok": True, "reply": reply})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


app.register_blueprint(bp, url_prefix="/api", name="api")
app.register_blueprint(bp, url_prefix="/api/v1", name="api_v1")


# ── Telegram commander bridge functions ──

def start_bot_from_telegram():
    global bot_thread, bot_running, student_status, student_results
    if bot_running:
        if telegram_commander:
            telegram_commander.send("Bot is already running")
        return
    students = _get_loaded_students()
    if not students:
        if telegram_commander:
            telegram_commander.send("No students loaded. Upload a CSV config first.")
        return
    student_status.clear()
    student_results.clear()
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break
    bot_thread = threading.Thread(target=run_students_web, args=(students, True), daemon=True)
    bot_thread.start()
    if telegram_commander:
        telegram_commander.send(f"Bot started for {len(students)} student(s) (headless)")


def load_config_csv(csv_path: str) -> bool:
    global config_path
    if not Path(csv_path).exists():
        return False
    try:
        students = bot.load_all_students(csv_path)
        if not students:
            return False
        project_dst = str(PROJECT_DIR / "config.csv")
        import shutil
        shutil.copy2(csv_path, project_dst)
        config_path = project_dst
        return True
    except Exception:
        return False


def stop_all():
    global bot_stop_event
    bot_stop_event.set()


def check_slot(level: str, city: str) -> bool:
    try:
        sched = bot.get_schedule()
        for entry in sched:
            if isinstance(entry, dict):
                if entry.get("level", "").upper() == level.upper() and entry.get("city", "").lower() == city.lower():
                    return True
            else:
                if getattr(entry, "level", "").upper() == level.upper() and getattr(entry, "city", "").lower() == city.lower():
                    return True
        return False
    except Exception:
        return False


def restart_bot():
    global bot_running, bot_thread
    if bot_running:
        stop_all()
        wait_start = time.time()
        while bot_running and time.time() - wait_start < 30:
            time.sleep(1)
    start_bot_from_telegram()


def _make_bounded_server(host: str, port: int, max_threads: int):
    """Capped threaded WSGI server.

    Flask's built-in ``app.run(threaded=True)`` spawns an unbounded thread per
    request. On Railway's 512MB Hobby container, health checks + SSE clients +
    websocket clients together blow past the container's thread limit, and every
    new request then dies with ``RuntimeError: can't start new thread`` — which
    is also why the booking worker thread silently never starts (no logs).

    This server caps concurrent request threads via a semaphore: excess
    connections wait in the backlog instead of creating a thread each.
    """
    import socketserver
    from werkzeug.serving import ThreadedWSGIServer

    class _BoundedThreadedWSGIServer(ThreadedWSGIServer):
        daemon_threads = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._slots = threading.BoundedSemaphore(max_threads)

        def process_request(self, request, client_address):
            self._slots.acquire()
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._slots.release()

    return _BoundedThreadedWSGIServer(host, port, app)


def main():
    global config_path
    default_cfg = PROJECT_DIR / "config.csv"
    if default_cfg.exists():
        config_path = str(default_cfg)

    global telegram_commander
    if telegram_token and telegram_chat_id:
        telegram_commander = TelegramCommander(telegram_token, telegram_chat_id, sys.modules[__name__])
        telegram_commander.start()
        print("  Telegram Commander: ACTIVE")
    else:
        print("  Telegram Commander: DISABLED (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")

    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")

    public_url = f"http://localhost:{port}" if host == "0.0.0.0" else f"http://{host}:{port}"

    print("=" * 55)
    print("  Goethe Booking Bot - Web Control Panel")
    print()
    print(f"  Local:   http://localhost:{port}")
    print(f"  Network: http://{host}:{port}")
    print()
    print("  Use ngrok for a public URL:")
    print("    ngrok http http://localhost:{port}")
    print()
    print("  Deploy on Railway / Fly.io / Render:")
    print("    Set PORT env to your port (default 5000)")
    print("=" * 55)
    print("  Press Ctrl+C to stop the server")

    max_threads = int(os.environ.get("MAX_SERVER_THREADS", "16"))
    _make_bounded_server(host, port, max_threads).serve_forever()


if __name__ == "__main__":
    main()
