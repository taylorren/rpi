"""SQLite persistence: the system of record.

Deliberately **local** SQLite. The fetcher runs on a separate Linux box and
ships news as JSONL; it must never open this database over a network share,
because SQLite's locking over SMB/NFS is unreliable and a well-known cause of
silent corruption.

Schema notes
------------
* ``items`` is keyed by the fetcher's content hash, so ingesting the same
  inbox file twice is a no-op.
* ``analyses`` is keyed by ``(item_id, schema_version)``. That means bumping the
  schema version automatically re-queues every item for analysis, while the old
  scores are preserved. Scores produced under different schemas are **not**
  comparable, so they must never be mixed in one index.
* ``rpi_snapshots`` is keyed by timestamp *and* the config/schema versions that
  produced it, so a chart can tell when settings changed underneath it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_files (
    name        TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    records     INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    source_url   TEXT,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    link         TEXT,
    published    TEXT,
    fetched_at   TEXT,
    ingested_at  TEXT NOT NULL,
    inbox_schema INTEGER
);

CREATE INDEX IF NOT EXISTS idx_items_published ON items(published);

CREATE TABLE IF NOT EXISTS analyses (
    item_id         TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    schema_version  INTEGER NOT NULL,
    analyzed_at     TEXT NOT NULL,
    sentiment       TEXT,
    sentiment_probs TEXT,
    impact          INTEGER,
    impact_expected REAL,
    impact_probs    TEXT,
    scope           TEXT,
    scope_probs     TEXT,
    latency_ms      REAL,
    raw_response    TEXT,
    PRIMARY KEY (item_id, schema_version)
);

CREATE INDEX IF NOT EXISTS idx_analyses_version ON analyses(schema_version);

CREATE TABLE IF NOT EXISTS analysis_errors (
    item_id        TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 1,
    error          TEXT NOT NULL,
    last_attempt   TEXT NOT NULL,
    PRIMARY KEY (item_id, schema_version)
);

CREATE TABLE IF NOT EXISTS rpi_snapshots (
    ts             TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    level          REAL NOT NULL,
    s_value        REAL NOT NULL,
    item_count     INTEGER NOT NULL,
    PRIMARY KEY (ts, config_version, schema_version)
);

-- De-duplication -----------------------------------------------------------
-- Clusters are DERIVED and are rebuilt from scratch; the expensive part is
-- the pair verdicts, which are cached so the model is never asked twice.

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    representative  TEXT NOT NULL,
    first_published TEXT,
    member_count    INTEGER NOT NULL DEFAULT 1,
    rebuilt_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_members (
    item_id           TEXT PRIMARY KEY,
    cluster_id        INTEGER NOT NULL,
    is_representative INTEGER NOT NULL DEFAULT 0,
    similarity        REAL,
    decided_by        TEXT
);

CREATE INDEX IF NOT EXISTS idx_members_cluster ON cluster_members(cluster_id);

-- Pair verdicts are cached because they cost a model call. Keyed on the
-- ordered pair, so (a,b) and (b,a) share one row.
CREATE TABLE IF NOT EXISTS duplicate_pairs (
    item_a     TEXT NOT NULL,
    item_b     TEXT NOT NULL,
    same       INTEGER NOT NULL,
    score      REAL NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (item_a, item_b)
);
"""


def utcnow_iso() -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and ensure the schema exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #

def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)))


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #

def file_needs_ingest(conn: sqlite3.Connection, name: str, sha256: str) -> bool:
    row = conn.execute(
        "SELECT sha256 FROM ingested_files WHERE name = ?", (name,)).fetchone()
    return row is None or row["sha256"] != sha256


def record_file(conn: sqlite3.Connection, name: str, sha256: str,
                records: int) -> None:
    conn.execute(
        "INSERT INTO ingested_files(name, sha256, records, ingested_at) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "  sha256 = excluded.sha256, records = excluded.records, "
        "  ingested_at = excluded.ingested_at",
        (name, sha256, records, utcnow_iso()))


