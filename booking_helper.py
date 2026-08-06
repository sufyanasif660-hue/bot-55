#!/usr/bin/env python3
"""
Goethe Exam Booking Bot - Fully Automated
==========================================
Extends https://github.com/alyankabir17/A1_Bot with:
- Multi-level support (A1, A2, B1)
- Full login automation (CAS)
- Registration form fill + submit
- Multi-student parallel execution
- Telegram notifications
- Screenshot capture on confirmation

Usage:
  python booking_helper.py --config config.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import random
import re
import string
import sys
import time
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None  # type: ignore[assignment]
    HAS_CURL_CFFI = False
from selenium import webdriver
try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False
from selector_fallbacks import (
    find_element_fallback,
    find_elements_fallback,
    wait_for_any_selector,
)
from proxy_rotator import ProxyRotator
from confirmation_parser import parse_confirmation_text, parse_confirmation_url, summarize as summarize_confirmation
from circuit_breaker import CircuitBreaker
from selenium.common.exceptions import (
    NoSuchElementException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait, Select
from webdriver_manager.chrome import ChromeDriverManager

try:
    from plyer import notification as plyer_notify
except Exception:
    plyer_notify = None

import db
import notifications

# ── Exam level → page URL mapping ──
EXAM_URLS = {
    "A1": os.environ.get("MOCK_A1_URL", "https://www.goethe.de/ins/pk/en/spr/prf/gzsd1.cfm"),
    "A2": os.environ.get("MOCK_A2_URL", "https://www.goethe.de/ins/pk/en/spr/prf/gzsd2.cfm"),
    "B1": os.environ.get("MOCK_B1_URL", "https://www.goethe.de/ins/pk/en/spr/prf/gzb1.cfm"),
}

# ── REST API pre-check config ──
API_BASE = "https://www.goethe.de/rest/examfinder/exams/institute/O%2010000366"
API_REFERERS = {
    "A1": "https://www.goethe.de/ins/pk/en/spr/prf/gzsd1.cfm",
    "A2": "https://www.goethe.de/ins/pk/en/spr/prf/gzsd2.cfm",
    "B1": "https://www.goethe.de/ins/pk/en/spr/prf/gzb1.cfm",
}
API_BASE_PARAMS = {
    "countryIsoCode": "pk", "count": "20", "start": "1",
    "langId": "1", "timezone": "47",
}
API_LEVEL_PARAMS = {
    "A1": {"category": "E004", "type": "ER"},
    "A2": {"category": "E005", "type": "ER"},
    "B1": {"category": "E006", "type": "ER"},
}

# ── Polling configuration ──
DEFAULT_POLL_INTERVAL = 10
MIN_HUMAN_DELAY = 0.3
MAX_HUMAN_DELAY = 1.0

BURST_BEFORE_SECONDS = 10
BURST_AFTER_SECONDS = 600
BURST_PRE_POLL = 2.0
BURST_POST_POLL_MIN = 1.0
BURST_POST_POLL_MAX = 2.0
BURST_CRASH_RETRY = 1.0

# Configurable polling jitter (env overrides)
_BASE_POLL = float(os.environ.get("POLL_INTERVAL", "10"))
_JITTER_MAX = float(os.environ.get("POLL_JITTER", "5"))
DEFAULT_POLL_INTERVAL = _BASE_POLL + random.uniform(0, _JITTER_MAX)

# ── Scheduled booking hours — only poll aggressively during these ──
ACTIVE_HOURS_START = int(os.environ.get("ACTIVE_HOURS_START", "7"))   # 7 AM PKT
ACTIVE_HOURS_END = int(os.environ.get("ACTIVE_HOURS_END", "20"))      # 8 PM PKT
SLEEP_POLL_INTERVAL = int(os.environ.get("SLEEP_POLL_INTERVAL", "300"))  # 5 min during quiet hours

# ── Proxy rotation ──
PROXY_LIST = [p.strip() for p in os.environ.get("PROXY_LIST", "").split(",") if p.strip()]
PROXY_ROTATOR = ProxyRotator(logger=logging.getLogger("proxy_rotator"))
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
VIEWPORTS = [(1920, 1080), (1366, 768), (1536, 864), (1440, 900), (1280, 720)]
CAPTCHA_API_KEY = os.environ.get("CAPTCHA_API_KEY", "")
MAX_SMART_RETRIES = int(os.environ.get("MAX_SMART_RETRIES", "2"))
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "10"))
CIRCUIT_BREAKER_COOLDOWN = int(os.environ.get("CIRCUIT_BREAKER_COOLDOWN", "900"))


CIRCUIT_BREAKER = CircuitBreaker(threshold=CIRCUIT_BREAKER_THRESHOLD, cooldown=CIRCUIT_BREAKER_COOLDOWN)


def _classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "503" in msg or "block" in msg or "captcha" in msg or "rate limit" in msg or "429" in msg:
        return "block"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "generic"

# ── Exam schedule (Pakistan 2026) ──
EXAM_SCHEDULE = [
    {"level": "A1", "city": "Islamabad", "exam_date": "18-19 Jul 2026", "registration_period": "26 Jun - 17 Jul 2026", "bookable_from": "26.06.2026", "price_full": "PKR 25,000", "price_reduced": "PKR 16,500", "status": "upcoming"},
    {"level": "A1", "city": "Lahore", "exam_date": "24 Jul 2026", "registration_period": "3 Jul - 23 Jul 2026", "bookable_from": "03.07.2026", "price_full": "PKR 25,000", "price_reduced": "PKR 16,500", "status": "upcoming"},
    {"level": "A1", "city": "Karachi", "exam_date": "31 Jul - 1 Aug 2026", "registration_period": "17 Jul - 30 Jul 2026", "bookable_from": "17.07.2026", "price_full": "PKR 25,000", "price_reduced": "PKR 16,500", "status": "upcoming"},
    {"level": "A1", "city": "Lahore", "exam_date": "21 Aug 2026", "registration_period": "7 Aug - 20 Aug 2026", "bookable_from": "07.08.2026", "price_full": "PKR 25,000", "price_reduced": "PKR 16,500", "status": "upcoming"},
    {"level": "A2", "city": "Karachi", "exam_date": "3-4 Jul 2026", "registration_period": "19 Jun - 2 Jul 2026", "bookable_from": "19.06.2026", "price_full": "PKR 25,000", "price_reduced": "PKR 16,500", "status": "upcoming"},
    {"level": "A2", "city": "Islamabad", "exam_date": "18-19 Jul 2026", "registration_period": "26 Jun - 17 Jul 2026", "bookable_from": "26.06.2026", "price_full": "PKR 25,000", "price_reduced": "PKR 16,500", "status": "upcoming"},
    {"level": "A2", "city": "Lahore", "exam_date": "25-26 Jul 2026", "registration_period": "3 Jul - 23 Jul 2026", "bookable_from": "03.07.2026", "price_full": "PKR 25,000", "price_reduced": "PKR 16,500", "status": "upcoming"},
    {"level": "B1", "city": "Lahore", "exam_date": "20-21 Jun 2026", "registration_period": "5 Jun - 18 Jun 2026", "bookable_from": "fully_booked", "price_full": "PKR 30,000", "price_reduced": "PKR 25,000", "status": "fully_booked"},
    {"level": "B1", "city": "Karachi", "exam_date": "3-4 Jul 2026", "registration_period": "19 Jun - 2 Jul 2026", "bookable_from": "19.06.2026", "price_full": "PKR 30,000", "price_reduced": "PKR 25,000", "status": "upcoming"},
    {"level": "B1", "city": "Islamabad", "exam_date": "18-19 Jul 2026", "registration_period": "26 Jun - 17 Jul 2026", "bookable_from": "26.06.2026", "price_full": "PKR 30,000", "price_reduced": "PKR 25,000", "status": "upcoming"},
    {"level": "B1", "city": "Lahore", "exam_date": "25-26 Jul 2026", "registration_period": "3 Jul - 23 Jul 2026", "bookable_from": "03.07.2026", "price_full": "PKR 30,000", "price_reduced": "PKR 25,000", "status": "upcoming"},
    {"level": "B1", "city": "Karachi", "exam_date": "31 Jul - 1 Aug 2026", "registration_period": "17 Jul - 29 Jul 2026", "bookable_from": "17.07.2026", "price_full": "PKR 30,000", "price_reduced": "PKR 25,000", "status": "upcoming"},
    {"level": "B1", "city": "Lahore", "exam_date": "22-23 Aug 2026", "registration_period": "7 Aug - 20 Aug 2026", "bookable_from": "07.08.2026", "price_full": "PKR 30,000", "price_reduced": "PKR 25,000", "status": "upcoming"},
]


def get_schedule() -> list:
    return EXAM_SCHEDULE

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def setup_logger(name: str = "booking_helper") -> logging.Logger:
    log_name = f"booking_helper_{dt.date.today().isoformat()}.log"
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    file_handler = logging.FileHandler(log_name, encoding="utf-8")
    file_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(file_fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    stream_handler.setFormatter(stream_fmt)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.info("Logging initialized. File: %s", log_name)
    return logger


def parse_bool(value: str) -> bool:
    v = str(value).strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Goethe Exam Booking Bot")
    parser.add_argument("--config", default="config.csv", help="Path to config.csv")
    parser.add_argument("--headless", type=parse_bool, default=False, help="Run Chrome headless")
    parser.add_argument("--telegram-token", default="", help="Telegram bot token")
    parser.add_argument("--telegram-chat-id", default="", help="Telegram chat ID")
    return parser.parse_args()


def load_all_students(path: str) -> List[Dict[str, str]]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    if config_path.suffix.lower() != ".csv":
        raise ValueError("Config must be .csv")

    with config_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            data = {k.strip(): str(v).strip() for k, v in row.items()}
            if data.get("name") or data.get("email"):
                rows.append(data)
        if not rows:
            raise ValueError("No student rows found in CSV")
    _validate_students(rows)
    return rows


VALID_LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}
VALID_CITIES = {"Karachi", "Lahore", "Islamabad"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_students(students: List[Dict]) -> None:
    errors = []
    for i, s in enumerate(students):
        idx = i + 1
        name = s.get("name", "").strip()
        email = s.get("email", "").strip()
        level = s.get("level", s.get("exam_level", "")).strip().upper()
        city = s.get("city", "").strip()
        dob = s.get("dob", "").strip()
        bdt = s.get("booking_datetime", "").strip()

        if not name:
            errors.append(f"Row {idx}: name is required")
        if not email:
            errors.append(f"Row {idx}: email is required")
        elif not _EMAIL_RE.match(email):
            errors.append(f"Row {idx}: invalid email '{email}'")
        if level and level not in VALID_LEVELS:
            errors.append(f"Row {idx}: invalid level '{level}' (valid: A1-C2)")
        if city and city not in VALID_CITIES:
            errors.append(f"Row {idx}: invalid city '{city}' (valid: Karachi, Lahore, Islamabad)")
        if dob:
            parts = dob.replace("-", ".").replace("/", ".").split(".")
            if len(parts) != 3 or not all(p.isdigit() for p in parts):
                errors.append(f"Row {idx}: invalid DOB format '{dob}' (use DD.MM.YYYY or DD/MM/YYYY)")
        if bdt:
            try:
                dt.datetime.fromisoformat(bdt)
            except ValueError:
                errors.append(f"Row {idx}: invalid booking_datetime '{bdt}' (use ISO format YYYY-MM-DDTHH:MM)")

    if errors:
        raise ValueError("Config validation failed:\n" + "\n".join(errors))


def parse_exam_time_str(raw: str) -> Optional[dt.datetime]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        # Tolerate 12-hour AM/PM times (e.g. "2026-07-03T12:16 PM" or "12:16 PM")
        # that the Fetch-Dates UI historically produced — ISO parse can't read them.
        if re.search(r"\b[AP]M\b", raw, re.I):
            norm = raw.replace("T", " ").strip()
            for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M:%S %p"):
                try:
                    return dt.datetime.strptime(norm, fmt)
                except ValueError:
                    pass
            today = dt.date.today().isoformat()
            return dt.datetime.strptime(f"{today} {norm}", "%Y-%m-%d %I:%M %p")
        if "T" in raw or "-" in raw:
            return dt.datetime.fromisoformat(raw)
        today = dt.date.today().isoformat()
        return dt.datetime.fromisoformat(f"{today}T{raw}")
    except ValueError:
        raise ValueError(f"Invalid date format: '{raw}' — expected format like 2026-07-17T10:00 or DD.MM.YYYY HH:MM")


def random_human_delay(min_sec: float = MIN_HUMAN_DELAY, max_sec: float = MAX_HUMAN_DELAY) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


def send_telegram(message: str, logger: logging.Logger) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram notification sent")
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


def notify(title: str, message: str, logger: logging.Logger) -> None:
    logger.info("NOTIFY: %s - %s", title, message)
    if plyer_notify is not None:
        try:
            plyer_notify.notify(title=title, message=message, timeout=8)
        except Exception as exc:
            logger.warning("Desktop notification failed: %s", exc)
    notifications.notify_all(title, message, logger)


_driver_counter = 0

def _apply_stealth(driver: webdriver.Chrome) -> None:
    """Apply CDP-based stealth patches to avoid detection."""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
                Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 1});
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'pdfViewerEnabled', {get: () => false});
            """
        })
    except Exception:
        pass


