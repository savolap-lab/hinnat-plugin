"""The plugin's side of the organisation storage (server/hub.py).

Standard library only: this runs in the plugin's embedded Python.

Three rules, in order:
  1. Never block a quote. No key, no network, a server error — the quote is
     computed from the data already on this machine, and says how old it is.
  2. Never lose a lesson. A word, a site rule or a proposal goes to the
     outbox first and is applied locally at once; the outbox is sent now and
     again at every session start until the server has it.
  3. Never show the key. It is read from the environment or the data folder
     and goes into one header, nowhere else.
"""
import datetime as dt
import gzip
import json
import os
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import paths

SERVER = (os.environ.get("HINNAT_SERVER") or "https://saldo.kannas.ai").rstrip("/")
KEY_FILE = paths.data(".key")
STATE_FILE = paths.data("state.json")
NAME_FILE = os.path.join(paths.ME, "name")
RULE_FILES = ("preferences.md", "kohteet.md", "sanasto.yaml")


def version():
    try:
        with open(os.path.join(paths.ROOT, ".claude-plugin", "plugin.json"),
                  encoding="utf-8") as fh:
            return json.load(fh).get("version") or "dev"
    except (OSError, ValueError):
        return "dev"


# Cloudflare answers 403 to urllib's default User-Agent (found 01.10.2026).
UA = f"hinnat-plugin/{version()}"


class HubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------ who and how

def key():
    """The organisation key: managed settings env, else the copy the
    session-start hook saved from the plugin's userConfig."""
    k = os.environ.get("HINNAT_KEY") or ""
    if k:
        return k.strip()
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def save_key(k):
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    if key_on_disk() == k:
        return
    with open(KEY_FILE, "w", encoding="utf-8") as fh:
        fh.write(k)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass


def key_on_disk():
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def user():
    try:
        with open(NAME_FILE, encoding="utf-8") as fh:
            return fh.read().strip() or "?"
    except OSError:
        return "?"


def set_user(name):
    name = " ".join(name.split())[:40]
    os.makedirs(paths.ME, exist_ok=True)
    with open(NAME_FILE, "w", encoding="utf-8") as fh:
        fh.write(name)
    return name


def state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ------------------------------------------------------------------- HTTP

def request(method, path, body=None, data=None, headers=None, timeout=20,
            raw=False, k=None):
    """-> parsed JSON (or the response object when raw=True)."""
    k = k if k is not None else key()
    if not k:
        raise HubError("avainta ei ole asetettu")
    h = {"Authorization": f"Bearer {k}", "User-Agent": UA,
         "X-Hinnat-User": urllib.parse.quote(user())}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(SERVER + path, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error") or ""
        except (ValueError, OSError):
            msg = ""
        raise HubError(msg or f"palvelin vastasi HTTP {e.code}", e.code) from e
    except (urllib.error.URLError, OSError) as e:
        raise HubError(f"palvelinta ei tavoitettu ({type(e).__name__})") from e
    if raw:
        return r
    with r:
        text = r.read().decode("utf-8")
    return json.loads(text) if text else {}


# ------------------------------------------------------------------- sync

def _fi(d):
    """'20260903' or ISO -> '03.09.'"""
    s = str(d or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[6:8]}.{s[4:6]}."
    try:
        x = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return x.strftime("%d.%m.")
    except ValueError:
        return s


def download_prices(etag, timeout=120):
    """Fetch the organisation's price database if it changed. -> True when new."""
    hdr = {"If-None-Match": f'"{etag}"'} if etag else {}
    r = request("GET", "/v1/prices", headers=hdr, raw=True, timeout=timeout)
    if r is None:
        return False
    gz = paths.PRICES_DB + ".gz"
    os.makedirs(os.path.dirname(gz), exist_ok=True)
    tmp = gz + ".download"
    with r, open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)
    with gzip.open(tmp) as test:                     # a cut-off download fails here
        test.read(16)
    os.replace(tmp, gz)
    # expand now, not during the first quote
    out = paths.PRICES_DB + ".tmp"
    with gzip.open(gz, "rb") as f, open(out, "wb") as o:
        shutil.copyfileobj(f, o, 1 << 20)
    os.replace(out, paths.PRICES_DB)
    return True