def upsert_items(conn: sqlite3.Connection,
                 records: Iterable[Mapping[str, Any]]) -> Tuple[int, int]:
    """Insert inbox records, ignoring ids already present.

    Returns ``(inserted, skipped)``.
    """
    now = utcnow_iso()
    inserted = 0
    skipped = 0
    for record in records:
        item_id = record.get("id")
        if not item_id:
            skipped += 1
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO items"
            "(id, source, source_url, title, summary, link, published,"
            " fetched_at, ingested_at, inbox_schema) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(item_id),
                str(record.get("source") or ""),
                record.get("source_url"),
                str(record.get("title") or ""),
                str(record.get("summary") or ""),
                record.get("link"),
                record.get("published"),
                record.get("fetched_at"),
                now,
                record.get("schema"),
            ))
        if cursor.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #

def pending_items(conn: sqlite3.Connection, schema_version: int,
                  limit: Optional[int] = None,
                  retry_failed: bool = False) -> List[sqlite3.Row]:
    """Items with no analysis at this schema version, oldest published first.

    Age ordering matters: news is a time series, and analysing oldest-first
    keeps partial runs chronologically coherent.

    Items already known to be duplicates are skipped, so a story covered by six
    outlets costs one analysis rather than six. Items that have not been
    clustered yet are treated as representatives and are analysed.
    """
    sql = [
        "SELECT i.* FROM items i",
        "WHERE NOT EXISTS (SELECT 1 FROM analyses a",
        "                  WHERE a.item_id = i.id AND a.schema_version = ?)",
        "AND NOT EXISTS (SELECT 1 FROM cluster_members m",
        "                WHERE m.item_id = i.id AND m.is_representative = 0)",
    ]
    params: List[Any] = [schema_version]
    if not retry_failed:
        sql.append("AND NOT EXISTS (SELECT 1 FROM analysis_errors e")
        sql.append("                WHERE e.item_id = i.id AND e.schema_version = ?)")
        params.append(schema_version)
    sql.append("ORDER BY COALESCE(i.published, i.fetched_at) ASC, i.id ASC")
    if limit is not None:
        sql.append("LIMIT ?")
        params.append(int(limit))
    return list(conn.execute("\n".join(sql), params))


def save_analysis(conn: sqlite3.Connection, item_id: str, schema_version: int,
                  sentiment: Optional[str], impact: Optional[int],
                  impact_expected: Optional[float], scope: Optional[str],
                  probabilities: Mapping[str, Mapping[str, float]],
                  latency_ms: Optional[float],
                  raw_response: Optional[Mapping[str, Any]] = None) -> None:
    conn.execute(
        "INSERT INTO analyses"
        "(item_id, schema_version, analyzed_at, sentiment, sentiment_probs,"
        " impact, impact_expected, impact_probs, scope, scope_probs,"
        " latency_ms, raw_response) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id, schema_version) DO UPDATE SET "
        "  analyzed_at = excluded.analyzed_at, sentiment = excluded.sentiment,"
        "  sentiment_probs = excluded.sentiment_probs, impact = excluded.impact,"
        "  impact_expected = excluded.impact_expected,"
        "  impact_probs = excluded.impact_probs, scope = excluded.scope,"
        "  scope_probs = excluded.scope_probs, latency_ms = excluded.latency_ms,"
        "  raw_response = excluded.raw_response",
        (
            item_id, schema_version, utcnow_iso(),
            sentiment, _dumps(probabilities.get("sentiment")),
            impact, impact_expected, _dumps(probabilities.get("impact")),
            scope, _dumps(probabilities.get("scope")),
            latency_ms, _dumps(raw_response),
        ))
    # A successful analysis clears any previous failure.
    conn.execute("DELETE FROM analysis_errors WHERE item_id = ? AND schema_version = ?",
                 (item_id, schema_version))


def record_analysis_error(conn: sqlite3.Connection, item_id: str,
                          schema_version: int, error: str) -> None:
    conn.execute(
        "INSERT INTO analysis_errors"
        "(item_id, schema_version, attempts, error, last_attempt) "
        "VALUES(?, ?, 1, ?, ?) "
        "ON CONFLICT(item_id, schema_version) DO UPDATE SET "
        "  attempts = attempts + 1, error = excluded.error, "
        "  last_attempt = excluded.last_attempt",
        (item_id, schema_version, error[:500], utcnow_iso()))