def _parse_proxy(proxy: str):
    """Return (host, port, user, pass) from a proxy string. Supports
    http://user:pass@host:port, user:pass@host:port, host:port."""
    import urllib.parse as _up
    s = proxy if "://" in proxy else "http://" + proxy
    p = _up.urlparse(s)
    return p.hostname, p.port, (p.username or ""), (p.password or "")


def create_driver(use_headless: bool, logger: logging.Logger, proxy: Optional[str] = None, profile_name: str = "default") -> webdriver.Chrome:
    global _driver_counter
    _driver_counter += 1
    options = Options()

    # ── Browser fingerprint randomization ──
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    options.add_argument(f"--user-agent={ua}")

    # ── Platform-specific setup ──
    if os.name != "nt":
        for chrome_bin in ["/opt/google/chrome/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]:
            if Path(chrome_bin).exists():
                options.binary_location = chrome_bin
                break
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "/dev/null"
        os.environ["DISPLAY"] = ":99"
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-default-apps")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")

    # ── Memory optimization for Railway 512MB ──
    options.add_argument("--process-per-site")
    options.add_argument("--no-zygote")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-component-extensions-with-background-pages")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-features=VizDisplayCompositor,TranslateUI,ChromeWhatsNewUI,InterestFeedContentSuggestions,OptimizationHints")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--log-level=3")
    options.add_argument("--silent-debugger-extension-api")
    options.add_argument("--disable-search-engine-choice-screen")

    if use_headless or os.name != "nt":
        options.add_argument("--headless=new")
    options.add_argument(f"--window-size={min(vp[0],1280)},{min(vp[1],720)}")

    # ── Proxy ──
    if proxy:
        _phost, _pport, _puser, _ppass = _parse_proxy(proxy)
        if _puser and _ppass and _phost and _pport:
            # Chrome --proxy-server ignores user:pass and MV2 auth-extensions are
            # dead on Chrome 127+. Route through a localhost forwarder that adds
            # Basic auth to the upstream proxy (tunnels HTTPS via CONNECT, no MITM).
            from proxy_auth_forward import start_auth_forwarder
            _lport = start_auth_forwarder(_phost, _pport, _puser, _ppass)
            options.add_argument(f"--proxy-server=http://127.0.0.1:{_lport}")
            logger.info("Auth proxy via local forwarder 127.0.0.1:%s -> %s:%s", _lport, _phost, _pport)
        else:
            options.add_argument(f"--proxy-server={proxy}")
            logger.info("Using proxy: %s", proxy)
    options.add_argument("--disable-extensions")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US,en")
    options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', profile_name)[:64]
    profile_dir = Path.home() / "goethe-bot-profiles" / safe_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")

    max_attempts = 3
    last_exc = None

    # ── undetected-chromedriver path (stealth) ──
    # Skippable via DISABLE_UC=1 — uc is buggy on some Chrome/Windows combos
    # (fails, spawns zombie Chrome, locks the profile). Local runs from a clean
    # residential IP don't need uc's stealth, so plain Selenium is more reliable.
    _uc_disabled = os.environ.get("DISABLE_UC", "").lower() in ("1", "true", "yes")
    if HAS_UC and not _uc_disabled:
        for attempt in range(1, max_attempts + 1):
            try:
                driver = uc.Chrome(options=options, version_main=None, use_subprocess=True)
                _apply_stealth(driver)
                logger.info("Driver created (undetected-chromedriver): UA=%s, VP=%sx%s%s", ua[:50], vp[0], vp[1], f", proxy={proxy}" if proxy else "")
                return driver
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts:
                    logger.warning("uc.Chrome attempt %d/%d failed: %s. Retrying in 5s...", attempt, max_attempts, exc)
                    time.sleep(5)
                else:
                    logger.warning("undetected-chromedriver failed, falling back to standard selenium: %s", exc)

    # ── Standard selenium fallback ──
    if os.name != "nt":
        system_driver = next((p for p in ["/usr/local/bin/chromedriver", "/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"] if Path(p).exists()), None)
        if system_driver:
            service = Service(system_driver)
        else:
            # Empty Service() → Selenium Manager downloads the chromedriver that
            # MATCHES the installed Chrome. webdriver_manager grabbed the latest
            # (151) vs Railway's Chrome 150 → "chromedriver unexpectedly exited -5".
            service = Service()
    else:
        service = Service(ChromeDriverManager().install())
        service.creation_flags = 0

    for attempt in range(1, max_attempts + 1):
        try:
            driver = webdriver.Chrome(service=service, options=options)
            _apply_stealth(driver)
            break
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                logger.warning("Chrome launch attempt %d/%d failed: %s. Retrying in 5s...", attempt, max_attempts, exc)
                time.sleep(5)
            else:
                raise

    logger.info("Driver created: UA=%s, VP=%sx%s%s", ua[:50], vp[0], vp[1], f", proxy={proxy}" if proxy else "")
    return driver


def is_blocked_response(driver: webdriver.Chrome) -> bool:
    title = (driver.title or "").lower()
    if any(t in title for t in ["429", "503", "too many requests", "access denied", "attention required"]):
        return True
    try:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        body = ""
    strong = ["checking your browser", "verify you are human", "just a moment", "ray id"]
    return any(p in body for p in strong)


def bounded_backoff(attempt: int, base: int = 3, cap: int = 60) -> int:
    return min(cap, int(base * (2 ** max(0, attempt - 1))))


def long_cooldown() -> int:
    return random.randint(120, 300)


def wait_for_finder(driver: webdriver.Chrome, timeout: int = 40) -> WebElement:
    result = wait_for_any_selector(driver, "finder_container", timeout=timeout)
    if result is None:
        raise TimeoutException("Exam finder container did not load.")
    return result


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def looks_clickable(button: WebElement) -> bool:
    if not button.is_displayed():
        return False
    if not button.is_enabled():
        return False
    cls = normalize_text(button.get_attribute("class") or "")
    if any(x in cls for x in ["disabled", "nicht-buchbar", "gray", "grey"]):
        return False
    aria = normalize_text(button.get_attribute("aria-disabled") or "")
    if aria in {"true", "1"}:
        return False
    return True


def find_book_buttons(driver: webdriver.Chrome) -> List[WebElement]:
    buttons = find_elements_fallback(driver, "book_button", timeout=10)
    if not buttons:
        fallback = driver.find_elements(By.CSS_SELECTOR, "a.standard, button.standard")
        text_filtered = []
        for item in fallback:
            txt = normalize_text(item.text)
            if any(t in txt for t in ["book", "next", "buchen", "weiter", "select", "module"]):
                text_filtered.append(item)
        buttons = text_filtered
    if not buttons:
        all_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'book') or contains(@href, 'buchen') or contains(@href, 'anmelden')]")
        text_filtered = [a for a in all_links if looks_clickable(a)]
        buttons = text_filtered
    return [b for b in buttons if looks_clickable(b)]


def button_row_text(button: WebElement) -> str:
    try:
        row = button.find_element(By.XPATH, "ancestor::*[self::tr or self::li or self::div][1]")
        return normalize_text(row.text)
    except Exception:
        return normalize_text(button.text)


def pick_preferred_button(buttons: Sequence[WebElement], preferred_city: str) -> Optional[WebElement]:
    if not buttons:
        return None
    pref = normalize_text(preferred_city)
    if pref:
        for b in buttons:
            if pref in button_row_text(b):
                return b
    return buttons[0]


def human_move_and_click(driver: webdriver.Chrome, element: WebElement) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    try:
        driver.execute_script("arguments[0].removeAttribute('target');", element)
    except Exception:
        pass
    random_human_delay()
    actions = ActionChains(driver)
    actions.move_to_element(element)
    actions.pause(random.uniform(0.05, 0.2))
    actions.move_by_offset(random.randint(-2, 2), random.randint(-2, 2))
    actions.pause(random.uniform(0.03, 0.1))
    actions.click()
    actions.perform()


# ── Advanced human behavior simulation ──

def random_scroll(driver: webdriver.Chrome):
    """Small random scroll to mimic reading."""
    delta = random.randint(-150, 300)
    driver.execute_script(f"window.scrollBy(0, {delta});")
    time.sleep(random.uniform(0.3, 1.2))


def random_mouse_wander(driver: webdriver.Chrome):
    """Move mouse to a random location on the page."""
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        w = body.size.get("width", 800)
        h = body.size.get("height", 600)
        ox = random.randint(0, max(w - 100, 100))
        oy = random.randint(0, max(h - 100, 100))
        actions = ActionChains(driver)
        actions.move_by_offset(ox, oy)
        actions.pause(random.uniform(0.1, 0.4))
        actions.perform()
    except Exception:
        pass


def human_pause_between_fields():
    """Pause as if user is reading the next field."""
    time.sleep(random.uniform(0.4, 1.8))


def simulate_human_typing(element: WebElement, text: str) -> None:
    """Type text with realistic human-like bursts and pauses."""
    element.clear()
    element.click()
    for ch in text:
        element.send_keys(ch)
        if random.random() < 0.08:
            time.sleep(random.uniform(0.3, 0.9))
        elif random.random() < 0.03:
            time.sleep(random.uniform(1.0, 2.5))
        else:
            time.sleep(random.uniform(0.04, 0.15))


# ── CAPTCHA solving (2Captcha) ──

