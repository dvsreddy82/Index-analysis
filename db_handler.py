"""MongoDB persistence layer for the `nse_analytics` database.

Collections
-----------
index_ohlcv       { symbol, timeframe, timestamp, open, high, low, close, volume }
                  unique compound index (symbol, timeframe, timestamp)
fii_dii_activity  { date, fii_cash_net, dii_cash_net,
                    fii_index_futures_long_short_ratio, client_ratio, ... }
                  unique index (date)
sr_levels         { symbol, calculated_date, timeframe, support_zones, resistance_zones,
                    poc_level, vah, val, pivots, signal }
                  unique compound index (symbol, timeframe, calculated_date)
macro_series      { series, date, close }   (catalyst module: crude, USD/INR, ...)

All writes are idempotent upserts keyed on the natural key, so re-running the
daily sync never creates duplicates.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError

import config

log = logging.getLogger(__name__)

# JSON-schema validators: loose enough to accept optional enrichment fields,
# strict enough to reject malformed rows at the database boundary.
_NUM = {"bsonType": ["double", "int", "long", "decimal", "null"]}
SCHEMAS: dict[str, dict] = {
    "index_ohlcv": {
        "bsonType": "object",
        "required": ["symbol", "timeframe", "timestamp", "open", "high", "low", "close"],
        "properties": {
            "symbol": {"bsonType": "string"},
            "timeframe": {"enum": config.STORED_TIMEFRAMES},
            "timestamp": {"bsonType": "date"},
            "open": _NUM, "high": _NUM, "low": _NUM, "close": _NUM, "volume": _NUM,
        },
    },
    "fii_dii_activity": {
        "bsonType": "object",
        "required": ["date"],
        "properties": {
            "date": {"bsonType": "date"},
            "fii_cash_net": _NUM,
            "dii_cash_net": _NUM,
            "fii_index_futures_long_short_ratio": _NUM,
            "client_ratio": _NUM,
            "pcr": _NUM,
        },
    },
    "sr_levels": {
        "bsonType": "object",
        "required": ["symbol", "calculated_date", "timeframe"],
        "properties": {
            "symbol": {"bsonType": "string"},
            "calculated_date": {"bsonType": "date"},
            "timeframe": {"bsonType": "string"},
            "support_zones": {"bsonType": "array"},
            "resistance_zones": {"bsonType": "array"},
            "poc_level": _NUM,
        },
    },
    "macro_series": {
        "bsonType": "object",
        "required": ["series", "date", "close"],
        "properties": {
            "series": {"bsonType": "string"},
            "date": {"bsonType": "date"},
            "close": _NUM,
        },
    },
}


def _to_utc(ts) -> datetime:
    """Normalise any timestamp-like to a tz-aware UTC datetime for BSON."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(config.IST)
    return t.tz_convert("UTC").to_pydatetime()


def _date_key(d) -> datetime:
    """Calendar dates are stored as midnight UTC of the IST trading date."""
    t = pd.Timestamp(d)
    if t.tzinfo is not None:
        t = t.tz_convert(config.IST).tz_localize(None)
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc)