def pending_count(conn: sqlite3.Connection, schema_version: int) -> int:
    """Representatives still awaiting analysis.

    This is the backlog gauge. A number that keeps growing means the scoring
    service is not consuming work - the one failure that would otherwise be
    invisible, because every other stage keeps succeeding around it.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM items i"
        " WHERE NOT EXISTS (SELECT 1 FROM analyses a"
        "                   WHERE a.item_id = i.id AND a.schema_version = ?)"
        "   AND NOT EXISTS (SELECT 1 FROM cluster_members m"
        "                   WHERE m.item_id = i.id AND m.is_representative = 0)",
        (schema_version,)).fetchone()
    return int(row["n"]) if row else 0


def last_analysis_time(conn: sqlite3.Connection,
                       schema_version: int) -> Optional[str]:
    """When the most recent analysis was stored, or None."""
    row = conn.execute(
        "SELECT MAX(analyzed_at) AS t FROM analyses WHERE schema_version = ?",
        (schema_version,)).fetchone()
    return str(row["t"]) if row and row["t"] else None


def analysed_rows(conn: sqlite3.Connection, schema_version: int) -> List[sqlite3.Row]:
    """Analysed items with the fields the index calculation and audit list need.

    Where an item belongs to a cluster, the cluster's ``first_published`` is
    exposed as the event time: a story should enter the time series when it
    first broke, not when the slowest outlet got round to covering it.

    A cluster contributes at most one analysed item. Prefer the current
    representative, but fall back to any already analysed member so re-electing
    a richer representative cannot temporarily remove the story from the index.
    """
    return list(conn.execute(
        "WITH analysed AS ("
        " SELECT i.id, i.title, i.link, i.source, i.published, i.fetched_at,"
        "        a.analyzed_at, a.sentiment, a.impact_expected, a.impact, a.scope,"
        "        m.cluster_id, m.is_representative,"
        "        c.first_published AS cluster_first_published,"
        "        c.member_count AS cluster_member_count,"
        "        COALESCE(CAST(m.cluster_id AS TEXT), i.id) AS group_key,"
        "        CASE WHEN m.item_id IS NULL OR i.id = c.representative"
        "             THEN 0 ELSE 1 END AS representative_rank"
        " FROM analyses a"
        " JOIN items i ON i.id = a.item_id"
        " LEFT JOIN cluster_members m ON m.item_id = i.id"
        " LEFT JOIN clusters c ON c.cluster_id = m.cluster_id"
        " WHERE a.schema_version = ?"
        "), ranked AS ("
        " SELECT *, ROW_NUMBER() OVER ("
        "   PARTITION BY group_key"
        "   ORDER BY representative_rank ASC, analyzed_at DESC, id ASC"
        " ) AS row_number"
        " FROM analysed"
        ")"
        "SELECT id, title, link, source, published, fetched_at,"
        "       sentiment, impact_expected, impact, scope,"
        "       cluster_id, is_representative, cluster_first_published,"
        "       cluster_member_count"
        " FROM ranked"
        " WHERE row_number = 1"
        " ORDER BY COALESCE(cluster_first_published, published, fetched_at) ASC",
        (schema_version,)))


# --------------------------------------------------------------------------- #
# de-duplication
# --------------------------------------------------------------------------- #

def all_items(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every item, oldest event time first."""
    return list(conn.execute(
        "SELECT id, title, summary, link, source, published, fetched_at"
        " FROM items"
        " ORDER BY COALESCE(published, fetched_at) ASC, id ASC"))