def detect_captcha(driver: webdriver.Chrome) -> Optional[str]:
    """Check if a CAPTCHA iframe, element, or v3 site key is on the page."""
    captcha_selectors = [
        "iframe[src*='recaptcha']", "iframe[src*='captcha']",
        ".g-recaptcha", "#recaptcha", "[class*='captcha']",
        "iframe[title*='captcha']",
    ]
    for css in captcha_selectors:
        els = driver.find_elements(By.CSS_SELECTOR, css)
        if els:
            return css
    # Check for reCAPTCHA v3 (invisible) — look for api.js?render= in scripts
    try:
        scripts = driver.find_elements(By.TAG_NAME, "script")
        for s in scripts:
            src = s.get_attribute("src") or ""
            if "recaptcha/api.js" in src:
                return "recaptcha_v3"
    except Exception:
        pass
    # Also check ___grecaptcha_cfg in JS
    try:
        has_v3 = driver.execute_script(
            "return typeof ___grecaptcha_cfg !== 'undefined' && "
            "___grecaptcha_cfg && ___grecaptcha_cfg.clients && "
            "Object.keys(___grecaptcha_cfg.clients).length > 0"
        )
        if has_v3:
            return "recaptcha_v3"
    except Exception:
        pass
    return None


def solve_captcha(driver: webdriver.Chrome, logger: logging.Logger) -> bool:
    """Solve reCAPTCHA v2 or v3 using 2Captcha."""
    if not CAPTCHA_API_KEY:
        logger.warning("CAPTCHA detected but no CAPTCHA_API_KEY set")
        return False
    try:
        page_url = driver.current_url

        # Try to find site key from v3 config or v2 element
        site_key = driver.execute_script("""
            try {
                var cfg = ___grecaptcha_cfg;
                if (cfg && cfg.clients) {
                    for (var k in cfg.clients) {
                        var c = cfg.clients[k];
                        if (c && c.sitekey) return c.sitekey;
                    }
                }
            } catch(e) {}
            try {
                var el = document.querySelector('.g-recaptcha');
                if (el) return el.getAttribute('data-sitekey');
            } catch(e) {}
            return null;
        """)
        if not site_key:
            logger.warning("No reCAPTCHA site key found on page")
            return False
        logger.info("reCAPTCHA site key found: %s", site_key)

        # Detect if v3 (invisible) — check for render=explicit or render=sitekey in scripts
        is_v3 = driver.execute_script("""
            try {
                var scripts = document.querySelectorAll('script[src*=\"recaptcha/api.js\"]');
                for (var i = 0; i < scripts.length; i++) {
                    var src = scripts[i].getAttribute('src') || '';
                    if (src.indexOf('render=') !== -1) return true;
                }
            } catch(e) {}
            return false;
        """) or False

        payload = {
            "key": CAPTCHA_API_KEY, "method": "userrecaptcha",
            "googlekey": site_key, "pageurl": page_url,
            "json": 1,
        }
        if is_v3:
            payload["version"] = "v3"
            payload["action"] = "verify"
            payload["min_score"] = "0.5"
            logger.info("2Captcha: solving reCAPTCHA v3 (invisible)")

        resp = requests.post("https://2captcha.com/in.php", data=payload, timeout=30)
        data = resp.json()
        if data.get("status") != 1:
            logger.warning("2Captcha send failed: %s", data)
            return False
        captcha_id = data["request"]

        for _ in range(60):
            time.sleep(5)
            result = requests.get("https://2captcha.com/res.php", params={
                "key": CAPTCHA_API_KEY, "action": "get", "id": captcha_id, "json": 1,
            }, timeout=15).json()
            if result.get("status") == 1:
                token = result["request"]
                if is_v3:
                    driver.execute_script(
                        f"document.querySelector('textarea#g-recaptcha-response')?.remove();"
                    )
                    ta = driver.execute_script("""
                        var ta = document.createElement('textarea');
                        ta.id = 'g-recaptcha-response';
                        ta.style.display = 'none';
                        document.body.appendChild(ta);
                        return ta;
                    """)
                    driver.execute_script(f"arguments[0].value = '{token}';", ta)
                    for key in ['g-recaptcha-response', 'g-recaptcha-response-data']:
                        driver.execute_script(f"""
                            try {{ window['{key}'] = '{token}'; }} catch(e) {{}}
                        """)
                else:
                    driver.execute_script(
                        f"document.getElementById('g-recaptcha-response').innerHTML='{token}';"
                    )
                    driver.execute_script(f"___grecaptcha_cfg.clients[0].callback('{token}');")
                logger.info("reCAPTCHA solved (%s)", "v3" if is_v3 else "v2")
                return True
            if result.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                logger.warning("reCAPTCHA unsolvable")
                return False
        logger.warning("reCAPTCHA timeout")
        return False
    except Exception as exc:
        logger.warning("reCAPTCHA solve error: %s", exc)
        return False


# ── Proxy support in create_driver ──
# Proxy is applied inside create_driver when a proxy string is passed

# ── Smart retry wrapper ──

def smart_retry(student: Dict[str, str], use_headless: bool, logger: logging.Logger,
                stop_event: threading.Event, attempt: int = 1,
                immediate: bool = False) -> Dict[str, str]:
    """Run student flow with smart retry + exponential backoff + jitter.
    Classifies transient vs permanent failures; gives up earlier on the latter."""
    result = run_student_flow(student, use_headless, logger, stop_event, immediate=immediate)
    status = result.get("status", "failed")
    error_msg = result.get("error", "").lower() if result else ""

    if status in ("failed", "error") and attempt <= MAX_SMART_RETRIES:
        is_transient = any(kw in error_msg for kw in [
            "timeout", "connection", "temporary", "unavailable",
            "gateway", "bad gateway", "service unavailable",
        ])
        retries_left = MAX_SMART_RETRIES if is_transient else 1
        if attempt > retries_left:
            logger.warning("Not retrying %s — permanent error: %s",
                           student.get("name", "?"), error_msg[:80])
            return result

        logger.warning("Smart retry %d/%d for %s (transient=%s)",
                       attempt, MAX_SMART_RETRIES, student.get("name", "?"), is_transient)
        if not CIRCUIT_BREAKER.wait_until_allowed(poll=5.0, stop_event=stop_event):
            result["status"] = "stopped"
            return result

        base_wait = 30 if is_transient else 60
        delay = random.uniform(base_wait, base_wait * 1.5) * min(attempt, 3)
        logger.info("Backoff: waiting %.1fs before retry", delay)
        for _ in range(int(delay)):
            if stop_event.is_set():
                result["status"] = "stopped"
                return result
            time.sleep(1)

        result = smart_retry(student, use_headless, logger, stop_event, attempt + 1, immediate=immediate)
    return result


# ── Slot pre-check ──

def check_slot_availability(student: Dict[str, str], logger: logging.Logger) -> Dict:
    """Quick pre-check: open the exam page and report available slots.
    Returns {available: bool, slots_found: int, message: str, details: List[Dict]}."""
    result = {"available": False, "slots_found": 0, "message": "", "details": []}
    driver = None
    try:
        from bs4 import BeautifulSoup
        pname = f"{student.get('name','')}_{student.get('level','')}_{student.get('city','')}"
        driver = create_driver(use_headless=True, logger=logger, profile_name=pname)
        exam_url = get_exam_url(student.get("level", student.get("exam_level", "A1")))
        logger.info("Slot pre-check: loading %s", exam_url)
        driver.get(exam_url)
        wait_for_document_ready(driver)
        time.sleep(3)
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import NoSuchElementException, TimeoutException
        try:
            close_btn = driver.find_element(By.CSS_SELECTOR, "button.close, .modal-close, [data-dismiss='modal']")
            close_btn.click()
            time.sleep(1)
        except (NoSuchElementException, TimeoutException):
            pass
        page = driver.page_source
        soup = BeautifulSoup(page, "html.parser")
        buttons = soup.find_all("button", string=re.compile(r"book\s*now", re.I))
        links = soup.find_all("a", string=re.compile(r"book\s*now", re.I))
        slots = buttons + links
        if not slots:
            spans = soup.find_all("span", class_=re.compile(r"book|slot|available", re.I))
            slots = [s for s in spans if "book" in s.get_text(strip=True).lower()]
        if slots:
            result["available"] = True
            result["slots_found"] = len(slots)
            result["message"] = f"Found {len(slots)} bookable slot(s) for {student.get('name', '?')}"
        else:
            result["message"] = f"No bookable slots detected for {student.get('name', '?')}"
        result["details"] = [{"text": s.get_text(strip=True)[:100]} for s in slots[:10]]
    except Exception as exc:
        logger.warning("Slot pre-check failed: %s", exc)
        result["message"] = f"Pre-check error: {exc}"
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return result


def check_slot_via_api(level: str, logger: logging.Logger) -> Dict:
    """REST API pre-check for exam availability.
    Uses curl_cffi TLS fingerprinting to bypass Akamai.
    Returns {available, slots_found, message, exams, api_ok} or
    {api_ok: False, message, fallback: True} on failure."""
    result = {"available": False, "slots_found": 0, "message": "", "exams": [], "api_ok": False}
    if not HAS_CURL_CFFI:
        result["message"] = "curl_cffi not installed — skipping API pre-check"
        return result
    try:
        level = level.upper().strip()
        level_key = level if level in API_LEVEL_PARAMS else "B1"
        level_params = API_LEVEL_PARAMS[level_key]
        level_referer = API_REFERERS.get(level_key, API_REFERERS["B1"])
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": level_referer,
            "Origin": "https://www.goethe.de",
        }
        sess = curl_requests.Session()
        sess.headers.update(headers)
        sess.get(level_referer, impersonate="chrome", timeout=15)

        params = {**API_BASE_PARAMS, **level_params}
        resp = sess.get(API_BASE, params=params, impersonate="chrome", timeout=15)
        ct = (resp.headers.get("Content-Type") or "").lower()
        if resp.status_code != 200 or "application/json" not in ct:
            logger.debug("API pre-check: status=%d ct=%s body=%s", resp.status_code, ct, resp.text[:120])
            result["message"] = f"API returned {resp.status_code} (not JSON)"
            return result

        data = resp.json()
        exams = data.get("exams") or data.get("data") or data.get("results") or (data if isinstance(data, list) else [data])
        if not isinstance(exams, list):
            exams = [exams]

        bookable = []
        for ex in exams:
            if not isinstance(ex, dict):
                continue
            txt = (ex.get("availabilityText") or "").lower()
            disabled = ex.get("disabled") or ex.get("availabilityState") == "disabled"
            if not disabled and ("select" in txt or "book" in txt or "buchen" in txt or "next" in txt):
                bookable.append(ex)

        result["api_ok"] = True
        result["exams"] = bookable
        if bookable:
            result["available"] = True
            result["slots_found"] = len(bookable)
            names = [ex.get("courselevelShortcut", "") or ex.get("level", "") for ex in bookable]
            locs = [ex.get("locationName", "") or ex.get("city", "") for ex in bookable]
            result["message"] = f"API: {len(bookable)} bookable — {' '.join(f'{n}@{l}' for n, l in zip(names, locs))}"
        else:
            result["message"] = f"API: no bookable slots (found {len(exams)} total exams)"
        return result

    except Exception as exc:
        logger.debug("API pre-check error: %s", exc)
        result["message"] = f"API error: {exc}"
        return result


# ── Form scanner (pre-flight check) ──