def write_rules(files):
    os.makedirs(paths.ORG_RULES, exist_ok=True)
    for name in RULE_FILES:
        text = files.get(name) or ""
        with open(os.path.join(paths.ORG_RULES, name), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(text)
    reapply_outbox()


def sync():
    """Outbox first, then the bundle. -> (bundle or None, [notes])"""
    notes = []
    sent, failed, left = flush_outbox()
    if sent:
        notes.append(f"{sent} opetusta lähetetty")
    for f in failed:
        notes.append(f"OPETUS HYLÄTTIIN: {f}")
    if left:
        notes.append(f"{left} opetusta odottaa lähetystä")
    st = state()
    b = request("GET", "/v1/bundle")
    st["tenant"] = b["tenant"]
    p = b.get("prices")
    # The price database stays on the server (the plugin is a thin client
    # since 02.10.2026); only what it says about itself is kept here.
    if p and p.get("etag") != st.get("prices_etag") and st.get("prices_etag"):
        notes.append(f"uusi hinnasto ({p.get('by')}, {_fi(p.get('uploaded_at'))})")
    if p:
        st["prices_etag"], st["prices"] = p["etag"], p
    r = b.get("rules") or {}
    if r.get("version") != st.get("rules_version"):
        rules = request("GET", "/v1/rules")
        write_rules(rules["files"])
        if st.get("rules_version") is not None:
            notes.append("säännöt päivittyivät")
        st["rules_version"], st["rules_updated_at"] = rules["version"], rules["updated_at"]
    st["pending_proposals"] = b.get("pending_proposals", 0)
    st["last_sync"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save_state(st)
    return b, notes


# ----------------------------------------------------------------- outbox

def queue(method, path, body, apply_local=None):
    """Write the lesson to the outbox, apply it here, then try to send it.
    -> (sent: bool, message)"""
    os.makedirs(paths.OUTBOX, exist_ok=True)
    uid = time.strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)
    body = dict(body, uid=uid)
    item = {"method": method, "path": path, "body": body, "local": apply_local,
            "queued_at": dt.datetime.now().isoformat(timespec="seconds"),
            "user": user()}
    fn = os.path.join(paths.OUTBOX, uid + ".json")
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(item, fh, ensure_ascii=False)
    if apply_local:
        _apply_local(apply_local)
    try:
        resp = request(method, path, body=body)
    except HubError as e:
        if e.status and 400 <= e.status < 500 and e.status not in (401, 408, 429):
            _discard(fn, str(e))
            return False, str(e)
        return False, f"tallennettu, lähetetään kun palvelin vastaa ({e})"
    os.remove(fn)
    return True, resp


def _discard(fn, why):
    """The server said no for a reason that a retry will not change."""
    bad = os.path.join(paths.OUTBOX, "hylatyt")
    os.makedirs(bad, exist_ok=True)
    with open(fn, encoding="utf-8") as fh:
        item = json.load(fh)
    item["refused"] = why
    with open(os.path.join(bad, os.path.basename(fn)), "w", encoding="utf-8") as fh:
        json.dump(item, fh, ensure_ascii=False)
    os.remove(fn)                    # its local copy goes with the next rules download


def pending():
    try:
        names = sorted(n for n in os.listdir(paths.OUTBOX) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(paths.OUTBOX, n), encoding="utf-8") as fh:
                out.append((os.path.join(paths.OUTBOX, n), json.load(fh)))
        except (OSError, ValueError):
            continue
    return out


def flush_outbox():
    """-> (sent, [refusal messages], still waiting)"""
    sent, failed = 0, []
    items = pending()
    for i, (fn, item) in enumerate(items):
        try:
            request(item["method"], item["path"], body=item["body"])
        except HubError as e:
            if e.status and 400 <= e.status < 500 and e.status not in (401, 408, 429):
                _discard(fn, str(e))
                failed.append(f"{_describe(item)}: {e}")
                continue
            return sent, failed, len(items) - i      # server down: try next time
        os.remove(fn)
        sent += 1
    return sent, failed, 0


def _describe(item):
    loc = item.get("local") or {}
    if loc.get("kind") == "word":
        return f'sana "{loc["word"]}"'
    if loc.get("kind") == "site":
        return f'kohdesääntö {loc["kohde"]}'
    return "ehdotus"


def _apply_local(loc):
    """Make a queued lesson work on this machine before the server has it."""
    os.makedirs(paths.ORG_RULES, exist_ok=True)
    if loc.get("kind") == "word":
        fn = os.path.join(paths.ORG_RULES, "sanasto.yaml")
        line = f'"{loc["word"]}": "{loc["target"]}"'
        try:
            with open(fn, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        if line not in text:
            with open(fn, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(("" if text.endswith("\n") or not text else "\n")
                         + line + "  # odottaa lähetystä\n")
    elif loc.get("kind") == "site":
        fn = os.path.join(paths.ORG_RULES, "kohteet.md")
        with open(fn, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"\n## {loc['kohde']}\n\n- {loc['rule']} (odottaa lähetystä)\n")


def reapply_outbox():
    """A rules download replaces the local files; lessons still in the outbox
    go back on top so they keep working until the server has them."""
    for _fn, item in pending():
        if item.get("local"):
            _apply_local(item["local"])
