"""Onninen stock from this machine, in the full Python that has requests.

    <full python> onninen_check.py <data folder> <ids.json>

ids.json is {code: Onninen page id} from the server's quote plan; the price
database is not on this machine any more (the plugin is a thin client). The
ids go into a small local table the Onninen adapter reads, next to its cache.
Prints one JSON object: {"avail": {code: [availability]}, "skipped": reason}.
Never prints a credential or the session.
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if not os.path.isdir(os.path.join(ROOT, "lib")):
    ROOT = os.path.dirname(ROOT)


def main(data, ids_path):
    os.environ["HINNAT_DATA"] = data
    sys.path.insert(0, ROOT)
    from adapters import onninen_live
    from adapters.stock_core import NotAuthenticated, as_dict
    from lib import credentials

    with open(ids_path, encoding="utf-8") as fh:
        ids = json.load(fh)
    out = {"avail": {}, "skipped": None}
    if not os.path.exists(credentials.state_path("onninen")):
        out["skipped"] = "ei Onnisen kirjautumista"
        return out
    db = os.path.join(data, "onninen.sqlite")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS products (supplier TEXT, code TEXT, sap TEXT, "
                "PRIMARY KEY (supplier, code))")
    con.executemany("INSERT OR REPLACE INTO products VALUES ('onninen', ?, ?)",
                    [(c, s) for c, s in ids.items() if s])
    con.execute("CREATE TABLE IF NOT EXISTS stock_log (ts TEXT, supplier TEXT, code TEXT, "
                "location TEXT, qty REAL, source TEXT)")
    con.commit()
    con.close()
    try:
        got = onninen_live.fetch(list(ids), db)
        out["avail"] = {c: [as_dict(a) for a in rows] for c, rows in got.items()}
    except NotAuthenticated:
        out["skipped"] = "Onnisen istunto on vanhentunut"
    except Exception as e:                                   # noqa: BLE001
        out["skipped"] = f"{type(e).__name__}: {str(e)[:140]}"
    return out


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2]), ensure_ascii=False))