def scan_booking_form(student: Dict[str, str], logger: logging.Logger, cookies: Optional[List[Dict]] = None) -> Dict:
    """Login to Goethe, open the booking form, and scan all form fields.
    Compares found fields against selector_fallbacks known keys.
    Returns {ok: bool, fields: List[Dict], known_keys_found: int, known_keys_total: int, message: str}."""
    result = {"ok": False, "fields": [], "known_keys_found": 0, "known_keys_total": 0, "message": ""}
    driver = None
    try:
        from selector_fallbacks import ELEMENT_SELECTORS
        pname = f"{student.get('name','')}_{student.get('level','')}_{student.get('city','')}"
        _scan_proxy = None
        try:
            _scan_proxy = PROXY_ROTATOR.get()
        except Exception:
            _scan_proxy = None
        driver = create_driver(use_headless=True, logger=logger, proxy=_scan_proxy, profile_name=pname)
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import NoSuchElementException

        # Try saved cookies first (from parameter or DB)
        logged_in = False
        if not cookies:
            raw = db.get_state("goethe_cookies", "")
            if raw:
                try:
                    cookies = json.loads(raw)
                    logger.info("Loaded %d cookies from DB for scan_booking_form", len(cookies))
                except Exception:
                    pass
        if cookies:
            driver.get("https://login.goethe.de/cas/login")
            wait_for_document_ready(driver)
            time.sleep(1)
            for c in cookies:
                try:
                    driver.add_cookie(c)
                except Exception:
                    pass
            driver.get("https://login.goethe.de/cas/login")
            wait_for_document_ready(driver)
            time.sleep(2)
            if "login" not in driver.current_url.lower():
                logged_in = True
                logger.info("✓ Logged in using saved cookies")

        if not logged_in:
            driver.get("https://login.goethe.de/cas/login")
            wait_for_document_ready(driver)
            time.sleep(2)
            ok = login_to_goethe(driver, student.get("email", ""), student.get("password", ""), logger)
            if not ok:
                result["message"] = "Login failed: " + get_last_login_error()
                return result

        exam_url = get_exam_url(student.get("level", student.get("exam_level", "A1")))
        logger.info("Form scanner: navigating to %s", exam_url)
        driver.get(exam_url)
        wait_for_document_ready(driver)
        time.sleep(5)

        try:
            close_btn = driver.find_element(By.CSS_SELECTOR, "button.close, .modal-close, [data-dismiss='modal']")
            close_btn.click()
            time.sleep(1)
        except NoSuchElementException:
            pass

        try:
            finder_container = driver.find_element(By.CSS_SELECTOR, "#pr_finder_9523459, .pr-finder, [class*='finder']")
            finder_container.click()
            time.sleep(3)
        except NoSuchElementException:
            pass

        try:
            book_btn = driver.find_element(By.XPATH, "//a[contains(text(),'Book') or contains(text(),'book')]")
            book_btn.click()
            time.sleep(3)
        except NoSuchElementException:
            pass

        continue_btn = None
        for sel in ["a.standard", "button.standard", ".btn-primary", "[class*='continue']"]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    txt = el.text.strip().lower()
                    if txt in ("continue", "weiter"):
                        continue_btn = el
                        break
            except NoSuchElementException:
                pass
        if continue_btn:
            continue_btn.click()
            time.sleep(3)

        book_for_self = None
        for sel in ["a.standard", "button.standard", ".btn-primary", "[class*='book']"]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    txt = el.text.strip().lower()
                    if "myself" in txt or "selbst" in txt or "buchen" in txt:
                        book_for_self = el
                        break
            except NoSuchElementException:
                pass
        if book_for_self:
            book_for_self.click()
            time.sleep(3)

        script = """
            var fields = [];
            var els = document.querySelectorAll('input:not([type=hidden]):not([type=submit]), select, textarea');
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                var rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    fields.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        placeholder: el.placeholder || '',
                        class: el.className || '',
                        label: (function(){var l;try{l=document.querySelector('label[for=\\\"'+el.id+'\\\"]')}catch(e){}return l?l.innerText.trim():''})(),
                        visible: rect.top > -100 && rect.top < window.innerHeight,
                        value: el.value || '',
                        options: (el.tagName.toLowerCase() === 'select')
                            ? Array.prototype.map.call(el.options, function(o){return o.text.trim();})
                            : []
                    });
                }
            }
            return fields;
        """
        fields = driver.execute_script(script)

        known_keys = ELEMENT_SELECTORS.keys()
        found_keys = set()
        for f in fields:
            for key in known_keys:
                if any(f.get("name") in sel[1] or f.get("id") in sel[1] for sel in ELEMENT_SELECTORS[key]):
                    found_keys.add(key)

        result["fields"] = fields
        # Surface every dropdown's options (motivation, phone prefix, DOB, etc.)
        result["dropdowns"] = {
            (f.get("name") or f.get("id") or f.get("label") or "select"): f.get("options")
            for f in fields if f.get("tag") == "select" and f.get("options")
        }
        result["known_keys_found"] = len(found_keys)
        result["known_keys_total"] = len(known_keys)
        missing = set(known_keys) - found_keys
        result["missing_keys"] = sorted(missing)
        result["ok"] = True
        result["message"] = f"Scanned {len(fields)} form fields, {len(found_keys)}/{len(known_keys)} known selectors matched"
        logger.info("Form scan complete: %s", result["message"])
    except Exception as exc:
        logger.warning("Form scan failed: %s", exc)
        result["message"] = f"Scan error: {exc}"
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return result


# ── Scheduled mode ──

SCHEDULED_CHECK_INTERVAL = 30  # seconds


def is_active_hours() -> bool:
    """Check if current time is within configured active booking hours."""
    now_hour = dt.datetime.now().hour
    if ACTIVE_HOURS_START <= ACTIVE_HOURS_END:
        return ACTIVE_HOURS_START <= now_hour < ACTIVE_HOURS_END
    else:
        return now_hour >= ACTIVE_HOURS_START or now_hour < ACTIVE_HOURS_END

def scheduled_wait(booking_time_str: str, logger: logging.Logger, stop_event: threading.Event) -> bool:
    """Wait until the booking time arrives. Returns True if it's time."""
    if not booking_time_str:
        return True
    try:
        target = parse_exam_time_str(booking_time_str)
    except Exception as exc:
        logger.warning("Invalid booking_datetime '%s' — %s. Starting immediately.", booking_time_str, exc)
        return True

    logger.info("Scheduled mode: waiting until %s", target.strftime("%Y-%m-%d %H:%M:%S"))
    while not stop_event.is_set():
        now = dt.datetime.now()
        if now >= target:
            logger.info("Scheduled time reached, starting bot")
            return True
        remaining = (target - now).total_seconds()
        if remaining <= 10:
            logger.info("Less than 10s to go, entering burst mode")
            time.sleep(0.5)
        else:
            gap = min(SCHEDULED_CHECK_INTERVAL, remaining / 2)
            for _ in range(int(gap)):
                if stop_event.is_set():
                    return False
                time.sleep(1)
    return False


def enforce_single_tab(driver: webdriver.Chrome) -> None:
    handles = driver.window_handles
    if len(handles) <= 1:
        return
    keep = handles[-1]
    for h in handles:
        if h == keep:
            continue
        try:
            driver.switch_to.window(h)
            driver.close()
        except Exception:
            pass
    driver.switch_to.window(keep)


def wait_for_document_ready(driver: webdriver.Chrome, timeout: int = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        state = driver.execute_script("return document.readyState")
        if state == "complete":
            return
        time.sleep(0.5)
    raise TimeoutException("Document not ready in time.")


def wait_and_find(driver: webdriver.Chrome, css_selector: str, timeout: int = 15) -> Optional[WebElement]:
    try:
        wait = WebDriverWait(driver, timeout)
        return wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, css_selector)))
    except (TimeoutException, NoSuchElementException):
        return None


def type_slowly(element: WebElement, text: str) -> None:
    element.clear()
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.01, 0.05))


def click_continue_button(driver: webdriver.Chrome, logger: logging.Logger, timeout: int = 90) -> None:
    random_human_delay(0.3, 0.8)
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            wait_for_document_ready(driver, timeout=timeout)
            button = find_element_fallback(driver, "continue_button", timeout=timeout, logger=logger)
            if button is None:
                raise NoSuchElementException("Continue button not found with any selector")
            logger.info("'Continue' button found. Clicking...")
            human_move_and_click(driver, button)
            return
        except StaleElementReferenceException:
            if attempt < max_attempts:
                logger.warning("Stale element on Continue button, retrying %d/%d", attempt, max_attempts)
                time.sleep(2)
            else:
                raise


def click_book_for_myself(driver: webdriver.Chrome, logger: logging.Logger, timeout: int = 90) -> None:
    random_human_delay(0.3, 0.8)
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            wait_for_document_ready(driver, timeout=timeout)
            button = find_element_fallback(driver, "book_for_myself", timeout=timeout, logger=logger)
            if button is None:
                raise NoSuchElementException("Book for myself button not found with any selector")
            logger.info("'Book for myself' button found. Clicking...")
            human_move_and_click(driver, button)
            return
        except StaleElementReferenceException:
            if attempt < max_attempts:
                logger.warning("Stale element on Book for Myself button, retrying %d/%d", attempt, max_attempts)
                time.sleep(2)
            else:
                raise


def _load_saved_cookies(driver: webdriver.Chrome, logger: logging.Logger) -> bool:
    """Try to load saved Goethe cookies. Returns True if still on login page (need fresh login)."""
    raw = db.get_state("goethe_cookies", "")
    if not raw:
        logger.info("No saved cookies found — need fresh login")
        return False
    try:
        cookies = json.loads(raw)
        if not cookies:
            return False
        logger.info("Found %d saved cookies — attempting injection", len(cookies))
        driver.get("https://login.goethe.de/cas/login")
        wait_for_document_ready(driver)
        time.sleep(1)
        for c in cookies:
            try:
                driver.add_cookie(c)
            except Exception:
                pass
        driver.get("https://login.goethe.de/cas/login")
        wait_for_document_ready(driver)
        time.sleep(2)
        if "login" not in driver.current_url.lower() and "cas/login" not in driver.current_url.lower():
            logger.info("✓ Logged in using saved cookies!")
            return True
        logger.info("Cookies present but login page still showing — they may have expired")
        return False
    except Exception as exc:
        logger.warning("Cookie injection error: %s", exc)
        return False


def _save_session_cookies(driver: webdriver.Chrome, logger: logging.Logger) -> None:
    """Capture current session cookies and save to DB for future reuse."""
    try:
        cookies = driver.get_cookies()
        if cookies:
            db.set_state("goethe_cookies", json.dumps(cookies))
            logger.info("✓ Saved %d session cookies for future reuse", len(cookies))
    except Exception as exc:
        logger.warning("Failed to save cookies: %s", exc)


def login_to_goethe(driver: webdriver.Chrome, email: str, password: str, logger: logging.Logger) -> bool:
    logger.info("══ STEP 4: Logging in to My Goethe.de ══")
    # Try saved cookies first
    if _load_saved_cookies(driver, logger):
        return True
    for attempt in range(1, 4):
        try:
            if _login_attempt(driver, email, password, logger):
                _save_session_cookies(driver, logger)
                return True
            if attempt < 3:
                logger.warning("Login attempt %d failed, reloading page and retrying...", attempt)
                driver.get("https://login.goethe.de/cas/login")
                wait_for_document_ready(driver, timeout=30)
                time.sleep(2)
        except StaleElementReferenceException:
            if attempt < 3:
                logger.warning("Stale element during login, retrying %d/3", attempt)
                time.sleep(2)
                driver.get("https://login.goethe.de/cas/login")
                wait_for_document_ready(driver, timeout=30)
            else:
                logger.error("Login failed after 3 attempts due to stale elements")
                return False
    return False