def clear_clusters(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM cluster_members")
    conn.execute("DELETE FROM clusters")
    conn.commit()


def add_cluster(conn: sqlite3.Connection, representative: str,
                first_published: Optional[str]) -> int:
    cursor = conn.execute(
        "INSERT INTO clusters(representative, first_published, member_count,"
        " rebuilt_at) VALUES(?, ?, 1, ?)",
        (representative, first_published, utcnow_iso()))
    return int(cursor.lastrowid or 0)


def add_member(conn: sqlite3.Connection, item_id: str, cluster_id: int,
               is_representative: bool, similarity: Optional[float],
               decided_by: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO cluster_members(item_id, cluster_id, is_representative,"
        " similarity, decided_by) VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET cluster_id = excluded.cluster_id,"
        " is_representative = excluded.is_representative,"
        " similarity = excluded.similarity, decided_by = excluded.decided_by",
        (item_id, cluster_id, 1 if is_representative else 0, similarity, decided_by))


def set_cluster_representative(conn: sqlite3.Connection, cluster_id: int,
                               representative: str,
                               first_published: Optional[str]) -> None:
    conn.execute("UPDATE clusters SET representative = ?, first_published = ? "
                 "WHERE cluster_id = ?",
                 (representative, first_published, cluster_id))


def finalise_clusters(conn: sqlite3.Connection) -> None:
    """Refresh member counts and commit."""
    conn.execute(
        "UPDATE clusters SET member_count = ("
        "  SELECT COUNT(*) FROM cluster_members m"
        "  WHERE m.cluster_id = clusters.cluster_id)")
    conn.commit()


def cluster_representatives(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """One row per cluster: the representative item, for candidate matching."""
    return list(conn.execute(
        "SELECT i.id, i.title, i.summary, i.link, i.source, i.published,"
        "       i.fetched_at, c.cluster_id, c.first_published, c.member_count"
        " FROM clusters c JOIN items i ON i.id = c.representative"
        " ORDER BY COALESCE(c.first_published, i.published, i.fetched_at) ASC"))


def pair_verdict(conn: sqlite3.Connection, a: str, b: str) -> Optional[sqlite3.Row]:
    first, second = (a, b) if a <= b else (b, a)
    return conn.execute(
        "SELECT same, score, decided_by FROM duplicate_pairs"
        " WHERE item_a = ? AND item_b = ?", (first, second)).fetchone()


def save_pair_verdict(conn: sqlite3.Connection, a: str, b: str, same: bool,
                      score: float, decided_by: str) -> None:
    first, second = (a, b) if a <= b else (b, a)
    conn.execute(
        "INSERT INTO duplicate_pairs(item_a, item_b, same, score, decided_by,"
        " decided_at) VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(item_a, item_b) DO UPDATE SET same = excluded.same,"
        " score = excluded.score, decided_by = excluded.decided_by,"
        " decided_at = excluded.decided_at",
        (first, second, 1 if same else 0, score, decided_by, utcnow_iso()))


def duplicate_groups(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Clusters with more than one member, for reporting."""
    return list(conn.execute(
        "SELECT c.cluster_id, c.representative, c.first_published,"
        "       c.member_count, i.title, i.source"
        " FROM clusters c JOIN items i ON i.id = c.representative"
        " WHERE c.member_count > 1"
        " ORDER BY c.member_count DESC, c.first_published ASC"))


def cluster_sources(conn: sqlite3.Connection, cluster_id: int) -> List[str]:
    return [row[0] for row in conn.execute(
        "SELECT i.source FROM cluster_members m JOIN items i ON i.id = m.item_id"
        " WHERE m.cluster_id = ? ORDER BY i.source", (cluster_id,))]


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #

def save_snapshot(conn: sqlite3.Connection, ts: str, config_version: int,
                  schema_version: int, level: float, s_value: float,
                  item_count: int) -> None:
    conn.execute(
        "INSERT INTO rpi_snapshots"
        "(ts, config_version, schema_version, level, s_value, item_count) "
        "VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ts, config_version, schema_version) DO UPDATE SET "
        "  level = excluded.level, s_value = excluded.s_value,"
        "  item_count = excluded.item_count",
        (ts, config_version, schema_version, level, s_value, item_count))


def snapshots(conn: sqlite3.Connection, config_version: int,
              schema_version: int) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT ts, level, s_value, item_count FROM rpi_snapshots"
        " WHERE config_version = ? AND schema_version = ? ORDER BY ts ASC",
        (config_version, schema_version)))


def clear_snapshots(conn: sqlite3.Connection, config_version: int,
                    schema_version: int) -> int:
    cursor = conn.execute(
        "DELETE FROM rpi_snapshots WHERE config_version = ? AND schema_version = ?",
        (config_version, schema_version))
    conn.commit()
    return cursor.rowcount


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Row counts for a quick status report."""
    out: Dict[str, int] = {}
    for table in ("items", "analyses", "analysis_errors", "rpi_snapshots",
                  "clusters", "duplicate_pairs"):
        row = conn.execute("SELECT COUNT(*) AS n FROM {}".format(table)).fetchone()
        out[table] = int(row["n"]) if row else 0
    return out


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
