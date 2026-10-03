"""One launcher for everything the plugin does. Claude runs it; people don't.

    python/python hinnat.py --data <CLAUDE_PLUGIN_DATA> <command> ...

    ohje                              the detailed instructions (from the server)
    avain                             open the key window again
    find "MMJ 5x6" --qty 100          one item
    bom lista.csv [--kohde K] ...     a list -> Excel and order files
    opeta "sana" "kohde" [--selitys]  a word, for the whole organisation
    kohde "Kohde" "sääntö"            a site rule, for the whole organisation
    saanto minulle|yritykselle "…"    a rule: mine now, or a company proposal
    ehdotukset | hyvaksy N | hylkaa N company rule proposals (approvers)
    hinnasto --supplier X FILE…       a new price list, built on the server
    hinnasto --palauta                put the previous price list back
    tarjoukset | hae QID              the organisation's earlier quotes
    nimi "Etunimi"                    who is teaching (the log, not a login)
    selftest | status | sync
    onninen status|setup|login        Onninen live stock from this machine

The plugin is a thin client (02.10.2026): pricing, the split decision, the
Excel and the price-list build run on the server behind the organisation key.
What runs here: reading the list, Onninen's stock (Onninen walls out the
server), saving the files, and the lessons outbox.
"""
import argparse
import base64
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

# The embedded Python (python._pth) does not put the script's own folder on
# sys.path, so common.py beside it has to be added by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def _guess_data():
    """If ${CLAUDE_PLUGIN_DATA} ever arrives empty, find the folder the
    session-start hook filled (~/.claude/plugins/data/hinnat-*)."""
    base = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data")
    try:
        found = [os.path.join(base, d) for d in os.listdir(base)
                 if d.startswith("hinnat")
                 and os.path.exists(os.path.join(base, d, "state.json"))]
    except OSError:
        return ""
    return max(found, key=os.path.getmtime) if found else ""


def _data_dir(argv):
    if len(argv) > 1 and argv[0] == "--data":
        d, argv = argv[1], argv[2:]
    else:
        d = os.environ.get("HINNAT_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA") or ""
    if not d or "${" in d:
        d = _guess_data()
    return d, argv


DATA, ARGV = _data_dir(sys.argv[1:])
if not DATA:
    sys.exit("hinnat: --data <pluginin datakansio> puuttuu")
common.setup(DATA)
ROOT = common.ROOT

from lib import hub, paths  # noqa: E402


class KeyRefused(Exception):
    pass


def call(method, path, **kw):
    """hub.request, but a refused key opens the key window once and retries."""
    try:
        return hub.request(method, path, **kw)
    except hub.HubError as e:
        if e.status != 401:
            raise
    key, why = common.ensure_key(hub, refused=True)
    if not key:
        raise KeyRefused(why)
    return hub.request(method, path, **kw)


def _quotes_dir():
    """tarjoukset/ in the folder the user opened this conversation in — that
    is where they look. The Bash tool runs there. The plugin's own data folder
    only when that folder cannot be written."""
    here = os.getcwd()
    inside_plugin = os.path.join(".claude", "plugins") in here.replace("/", os.sep)
    if not inside_plugin and os.access(here, os.W_OK):
        return os.path.join(here, "tarjoukset")
    return paths.QUOTES