_last_login_error = ""

def get_last_login_error() -> str:
    return _last_login_error

def _accept_cookie_consent(driver: webdriver.Chrome, logger: Optional[logging.Logger] = None) -> None:
    """Actually ACCEPT the Usercentrics consent (not just hide it).

    The CAS login page has no reCAPTCHA; it uses Usercentrics, whose uc-block
    script can gate interactions until consent is given. Hiding the banner does
    NOT give consent — we must call the UC API / click the accept button (which
    lives in a shadow root, so DOM selectors alone can't reach it).
    """
    try:
        driver.execute_script("""
            try {
              if (window.UC_UI && UC_UI.acceptAllConsents) {
                UC_UI.acceptAllConsents().then(function(){ if (UC_UI.closeCUI) UC_UI.closeCUI(); }).catch(function(){});
              }
            } catch (e) {}
            try {
              var root = document.querySelector('#usercentrics-root');
              if (root && root.shadowRoot) {
                var b = root.shadowRoot.querySelector('[data-testid="uc-accept-all-button"], button[data-testid*="accept"], [aria-label*="Accept All"]');
                if (b) b.click();
              }
            } catch (e) {}
        """)
        time.sleep(1.2)
    except Exception as exc:
        if logger:
            logger.debug("consent accept error: %s", exc)
    # Hide any leftover banner so it never overlaps the form.
    try:
        for sel in ["#usercentrics-root", ".uc-banner", ".cookie-consent", "[aria-label*='cookie']"]:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    driver.execute_script("arguments[0].style.display='none'", el)
    except Exception:
        pass


def _dismiss_cookie_consent(driver: webdriver.Chrome) -> None:
    # Back-compat wrapper — now accepts consent instead of only hiding it.
    _accept_cookie_consent(driver)

def _login_attempt(driver: webdriver.Chrome, email: str, password: str, logger: logging.Logger) -> bool:
    global _last_login_error
    try:
        wait_for_document_ready(driver, timeout=30)
        _dismiss_cookie_consent(driver)
        random_human_delay()

        email_input = find_element_fallback(driver, "login_email", timeout=15, logger=logger)
        if email_input is None:
            _last_login_error = "Email field not found on login page"
            logger.warning(_last_login_error)
            logger.info("Page URL: %s", driver.current_url)
            logger.info("Page title: %s", driver.title)
            body = driver.find_element(By.TAG_NAME, "body").text[:500]
            logger.info("Page body preview: %s", body)
            return False

        type_slowly(email_input, email)
        random_human_delay()

        pwd_input = find_element_fallback(driver, "login_password", timeout=10, logger=logger)
        if pwd_input is None:
            _last_login_error = "Password field not found on login page"
            logger.warning(_last_login_error)
            return False
        type_slowly(pwd_input, password)
        random_human_delay()

        try:
            checkbox = find_element_fallback(driver, "login_checkbox_stay", timeout=5)
            if checkbox and checkbox.is_displayed():
                human_move_and_click(driver, checkbox)
        except (NoSuchElementException, TimeoutException):
            pass

        # reCAPTCHA fallback: if a captcha is present and CAPTCHA_API_KEY is set,
        # solve it via 2Captcha before submitting. No-op (logs a warning) when no
        # key is configured, so this is safe when relying on a clean-IP/proxy path.
        try:
            if detect_captcha(driver):
                logger.info("CAPTCHA detected on login page — attempting 2Captcha solve")
                if not solve_captcha(driver, logger):
                    logger.warning("CAPTCHA present but not solved (no CAPTCHA_API_KEY or solve failed)")
        except Exception as cap_exc:
            logger.warning("CAPTCHA handling error: %s", cap_exc)

        submit_btn = find_element_fallback(driver, "login_submit", timeout=10, logger=logger)
        if submit_btn is None:
            _last_login_error = "Submit button not found on login page"
            logger.warning(_last_login_error)
            return False
        logger.info("Clicking login submit button...")
        try:
            human_move_and_click(driver, submit_btn)
        except Exception:
            driver.execute_script("arguments[0].click();", submit_btn)

        wait_for_document_ready(driver, timeout=30)
        random_human_delay()

        current_url = driver.current_url.lower()
        logger.info("Post-login URL: %s", current_url)
        if "login" in current_url or "cas/login" in current_url:
            error_el = driver.find_elements(By.CSS_SELECTOR, ".error, .alert, .message-error, .errortext")
            err_text = ""
            for el in error_el:
                if el.is_displayed():
                    err_text = el.text.strip()
                    break
            if err_text:
                _last_login_error = "Login error: " + err_text[:200]
            else:
                _last_login_error = "Still on login page — no visible error. Page title: " + driver.title
            logger.warning(_last_login_error)
            return False

        logger.info("★ LOGIN SUCCESSFUL")
        return True

    except NoSuchElementException as exc:
        _last_login_error = f"Login failed - element not found: {exc}"
        logger.error(_last_login_error)
        logger.info("Current URL: %s", driver.current_url)
        try:
            page_html = driver.page_source
            with open("debug_login.html", "w", encoding="utf-8") as f:
                f.write(page_html)
            logger.info("Saved to debug_login.html")
        except Exception:
            pass
        return False
    except Exception as exc:
        _last_login_error = f"Login error: {exc}"
        logger.exception(_last_login_error)
        return False


def fill_registration_form(driver: webdriver.Chrome, student: Dict[str, str], logger: logging.Logger) -> bool:
    logger.info("══ STEP 5: Filling registration form ══")
    for attempt in range(1, 4):
        try:
            return _fill_attempt(driver, student, logger)
        except StaleElementReferenceException:
            if attempt < 3:
                logger.warning("Stale element during form fill, retrying %d/3", attempt)
                time.sleep(2)
            else:
                logger.error("Form fill failed after 3 attempts due to stale elements")
                return False

def _fill_attempt(driver: webdriver.Chrome, student: Dict[str, str], logger: logging.Logger) -> bool:
    logger.info("══ STEP 5: Filling registration form ══")
    try:
        wait_for_document_ready(driver, timeout=30)
        random_human_delay(0.5, 1.0)

        logger.info("Current URL after login: %s", driver.current_url)
        logger.info("Page title: %s", driver.title)

        fields = {
            "form_name": student.get("name", ""),
            "form_dob": student.get("dob", ""),
            "form_place_of_birth": student.get("place_of_birth", ""),
            "form_phone": student.get("phone", ""),
        }

        for selector_key, value in fields.items():
            if not value:
                continue
            try:
                el = find_element_fallback(driver, selector_key, timeout=5, logger=logger)
                if el and el.is_displayed():
                    tag = el.tag_name.lower()
                    if tag == "select":
                        Select(el).select_by_visible_text(value)
                    else:
                        type_slowly(el, value)
                    random_human_delay(0.1, 0.2)
                    logger.info("Filled %s = %s", selector_key, value[:30])
            except (NoSuchElementException, TimeoutException):
                logger.debug("Field not found: %s", selector_key)
                continue

        try:
            address = student.get("address", "")
            if address:
                addr_el = find_element_fallback(driver, "form_address", timeout=5)
                if addr_el and addr_el.is_displayed():
                    type_slowly(addr_el, address)
                    random_human_delay()
        except (NoSuchElementException, TimeoutException):
            pass

        try:
            terms = find_elements_fallback(driver, "form_terms", timeout=5)
            for cb in terms:
                if cb.is_displayed() and not cb.is_selected():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
                    driver.execute_script("arguments[0].click();", cb)
                    random_human_delay()
                    break
        except (NoSuchElementException, TimeoutException):
            pass

        try:
            level_val = student.get("level", student.get("exam_level", ""))
            if level_val:
                level_el = find_element_fallback(driver, "form_level", timeout=5)
                if level_el and level_el.is_displayed():
                    Select(level_el).select_by_visible_text(level_val)
                    random_human_delay()
        except (NoSuchElementException, TimeoutException):
            logger.debug("Level dropdown not found")

        random_human_delay(0.3, 0.8)

        submit_btn = find_element_fallback(driver, "form_submit", timeout=10, logger=logger)

        if submit_btn is None:
            for fallback_selector in [
                "button.btn-primary, button.btn, button.standard",
                "input[type='submit'], button[type='submit']",
            ]:
                try:
                    btns = driver.find_elements(By.CSS_SELECTOR, fallback_selector)
                    for btn in btns:
                        if btn.is_displayed() and btn.is_enabled():
                            submit_btn = btn
                            break
                    if submit_btn:
                        break
                except Exception:
                    continue

        if submit_btn is None:
            all_buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in all_buttons:
                txt = normalize_text(btn.text)
                if any(x in txt for x in ["submit", "book", "register", "confirm", "send", "absenden", "buchen", "bestätigen"]):
                    if btn.is_displayed() and btn.is_enabled():
                        submit_btn = btn
                        break

        if submit_btn:
            logger.info("Clicking submit button...")
            human_move_and_click(driver, submit_btn)
            wait_for_document_ready(driver, timeout=30)
            random_human_delay()
            logger.info("Form submitted. Post-submit URL: %s", driver.current_url)
            return True
        else:
            logger.warning("Submit button not found! Dumping page for debugging.")
            save_failure_evidence(driver, "debug_no_submit")
            return False
    except Exception as exc:
        logger.exception("Form fill error: %s", exc)
        return False


def save_failure_evidence(driver: webdriver.Chrome, label: str,
                          driver_available: bool = True) -> None:
    """Save screenshot + page source for debugging failures.

    label is used as prefix (e.g. 'step1', 'error'). Files are written
    to the current directory with timestamp. Safe to call when
    driver is None or has crashed — set driver_available=False to skip.
    """
    if not driver_available or driver is None:
        return
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        driver.save_screenshot(f"{label}_{timestamp}.png")
    except Exception:
        pass
    try:
        html = driver.page_source
        with open(f"{label}_{timestamp}.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass


def capture_confirmation(driver: webdriver.Chrome, student_name: str, logger: logging.Logger) -> Dict[str, str]:
    logger.info("══ STEP 6: Capturing confirmation ══")
    result = {"name": student_name, "status": "unknown", "url": driver.current_url}

    try:
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^a-zA-Z0-9]", "_", student_name)
        screenshot_path = f"confirmation_{safe_name}_{timestamp}.png"
        driver.save_screenshot(screenshot_path)
        logger.info("Screenshot saved: %s", screenshot_path)
        result["screenshot"] = screenshot_path
    except Exception as exc:
        logger.warning("Screenshot failed: %s", exc)

    try:
        body = driver.find_element(By.TAG_NAME, "body").text
        parsed = parse_confirmation_text(body)
        url_info = parse_confirmation_url(driver.current_url)

        if parsed["reference"]:
            result["reference"] = parsed["reference"]
            logger.info("Booking reference found: %s", result["reference"])
        if parsed["exam_date"]:
            result["exam_date"] = parsed["exam_date"]
        if parsed["exam_time"]:
            result["exam_time"] = parsed["exam_time"]
        if parsed["exam_level"]:
            result["exam_level"] = parsed["exam_level"]
        if parsed["exam_city"]:
            result["exam_city"] = parsed["exam_city"]
        if url_info.get("booking_reference_url"):
            result["url_reference"] = url_info["booking_reference_url"]

        result["status"] = parsed.get("status", "unknown")
        logger.info("Confirmation parsed: %s", summarize_confirmation(parsed))
    except Exception as exc:
        logger.warning("Error reading confirmation page: %s", exc)

    return result


