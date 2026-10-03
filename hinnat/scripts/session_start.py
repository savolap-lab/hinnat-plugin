"""SessionStart hook: key, rules, the instructions, one status line.

Runs in the plugin's embedded Python, started directly by Claude Code (exec
form, no shell — hooks/hooks.json). Claude's first answer waits for it, so a
user who pastes a list as the very first message gets the key window first
and then the quote, with no round in between. Whatever goes wrong it prints
what it knows and exits 0: a session must start.

Its stdout reaches Claude: the status lines are written for Claude to relay,
and the detailed instructions (server/playbook.md, behind the key) are printed
between markers the plugin's skill points to.
"""
import json
import os
import shutil
import subprocess
import sys
import time

# The embedded Python (python._pth) does not put the script's own folder on
# sys.path, so common.py beside it has to be added by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DATA = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("HINNAT_DATA") or ""


MAC_PYTHONS = ("/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
               "/opt/homebrew/bin/python3", "/usr/local/bin/python3")


def _candidates():
    if sys.platform != "darwin":
        return (["py", "-3"], ["python3"], ["python"])
    # The app's PATH on a Mac often lacks python.org's and Homebrew's folders,
    # so name them. /usr/bin/python3 is a stub until the developer tools are
    # installed: asking it anything opens an install dialog, every session.
    found = [[p] for p in MAC_PYTHONS if os.path.exists(p)]
    stub = shutil.which("python3") == "/usr/bin/python3"
    if stub:
        try:
            dev = subprocess.run(["xcode-select", "-p"], capture_output=True,
                                 timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            dev = False
        if not dev:
            return found
    return found + [["python3"]]


def capabilities(paths):
    """Can this machine check Onninen itself (full Python + Playwright)?"""
    full = None
    for exe in _candidates():
        if not shutil.which(exe[0]):
            continue
        try:
            r = subprocess.run(exe + ["-c", "import sys; print(sys.executable)"],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        path = r.stdout.strip()
        if r.returncode == 0 and path and "WindowsApps" not in path:   # Store stub
            full = path
            break
    playwright = False
    if full:
        try:
            playwright = subprocess.run([full, "-c", "import playwright, requests, yaml"],
                                        capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    from lib import credentials
    st = credentials.state_path("onninen")
    caps = {"python_full": full, "playwright": playwright,
            "onninen_session_hours": (round((time.time() - os.path.getmtime(st)) / 3600, 1)
                                      if os.path.exists(st) else None)}
    with open(paths.data("capabilities.json"), "w", encoding="utf-8") as fh:
        json.dump(caps, fh)
    return caps


def fi(d):
    """'20260903' or '2026-10-01T…' -> '03.09.'"""
    s = str(d or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[6:8]}.{s[4:6]}."
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}.{s[5:7]}."
    return s


def main():
    if not DATA:
        print("hinnat: datakansiota ei löytynyt (CLAUDE_PLUGIN_DATA) — plugin ei ole "
              "asennettu oikein")
        return
    common.setup(DATA)
    from lib import hub, paths

    opt = (os.environ.get("CLAUDE_PLUGIN_OPTION_KEY") or "").strip()
    if opt and not os.environ.get("HINNAT_KEY"):
        hub.save_key(opt)        # Bash-tool commands never see plugin options
    key, name = common.ensure_key(hub)
    if not key:
        print(f"hinnat: {name}. Pyydä käyttäjää sanomaan \"syötä avain\" (sinä ajat "
              f"silloin hinnat avain) tai aloittamaan uusi keskustelu. Avain saadaan "
              f"Pasilta (pasi@kannas.ai). Älä pyydä avainta chattiin.")
        return

    caps = {}
    try:
        caps = capabilities(paths)
    except Exception:                                        # noqa: BLE001
        pass

    notes, b = [], None
    try:
        b, notes = hub.sync()
    except hub.HubError as e:
        print(f"hinnat: hinnoittelupalvelu ei vastaa ({e}). Tarjouksia ei voi laskea "
              f"ennen kuin se vastaa — sano se käyttäjälle, älä arvaa hintoja.")
        return

    st = hub.state()
    parts = [f"organisaatio {b['tenant']['name']}"]
    sups = ((st.get("prices") or {}).get("report") or {}).get("suppliers") or {}
    if sups:
        parts.append("hinnasto " + ", ".join(
            f"{s.capitalize()} {fi(v.get('newest') if isinstance(v, dict) else v)}"
            for s, v in sorted(sups.items())))
    elif st.get("prices"):
        parts.append(f"hinnasto päivitetty {fi(st['prices'].get('uploaded_at'))}")
    else:
        parts.append("EI HINNASTOA — pyydä käyttäjää antamaan tukkujen hinnastotiedostot "
                     "(hinnat hinnasto)")
    if st.get("rules_updated_at"):
        parts.append(f"säännöt {fi(st['rules_updated_at'])}")
    if st.get("pending_proposals"):
        parts.append(f"{st['pending_proposals']} sääntöehdotusta odottaa hyväksyntää")
    print("hinnat: " + " · ".join(parts + notes))
    if hub.user() == "?":
        print("hinnat: käyttäjän nimeä ei ole tallennettu. Kysy ennen ensimmäistä "
              "opetusta tai hinnaston latausta: \"Miten merkitään kuka opetti sanan? "
              "Etunimi riittää.\" ja tallenna se: hinnat nimi <nimi>.")
    if caps.get("playwright"):
        h = caps.get("onninen_session_hours")
        if h is not None and h < 8:
            print(f"hinnat: Onnisen live-saldo käytössä (kirjauduttu {h:g} h sitten).")
        else:
            print("hinnat: Onnisen live-saldo käytössä, mutta kirjautuminen todennäköisesti "
                  "vanhentunut. Kun ajat tarjouksen, sano SAMASSA viestissä ennen komentoa: "
                  "\"Onnisen kirjautumisikkuna aukeaa — kirjaudu omilla tunnuksillasi, ikkuna "
                  "sulkeutuu itsestään ja laskenta jatkuu sillä välin.\"")
    mine = os.path.join(paths.ME, "preferences.md")
    if os.path.exists(mine) and os.path.getsize(mine) > 0:
        print("hinnat: käyttäjällä on omia sääntöjä (me/preferences.md) — sano "
              "tarjouksen alussa \"omat sääntösi voimassa: …\".")
    try:
        text = hub.request("GET", "/v1/instructions")["text"]
        print("\n=== HINNAT-OHJE ===\n" + text.strip() + "\n=== /HINNAT-OHJE ===")
    except hub.HubError as e:
        print(f"hinnat: ohjetta ei saatu ({e}) — aja hinnat ohje ennen ensimmäistä tarjousta.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                                   # noqa: BLE001
        print(f"hinnat: käynnistystarkistus epäonnistui ({type(e).__name__}).")
    sys.exit(0)