def _caps():
    try:
        with open(paths.data("capabilities.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _slug(s):
    s = (s or "").lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("å", "a")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40]


def _opts(a):
    return {"allow_hf": a.allow_hf or None, "require_stock": a.require_stock,
            "exclude_mfg": a.exclude_mfg, "max_split": getattr(a, "max_split", None),
            "kohde": getattr(a, "kohde", None)}


def _flags(ap):
    ap.add_argument("--allow-hf", action="store_true")
    ap.add_argument("--require-stock", choices=["V", "E"])
    ap.add_argument("--exclude-mfg", action="append")
    ap.add_argument("--json", action="store_true")       # always JSON; accepted


# ---------------------------------------------------------------- pricing

def cmd_find(_a, rest):
    ap = argparse.ArgumentParser(prog="hinnat find")
    ap.add_argument("request")
    ap.add_argument("--qty", type=float, default=1)
    _flags(ap)
    a = ap.parse_args(rest)
    r = call("POST", "/v1/find", body={"request": a.request, "qty": a.qty,
                                       "opts": _opts(a)}, timeout=60)
    print(json.dumps(r, ensure_ascii=False, indent=1))


def _read_list(path):
    with open(path, encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        if "request" not in (rd.fieldnames or []):
            sys.exit(f"{path}: tarvitaan otsikkorivi, vähintään 'request,qty'")
        return [{"request": (r.get("request") or "").strip(),
                 "qty": (r.get("qty") or "1").strip(),
                 "need_by": (r.get("need_by") or "").strip() or None,
                 "note": (r.get("note") or "").strip()}
                for r in rd if (r.get("request") or "").strip()]


SESSION_FRESH_H = 10        # Onninen keeps a session about a working day


def _onninen_ready(py):
    """Onninen's login, when the stored session is old or missing: the window
    opens beside the server's plan call, so nobody waits twice."""
    state = _onninen_state()
    if os.path.exists(state) and             (time.time() - os.path.getmtime(state)) / 3600 < SESSION_FRESH_H:
        return
    _onninen_login(py)


def _onninen_fetch(py, ids):
    """{code: page id} -> {"avail": {code: [availability]}, "skipped": reason}"""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ids, fh)
    try:
        r = subprocess.run([py, os.path.join(common.HERE, "onninen_check.py"), DATA, path],
                           capture_output=True, text=True, encoding="utf-8", timeout=600)
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as e:
        return {"avail": {}, "skipped": f"Onnisen tarkistus ei onnistunut ({type(e).__name__})"}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def cmd_bom(_a, rest):
    ap = argparse.ArgumentParser(prog="hinnat bom")
    ap.add_argument("bom")
    ap.add_argument("--kohde")
    ap.add_argument("--max-split", type=int)
    ap.add_argument("--no-check-stock", action="store_true")
    ap.add_argument("--no-share", action="store_true")
    ap.add_argument("--no-onninen", action="store_true")
    ap.add_argument("--out")                              # ignored: the server names it
    _flags(ap)
    a = ap.parse_args(rest)
    lines = _read_list(a.bom)
    opts = dict(_opts(a), check_stock=not a.no_check_stock)

    # Onninen's login, if needed, opens now — while the server prices the
    # list and starts Sonepar/Ahlsell stock in the background.
    caps = _caps()
    py = caps.get("python_full") if caps.get("playwright") else None
    login = None
    if py and opts["check_stock"] and not a.no_onninen:
        login = threading.Thread(target=_onninen_ready, args=(py,), daemon=True)
        login.start()
    plan = call("POST", "/v1/quote/plan", body={"lines": lines, "opts": opts}, timeout=120)
    onninen, note = {}, None
    wanted = {x["code"]: x["sap"] for x in plan.get("onninen") or [] if x.get("sap")}
    if login:
        login.join()
        if wanted:
            got = _onninen_fetch(py, wanted)
            if "vanhentunut" in str(got.get("skipped")):
                _onninen_login(py)                      # expired after all: once more
                got = _onninen_fetch(py, wanted)
            onninen = got.get("avail") or {}
            note = got.get("skipped")
    elif wanted and opts["check_stock"] and not a.no_onninen and not py:
        note = None              # the server's note already says monthly V/E03

    r = call("POST", "/v1/quote", body={"lines": lines, "opts": opts, "onninen": onninen},
             timeout=300)
    result, files = r["result"], r["files"]
    if note:
        result.setdefault("live_check", []).append(f"onninen: EI TARKISTETTU — {note}")
    folder = _quotes_dir()
    os.makedirs(folder, exist_ok=True)
    saved = {}
    for name, b64 in files.items():
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(b64))
        saved[name] = path
    result["excel"] = saved.get(result["excel"], result["excel"])
    for f in result.get("order_files") or []:
        f["path"] = saved.get(f["path"], f["path"])
    base = os.path.basename(result["excel"]).replace("_tarjous.xlsx", "")
    bom_copy = os.path.join(folder, base + "_bom.csv")
    if os.path.abspath(a.bom) != os.path.abspath(bom_copy):
        shutil.copyfile(a.bom, bom_copy)
    result["tiedostot"] = _file_list(result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    # The files go to the user as attachments, every one of them (03.10.2026,
    # the buyers' decision): the Excel, and the Sonepar CSV whenever Sonepar
    # has lines — that one goes into their shop. A link opens the file only
    # inside Claude, where cells cannot be copied out; the attachment opens
    # in Excel and "show in folder" finds it.
    print("\nLIITTEET — lähetä nämä kaikki käyttäjälle liitteinä yhdellä kertaa "
          "(display: attach):")
    for f in result["tiedostot"]:
        print(f"- {f['polku']}  — {f['mika']}")
    if not a.no_share:
        _share_quote(f"{base}_{os.urandom(2).hex()}", result["excel"], bom_copy, result)


ORDER_FILE_TEXT = {
    "sonepar": "Sonepar-tilaus: lataa Soneparin verkkokaupassa kohdasta \"Tilaa tiedostosta\"",
}


def _link(path):
    """Relative to the conversation folder, forward slashes: clickable in the
    app. Outside it (the data-folder fallback) absolute, in <> for spaces."""
    try:
        rel = os.path.relpath(path)
    except ValueError:                                       # another drive
        rel = path
    if rel.startswith(".."):
        rel = path
    rel = rel.replace(os.sep, "/")
    return f"<{rel}>" if " " in rel else rel


def _file_list(result):
    """The Excel first, then every order file, each with what it is for."""
    out = [{"nimi": os.path.basename(result["excel"]), "polku": result["excel"],
            "linkki": _link(result["excel"]),
            "mika": "tarjous Excelinä; Onnisen ja Ahlsellin tilausrivit liitetään sen "
                    "Tilaus-välilehdiltä"}]
    for f in result.get("order_files") or []:
        sup = f.get("supplier") or ""
        out.append({"nimi": os.path.basename(f["path"]), "polku": f["path"],
                    "linkki": _link(f["path"]),
                    "mika": ORDER_FILE_TEXT.get(sup, f"{sup.capitalize()}-tilaustiedosto")
                            + (f" ({f['rows']} riviä)" if f.get("rows") else "")})
    return out


def _share_quote(qid, out, bom_copy, result):
    """The organisation's quote history. A failure is a note: the Excel is here."""
    files = [out, bom_copy] + [f["path"] for f in result.get("order_files") or []]
    names = []
    try:
        for f in files:
            if not os.path.exists(f):
                continue
            name = os.path.basename(f)[:120]
            with open(f, "rb") as fh:
                hub.request("PUT", f"/v1/quotes/{qid}/{name}", data=fh.read(),
                            headers={"Content-Type": "application/octet-stream"},
                            timeout=15)
            names.append(name)
        meta = {"kohde": result.get("kohde"), "total": result.get("total"),
                "lines": len(result.get("lines") or []), "files": names,
                "price_file_valid_from": result.get("price_file_valid_from")}
        hub.request("PUT", f"/v1/quotes/{qid}/meta.json",
                    data=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, timeout=15)
        print(f"\n(tarjous tallennettu organisaation tarjoushistoriaan: {qid})")
    except hub.HubError as e:
        print(f"\n(tarjousta ei tallennettu tarjoushistoriaan: {e} — Excel on koneella)")


# ---------------------------------------------------------------- lessons

def cmd_opeta(a, _rest):
    from lib import sanasto
    word = " ".join(a.sana.split()).lower()
    target = " ".join(a.kohde.split())
    try:
        sanasto.parse(f'"{word}": "{target}"')
    except ValueError as e:
        sys.exit(f"ei kelpaa sanastoon: {e}")
    have = {k: t for _p, t, k in sanasto.load(paths.SANASTO[0])}
    if have.get(word) == target:
        print(f'"{word}" on jo organisaation sanastossa: {target}')
        return
    if word in have:
        sys.exit(f'"{word}" tarkoittaa organisaation sanastossa jo "{have[word]}". '
                 f"Kumpi on oikein? Muutos menee Pasin tai hyväksyjän kautta.")
    ok, resp = hub.queue("POST", "/v1/rules/sanasto",
                         {"word": word, "target": target, "note": a.selitys or ""},
                         apply_local={"kind": "word", "word": word, "target": target})
    if ok:
        print(f'opetettu koko organisaatiolle: "{word}" -> {target} '
              f'(toimii heti seuraavassa haussa)')
    elif "tallennettu" in str(resp):
        print(f'"{word}" -> {target}: {resp}.')
    else:
        sys.exit(f"palvelin hylkäsi: {resp}")


def cmd_kohde(a, _rest):
    ok, resp = hub.queue("POST", "/v1/rules/kohde", {"kohde": a.nimi, "rule": a.saanto},
                         apply_local={"kind": "site", "kohde": a.nimi, "rule": a.saanto})
    if ok:
        print(f"kohdesääntö tallennettu organisaatiolle: {a.nimi}: {a.saanto}")
    elif "tallennettu" in str(resp):
        print(f"{a.nimi}: {resp}. Toimii tällä koneella jo nyt.")
    else:
        sys.exit(f"palvelin hylkäsi: {resp}")


def cmd_saanto(a, _rest):
    text = " ".join(a.teksti.split())
    if a.kenelle == "minulle":
        os.makedirs(paths.ME, exist_ok=True)
        fn = os.path.join(paths.ME, "preferences.md")
        new = not os.path.exists(fn)
        with open(fn, "a", encoding="utf-8", newline="\n") as fh:
            if new:
                fh.write("# Omat sääntöni\n\nVain tällä koneella. Luetaan "
                         "organisaation sääntöjen jälkeen — nämä voittavat.\n\n")
            fh.write(f"- {text} ({dt.date.today():%d.%m.%Y})\n")
        print(f"oma sääntö tallennettu vain tälle koneelle: {text}")
        return
    ok, resp = hub.queue("POST", "/v1/rules/proposal", {"text": text})
    if ok:
        appr = ", ".join(resp.get("approvers") or []) or "ei vielä nimetty"
        print(f"ehdotus jätetty koko yritykselle (hyväksyjä: {appr}): {text}")
    elif "tallennettu" in str(resp):
        print(f"ehdotus: {resp}")
    else:
        sys.exit(f"palvelin hylkäsi: {resp}")


def cmd_ehdotukset(_a, _rest):
    r = call("GET", "/v1/rules/proposals")
    print(f"hyväksyjät: {', '.join(r['approvers']) or 'ei nimetty'}")
    if not r["proposals"]:
        print("ei avoimia ehdotuksia")
    for p in r["proposals"]:
        print(f"  {p['id']:>3}  {p['text']}  ({p['by']}, {hub._fi(p['at'])})")


def cmd_decide(action):
    def run(a, _rest):
        for pid in a.id:
            try:
                p = call("POST", f"/v1/rules/proposals/{pid}", body={"action": action})
                print(f"  {pid}: {'hyväksytty' if action == 'approve' else 'hylätty'} — "
                      f"{p['text']}")
            except hub.HubError as e:
                print(f"  {pid}: {e}")
        if action == "approve":
            try:
                hub.sync()
            except hub.HubError:
                pass
    return run


# ------------------------------------------------------------- price list

def cmd_hinnasto(a, _rest):
    if a.palauta:
        info = call("POST", "/v1/prices/restore")
        print(f"edellinen hinnasto palautettu ({info.get('by')}, "
              f"{hub._fi(info.get('uploaded_at'))}); käytössä heti kaikilla")
        return
    if not a.supplier or not a.files:
        sys.exit("anna --supplier onninen|sonepar|ahlsell ja hinnastotiedostot")
    period = a.period or dt.date.today().strftime("%Y-%m")
    for f in a.files:
        name = os.path.basename(f)
        with open(f, "rb") as fh:
            call("PUT", f"/v1/prices/raw/{a.supplier}/{name}?period={period}",
                 data=fh.read(), headers={"Content-Type": "application/octet-stream"},
                 timeout=600)
        print(f"  ladattu: {name}")
    r = call("POST", "/v1/prices/build", body={"supplier": a.supplier, "period": period},
             timeout=600)
    rep = r["report"]
    for s, v in sorted(rep["suppliers"].items()):
        nv = v.get("newest") or ""
        print(f"  {s:<8} {hub._fi(nv)}{nv[:4]}  {v['rows']:>7,} riviä, "
              f"{v['no_ean']:,} ilman EAN:ia")
    print("hinnasto käytössä heti koko organisaatiolla (edellinen säilyy varmuuskopiona)")


# ----------------------------------------------------------------- quotes

def cmd_tarjoukset(_a, _rest):
    qs = call("GET", "/v1/quotes")["quotes"]
    for q in qs[-30:][::-1]:
        tot = f"{q['total']:,.2f} €".replace(",", " ") if q.get("total") else "—"
        print(f"  {q['id']:<34} {q.get('kohde') or 'ei kohdetta':<20} {tot:>14}  "
              f"{q.get('by')}")


def cmd_hae(a, _rest):
    qs = {q["id"]: q for q in call("GET", "/v1/quotes")["quotes"]}
    q = qs.get(a.qid)
    if not q:
        sys.exit("tarjousta ei löydy")
    folder = _quotes_dir()
    os.makedirs(folder, exist_ok=True)
    for name in q.get("files") or []:
        r = hub.request("GET", f"/v1/quotes/{a.qid}/{name}", raw=True)
        path = os.path.join(folder, name)
        with r, open(path, "wb") as fh:
            shutil.copyfileobj(r, fh)
        print(f"  -> {path}")


# ------------------------------------------------------------------ misc

def cmd_ohje(_a, _rest):
    print(call("GET", "/v1/instructions")["text"])


def cmd_avain(_a, _rest):
    key, name = common.ensure_key(hub, refused=True)
    print(f"avain tallennettu: {name}" if key else f"avainta ei tallennettu: {name}")


def cmd_nimi(a, _rest):
    print(f"tallennettu: {hub.set_user(a.nimi)}")


def cmd_sync(_a, _rest):
    try:
        b, notes = hub.sync()
        print("ok: " + (", ".join(notes) or "kaikki ajan tasalla")
              + f" · {b['tenant']['name']}")
    except hub.HubError as e:
        print(f"palvelinta ei tavoitettu: {e}")


def cmd_status(_a, _rest):
    st = hub.state()
    print(json.dumps({"tenant": st.get("tenant"), "prices": st.get("prices"),
                      "rules_updated_at": st.get("rules_updated_at"),
                      "last_sync": st.get("last_sync"), "user": hub.user(),
                      "outbox": len(hub.pending()), "capabilities": _caps(),
                      "data": DATA}, ensure_ascii=False, indent=1))


def cmd_selftest(_a, _rest):
    ok = True

    def line(good, text):
        nonlocal ok
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {text}")
    line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}")
    try:
        b = call("GET", "/v1/bundle", timeout=20)
        line(True, f"avain ja palvelin: {b['tenant']['name']}")
        sups = ((b.get("prices") or {}).get("report") or {}).get("suppliers") or {}
        line(bool(sups), "hinnasto: " + (", ".join(
            f"{s} {hub._fi(v.get('newest'))}" for s, v in sorted(sups.items())) or "puuttuu"))
    except (hub.HubError, KeyRefused) as e:
        line(False, f"avain tai palvelin: {e}")
    caps = _caps()
    line(True, "Onnisen live-saldo: " + ("käytössä" if caps.get("playwright")
                                          else "ei (kuukausitiedoston V/E03)"))
    waiting = len(hub.pending())
    if waiting:
        print(f"  !!    {waiting} opetusta odottaa lähetystä palvelimelle")