def checkpoint_all_running_students() -> int:
    """Save checkpoint for all in-progress students. Called on shutdown."""
    import db as _db
    students = _db.get_students()
    running = [s for s in students if s.get("status") in ("running", "in_progress")]
    for s in running:
        key = f"{s['name']}|{s.get('level', s.get('exam_level', ''))}|{s['city']}"
        _db.save_checkpoint(key, 1)
    return len(running)


def get_exam_url(level: str) -> str:
    level = level.upper().strip()
    return EXAM_URLS.get(level, EXAM_URLS["A1"])


def _is_wicket_page(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    if "wicket" in url or "coesession" in url or "coe/options" in url:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "high traffic" in body or "session" in body or "timeout" in body or "expired" in body:
            return True
    return False


def _is_cas_login_page(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    return "login.goethe.de" in url or "cas/login" in url


def _handle_cas_login_if_needed(driver: webdriver.Chrome, student: Dict[str, str],
                                 logger: logging.Logger) -> bool:
    url = driver.current_url.lower()
    if "login.goethe.de" not in url and "cas/login" not in url:
        # Not on login page — save cookies for future reuse
        _save_session_cookies(driver, logger)
        return True
    logger.info("CAS login page detected — attempting login")
    email = student.get("email", "")
    password = student.get("password", "")
    if not email or not password:
        logger.warning("No credentials for CAS login — aborting")
        return False
    return login_to_goethe(driver, email, password, logger)


def _click_continue_wizard(driver: webdriver.Chrome, logger: logging.Logger, timeout: int = 30) -> bool:
    try:
        btn = find_element_fallback(driver, "continue_button", timeout=timeout, logger=logger)
        if btn is None:
            btns = driver.find_elements(By.CSS_SELECTOR, "button.btn-primary, button.primary, button[type='submit']")
            for b in btns:
                if b.is_displayed() and b.is_enabled():
                    btn = b
                    break
        if btn is None:
            btns = driver.find_elements(By.TAG_NAME, "button")
            for b in btns:
                txt = normalize_text(b.text)
                if any(x in txt for x in ["continue", "weiter", "next", "submit", "save", "speichern"]):
                    if b.is_displayed() and b.is_enabled():
                        btn = b
                        break
        if btn is None:
            logger.warning("No Continue button found")
            return False
        logger.info("Clicking Continue button in wizard")
        human_move_and_click(driver, btn)
        wait_for_document_ready(driver, timeout=timeout)
        random_human_delay(0.3, 0.8)
        return True
    except Exception as exc:
        logger.warning("_click_continue_wizard error: %s", exc)
        return False


def _fill_text_input(driver: webdriver.Chrome, selectors: List[str], value: str,
                     logger: logging.Logger, timeout: int = 5) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            el = find_element_fallback(driver, sel, timeout=timeout, logger=logger)
            if el and el.is_displayed():
                simulate_human_typing(el, value)
                return True
        except (NoSuchElementException, TimeoutException):
            continue
    return False


def _fill_select_by_visible(driver: webdriver.Chrome, selectors: List[str], value: str,
                             logger: logging.Logger, timeout: int = 5) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            el = find_element_fallback(driver, sel, timeout=timeout, logger=logger)
            if el and el.is_displayed() and el.tag_name.lower() == "select":
                Select(el).select_by_visible_text(value)
                return True
        except (NoSuchElementException, TimeoutException):
            continue
    return False


def _select_dropdown_first_valid(driver: webdriver.Chrome, sel_key: str, value: str,
                                 logger: logging.Logger, timeout: int = 5) -> bool:
    """Select `value` (exact match, then partial). If empty or no match, pick the
    first real (non-placeholder) option so a required dropdown never stalls the
    wizard. Used for motivation where the exact option text is unknown."""
    el = find_element_fallback(driver, sel_key, timeout=timeout, logger=logger)
    if not (el and el.is_displayed() and el.tag_name.lower() == "select"):
        return False
    sel = Select(el)
    opts = sel.options
    v = (value or "").strip().lower()
    if v:
        for i, o in enumerate(opts):
            if o.text.strip().lower() == v:
                sel.select_by_index(i)
                logger.info("Dropdown %s: exact '%s'", sel_key, o.text.strip())
                return True
        for i, o in enumerate(opts):
            if o.text.strip() and v in o.text.strip().lower():
                sel.select_by_index(i)
                logger.info("Dropdown %s: partial '%s'", sel_key, o.text.strip())
                return True
    _placeholders = ("why are you", "please", "select", "choose", "---", "bitte", "auswähl")
    for i, o in enumerate(opts):
        t = o.text.strip()
        ov = (o.get_attribute("value") or "").strip()
        if t and ov and not any(p in t.lower() for p in _placeholders):
            sel.select_by_index(i)
            logger.info("Dropdown %s: fallback first-valid '%s'", sel_key, t)
            return True
    return False


def _ensure_session(driver: webdriver.Chrome, student: Dict[str, str],
                    logger: logging.Logger, step_label: str = "") -> None:
    """Check if we're on a CAS login page (session expired) and re-login."""
    if _is_cas_login_page(driver):
        logger.warning("Session expired during %s — re-logging in", step_label)
        if not _handle_cas_login_if_needed(driver, student, logger):
            logger.warning("Re-login failed during %s", step_label)


_EXAM_MODULES = ["reading", "listening", "writing", "speaking"]


def _select_modules(driver: webdriver.Chrome, student: Dict[str, str], logger: logging.Logger) -> None:
    """B1 SELECTION page has 4 module checkboxes (Reading/Listening/Writing/Speaking).
    Tick only the student's chosen modules (default: all), then Continue. No-op when
    there are no module checkboxes (A1/A2 = whole exam, nothing to pick)."""
    raw = (student.get("modules", "") or "").strip().lower()
    want = [m for m in _EXAM_MODULES if m in raw] if raw else list(_EXAM_MODULES)
    try:
        found = False
        for m in _EXAM_MODULES:
            cbs = driver.find_elements(
                By.XPATH,
                "//label[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                "'abcdefghijklmnopqrstuvwxyz'),'%s')]//input[@type='checkbox'] | "
                "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                "'abcdefghijklmnopqrstuvwxyz'),'%s')]/preceding::input[@type='checkbox'][1]" % (m, m),
            )
            for cb in cbs:
                if not cb.is_displayed():
                    continue
                found = True
                if (m in want) != cb.is_selected():
                    driver.execute_script("arguments[0].click();", cb)
                break
        if found:
            logger.info("Modules selected: %s", ", ".join(want))
            _click_continue_wizard(driver, logger)
            wait_for_document_ready(driver, timeout=20)
    except Exception as exc:
        logger.debug("Module selection skipped: %s", exc)
    # "Book for me / for my child" intermediate (B1 shows it; harmless elsewhere)
    try:
        b = find_element_fallback(driver, "book_for_myself", timeout=3, logger=logger)
        if b and b.is_displayed():
            human_move_and_click(driver, b)
            wait_for_document_ready(driver, timeout=20)
    except Exception:
        pass


def _fill_step_personal_data_1(driver: webdriver.Chrome, student: Dict[str, str],
                                logger: logging.Logger) -> bool:
    logger.info("══ Wizard Step 1: Personal Data (Name & Birth) ══")
    try:
        _ensure_session(driver, student, logger, "Step 1")
        wait_for_document_ready(driver, timeout=30)
        random_human_delay(0.5, 1.5)

        _fill_text_input(driver, ["first_name"], student.get("first_name", student.get("name", "").split()[0] if student.get("name") else ""), logger)
        parts = student.get("name", "").split()
        surname = student.get("surname", parts[-1] if len(parts) > 1 else "")
        _fill_text_input(driver, ["surname"], surname, logger)

        dob = student.get("dob", "")
        if dob:
            parts = dob.replace("-", "/").replace(".", "/").split("/")
            if len(parts) == 3:
                _fill_select_by_visible(driver, ["dob_day"], parts[0], logger)
                _fill_select_by_visible(driver, ["dob_month"], parts[1], logger)
                _fill_select_by_visible(driver, ["dob_year"], parts[2], logger)

        email_val = student.get("email", "")
        _fill_text_input(driver, ["email_field"], email_val, logger)

        contact_number = student.get("contact_number", student.get("passport_number", ""))
        if contact_number:
            _fill_text_input(driver, ["contact_number"], contact_number, logger)

        if not _click_continue_wizard(driver, logger):
            save_failure_evidence(driver, "debug_step1_no_continue")
            return False
        logger.info("★ Step 1 done")
        return True
    except Exception as exc:
        save_failure_evidence(driver, "debug_step1_error")
        logger.exception("Step 1 error: %s", exc)
        return False


def _fill_step_personal_data_2(driver: webdriver.Chrome, student: Dict[str, str],
                                logger: logging.Logger) -> bool:
    logger.info("══ Wizard Step 2: Personal Data (Address & Motivation) ══")
    try:
        _ensure_session(driver, student, logger, "Step 2")
        wait_for_document_ready(driver, timeout=30)
        random_human_delay(0.5, 1.5)

        _fill_select_by_visible(driver, ["country_dropdown"], student.get("country", "Pakistan"), logger)
        _fill_text_input(driver, ["postal_code"], student.get("postal_code", ""), logger)
        _fill_text_input(driver, ["street_field"], student.get("street", ""), logger)
        _fill_text_input(driver, ["house_number"], student.get("house_number", ""), logger)
        _fill_text_input(driver, ["additional_address"], student.get("additional_address", ""), logger)
        _fill_text_input(driver, ["location_city"], student.get("city", ""), logger)
        _fill_select_by_visible(driver, ["phone_prefix"], student.get("phone_prefix", ""), logger)

        phone = student.get("phone", "")
        if phone:
            _fill_text_input(driver, ["form_phone"], phone, logger)

        _fill_text_input(driver, ["form_place_of_birth"], student.get("place_of_birth", ""), logger)
        _select_dropdown_first_valid(driver, "motivation_dropdown", student.get("motivation", ""), logger)

        if not _click_continue_wizard(driver, logger):
            save_failure_evidence(driver, "debug_step2_no_continue")
            return False
        logger.info("★ Step 2 done")
        return True
    except Exception as exc:
        save_failure_evidence(driver, "debug_step2_error")
        logger.exception("Step 2 error: %s", exc)
        return False


def _fill_step_payment(driver: webdriver.Chrome, student: Dict[str, str],
                        logger: logging.Logger) -> bool:
    logger.info("══ Wizard Step 3: Payment Method ══")
    try:
        _ensure_session(driver, student, logger, "Step 3")
        wait_for_document_ready(driver, timeout=30)
        random_human_delay(0.5, 1.5)

        invoice_el = find_element_fallback(driver, "invoice_option", timeout=10, logger=logger)
        if invoice_el and invoice_el.is_displayed():
            logger.info("Selecting Invoice payment option")
            human_move_and_click(driver, invoice_el)
            random_human_delay(0.3, 0.8)
        else:
            logger.warning("Invoice option not found — trying generic radio/option")
            radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
            for r in radios:
                parent = driver.execute_script("return arguments[0].parentElement.textContent", r)
                if parent and "invoice" in parent.lower():
                    if r.is_displayed() and r.is_enabled():
                        human_move_and_click(driver, r)
                        random_human_delay(0.3, 0.8)
                        break

        if not _click_continue_wizard(driver, logger):
            save_failure_evidence(driver, "debug_step3_no_continue")
            return False
        logger.info("★ Step 3 done")
        return True
    except Exception as exc:
        save_failure_evidence(driver, "debug_step3_error")
        logger.exception("Step 3 error: %s", exc)
        return False


