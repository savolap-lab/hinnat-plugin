"""Availability, the Onninen adapter and the stock cache — the part of the
live check a buyer's machine needs (the plugin ships this file only;
adapters/stock.py adds the suppliers the server checks).
"""
import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field

UNKNOWN = "unknown"


class NotAuthenticated(RuntimeError):
    """The page rendered, but quantities are withheld.

    Both suppliers serve the availability block to anonymous visitors with the
    numbers stripped: Onninen keeps plantType and `available` but drops
    `quantity`; Sonepar omits `deliveryIndicators` entirely. Reporting that as
    a schema change would send someone hunting a parser bug that is not there.
    """


@dataclass
class Availability:
    supplier: str
    code: str
    location: str                 # plant / facility id
    location_type: str            # DISTRIBUTION_CENTRE | EXPRESS | PICKUP
    qty: float | None             # None => unknown
    unit: str = ""
    replenishments: list = field(default_factory=list)   # [(date, qty)]
    unlimited_from: str | None = None                    # ISO date
    order_before: str | None = None                      # ISO datetime cutoff
    available_from: str | None = None                    # ISO date, not before
    source: str = UNKNOWN
    fetched_at: str = ""

    def available_by(self, when):
        """Quantity obtainable by `when` (date), counting inbound deliveries.

        This is the question that matters: not "what is there now" but "what
        will be there when the order actually goes in".
        """
        if self.qty is None:
            return None
        when = when if isinstance(when, dt.date) else dt.date.fromisoformat(str(when))
        if self.available_from:
            # Sonepar quotes a delivery quantity together with the date it can
            # actually be delivered. Counting it before that date would pass a
            # line that cannot arrive in time.
            if when < dt.date.fromisoformat(self.available_from[:10]):
                return 0.0
        if self.unlimited_from and when >= dt.date.fromisoformat(self.unlimited_from):
            return float("inf")
        total = self.qty
        for d, q in self.replenishments:
            if dt.date.fromisoformat(str(d)) <= when:
                total += q
        return total


class Adapter:
    supplier = "?"

    def fetch(self, codes, session=None):
        raise NotImplementedError


class OnninenAdapter(Adapter):
    """Onninen returns one entry per plant.

    Observed shape (21.08.2026):
        {"plant": "1002", "plantType": "DISTRIBUTION_CENTRE",
         "quantity": 865, "available": true,
         "replenishments": [{"date": "2026-08-25", "quantity": 17343}, ...],
         "batches": [{"batch": "1", "availableQuantity": 8}, ...],
         "unlimitedReplenishmentDate": "2026-09-15"}

    `batches` is per-drum. We deliberately ignore it for now: the buyers said
    availability total is the deciding factor. If continuous-length ever
    matters, the check becomes max(batch) >= qty instead of sum >= qty.
    """
    supplier = "onninen"

    def parse(self, payload, code, now=None):
        out = []
        now = now or dt.datetime.now(dt.timezone.utc).isoformat()
        for p in payload:
            # Structure must be there. `quantity` is withheld from anonymous
            # visitors, so its absence means unknown — never zero, and never a
            # crash. looks_unauthenticated() turns all-unknown into a clear
            # message one level up.
            for k in ("plant", "plantType"):
                if k not in p:
                    raise KeyError(f"onninen stock payload missing {k!r} — "
                                   f"schema changed, refusing to guess")
            raw_qty = p.get("quantity")
            if raw_qty is None:
                qty = None
            elif p.get("available"):
                qty = float(raw_qty)
            else:
                qty = 0.0
            out.append(Availability(
                supplier=self.supplier, code=code,
                location=str(p["plant"]), location_type=p["plantType"],
                qty=qty,
                replenishments=[(r["date"], float(r["quantity"]))
                                for r in p.get("replenishments") or []],
                unlimited_from=p.get("unlimitedReplenishmentDate"),
                source="onninen:getStocks", fetched_at=now,
            ))
        return out

    def fetch(self, codes, session=None):
        raise NotImplementedError(
            "wire up the authenticated session, then call self.parse(payload, code)")


def as_dict(a):
    """Availability -> plain JSON. The saldo service sends exactly this."""
    return {"supplier": a.supplier, "code": a.code, "location": a.location,
            "location_type": a.location_type, "qty": a.qty, "unit": a.unit,
            "replenishments": [[str(d), q] for d, q in a.replenishments],
            "unlimited_from": a.unlimited_from, "order_before": a.order_before,
            "available_from": a.available_from, "source": a.source,
            "fetched_at": a.fetched_at}


def from_dict(d):
    """Inverse of as_dict. Unknown keys are ignored; a missing qty is None."""
    return Availability(
        supplier=d["supplier"], code=str(d["code"]),
        location=d.get("location") or "", location_type=d.get("location_type") or "",
        qty=d.get("qty"), unit=d.get("unit") or "",
        replenishments=[(r[0], r[1]) for r in d.get("replenishments") or []],
        unlimited_from=d.get("unlimited_from"), order_before=d.get("order_before"),
        available_from=d.get("available_from"), source=d.get("source") or UNKNOWN,
        fetched_at=d.get("fetched_at") or "")


def looks_unauthenticated(avails):
    """True when the block parsed but carries no quantity anywhere.

    That is exactly what an anonymous request returns, and it is worth saying
    so plainly rather than letting every line come back "unknown" with no
    explanation.
    """
    return bool(avails) and all(a.qty is None for a in avails)


def cached(db, supplier, code, ttl):
    """What a live fetch returned for this code less than `ttl` s ago.

    -> [Availability] or None. The adapters used to look at stock_log, find a
    recent row and then *skip* the code — so a second quote within two hours
    came back unverified instead of with the numbers. stock_log cannot give
    them back either: it keeps qty per location, not replenishments or
    delivery dates. So the full rows live in their own table.
    """
    if not ttl:
        return None
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT ts, payload FROM live_cache "
                              "WHERE supplier=? AND code=?",
                              (supplier, code)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:                                   # no table yet
        return None
    if not row:
        return None
    try:
        age = (dt.datetime.now(dt.timezone.utc)
               - dt.datetime.fromisoformat(row[0])).total_seconds()
        rows = [from_dict(d) for d in json.loads(row[1])]
    except (ValueError, KeyError, TypeError):
        return None
    return rows if 0 <= age < ttl else None


def remember(db, supplier, code, avails):
    """Keep a fetch for cached(). A failure to write is not a failed fetch."""
    try:
        con = sqlite3.connect(db)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS live_cache (supplier TEXT, "
                        "code TEXT, ts TEXT, payload TEXT, "
                        "PRIMARY KEY (supplier, code))")
            con.execute("INSERT OR REPLACE INTO live_cache VALUES (?,?,?,?)",
                        (supplier, code,
                         dt.datetime.now(dt.timezone.utc).isoformat(),
                         json.dumps([as_dict(a) for a in avails])))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def log(db, avails):
    """Every reading is kept. After a few months this tells you which items
    never need checking and which are chronically tight."""
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO stock_log VALUES (?,?,?,?,?,?)",
        [(a.fetched_at, a.supplier, a.code, f"{a.location_type}:{a.location}",
          a.qty, a.source) for a in avails])
    con.commit()
    con.close()
