"""Live stock from Onninen — Playwright for login, plain requests for data.

SCAFFOLD. The payload mapping is done and tested against a real captured
response; the endpoint URL and the login form selectors must be filled in on
a machine that can actually reach the site. See docs/live-stock.md.

Design rules that must not be broken:
  * Playwright logs in ONCE and the session is reused. It is not used to
    scrape rendered pages.
  * A parse failure returns UNKNOWN. It never returns a number.
  * Requests are serial, cached, and only made for the shortlist.
"""
import datetime as dt
import json
import os
import sqlite3
import time

from lib import credentials
from parsers import embedded_json

from .stock_core import (Availability, NotAuthenticated, OnninenAdapter, cached,
                    looks_unauthenticated, remember)

STATE = credentials.state_path("onninen")

# rules/suppliers.yaml is the single source of truth for URLs and selectors.
# These module constants are only a fallback for a caller that has no PyYAML.
CACHE_TTL_SECONDS = 2 * 3600


def config():
    """Per-supplier settings from rules/suppliers.yaml, or {} if unreadable."""
    try:
        import yaml
    except ImportError:
        return {}
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "rules", "suppliers.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("onninen") or {}
    except OSError:
        return {}


def stock_url():
    return config().get("stock_url")


def login_url():
    return config().get("login_url") or "https://www.onninen.fi/login"


def login(username=None, password=None, headless=True):
    """Run once. Stores the session so later calls need no browser.

    Credentials default to HINNAT_ONNINEN_USER / _PASS, read from the
    environment or .env (see lib/credentials.py). Prefer the CLI:

        python3 login.py onninen

    Passing a password positionally still works for a scripted caller, but
    never type one on the command line — it lands in your shell history.
    """
    from playwright.sync_api import sync_playwright        # local import

    if username is None or password is None:
        username, password = credentials.get("onninen")

    credentials.ensure_secrets_dir()
    with sync_playwright() as p:
        br = p.chromium.launch(headless=headless)
        ctx = br.new_context()
        page = ctx.new_page()
        page.goto(login_url(), wait_until="domcontentloaded")
        page.fill('input[type="email"], input[name="username"]', username)
        page.fill('input[type="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        ctx.storage_state(path=STATE)
        br.close()
    os.chmod(STATE, 0o600)                                 # a live session
    return STATE


def page_ids(db, codes):
    """sähkönumero -> the AEK725-style code the product page is keyed on.

    Onninen's shop addresses pages by its own code, not the sähkönumero. We
    already hold it in products.sap for every row, so no extra data is needed.
    """
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = {}
    for chunk in (codes[i:i + 400] for i in range(0, len(codes), 400)):
        q = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT code, sap FROM products WHERE supplier='onninen' "
                f"AND code IN ({q})", list(chunk)):
            if r["sap"]:
                out[r["code"]] = r["sap"]
    con.close()
    return out


def fetch(codes, db, delay=1.0, session=None, cache_ttl=CACHE_TTL_SECONDS):
    """Serial, cached, shortlist-only. Returns {code: [Availability]}.

    Onninen serves no stock XHR — the product page is server-rendered and
    carries the availability JSON in the document. So we request the page and
    lift the payload out of it (parsers/embedded_json.py). The adapter that
    consumes it is unchanged, and still raises if the schema moves.
    """
    cfg = config()
    tmpl = cfg.get("stock_url") or cfg.get("product_url")
    if not tmpl:
        raise NotImplementedError(
            "stock_url is not set for onninen in rules/suppliers.yaml")
    import requests                                        # local import

    s = session
    if s is None:
        with open(STATE, encoding="utf-8") as fh:
            state = json.load(fh)
        s = requests.Session()
        for c in state.get("cookies", []):
            s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    s.headers["User-Agent"] = os.environ.get(
        "HINNAT_UA", "hinnat/0.1 (internal procurement integration)")

    ids = page_ids(db, list(codes))
    ad = OnninenAdapter()
    out = {}
    for code in codes:
        hit = cached(db, "onninen", code, cache_ttl)
        if hit is not None:
            out[code] = hit
            continue
        pid = ids.get(code)
        if pid is None:
            continue              # no page id -> unknown, never a guessed zero
        r = s.get(tmpl.replace("{id}", pid), timeout=20)
        r.raise_for_status()
        rows = ad.parse(embedded_json.availability(r.text), code)
        if looks_unauthenticated(rows):
            raise NotAuthenticated(
                f"onninen returned no quantities for {code} — the page renders "
                f"for anonymous visitors but withholds the numbers. Log in "
                f"first:  python3 login.py onninen")
        out[code] = rows
        remember(db, "onninen", code, rows)
        time.sleep(delay)                                  # one at a time, always
    return out


def unknown(code):
    return Availability(supplier="onninen", code=code, location="",
                        location_type="", qty=None, source="unavailable",
                        fetched_at=dt.datetime.now(dt.timezone.utc).isoformat())