# ---------------------------------------------------------------- Onninen

def _onninen_state():
    from lib import credentials
    return credentials.state_path("onninen")


def _onninen_login(py):
    """A browser window opens; the person logs in and closes it. No password
    passes through Claude or a file. Then one product page proves it."""
    print("Selainikkuna aukeaa: kirjaudu Onniselle omilla tunnuksillasi — ikkuna "
          "sulkeutuu itsestään, kun kirjautuminen on valmis.", flush=True)
    r = subprocess.run([py, os.path.join(ROOT, "login.py"), "onninen", "--manual"],
                       env=dict(os.environ, HINNAT_DATA=DATA, HINNAT_NO_CONSOLE="1"),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    ok = r.returncode == 0 and os.path.exists(_onninen_state())
    print("Onnisen kirjautuminen tallennettu." if ok else
          "Onnisen kirjautumista ei tallennettu.")
    return ok


def cmd_onninen(a, _rest):
    caps = _caps()
    if a.action == "login":
        if not (caps.get("python_full") and caps.get("playwright")):
            sys.exit("Onnisen kirjautuminen tarvitsee Pythonin ja selainosan: "
                     "aja ensin hinnat onninen setup")
        _onninen_login(caps["python_full"])
        return
    if a.action == "status":
        st = _onninen_state()
        age = round((time.time() - os.path.getmtime(st)) / 3600, 1) if os.path.exists(st) else None
        print(json.dumps({"python_full": caps.get("python_full"),
                          "playwright": caps.get("playwright"),
                          "istunto_tuntia_vanha": age}, indent=1))
        return
    if not caps.get("python_full") and sys.platform == "darwin":
        print("VAIHE 1: koneella ei ole Pythonia. Pyydä käyttäjää asentamaan se itse:\n"
              "  https://www.python.org/downloads/macos/ — lataa 'macOS 64-bit universal2 "
              "installer' ja asenna (tuplaklikkaa .pkg). Homebrew'n Python ei käy: se "
              "estää pip-asennukset.\n"
              "Aloita sitten uusi keskustelu ja aja tämä uudestaan.")
        return
    if not caps.get("python_full"):
        print("VAIHE 1: koneella ei ole Pythonia. Ehdota käyttäjälle (hänen luvallaan):\n"
              "  winget install --id Python.Python.3.12 -e --accept-source-agreements "
              "--accept-package-agreements\n"
              "Jos wingetiä ei ole: https://www.python.org/downloads/ — rasti kohtaan "
              "'Add python.exe to PATH'. Aloita sitten uusi keskustelu ja aja tämä uudestaan.")
        return
    py = caps["python_full"]
    if not caps.get("playwright"):
        print("VAIHE 2: asenna selainosa (aja nämä):\n"
              f'  "{py}" -m pip install playwright requests pyyaml\n'
              f'  "{py}" -m playwright install chromium\n'
              "Aloita sitten uusi keskustelu ja aja tämä uudestaan.")
        return
    _onninen_login(py)


def main():
    ap = argparse.ArgumentParser(prog="hinnat")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("selftest", "ehdotukset", "tarjoukset", "sync", "status", "ohje", "avain"):
        sub.add_parser(name)
    p = sub.add_parser("opeta")
    p.add_argument("sana")
    p.add_argument("kohde")
    p.add_argument("--selitys")
    p = sub.add_parser("kohde")
    p.add_argument("nimi")
    p.add_argument("saanto")
    p = sub.add_parser("saanto")
    p.add_argument("kenelle", choices=["minulle", "yritykselle"])
    p.add_argument("teksti")
    for name in ("hyvaksy", "hylkaa"):
        sub.add_parser(name).add_argument("id", type=int, nargs="+")
    p = sub.add_parser("hinnasto")
    p.add_argument("files", nargs="*")
    p.add_argument("--supplier", choices=["onninen", "sonepar", "ahlsell"])
    p.add_argument("--period")
    p.add_argument("--palauta", action="store_true")
    sub.add_parser("hae").add_argument("qid")
    sub.add_parser("nimi").add_argument("nimi")
    p = sub.add_parser("onninen")
    p.add_argument("action", choices=["status", "setup", "login"])

    if ARGV and ARGV[0] in ("find", "bom"):
        a, rest = argparse.Namespace(cmd=ARGV[0]), ARGV[1:]
    else:
        a, rest = ap.parse_args(ARGV), []
    handlers = {
        "selftest": cmd_selftest, "find": cmd_find, "bom": cmd_bom,
        "opeta": cmd_opeta, "kohde": cmd_kohde, "saanto": cmd_saanto,
        "ehdotukset": cmd_ehdotukset, "hyvaksy": cmd_decide("approve"),
        "hylkaa": cmd_decide("reject"), "hinnasto": cmd_hinnasto,
        "tarjoukset": cmd_tarjoukset, "hae": cmd_hae, "nimi": cmd_nimi,
        "sync": cmd_sync, "status": cmd_status, "onninen": cmd_onninen,
        "ohje": cmd_ohje, "avain": cmd_avain,
    }
    try:
        handlers[a.cmd](a, rest)
    except KeyRefused as e:
        sys.exit(f"hinnat: avain puuttuu tai ei kelpaa ({e}). Sano \"syötä avain\".")
    except hub.HubError as e:
        msg = str(e)
        if e.status is None or (e.status or 0) >= 500:
            msg = f"hinnoittelupalvelu ei vastaa juuri nyt ({e}) — yritä hetken päästä"
        sys.exit(f"hinnat: {msg}")


if __name__ == "__main__":
    main()