def _clean(v):
    """Convert numpy/pandas scalars & NaN to BSON-friendly values."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        return v
    if hasattr(v, "item"):
        return v.item()
    return v


class DBHandler:
    """Thin, thread-safe (PyMongo clients are) wrapper around nse_analytics."""

    def __init__(self, uri: str = config.MONGO_URI, db_name: str = config.MONGO_DB,
                 timeout_ms: int = config.MONGO_TIMEOUT_MS):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, tz_aware=True,
                                  appname="nse-analytics-terminal")
        # Fail fast with a clear error instead of hanging on the first query.
        try:
            self.client.admin.command("ping")
        except ServerSelectionTimeoutError as exc:
            raise ConnectionError(
                f"MongoDB not reachable at {uri}. Start it with "
                f"`brew services start mongodb-community` (macOS) or "
                f"`sudo systemctl start mongod` (Linux)."
            ) from exc
        self.db = self.client[db_name]
        self.ohlcv: Collection = self.db["index_ohlcv"]
        self.fii_dii: Collection = self.db["fii_dii_activity"]
        self.sr_levels: Collection = self.db["sr_levels"]
        self.macro: Collection = self.db["macro_series"]
        # broker / trade journal (see trades_engine.py); key fields per collection
        self.journal_keys = {"broker_fills": ("fill_id",), "broker_orders": ("order_no", "date"),
                             "pnl_daily": ("date",), "trade_journal": ("strategy_id",), "ai_reviews": ("review_id",),
                             "option_chain_snapshots": ("underlying", "expiry", "ts"), "sync_log": ("job",),
                             "broker_positions": ("date",),
                             "index_constituents": ("index",), "index_weights": ("index",)}
        self.ensure_schema()

    # ------------------------------------------------------------ setup
    def ensure_schema(self) -> None:
        existing = set(self.db.list_collection_names())
        for name, schema in SCHEMAS.items():
            validator = {"$jsonSchema": schema}
            try:
                if name not in existing:
                    self.db.create_collection(name, validator=validator,
                                              validationLevel="moderate")
                else:
                    self.db.command("collMod", name, validator=validator,
                                    validationLevel="moderate")
            except PyMongoError as exc:  # e.g. insufficient privileges
                log.warning("Could not apply validator to %s: %s", name, exc)

        self.ohlcv.create_index(
            [("symbol", ASCENDING), ("timeframe", ASCENDING), ("timestamp", ASCENDING)],
            unique=True, name="symbol_tf_ts_unique")
        self.ohlcv.create_index([("symbol", ASCENDING), ("timestamp", DESCENDING)],
                                name="symbol_ts")
        self.fii_dii.create_index([("date", ASCENDING)], unique=True, name="date_unique")
        self.sr_levels.create_index(
            [("symbol", ASCENDING), ("timeframe", ASCENDING), ("calculated_date", DESCENDING)],
            unique=True, name="symbol_tf_date_unique")
        self.macro.create_index([("series", ASCENDING), ("date", ASCENDING)],
                                unique=True, name="series_date_unique")
        for name, keys in self.journal_keys.items():
            self.db[name].create_index([(k, ASCENDING) for k in keys], unique=True, name="_".join(keys) + "_unique")
        self.db["broker_fills"].create_index([("time", DESCENDING)], name="time")
        self.db["option_chain_snapshots"].create_index([("underlying", ASCENDING), ("ts", DESCENDING)],
                                                       name="underlying_ts")

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _bulk_upsert(coll: Collection, ops: list[UpdateOne]) -> int:
        if not ops:
            return 0
        res = coll.bulk_write(ops, ordered=False)
        return res.upserted_count + res.modified_count

    # ------------------------------------------------------------ OHLCV
    def upsert_ohlcv(self, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        """df: DatetimeIndex + open/high/low/close/volume columns."""
        ops = []
        for ts, row in df.iterrows():
            key = {"symbol": symbol, "timeframe": timeframe, "timestamp": _to_utc(ts)}
            doc = {c: _clean(row.get(c)) for c in ("open", "high", "low", "close", "volume")}
            if doc["close"] is None:
                continue
            doc["volume"] = doc["volume"] or 0
            doc["updated_at"] = datetime.now(timezone.utc)
            ops.append(UpdateOne(key, {"$set": doc}, upsert=True))
        n = self._bulk_upsert(self.ohlcv, ops)
        log.info("OHLCV %s %s: %d rows written", symbol, timeframe, n)
        return n

    def load_ohlcv(self, symbol: str, timeframe: str,
                   start: datetime | None = None, limit: int | None = None) -> pd.DataFrame:
        q: dict = {"symbol": symbol, "timeframe": timeframe}
        if start is not None:
            q["timestamp"] = {"$gte": _to_utc(start)}
        cur = self.ohlcv.find(q, {"_id": 0, "symbol": 0, "timeframe": 0, "updated_at": 0})
        if limit:
            cur = cur.sort("timestamp", DESCENDING).limit(limit)
        else:
            cur = cur.sort("timestamp", ASCENDING)
        df = pd.DataFrame(list(cur))
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(config.IST)
        return df.set_index("timestamp").sort_index()

    def latest_timestamp(self, symbol: str, timeframe: str) -> datetime | None:
        doc = self.ohlcv.find_one({"symbol": symbol, "timeframe": timeframe},
                                  sort=[("timestamp", DESCENDING)], projection={"timestamp": 1})
        return doc["timestamp"] if doc else None

    # ------------------------------------------------------------ FII / DII
    def upsert_fii_dii(self, records: Iterable[dict]) -> int:
        """Each record needs a `date`; other fields are merged ($set), so cash data
        and participant-OI data fetched separately for the same day combine."""
        ops = []
        for r in records:
            if not r.get("date"):
                continue
            key = {"date": _date_key(r["date"])}
            fields = {k: _clean(v) for k, v in r.items() if k != "date"}
            fields = {k: v for k, v in fields.items() if v is not None}
            if not fields:
                continue
            fields["updated_at"] = datetime.now(timezone.utc)
            ops.append(UpdateOne(key, {"$set": fields}, upsert=True))
        return self._bulk_upsert(self.fii_dii, ops)

    def load_fii_dii(self, start: datetime | None = None) -> pd.DataFrame:
        q = {"date": {"$gte": _date_key(start)}} if start is not None else {}
        df = pd.DataFrame(list(self.fii_dii.find(q, {"_id": 0, "updated_at": 0})
                               .sort("date", ASCENDING)))
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None).dt.normalize()
        return df.set_index("date").sort_index()

    def fii_dii_dates(self) -> set[pd.Timestamp]:
        return {pd.Timestamp(d["date"]).tz_localize(None).normalize()
                for d in self.fii_dii.find({"fii_index_futures_long_short_ratio": {"$ne": None}},
                                           {"date": 1})}

    # ------------------------------------------------------------ S/R levels
    def upsert_sr_levels(self, doc: dict) -> None:
        key = {"symbol": doc["symbol"], "timeframe": doc["timeframe"],
               "calculated_date": _date_key(doc["calculated_date"])}
        body = {k: v for k, v in doc.items() if k not in key}
        body["updated_at"] = datetime.now(timezone.utc)
        self.sr_levels.update_one(key, {"$set": body}, upsert=True)

    def latest_sr_levels(self, symbol: str, timeframe: str) -> dict | None:
        return self.sr_levels.find_one({"symbol": symbol, "timeframe": timeframe},
                                       {"_id": 0}, sort=[("calculated_date", DESCENDING)])

    # ------------------------------------------------------------ macro
    def upsert_macro(self, series: str, s: pd.Series) -> int:
        ops = [UpdateOne({"series": series, "date": _date_key(d)},
                         {"$set": {"close": _clean(v)}}, upsert=True)
               for d, v in s.dropna().items()]
        return self._bulk_upsert(self.macro, ops)

    def load_macro(self, start: datetime | None = None) -> pd.DataFrame:
        q = {"date": {"$gte": _date_key(start)}} if start is not None else {}
        df = pd.DataFrame(list(self.macro.find(q, {"_id": 0})))
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
        return df.pivot_table(index="date", columns="series", values="close").sort_index()


# ------------------------------------------------------------ broker / journal docs
def _doc_clean(d):
    if isinstance(d, dict):
        return {k: _doc_clean(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_doc_clean(v) for v in d]
    if isinstance(d, pd.Timestamp):
        return d.to_pydatetime()
    return _clean(d)


def _upsert_docs(self, name: str, docs: list[dict], insert_only: tuple[str, ...] = ()) -> int:
    """Upsert by the collection's natural key; `insert_only` fields (e.g. the
    market context stamped on a fill) are written the first time only."""
    keys = self.journal_keys[name]
    ops = []
    for d in docs:
        d = _doc_clean(d)
        key = {k: d[k] for k in keys}
        upd = {"$set": {k: v for k, v in d.items() if k not in key and k not in insert_only}}
        ins = {k: d[k] for k in insert_only if k in d}
        if ins:
            upd["$setOnInsert"] = ins
        upd["$set"]["updated_at"] = datetime.now(timezone.utc)
        ops.append(UpdateOne(key, upd, upsert=True))
    return self._bulk_upsert(self.db[name], ops)


def _find_docs(self, name: str, query: dict | None = None, sort: str | None = None,
               desc: bool = False, limit: int | None = None) -> list[dict]:
    cur = self.db[name].find(query or {}, {"_id": 0})
    if sort:
        cur = cur.sort(sort, DESCENDING if desc else ASCENDING)
    if limit:
        cur = cur.limit(limit)
    return list(cur)


def _delete_docs(self, name: str, query: dict) -> int:
    return self.db[name].delete_many(query).deleted_count


DBHandler.upsert_docs = _upsert_docs
DBHandler.find_docs = _find_docs
DBHandler.delete_docs = _delete_docs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = DBHandler()
    for name in SCHEMAS:
        print(f"{name:18s} docs={db.db[name].estimated_document_count():>7d} "
              f"indexes={list(db.db[name].index_information())}")
