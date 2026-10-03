#!/usr/bin/env python3
"""Log in to a supplier portal once and store the session.

    python3 login.py --status          # what is configured, without values
    python3 login.py onninen           # log in, save the session
    python3 login.py onninen --show    # watch it happen in a real browser

Credentials come from .env or the environment (see .env.example). If they are
not there you are prompted, and the input is not echoed and not stored — the
password never reaches your shell history either way.

Needs the live extras:  ./setup.sh live
"""
import argparse
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from lib import credentials  # noqa: E402

SUPPLIERS_YAML = os.path.join(ROOT, "rules", "suppliers.yaml")


def load_suppliers():
    """Read rules/suppliers.yaml. Needs PyYAML, which the live extras install."""
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML is not installed. Run:  ./setup.sh live")
    with open(SUPPLIERS_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def cmd_status(cfg):
    have = set(credentials.configured())
    warn = credentials.check_permissions()
    print(f"\n  .env: {credentials.ENV_FILE}"
          f"{'' if os.path.exists(credentials.ENV_FILE) else '  (does not exist yet)'}")
    if warn:
        print(f"  WARNING  {warn}")
    print(f"\n  {'supplier':<12}{'login':<12}{'session':<12}{'ready to use'}")
    for key, s in cfg.items():
        creds = "set" if key in have else "-"
        state = "yes" if os.path.exists(credentials.state_path(key)) else "-"
        if not s.get("enabled"):
            ready = "stub"
        elif os.path.exists(credentials.state_path(key)):
            ready = "yes (session saved)"
        elif not s.get("login_url"):
            ready = f"use: login.py {key} --manual"
        elif key not in have:
            ready = "no credentials"
        else:
            ready = "yes"
        print(f"  {key:<12}{creds:<12}{state:<12}{ready}")
    print("\n  No password is ever printed here.\n")


def cmd_login(cfg, supplier, show, manual=False):
    s = cfg.get(supplier)
    if s is None:
        sys.exit(f"{supplier}: not in {SUPPLIERS_YAML}. "
                 f"Known: {', '.join(sorted(cfg))}")
    start = s.get("login_url") or s.get("login_api_url") or s.get("stock_url")
    if manual:
        return _manual_login(supplier, start, s)
    if not s.get("login_url"):
        sys.exit(f"{supplier}: login_url is not set in {SUPPLIERS_YAML}.\n"
                 f"       This supplier may use an OAuth or MFA login that no "
                 f"selector can fill.\n"
                 f"       Log in by hand once instead:  login.py {supplier} --manual")

    prefix = s.get("env_prefix") or supplier
    try:
        user, password = credentials.get(prefix)
    except credentials.CredentialsMissing as e:
        print(f"\n{e}\n")
        user = input(f"{supplier} username: ").strip()
        password = getpass.getpass(f"{supplier} password (not echoed): ")
        if not user or not password:
            sys.exit("aborted — nothing entered")

    sel = s.get("selectors") or {}
    missing = [k for k in ("username", "password", "submit") if not sel.get(k)]
    if missing:
        sys.exit(f"{supplier}: selectors {missing} are not set in {SUPPLIERS_YAML}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run:  ./setup.sh live")

    credentials.ensure_secrets_dir()
    state = credentials.state_path(supplier)
    with sync_playwright() as p:
        # A container gives /dev/shm 64 MB; Chromium keeps shared memory there
        # and a heavy page (Onninen's is 500 KB of script) crashes the tab.
        # This flag moves it to /tmp and is harmless on a laptop.
        br = p.chromium.launch(headless=not show,
                               args=["--disable-dev-shm-usage"])
        ctx = br.new_context()
        page = ctx.new_page()
        page.goto(s["login_url"], wait_until="domcontentloaded")
        try:
            page.fill(sel["username"], user)
        except Exception as e:                             # noqa: BLE001
            # Say which page we got instead: a bot check, a consent wall or a
            # country redirect all look like "selector not found" otherwise.
            # Nothing has been typed yet, so nothing secret can be in this.
            try:
                title = page.title()
            except Exception:                              # noqa: BLE001
                title = "(the browser tab crashed)"
            br.close()
            sys.exit(f"{supplier}: login form not found ({type(e).__name__}) — "
                     f"got {page.url.split('?')[0]} titled {title[:80]!r}")
        if sel.get("dismiss"):
            # A consent banner laid over the submit button makes the click
            # time out. Always the reject-all choice: the login needs only
            # the strictly necessary cookies.
            try:
                page.click(sel["dismiss"], timeout=5000)
            except Exception:                              # noqa: BLE001
                pass                                       # no banner this time
        page.fill(sel["password"], password)
        page.click(sel["submit"])
        if s.get("login_done_url"):
            # An OAuth login (Sonepar's Azure B2C) bounces through several
            # redirects; the network goes idle in between. Wait until we are
            # back in the shop, or the saved session is a half-finished one.
            page.wait_for_url(s["login_done_url"], timeout=60000)
        page.wait_for_load_state("networkidle")
        ctx.storage_state(path=state)
        br.close()
    os.chmod(state, 0o600)
    # stderr: price_bom.py --json logs in through here, and its stdout must
    # stay one clean JSON document.
    print(f"  session saved: {state}", file=sys.stderr)
    print("  This file is a live login. It is gitignored — never share or "
          "commit it.", file=sys.stderr)


def _manual_login(supplier, start_url, cfg):
    """Open a real browser, let a person log in, then keep the session.

    This is the escape hatch for every login a script cannot drive: OAuth
    redirects, MFA codes, a captcha, a consent screen, a password change
    prompt. The human does what they would do anyway; we only save the
    resulting cookies. It needs no selectors, so it cannot break when the
    supplier restyles the form.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run:  ./setup.sh live")
    if not start_url:
        sys.exit(f"{supplier}: no URL to open. Set login_url or stock_url in "
                 f"{SUPPLIERS_YAML}")

    credentials.ensure_secrets_dir()
    state = credentials.state_path(supplier)
    # HINNAT_NO_CONSOLE: started from a chat. isatty() alone cannot tell —
    # on Windows the NUL device reports itself as a terminal.
    console = (not os.environ.get("HINNAT_NO_CONSOLE")
               and sys.stdin is not None and sys.stdin.isatty())
    print(f"\n  Opening {start_url}")
    if console:
        print("  Log in in the browser window, then come back here.\n")
    else:
        # Started from a chat: there is no console to press enter in. The
        # person logs in and closes the window; that is the signal.
        print("  Kirjaudu selainikkunassa. Ikkuna sulkeutuu itsestään, kun "
              "kirjautuminen on valmis (tai sulje se itse). Odotetaan enintään 10 min.",
              flush=True)
    with sync_playwright() as p:
        br = p.chromium.launch(headless=False)     # a person has to see it
        ctx = br.new_context()
        page = ctx.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        if console:
            input("  Press enter once you are logged in (the browser can stay "
                  "open): ")
            ctx.storage_state(path=state)
            n = len(ctx.cookies())
        else:
            n = _keep_until_closed(ctx, page, state, start_url)
        try:
            br.close()
        except Exception:                                    # noqa: BLE001
            pass                                             # closed by the person
    if not os.path.exists(state):
        sys.exit("  ikkuna suljettiin ennen kuin istuntoa ehdittiin tallentaa")
    os.chmod(state, 0o600)
    print(f"  session saved: {state}  ({n} cookies)")
    print("  This file is a live login. It is gitignored — never share it.")
    if n == 0:
        sys.exit("  WARNING: no cookies were captured. Did the login finish?")


def _keep_until_closed(ctx, page, state, start_url="", timeout=600, every=1.0):
    """Wait for the person to log in, keep the session, close the window.

    Done when any tab has left the login page after being on it: the session
    is saved a moment later and the window closes by itself. A window closed
    by hand is still saved — from the context, which outlives its pages —
    so a quick close right after logging in no longer keeps the pre-login
    cookies (02.10.2026: the login window came back a second time). -> cookies
    """
    import time
    from urllib.parse import urlparse
    login_path = urlparse(start_url).path.rstrip("/") if start_url else ""
    seen_login, end = False, time.time() + timeout

    def save():
        ctx.storage_state(path=state + ".tmp")
        os.replace(state + ".tmp", state)
        return len(ctx.cookies())

    n = 0
    while time.time() < end:
        try:
            pages = [p for p in ctx.pages if not p.is_closed()]
            if not pages:
                break
            paths = [urlparse(p.url).path.rstrip("/") for p in pages]
            if login_path and login_path in paths:
                seen_login = True
            elif seen_login and login_path:
                pages[0].wait_for_timeout(2500)     # let the session cookies land
                n = save()
                print("  Kirjautuminen tunnistettu — istunto tallennettu, ikkuna sulkeutuu.",
                      flush=True)
                return n
            n = save()
            pages[0].wait_for_timeout(every * 1000)
        except Exception:                                    # noqa: BLE001
            break                                            # window or browser gone
    try:
        n = save()                          # closed by hand: the context still has it
    except Exception:                                        # noqa: BLE001
        pass
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("supplier", nargs="?", help="e.g. onninen")
    ap.add_argument("--status", action="store_true",
                    help="show what is configured, without printing any value")
    ap.add_argument("--show", action="store_true",
                    help="run the browser visibly")
    ap.add_argument("--manual", action="store_true",
                    help="open a browser and log in by hand, then save the "
                         "session. Works with OAuth, MFA and captchas, and "
                         "needs no selectors — use this when the scripted "
                         "login fails or the supplier has no plain form.")
    a = ap.parse_args()

    cfg = load_suppliers()
    if a.status or not a.supplier:
        cmd_status(cfg)
        return
    cmd_login(cfg, a.supplier.lower(), a.show, manual=a.manual)


if __name__ == "__main__":
    main()