def _fill_step_promo(driver: webdriver.Chrome, student: Dict[str, str],
                      logger: logging.Logger) -> bool:
    logger.info("══ Wizard Step 4: Promotional Code ══")
    try:
        _ensure_session(driver, student, logger, "Step 4")
        wait_for_document_ready(driver, timeout=30)
        random_human_delay(0.5, 1.5)

        promo_val = student.get("promo_code", "")
        if promo_val:
            _fill_text_input(driver, ["promo_code"], promo_val, logger)
            apply_btn = find_element_fallback(driver, "apply_promo", timeout=5, logger=logger)
            if apply_btn and apply_btn.is_displayed():
                human_move_and_click(driver, apply_btn)
                random_human_delay(0.3, 0.8)

        if not _click_continue_wizard(driver, logger):
            save_failure_evidence(driver, "debug_step4_no_continue")
            return False
        logger.info("★ Step 4 done")
        return True
    except Exception as exc:
        save_failure_evidence(driver, "debug_step4_error")
        logger.exception("Step 4 error: %s", exc)
        return False


def _fill_step_review(driver: webdriver.Chrome, student: Dict[str, str],
                       logger: logging.Logger) -> bool:
    logger.info("══ Wizard Step 5: Review & Confirm ══")
    try:
        _ensure_session(driver, student, logger, "Step 5")
        wait_for_document_ready(driver, timeout=30)
        random_human_delay(0.5, 1.5)

        random_scroll(driver)
        random_human_delay(0.3, 0.8)

        confirm_btn = find_element_fallback(driver, "confirm_order", timeout=10, logger=logger)
        if confirm_btn is None:
            btns = driver.find_elements(By.TAG_NAME, "button")
            for b in btns:
                txt = normalize_text(b.text)
                if any(x in txt for x in ["confirm", "order", "submit", "book", "buchen", "bestätigen", "absenden"]):
                    if b.is_displayed() and b.is_enabled():
                        confirm_btn = b
                        break

        if confirm_btn is None:
            logger.warning("No confirm button found on review page")
            save_failure_evidence(driver, "debug_step5_no_confirm")
            return False

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", confirm_btn)
        random_human_delay(0.2, 0.5)
        human_move_and_click(driver, confirm_btn)
        wait_for_document_ready(driver, timeout=30)
        random_human_delay(0.5, 1.0)
        logger.info("★ Step 5 done — booking submitted!")
        return True
    except Exception as exc:
        logger.exception("Step 5 error: %s", exc)
        return False


# ── Profile URLs for post-booking verification ──
PROFILE_URLS = [
    "https://mein.goethe.de/",
    "https://mein.goethe.de/booking",
]


def verify_booking(driver: webdriver.Chrome, student: Dict[str, str],
                   reference: str, logger: logging.Logger) -> Dict[str, str]:
    """Navigate to Goethe profile page and verify the booking appears.

    Tries known profile URLs, searches for the reference number or
    booking indicators. Returns dict with verification status.
    """
    result = {"verified": False, "profile_screenshot": "", "detail": ""}
    name = student.get("name", "Unknown")

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", name)

    for url in PROFILE_URLS:
        try:
            logger.info("Verification: navigating to %s", url)
            driver.get(url)
            wait_for_document_ready(driver, timeout=20)
            random_human_delay(1.0, 2.0)

            if _is_cas_login_page(driver):
                logger.info("Verification: CAS login page at %s — skipping", url)
                result["detail"] = f"CAS login required at {url}"
                continue

            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()

            profile_screenshot = f"profile_{safe_name}_{timestamp}.png"
            try:
                driver.save_screenshot(profile_screenshot)
                result["profile_screenshot"] = profile_screenshot
            except Exception:
                pass

            if reference and reference.lower() in body_text:
                logger.info("✅ Verification PASSED — reference '%s' found in profile", reference)
                result["verified"] = True
                result["detail"] = f"Reference found on {url}"
                return result

            booking_keywords = ["booking", "buchung", "order", "bestellung",
                                "registered", "angemeldet", "exam", "prüfung",
                                "registration", "anmeldung"]
            found_keywords = [kw for kw in booking_keywords if kw in body_text]
            if found_keywords:
                result["detail"] = f"Booking keywords found at {url}: {', '.join(found_keywords)}"
                result["verified"] = True
                return result

            logger.info("Verification: no booking data found at %s", url)

        except Exception as exc:
            logger.warning("Verification error at %s: %s", url, exc)
            result["detail"] = f"Error at {url}: {str(exc)[:100]}"

    if not result.get("detail"):
        result["detail"] = "No profile URL responded with booking data"

    return result


def run_student_flow(student: Dict[str, str], use_headless: bool, logger: logging.Logger,
                     stop_event: threading.Event = None, proxy: Optional[str] = None,
                     immediate: bool = False) -> Dict[str, str]:
    """Execute full booking flow for one student.

    1. Wait until booking_datetime (continuous fast-poll in burst window)
    2. Load exam page → detect "Select modules" button
    3. Click through 5-step Wicket wizard (Personal Data × 2 → Payment → Promo → Review)
    4. Handle CAS login if session expired
    5. Screenshot confirmation + parse reference number
    6. Verify booking on profile page (navigate to mein.goethe.de)
    7. Update DB logs, status, checkpoint at each milestone

    Returns dict with status: booked/failed/stopped/error and details.
    """
    name = student.get("name", "Unknown")
    email = student.get("email", "")
    password = student.get("password", "")
    level = student.get("level", student.get("exam_level", ""))
    city = student.get("city", "Karachi")
    booking_time_str = student.get("booking_datetime", "")

    if stop_event is None:
        stop_event = threading.Event()

    if not immediate:
        if not scheduled_wait(booking_time_str, logger, stop_event):
            return {"name": name, "email": email, "level": level, "city": city, "status": "stopped"}

    assigned_proxy = proxy
    if not assigned_proxy:
        assigned_proxy = PROXY_ROTATOR.get()
        if assigned_proxy:
            logger.info("Assigned proxy: %s", assigned_proxy)

    logger.info("=" * 60)
    logger.info("STARTING STUDENT: %s | %s | %s | %s", name, email, level, city)
    logger.info("=" * 60)
    sk = f"{name}|{level}|{city}"
    db.add_log(sk, level, f"Starting booking — {level} {city}")

    exam_url = get_exam_url(level)
    try:
        exam_dt = parse_exam_time_str(booking_time_str) if booking_time_str else dt.datetime.now()
    except ValueError as exc:
        logger.error("Invalid booking_datetime for %s: %s", name, exc)
        return {"name": name, "email": email, "level": level, "city": city, "status": "failed", "error": str(exc)}

    student_key = f"{name}|{level}|{city}"
    resume_step = db.get_checkpoint(student_key)
    if resume_step > 0:
        logger.info("Resuming from checkpoint step %d", resume_step)

    driver = None
    result = {"name": name, "email": email, "level": level, "city": city, "status": "failed"}

    try:
        pname = f"{name}_{level}_{city}"
        driver = create_driver(use_headless, logger, proxy=assigned_proxy, profile_name=pname)
        logger.info("Monitoring URL: %s", exam_url)
        logger.info("Booking time: %s", booking_time_str)
        logger.info("Burst window: %s to %s",
                    (exam_dt - dt.timedelta(seconds=BURST_BEFORE_SECONDS)).strftime("%H:%M:%S"),
                    (exam_dt + dt.timedelta(seconds=BURST_AFTER_SECONDS)).strftime("%H:%M:%S"))

        step1_done = False
        page_loaded = False
        consecutive_errors = 0
        gap_printed = False

        logger.info("══ STEP 1: Waiting for booking button ('Select modules' / 'Book Now') ══")
        while True:
            if stop_event.is_set():
                logger.warning("Stop requested by user. Aborting.")
                result["status"] = "stopped"
                db.clear_checkpoint(student_key)
                return result

            if not CIRCUIT_BREAKER.allow_request():
                logger.warning("Circuit breaker open — pausing until cooldown expires")
                if not CIRCUIT_BREAKER.wait_until_allowed(poll=5.0, stop_event=stop_event):
                    result["status"] = "stopped"
                    return result
                continue

            burst = exam_dt - dt.timedelta(seconds=BURST_BEFORE_SECONDS) <= dt.datetime.now() <= exam_dt + dt.timedelta(seconds=BURST_AFTER_SECONDS)

            if not burst and not page_loaded:
                api_result = check_slot_via_api(level, logger)
                if api_result.get("api_ok"):
                    if api_result.get("available"):
                        logger.info("API pre-check: %s", api_result.get("message", "slots found!"))
                        db.add_log(sk, level, f"API pre-check: slots available — {api_result['message']}")
                    else:
                        logger.debug("API pre-check: no slots — %s", api_result.get("message", ""))
                        gap = max(15, DEFAULT_POLL_INTERVAL + random.randint(-10, 15))
                        for _ in range(int(gap)):
                            if stop_event.is_set():
                                result["status"] = "stopped"
                                return result
                            time.sleep(1)
                        continue

            try:
                if burst and page_loaded:
                    driver.refresh()
                else:
                    driver.get(exam_url)

                wait_for_document_ready(driver, timeout=15 if burst else 30)
                page_loaded = True

                if not burst and is_blocked_response(driver):
                    CIRCUIT_BREAKER.record_failure("block")
                    cd = long_cooldown()
                    logger.warning("Block detected. Cooling down %ss", cd)
                    for _ in range(cd):
                        if stop_event.is_set():
                            result["status"] = "stopped"
                            return result
                        time.sleep(1)
                    page_loaded = False
                    continue

                try:
                    wait_for_finder(driver, timeout=10 if burst else 40)
                except TimeoutException:
                    if burst:
                        time.sleep(BURST_CRASH_RETRY)
                        continue
                    raise

                if burst:
                    js_btn = driver.execute_script("""
                        var els = document.querySelectorAll('a.standard, button.standard, a.btnGruen, button.btnGruen');
                        for (var i = 0; i < els.length; i++) {
                            var t = (els[i].textContent || '').toLowerCase().trim();
                            if (t.indexOf('select') >= 0 || t.indexOf('book') >= 0 || t.indexOf('buchen') >= 0) {
                                if (els[i].offsetParent !== null && !els[i].disabled) return els[i].outerHTML;
                            }
                        }
                        return null;
                    """)
                    if js_btn:
                        logger.info("JS burst scan found button: %s", js_btn[:200])

                buttons = find_book_buttons(driver)
                logger.info("Found %d clickable button(s).", len(buttons))

                target_button = pick_preferred_button(buttons, city)

                if target_button is None:
                    bookable_text = find_element_fallback(driver, "bookable_from_text", timeout=3)
                    if bookable_text:
                        txt = bookable_text.text.replace("\n", " ").strip()
                        logger.info("Booking not open yet: %s", txt)
                        if not burst:
                            db.add_log(sk, level, f"⏳ No slot — {txt}")

                    now = dt.datetime.now()
                    if burst and now < exam_dt:
                        gap = BURST_PRE_POLL
                    elif burst:
                        gap = random.uniform(BURST_POST_POLL_MIN, BURST_POST_POLL_MAX)
                    elif not is_active_hours():
                        gap = random.uniform(SLEEP_POLL_INTERVAL, SLEEP_POLL_INTERVAL * 1.5)
                        if not gap_printed:
                            logger.info("Outside active hours (%d-%d) — polling every %ds",
                                        ACTIVE_HOURS_START, ACTIVE_HOURS_END, int(gap))
                            gap_printed = True
                    else:
                        gap_printed = False
                        gap = max(20, DEFAULT_POLL_INTERVAL + random.randint(-10, 15))
                    for _ in range(int(gap)):
                        if stop_event.is_set():
                            result["status"] = "stopped"
                            return result
                        time.sleep(1)
                    continue

                logger.info("★ STEP 1 DONE: 'Select modules' found! Clicking...")
                logger.info("Button text: '%s' | class: '%s'", normalize_text(target_button.text), target_button.get_attribute("class") or "")
                db.add_log(sk, level, f"★ Slot found! Clicking '{normalize_text(target_button.text)}' — {level} {city}")
                notify(f"Slot found for {name}", f"Exam: {level} | City: {city}", logger)
                human_move_and_click(driver, target_button)
                enforce_single_tab(driver)
                CIRCUIT_BREAKER.record_success()
                consecutive_errors = 0
                step1_done = True
                db.save_checkpoint(student_key, 1)
                break

            except (TimeoutException, StaleElementReferenceException, NoSuchElementException) as exc:
                consecutive_errors += 1
                gap = BURST_CRASH_RETRY if burst else bounded_backoff(consecutive_errors)
                logger.warning("Selenium error: %s. Retry in %.1fs", exc, gap)
                time.sleep(gap)
                page_loaded = False
            except WebDriverException as exc:
                CIRCUIT_BREAKER.record_failure(_classify_error(exc))
                consecutive_errors += 1
                gap = BURST_CRASH_RETRY * 2 if burst else bounded_backoff(consecutive_errors, base=5, cap=120)
                logger.error("WebDriver error: %s. Retry in %.1fs", exc, gap)
                time.sleep(gap)
                page_loaded = False
            except Exception as exc:
                logger.exception("Unexpected error: %s", exc)
                time.sleep(BURST_CRASH_RETRY)
                page_loaded = False

        if not step1_done:
            raise RuntimeError("Step 1 failed after all retries")

        random_human_delay(0.5, 1.0)
        if stop_event.is_set():
            logger.warning("Stop requested by user. Aborting.")
            result["status"] = "stopped"; return result

        if _is_wicket_page(driver):
            logger.info("High-traffic wicket page detected — refreshing until passed")
            db.add_log(sk, level, "⚠️ High-traffic wicket page — refreshing")
            for _ in range(30):
                if stop_event.is_set():
                    result["status"] = "stopped"; return result
                time.sleep(2)
                driver.refresh()
                wait_for_document_ready(driver, timeout=15)
                if not _is_wicket_page(driver):
                    logger.info("Wicket page cleared")
                    db.add_log(sk, level, "✅ Wicket page cleared")
                    break

        if not _handle_cas_login_if_needed(driver, student, logger):
            logger.warning("CAS login failed — proceeding anyway")

        random_human_delay(0.3, 0.8)

        # B1 module selection (Reading/Listening/Writing/Speaking). B1 only — A1/A2
        # have no module page, so leave that path untouched.
        if resume_step < 2 and str(student.get("level", "")).upper().startswith("B"):
            _select_modules(driver, student, logger)

        if resume_step >= 2:
            logger.info("⏩ Skipping Wizard Step 1 (Name & Birth)")
        else:
            db.add_log(sk, level, " Wizard Step 1: Personal Data (Name & Birth)")
            if not _fill_step_personal_data_1(driver, student, logger):
                db.add_log(sk, level, "❌ Step 1 (Name & Birth) failed")
                save_failure_evidence(driver, f"debug_step1_{name}")
                raise RuntimeError("Step 1 (Name & Birth) failed")
            db.add_log(sk, level, "✅ Step 1 done — Name & Birth")
            db.save_checkpoint(student_key, 2)

        random_human_delay(0.3, 0.8)
        if stop_event.is_set():
            logger.warning("Stop requested by user. Aborting.")
            result["status"] = "stopped"; return result

        if resume_step >= 3:
            logger.info("⏩ Skipping Wizard Step 2 (Address & Motivation)")
        else:
            db.add_log(sk, level, " Wizard Step 2: Address & Motivation")
            if not _fill_step_personal_data_2(driver, student, logger):
                db.add_log(sk, level, "❌ Step 2 (Address & Motivation) failed")
                save_failure_evidence(driver, f"debug_step2_{name}")
                raise RuntimeError("Step 2 (Address & Motivation) failed")
            db.add_log(sk, level, "✅ Step 2 done — Address & Motivation")
            db.save_checkpoint(student_key, 3)

        random_human_delay(0.3, 0.8)
        if stop_event.is_set():
            logger.warning("Stop requested by user. Aborting.")
            result["status"] = "stopped"; return result

        if resume_step >= 4:
            logger.info("⏩ Skipping Wizard Step 3 (Payment)")
        else:
            db.add_log(sk, level, " Wizard Step 3: Payment Method")
            if not _fill_step_payment(driver, student, logger):
                db.add_log(sk, level, "❌ Step 3 (Payment) failed")
                save_failure_evidence(driver, f"debug_step3_{name}")
                raise RuntimeError("Step 3 (Payment) failed")
            db.add_log(sk, level, "✅ Step 3 done — Payment method selected")
            db.save_checkpoint(student_key, 4)

        random_human_delay(0.3, 0.8)
        if stop_event.is_set():
            logger.warning("Stop requested by user. Aborting.")
            result["status"] = "stopped"; return result

        if resume_step >= 5:
            logger.info("⏩ Skipping Wizard Step 4 (Promo)")
        else:
            db.add_log(sk, level, " Wizard Step 4: Promotional Code")
            if not _fill_step_promo(driver, student, logger):
                db.add_log(sk, level, "❌ Step 4 (Promo) failed")
                save_failure_evidence(driver, f"debug_step4_{name}")
                raise RuntimeError("Step 4 (Promo) failed")
            db.add_log(sk, level, "✅ Step 4 done — Promo code")
            db.save_checkpoint(student_key, 5)

        random_human_delay(0.3, 0.8)
        if stop_event.is_set():
            logger.warning("Stop requested by user. Aborting.")
            result["status"] = "stopped"; return result

        if resume_step >= 6:
            logger.info("⏩ Skipping Wizard Step 5 (Review & Confirm)")
        else:
            db.add_log(sk, level, " Wizard Step 5: Review & Confirm")
            if not _fill_step_review(driver, student, logger):
                db.add_log(sk, level, "❌ Step 5 (Review & Confirm) failed")
                save_failure_evidence(driver, f"debug_step5_{name}")
                raise RuntimeError("Step 5 (Review & Confirm) failed")
            db.add_log(sk, level, "✅ Step 5 done — Booking submitted!")
            db.save_checkpoint(student_key, 6)

        random_human_delay(0.3, 0.8)
        if stop_event.is_set():
            logger.warning("Stop requested by user. Aborting.")
            result["status"] = "stopped"; return result

        conf = capture_confirmation(driver, name, logger)
        result.update(conf)
        result["status"] = conf.get("status", "submitted")
        ref = result.get("reference", "")
        if ref:
            db.add_log(sk, level, f"✅✅ BOOKING CONFIRMED — Ref: {ref}")
        else:
            db.add_log(sk, level, f"✅ Flow complete — status: {result['status']}")

        random_human_delay(1.0, 2.0)
        verification = verify_booking(driver, student, ref, logger)
        result["verified"] = verification["verified"]
        result["profile_screenshot"] = verification.get("profile_screenshot", "")
        if verification["verified"]:
            result["status"] = "verified"
            db.add_log(sk, level, f"✅✅ BOOKING VERIFIED on profile page — Ref: {ref}")
        else:
            detail = verification.get("detail", "no detail")
            logger.warning("Post-booking verification: %s", detail)
            db.add_log(sk, level, f"⚠️ Booking unverifiable — {detail}")

        db.clear_checkpoint(student_key)

        if assigned_proxy:
            PROXY_ROTATOR.mark_success(assigned_proxy)

        notifications.notify_all(f"Booking complete: {name}", f"{level} | {city} | Status: {result['status']}", logger)

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        result["status"] = "interrupted"
        db.add_log(sk, level, "⏹️ Interrupted by user")
    except Exception as exc:
        CIRCUIT_BREAKER.record_failure(_classify_error(exc))
        msg = str(exc)[:200]
        logger.exception("Flow error for %s: %s", name, exc)
        db.add_log(sk, level, f"❌ Failed: {msg}")
        if assigned_proxy:
            PROXY_ROTATOR.mark_failed(assigned_proxy)
        notify(f"❌ Booking failed: {name}", msg, logger)
        save_failure_evidence(driver, f"error_{name}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    logger.info("=" * 60)
    logger.info("RESULT for %s: %s", name, result["status"])
    logger.info("=" * 60)
    return result


def main() -> int:
    args = parse_args()
    logger = setup_logger()

    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if args.telegram_token:
        TELEGRAM_BOT_TOKEN = args.telegram_token
    if args.telegram_chat_id:
        TELEGRAM_CHAT_ID = args.telegram_chat_id

    logger.warning("=" * 60)
    logger.warning("  LEGAL DISCLAIMER:")
    logger.warning("  Automating Goethe-Institut registration likely violates their TOS.")
    logger.warning("  This tool is for EDUCATIONAL PURPOSES only. Use at your own risk.")
    logger.warning("  The author assumes NO LIABILITY for account bans, missed deadlines,")
    logger.warning("  visa delays, or any damages resulting from bot usage.")
    logger.warning("=" * 60)

    students = load_all_students(args.config)
    db.save_students(students)
    logger.info("Loaded %d student(s)", len(students))

    for i, s in enumerate(students):
        logger.info("  Student %d: %s | %s | %s | Booking: %s",
                    i + 1, s.get("name"), s.get("level", s.get("exam_level", "")), s.get("city"), s.get("booking_datetime"))

    threads = []
    results_list = []
    results_lock = threading.Lock()

    def run_one(s: Dict):
        key = f"{s.get('name', '?')}|{s.get('level', s.get('exam_level', '?'))}|{s.get('city', '?')}"
        student_logger = setup_logger(f"bot_{s.get('name', 'unknown')}_{s.get('level', '?')}")
        result = smart_retry(s, args.headless, student_logger, threading.Event())
        with results_lock:
            results_list.append(result)
        db.update_student_status(key, result.get("status", "failed"), result)

    for student in students:
        t = threading.Thread(target=run_one, args=(student,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    logger.info("=" * 60)
    logger.info("ALL STUDENTS COMPLETE")
    logger.info("=" * 60)

    summary_lines = []
    for r in results_list:
        line = f"{r.get('name', '?')}: {r.get('status', '?')}"
        if r.get("reference"):
            line += f" (Ref: {r['reference']})"
        summary_lines.append(line)
        logger.info(line)

    summary = "\n".join(summary_lines)
    notifications.notify_all("Booking Summary", summary, logger)

    print("\n" + "=" * 50)
    print("  BOOKING SUMMARY:")
    for line in summary_lines:
        print(f"  {line}")
    print("=" * 50)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
