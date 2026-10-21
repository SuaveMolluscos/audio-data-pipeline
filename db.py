#!/usr/bin/env python3
"""
db.py — Persistencia del crawler/analyzer musical.

Principios:

1. Una canción no se duplica: canonical_id UNIQUE
2. No se pierde información del analyzer: raw_json contiene el resultado completo.
3. Los datos importantes para recomendaciones también están estructurados.
4. Los tags NO se filtran. Todo tag producido por el analyzer se conserva.
5. La operación de guardar canción + artistas + tags + review es atómica.
6. SQLite: WAL, foreign_keys, thread-local connections.
7. Timestamps en formato SQLite nativo: 'YYYY-MM-DD HH:MM:SS' (UTC).

Cambios en esta revisión:
- FIX (estructural): insert_song y update_song_from_analysis mantenían
  ~60 columnas duplicadas en dos SQL distintos — agregar una columna
  significaba tocar 4 lugares, y olvidar uno dejaba datos desactualizados
  en silencio. Ahora _SONG_COLUMNS es la única fuente de verdad: el SQL
  de INSERT ... ON CONFLICT(canonical_id) DO UPDATE se genera a partir de
  esa tupla. update_song_from_analysis desaparece; insert_song hace
  upsert atómico y devuelve el id en ambos casos (inserción o
  actualización) vía un SELECT explícito, sin depender de lastrowid
  (que no es fiable cuando el UPSERT resuelve por UPDATE).
- ADD: columna artist_display_normalized + índice, para poder buscar
  canciones por artista cuando no hay artist_mbid (muchos artistas
  independientes no están en MusicBrainz).
- ADD: insert_from_analysis ahora recorre TODOS los artist_mbids del
  analyzer (antes solo enlazaba el primero). Los créditos secundarios
  se enlazan con role='featured' cuando hay un nombre disponible.
  Ahora mismo el analyzer solo expone el nombre del artista principal
  (`data["artist"]`), así que los créditos secundarios sin nombre se
  omiten en vez de crear un artista con nombre vacío — si más adelante
  analyzer.py expone `artist_names` (ya tiene los datos de MusicBrainz
  para hacerlo), esto empieza a completarse sin tocar db.py.
- ADD: PRAGMA user_version para versionar el esquema explícitamente.

CLI:
    python db.py init
    python db.py stats
    python db.py check
    python db.py reset --yes
    python db.py vacuum
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import unicodedata

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional


DEFAULT_DB_PATH = "db/tracks.db"
SCHEMA_VERSION = 3

_local = threading.local()


# ============================================================================
# UTILIDADES
# ============================================================================

def _now() -> str:
    """UTC timestamp en formato SQLite: 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize(value: str | None) -> str:
    """Normaliza texto para búsquedas y deduplicación lógica."""
    text = unicodedata.normalize("NFKD", (value or "").casefold().strip())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _json_default(value: Any) -> Any:
    """Serializa numpy floats y otros tipos no-JSON."""
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


# ============================================================================
# CONEXIONES
# ============================================================================

def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA temp_store = MEMORY")


def _resolve_db_path(db_path: str) -> str:
    if db_path == ":memory:":
        return db_path
    return str(Path(db_path).expanduser().resolve())


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Conexión específica por thread y por ruta de DB."""
    resolved = _resolve_db_path(db_path)
    connections = getattr(_local, "connections", None)
    if connections is None:
        connections = {}
        _local.connections = connections

    conn = connections.get(resolved)
    if conn is not None:
        return conn

    if resolved != ":memory:":
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        resolved,
        timeout=10,
        isolation_level=None,
        check_same_thread=True,
    )
    _configure(conn)
    connections[resolved] = conn
    return conn


def close_connection(db_path: str | None = None) -> None:
    connections = getattr(_local, "connections", {})
    if db_path is not None:
        resolved = _resolve_db_path(db_path)
        conn = connections.pop(resolved, None)
        if conn is not None:
            try:
                conn.execute("PRAGMA optimize")
            finally:
                conn.close()
        return

    for conn in list(connections.values()):
        try:
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()
    connections.clear()


@contextmanager
def transaction(db_path: str = DEFAULT_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Transacción atómica; usa SAVEPOINT si ya hay una abierta."""
    conn = get_connection(db_path)

    if conn.in_transaction:
        savepoint = f"sp_{id(conn)}_{threading.get_ident()}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield conn
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ============================================================================
# ESQUEMA
# ============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS songs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    canonical_id            TEXT NOT NULL UNIQUE,
    canonical_id_type       TEXT NOT NULL,
    provisional_identity    INTEGER NOT NULL DEFAULT 0,

    title                   TEXT NOT NULL,
    artist_display          TEXT NOT NULL,
    artist_display_normalized TEXT,

    album                   TEXT,
    genre                   TEXT,
    year                    TEXT,
    duration_sec            REAL,
    country                 TEXT,

    mbid                    TEXT,
    artist_mbid             TEXT,
    isrc                    TEXT,

    itunes_id               INTEGER,
    deezer_id               INTEGER,
    deezer_bpm              REAL,

    resolution_status       TEXT NOT NULL DEFAULT 'unknown',
    resolution_confidence   REAL,

    requires_review         INTEGER NOT NULL DEFAULT 0,

    audio_method            TEXT,
    audio_analyzed_sec      REAL,
    audio_segments          INTEGER,

    key_name                TEXT,
    key_confidence          REAL,
    key_margin              REAL,

    tempo_mean              REAL,
    tempo_std               REAL,
    tempo_min               REAL,
    tempo_max               REAL,

    energy_rms_mean         REAL,
    energy_rms_std          REAL,
    energy_rms_min          REAL,
    energy_rms_max          REAL,

    energy_norm_peak_mean   REAL,
    energy_norm_peak_std    REAL,
    energy_norm_peak_min    REAL,
    energy_norm_peak_max    REAL,

    acousticness_zcr_mean   REAL,
    acousticness_zcr_std    REAL,
    acousticness_zcr_min    REAL,
    acousticness_zcr_max    REAL,

    brightness_mean         REAL,
    brightness_std          REAL,
    brightness_min          REAL,
    brightness_max          REAL,

    zcr_mean                REAL,
    zcr_std                 REAL,
    zcr_min                 REAL,
    zcr_max                 REAL,

    rolloff_mean            REAL,
    rolloff_std             REAL,
    rolloff_min             REAL,
    rolloff_max             REAL,

    onset_mean              REAL,
    onset_std               REAL,
    onset_min               REAL,
    onset_max               REAL,

    rhythm_mean             REAL,
    rhythm_std              REAL,
    rhythm_min              REAL,
    rhythm_max              REAL,

    features_json           TEXT,
    completeness_json       TEXT,
    provenance_json         TEXT,
    raw_json                TEXT,

    original_query          TEXT,

    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    last_analyzed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_songs_artist_mbid ON songs(artist_mbid);
CREATE INDEX IF NOT EXISTS idx_songs_mbid        ON songs(mbid);
CREATE INDEX IF NOT EXISTS idx_songs_isrc        ON songs(isrc);
CREATE INDEX IF NOT EXISTS idx_songs_genre       ON songs(genre);
CREATE INDEX IF NOT EXISTS idx_songs_year        ON songs(year);
CREATE INDEX IF NOT EXISTS idx_songs_review
    ON songs(requires_review) WHERE requires_review = 1;
CREATE INDEX IF NOT EXISTS idx_songs_artist_display_normalized
    ON songs(artist_display_normalized);

CREATE TABLE IF NOT EXISTS song_analysis_versions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id                 INTEGER NOT NULL,
    analyzed_at             TEXT NOT NULL,
    analyzer_version        TEXT,
    ok                      INTEGER NOT NULL DEFAULT 0,
    requires_review         INTEGER NOT NULL DEFAULT 0,
    resolution_status       TEXT,
    resolution_confidence   REAL,
    completeness_json       TEXT,
    raw_json                TEXT NOT NULL,
    original_query          TEXT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_analysis_versions_song
    ON song_analysis_versions(song_id, analyzed_at DESC);

CREATE TABLE IF NOT EXISTS artists (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_mbid     TEXT UNIQUE,
    name            TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artists_name_normalized ON artists(name_normalized);

CREATE TABLE IF NOT EXISTS song_artists (
    song_id     INTEGER NOT NULL,
    artist_id   INTEGER NOT NULL,
    role        TEXT NOT NULL DEFAULT 'primary',
    PRIMARY KEY (song_id, artist_id, role),
    FOREIGN KEY (song_id)   REFERENCES songs(id)   ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_song_artists_artist ON song_artists(artist_id);

CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_normalized TEXT NOT NULL UNIQUE,
    songs_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_name_normalized ON tags(name_normalized);

CREATE TABLE IF NOT EXISTS song_tags (
    song_id     INTEGER NOT NULL,
    tag_id      INTEGER NOT NULL,
    weight      REAL,
    PRIMARY KEY (song_id, tag_id),
    FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_song_tags_tag ON song_tags(tag_id);

CREATE TABLE IF NOT EXISTS queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type       TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    dedupe_key      TEXT,
    priority        REAL NOT NULL DEFAULT 5.0,
    depth           INTEGER NOT NULL DEFAULT 0,
    source          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    worker_id       TEXT,
    lease_until     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_pending
    ON queue(status, priority DESC, created_at ASC)
    WHERE status = 'pending';

CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_active_dedupe
    ON queue(dedupe_key)
    WHERE dedupe_key IS NOT NULL
      AND status IN ('pending', 'processing');

CREATE TABLE IF NOT EXISTS review_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT NOT NULL,
    song_id         INTEGER,
    confidence      REAL,
    evidence_json   TEXT,
    candidates_json TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    resolution_note TEXT,
    resolved_at     TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_review_pending
    ON review_queue(status) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS failed_queries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT NOT NULL,
    error           TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE(query, error)
);

CREATE INDEX IF NOT EXISTS idx_failures_last_seen ON failed_queries(last_seen_at);

CREATE TABLE IF NOT EXISTS crawl_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT NOT NULL,
    songs_total     INTEGER,
    songs_verified  INTEGER,
    songs_ambiguous INTEGER,
    queue_pending   INTEGER,
    queue_done      INTEGER,
    failures_total  INTEGER,
    avg_seconds     REAL
);
"""


# ============================================================================
# CREACIÓN / MIGRACIÓN
# ============================================================================

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_database(conn: sqlite3.Connection) -> None:
    """Migración tolerante; no destruye datos existentes."""

    if _table_exists(conn, "songs"):
        columns = _columns(conn, "songs")
        additions = {
            "artist_display_normalized": "TEXT",
            "audio_method": "TEXT",
            "audio_analyzed_sec": "REAL",
            "audio_segments": "INTEGER",
            "key_name": "TEXT",
            "key_confidence": "REAL",
            "key_margin": "REAL",
            "tempo_mean": "REAL", "tempo_std": "REAL", "tempo_min": "REAL", "tempo_max": "REAL",
            "energy_rms_mean": "REAL", "energy_rms_std": "REAL", "energy_rms_min": "REAL", "energy_rms_max": "REAL",
            "energy_norm_peak_mean": "REAL", "energy_norm_peak_std": "REAL",
            "energy_norm_peak_min": "REAL", "energy_norm_peak_max": "REAL",
            "acousticness_zcr_mean": "REAL", "acousticness_zcr_std": "REAL",
            "acousticness_zcr_min": "REAL", "acousticness_zcr_max": "REAL",
            "brightness_mean": "REAL", "brightness_std": "REAL", "brightness_min": "REAL", "brightness_max": "REAL",
            "zcr_mean": "REAL", "zcr_std": "REAL", "zcr_min": "REAL", "zcr_max": "REAL",
            "rolloff_mean": "REAL", "rolloff_std": "REAL", "rolloff_min": "REAL", "rolloff_max": "REAL",
            "onset_mean": "REAL", "onset_std": "REAL", "onset_min": "REAL", "onset_max": "REAL",
            "rhythm_mean": "REAL", "rhythm_std": "REAL", "rhythm_min": "REAL", "rhythm_max": "REAL",
        }
        for column, definition in additions.items():
            if column not in columns:
                _add_column_if_missing(conn, "songs", column, definition)

        # Backfill de artist_display_normalized para filas existentes.
        rows = conn.execute(
            "SELECT id, artist_display FROM songs "
            "WHERE artist_display_normalized IS NULL OR artist_display_normalized = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE songs SET artist_display_normalized = ? WHERE id = ?",
                (_normalize(row["artist_display"]), row["id"]),
            )

    if _table_exists(conn, "tags"):
        _add_column_if_missing(conn, "tags", "name_normalized", "TEXT")
        _add_column_if_missing(conn, "tags", "songs_count", "INTEGER NOT NULL DEFAULT 0")

        rows = conn.execute("SELECT id, name FROM tags WHERE name_normalized IS NULL OR name_normalized = ''").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tags SET name_normalized = ? WHERE id = ?",
                (_normalize(row["name"]), row["id"]),
            )

        # Recalcular songs_count para tags migrados
        conn.execute("""
            UPDATE tags
            SET songs_count = (
                SELECT COUNT(*) FROM song_tags WHERE song_tags.tag_id = tags.id
            )
        """)

    if _table_exists(conn, "queue"):
        for column, definition in {
            "dedupe_key": "TEXT",
            "worker_id": "TEXT",
            "lease_until": "TEXT",
            "started_at": "TEXT",
            "finished_at": "TEXT",
        }.items():
            _add_column_if_missing(conn, "queue", column, definition)

    if _table_exists(conn, "review_queue"):
        _add_column_if_missing(conn, "review_queue", "resolution_note", "TEXT")
        _add_column_if_missing(conn, "review_queue", "resolved_at", "TEXT")

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def create_database(db_path: str = DEFAULT_DB_PATH) -> bool:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    _migrate_database(conn)
    return True


def reset_database(db_path: str = DEFAULT_DB_PATH, confirm: bool = False) -> None:
    if not confirm:
        raise RuntimeError("reset_database requiere confirm=True")
    conn = get_connection(db_path)
    conn.executescript("""
        DROP TABLE IF EXISTS crawl_stats;
        DROP TABLE IF EXISTS failed_queries;
        DROP TABLE IF EXISTS review_queue;
        DROP TABLE IF EXISTS song_analysis_versions;
        DROP TABLE IF EXISTS queue;
        DROP TABLE IF EXISTS song_tags;
        DROP TABLE IF EXISTS tags;
        DROP TABLE IF EXISTS song_artists;
        DROP TABLE IF EXISTS artists;
        DROP TABLE IF EXISTS songs;
    """)
    conn.executescript(SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ============================================================================
# EXTRACCIÓN DE FEATURES
# ============================================================================

def _feature_stats(
    features: dict | None, name: str
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Extrae (mean, std, min, max) de un feature."""
    if not isinstance(features, dict):
        return None, None, None, None
    value = features.get(name)
    if not isinstance(value, dict):
        return None, None, None, None
    return value.get("mean"), value.get("std"), value.get("min"), value.get("max")


def _extract_audio_features(data: dict) -> dict:
    """
    Extrae features del analyzer.

    El analyzer entrega todo dentro de data["features"] con esta forma:

        data["features"] = {
            "method": "librosa",
            "analyzed_duration_sec": 29.975,
            "segments_analyzed": 3,
            "statistics": {
                "tempo_bpm": {"mean":..., "std":..., "min":..., "max":...},
                ...
            },
            "key": {"key": "F#", "mode": "minor", "confidence":..., "key_margin":...},
        }
    """
    features = data.get("features") or {}
    stats = features.get("statistics") or {}

    key = features.get("key") or {}
    if isinstance(key, str):
        key_name = key
        key_confidence = None
        key_margin = None
    else:
        note = key.get("key") or key.get("name")
        mode = key.get("mode")
        if note and mode:
            key_name = f"{note} {mode}"
        else:
            key_name = note
        key_confidence = key.get("confidence")
        key_margin = key.get("key_margin") or key.get("margin")

    def s(name: str):
        return _feature_stats(stats, name)

    return {
        "audio_method": features.get("method"),
        "audio_analyzed_sec": features.get("analyzed_duration_sec"),
        "audio_segments": features.get("segments_analyzed"),
        "key_name": key_name,
        "key_confidence": key_confidence,
        "key_margin": key_margin,
        "tempo": s("tempo_bpm"),
        "energy_rms": s("energy_rms"),
        "energy_norm_peak": s("energy_norm_peak"),
        "acousticness": s("acousticness_proxy_zcr"),
        "brightness": s("brightness_centroid_hz"),
        "zcr": s("zero_crossing_rate"),
        "rolloff": s("spectral_rolloff_hz"),
        "onset": s("onset_rate"),
        "rhythm": s("rhythmic_regularity"),
    }


# ============================================================================
# SONGS
# ============================================================================
#
# _SONG_COLUMNS es la ÚNICA fuente de verdad para el esquema de `songs`
# usado en el upsert. El SQL de INSERT ... ON CONFLICT DO UPDATE se genera
# a partir de esta tupla, así que agregar una columna nueva significa
# tocar UN solo lugar (aquí + _song_insert_values), no cuatro.
#
_SONG_COLUMNS = (
    "canonical_id", "canonical_id_type", "provisional_identity",
    "title", "artist_display", "artist_display_normalized",
    "album", "genre", "year", "duration_sec", "country",
    "mbid", "artist_mbid", "isrc",
    "itunes_id", "deezer_id", "deezer_bpm",
    "resolution_status", "resolution_confidence", "requires_review",
    "audio_method", "audio_analyzed_sec", "audio_segments",
    "key_name", "key_confidence", "key_margin",
    "tempo_mean", "tempo_std", "tempo_min", "tempo_max",
    "energy_rms_mean", "energy_rms_std", "energy_rms_min", "energy_rms_max",
    "energy_norm_peak_mean", "energy_norm_peak_std",
    "energy_norm_peak_min", "energy_norm_peak_max",
    "acousticness_zcr_mean", "acousticness_zcr_std",
    "acousticness_zcr_min", "acousticness_zcr_max",
    "brightness_mean", "brightness_std", "brightness_min", "brightness_max",
    "zcr_mean", "zcr_std", "zcr_min", "zcr_max",
    "rolloff_mean", "rolloff_std", "rolloff_min", "rolloff_max",
    "onset_mean", "onset_std", "onset_min", "onset_max",
    "rhythm_mean", "rhythm_std", "rhythm_min", "rhythm_max",
    "features_json", "completeness_json", "provenance_json", "raw_json",
    "original_query",
    "created_at", "updated_at", "last_analyzed_at",
)

# created_at nunca se pisa en un conflicto (conserva la fecha de creación
# original); canonical_id tampoco, porque es la clave del conflicto.
_SONG_UPSERT_SQL = (
    "INSERT INTO songs (" + ", ".join(_SONG_COLUMNS) + ") "
    "VALUES (" + ", ".join("?" for _ in _SONG_COLUMNS) + ") "
    "ON CONFLICT(canonical_id) DO UPDATE SET "
    + ", ".join(
        (
            (
                f"{col} = COALESCE(NULLIF(excluded.{col}, ''), songs.{col})"
                if col in {"title", "artist_display", "artist_display_normalized"}
                else f"{col} = COALESCE(excluded.{col}, songs.{col})"
            )
            if col not in {
                "canonical_id_type", "provisional_identity",
                "resolution_status", "requires_review", "raw_json",
                "updated_at", "last_analyzed_at",
            }
            else f"{col} = excluded.{col}"
        )
        for col in _SONG_COLUMNS
        if col not in ("canonical_id", "created_at")
    )
)


def _song_insert_values(data: dict, result: dict | None = None) -> tuple:
    """Construye la tupla de valores para el upsert de songs, en el mismo
    orden que _SONG_COLUMNS."""
    identity = data.get("identity") or {}
    audio = _extract_audio_features(data)

    completeness = result.get("completeness") if result else data.get("completeness")
    requires_review = result.get("requires_review") if result else data.get("requires_review")
    artist_display = data.get("artist") or ""
    now = _now()

    return (
        identity.get("canonical_id"),
        identity.get("canonical_id_type"),
        1 if identity.get("provisional_identity") else 0,

        data.get("title") or "",
        artist_display,
        _normalize(artist_display),
        data.get("album"),
        data.get("genre"),
        data.get("year"),
        data.get("duration_sec"),
        data.get("country"),

        data.get("mbid"),
        data.get("artist_mbid"),
        data.get("isrc"),

        data.get("itunes_id"),
        data.get("deezer_id"),
        data.get("deezer_bpm"),

        identity.get("resolution_status") or "unknown",
        identity.get("resolution_confidence"),
        1 if requires_review else 0,

        audio["audio_method"],
        audio["audio_analyzed_sec"],
        audio["audio_segments"],
        audio["key_name"],
        audio["key_confidence"],
        audio["key_margin"],

        *audio["tempo"],
        *audio["energy_rms"],
        *audio["energy_norm_peak"],
        *audio["acousticness"],
        *audio["brightness"],
        *audio["zcr"],
        *audio["rolloff"],
        *audio["onset"],
        *audio["rhythm"],

        _json_dumps(data.get("features")),
        _json_dumps(completeness),
        _json_dumps(data.get("provenance")),
        _json_dumps(result if result is not None else data),
        data.get("original_query"),

        now,  # created_at (ignorado por el UPDATE si ya existía la fila)
        now,  # updated_at
        now,  # last_analyzed_at
    )


def exists_song(canonical_id: str, db_path: str = DEFAULT_DB_PATH) -> bool:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT 1 FROM songs WHERE canonical_id = ? LIMIT 1",
        (canonical_id,),
    ).fetchone()
    return row is not None


def get_song(song_id: int, db_path: str = DEFAULT_DB_PATH) -> Optional[dict]:
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
    return dict(row) if row else None


def get_song_by_canonical(canonical_id: str, db_path: str = DEFAULT_DB_PATH) -> Optional[dict]:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM songs WHERE canonical_id = ?", (canonical_id,)
    ).fetchone()
    return dict(row) if row else None


def insert_song(data: dict, db_path: str = DEFAULT_DB_PATH, result: dict | None = None) -> int:
    """
    Inserta o actualiza una canción (upsert atómico por canonical_id).

    Reemplaza lo que antes eran insert_song + update_song_from_analysis:
    un solo INSERT ... ON CONFLICT DO UPDATE, generado desde _SONG_COLUMNS.
    El id se obtiene siempre con un SELECT explícito — cur.lastrowid no es
    fiable cuando el UPSERT resuelve por la rama UPDATE en vez de INSERT.
    """
    identity = data.get("identity") or {}
    canonical_id = identity.get("canonical_id")
    if not canonical_id:
        raise ValueError("La canción no tiene canonical_id")

    values = _song_insert_values(data, result)
    conn = get_connection(db_path)
    conn.execute(_SONG_UPSERT_SQL, values)

    row = conn.execute(
        "SELECT id FROM songs WHERE canonical_id = ?", (canonical_id,)
    ).fetchone()
    return int(row["id"])


def insert_from_analysis(result: dict, db_path: str = DEFAULT_DB_PATH) -> Optional[int]:
    """Guarda el resultado completo del analyzer. Atómico."""
    data = result.get("data") or result.get("datos")
    if not data:
        return None

    identity = data.get("identity") or {}
    canonical_id = identity.get("canonical_id")
    if not canonical_id:
        return None

    with transaction(db_path):
        song_id = insert_song(data, db_path, result=result)

        provenance = data.get("provenance") or {}
        identity = data.get("identity") or {}
        conn = get_connection(db_path)
        now = _now()
        conn.execute(
            """
            INSERT INTO song_analysis_versions (
                song_id, analyzed_at, analyzer_version, ok, requires_review,
                resolution_status, resolution_confidence, completeness_json,
                raw_json, original_query, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                song_id,
                now,
                provenance.get("analyzer_version"),
                1 if result.get("ok") else 0,
                1 if result.get("requires_review") else 0,
                identity.get("resolution_status"),
                identity.get("resolution_confidence"),
                _json_dumps(result.get("completeness")),
                _json_dumps(result),
                data.get("original_query") or result.get("query"),
                now,
            ),
        )

        # Artistas: recorremos TODOS los artist_mbids, no solo el primero.
        # El analyzer hoy solo expone el nombre del artista principal
        # (data["artist"]); los créditos secundarios sin nombre disponible
        # se omiten en vez de crear un artista con nombre vacío.
        artist_mbids = data.get("artist_mbids") or (
            [data["artist_mbid"]] if data.get("artist_mbid") else []
        )
        artist_names = data.get("artist_names") or []

        seen_mbids = set()
        for idx, a_mbid in enumerate(artist_mbids):
            if not a_mbid or a_mbid in seen_mbids:
                continue
            seen_mbids.add(a_mbid)

            if idx < len(artist_names) and artist_names[idx]:
                name = artist_names[idx]
            elif idx == 0:
                name = data.get("artist") or ""
            else:
                name = None

            if not name:
                continue

            role = "primary" if idx == 0 else "featured"
            artist_id = upsert_artist(a_mbid, name, db_path)
            link_song_artist(song_id, artist_id, role, db_path)

        # Tags (no se filtran)
        for tag in data.get("tags") or []:
            if isinstance(tag, str):
                name, weight = tag, None
            elif isinstance(tag, dict):
                name, weight = tag.get("name"), tag.get("weight")
            else:
                continue
            if not name:
                continue
            tag_id = upsert_tag(name, db_path)
            link_song_tag(song_id, tag_id, weight, db_path)

        # Review si es ambiguo
        if result.get("requires_review"):
            add_to_review(
                query=data.get("original_query") or result.get("query") or "",
                song_id=song_id,
                confidence=identity.get("resolution_confidence"),
                evidence=identity.get("evidence") or [],
                candidates=data.get("itunes_candidates") or [],
                db_path=db_path,
            )

    return song_id


def update_song(song_id: int, db_path: str = DEFAULT_DB_PATH, **fields) -> None:
    """Actualiza columnas específicas."""
    allowed = {
        "title", "artist_display", "artist_display_normalized",
        "album", "genre", "year", "duration_sec", "country",
        "mbid", "artist_mbid", "isrc",
        "itunes_id", "deezer_id", "deezer_bpm",
        "resolution_status", "resolution_confidence", "requires_review",
        "audio_method", "audio_analyzed_sec", "audio_segments",
        "key_name", "key_confidence", "key_margin",
        "tempo_mean", "tempo_std", "tempo_min", "tempo_max",
        "energy_rms_mean", "energy_rms_std", "energy_rms_min", "energy_rms_max",
        "energy_norm_peak_mean", "energy_norm_peak_std",
        "energy_norm_peak_min", "energy_norm_peak_max",
        "acousticness_zcr_mean", "acousticness_zcr_std",
        "acousticness_zcr_min", "acousticness_zcr_max",
        "brightness_mean", "brightness_std", "brightness_min", "brightness_max",
        "zcr_mean", "zcr_std", "zcr_min", "zcr_max",
        "rolloff_mean", "rolloff_std", "rolloff_min", "rolloff_max",
        "onset_mean", "onset_std", "onset_min", "onset_max",
        "rhythm_mean", "rhythm_std", "rhythm_min", "rhythm_max",
        "features_json", "completeness_json", "provenance_json", "raw_json",
        "original_query", "last_analyzed_at",
    }
    invalid = set(fields) - allowed
    if invalid:
        raise ValueError(f"Columnas no permitidas: {sorted(invalid)}")
    if not fields:
        return

    fields["updated_at"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [song_id]

    conn = get_connection(db_path)
    conn.execute(f"UPDATE songs SET {columns} WHERE id = ?", values)


def count_songs(db_path: str = DEFAULT_DB_PATH) -> int:
    conn = get_connection(db_path)
    return conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]


# ============================================================================
# ARTISTS
# ============================================================================

def upsert_artist(artist_mbid: str, name: str, db_path: str = DEFAULT_DB_PATH) -> int:
    if not artist_mbid:
        raise ValueError("artist_mbid no puede estar vacío")

    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO artists (artist_mbid, name, name_normalized, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(artist_mbid) DO UPDATE SET
            name = excluded.name,
            name_normalized = excluded.name_normalized
        """,
        (artist_mbid, name, _normalize(name), _now()),
    )
    row = conn.execute("SELECT id FROM artists WHERE artist_mbid = ?", (artist_mbid,)).fetchone()
    return row["id"]


def link_song_artist(song_id: int, artist_id: int, role: str = "primary",
                     db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO song_artists (song_id, artist_id, role) VALUES (?, ?, ?)",
        (song_id, artist_id, role),
    )


def get_songs_by_artist_mbid(artist_mbid: str, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM songs WHERE artist_mbid = ? ORDER BY title",
        (artist_mbid,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_songs_by_artist_name(name: str, limit: int = 100,
                             db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """
    Busca por nombre normalizado, para artistas sin artist_mbid (frecuente
    en artistas independientes que MusicBrainz no tiene catalogados).
    """
    conn = get_connection(db_path)
    normalized = _normalize(name)
    rows = conn.execute(
        "SELECT * FROM songs WHERE artist_display_normalized = ? ORDER BY title LIMIT ?",
        (normalized, limit),
    ).fetchall()
    return [dict(row) for row in rows]


# ============================================================================
# TAGS
# ============================================================================

def upsert_tag(name: str, db_path: str = DEFAULT_DB_PATH) -> int:
    name = name.strip()
    if not name:
        raise ValueError("El tag no puede estar vacío")

    normalized = _normalize(name)
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO tags (name, name_normalized, songs_count, created_at)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(name_normalized) DO NOTHING
        """,
        (name, normalized, _now()),
    )
    row = conn.execute("SELECT id FROM tags WHERE name_normalized = ?", (normalized,)).fetchone()
    return row["id"]


def link_song_tag(song_id: int, tag_id: int, weight: Optional[float],
                  db_path: str = DEFAULT_DB_PATH) -> None:
    """
    Crea/actualiza el vínculo. Solo incrementa songs_count cuando es
    un vínculo NUEVO, no en cada update.
    """
    conn = get_connection(db_path)

    exists = conn.execute(
        "SELECT 1 FROM song_tags WHERE song_id = ? AND tag_id = ?",
        (song_id, tag_id),
    ).fetchone()

    conn.execute(
        """
        INSERT INTO song_tags (song_id, tag_id, weight)
        VALUES (?, ?, ?)
        ON CONFLICT(song_id, tag_id) DO UPDATE SET
            weight = COALESCE(excluded.weight, song_tags.weight)
        """,
        (song_id, tag_id, weight),
    )

    if not exists:
        conn.execute(
            "UPDATE tags SET songs_count = songs_count + 1 WHERE id = ?",
            (tag_id,),
        )


def get_songs_by_tag(tag_name: str, limit: int = 100,
                     db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    normalized = _normalize(tag_name)
    rows = conn.execute(
        """
        SELECT s.*, st.weight AS tag_weight
        FROM songs s
        JOIN song_tags st ON st.song_id = s.id
        JOIN tags t       ON t.id = st.tag_id
        WHERE t.name_normalized = ?
        ORDER BY st.weight DESC, s.title
        LIMIT ?
        """,
        (normalized, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_top_tags(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """Los tags con más canciones. Útil para priorizar expansión."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT id, name, songs_count FROM tags ORDER BY songs_count DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


# ============================================================================
# QUEUE
# ============================================================================

def enqueue(
    item_type: str,
    payload: dict,
    priority: float = 5.0,
    source: str = "manual",
    depth: int = 0,
    dedupe_key: Optional[str] = None,
    dedupe_completed: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> Optional[int]:
    conn = get_connection(db_path)
    now = _now()

    if dedupe_key is not None:
        statuses = ("pending", "processing", "done") if dedupe_completed else (
            "pending", "processing"
        )
        placeholders = ", ".join("?" for _ in statuses)
        existing = conn.execute(
            f"SELECT id FROM queue WHERE dedupe_key = ? "
            f"AND status IN ({placeholders}) LIMIT 1",
            (dedupe_key, *statuses),
        ).fetchone()
        if existing:
            return None

    try:
        cur = conn.execute(
            """
            INSERT INTO queue (
                item_type, payload_json, dedupe_key,
                priority, depth, source, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (item_type, _json_dumps(payload), dedupe_key,
             priority, depth, source, now, now),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        if dedupe_key is None:
            raise
        return None


def enqueue_many(items: list[dict], db_path: str = DEFAULT_DB_PATH) -> int:
    if not items:
        return 0
    inserted = 0
    with transaction(db_path):
        for item in items:
            qid = enqueue(
                item_type=item["item_type"],
                payload=item["payload"],
                priority=item.get("priority", 5.0),
                source=item.get("source", "manual"),
                depth=item.get("depth", 0),
                dedupe_key=item.get("dedupe_key"),
                db_path=db_path,
            )
            if qid is not None:
                inserted += 1
    return inserted


def pop_next(worker_id: str, lease_seconds: int = 300,
             db_path: str = DEFAULT_DB_PATH) -> Optional[dict]:
    """
    Reclama el siguiente trabajo.

    Usa transacción + SELECT/UPDATE/SELECT (compatible con cualquier
    SQLite moderno, sin depender de RETURNING).
    """
    if not worker_id:
        raise ValueError("worker_id es obligatorio")

    conn = get_connection(db_path)
    now = _now()

    with transaction(db_path):
        row = conn.execute(
            """
            SELECT id FROM queue
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return None

        queue_id = row["id"]

        conn.execute(
            """
            UPDATE queue SET
                status = 'processing',
                worker_id = ?,
                lease_until = datetime(?, ?),
                started_at = COALESCE(started_at, ?),
                updated_at = ?,
                attempts = attempts + 1
            WHERE id = ? AND status = 'pending'
            """,
            (worker_id, now, f"+{int(lease_seconds)} seconds", now, now, queue_id),
        )

        result = conn.execute(
            """
            SELECT id, item_type, payload_json, priority, depth,
                   source, attempts, worker_id, lease_until
            FROM queue WHERE id = ?
            """,
            (queue_id,),
        ).fetchone()

    if result is None:
        return None

    out = dict(result)
    out["payload"] = _json_loads(out.pop("payload_json"), {})
    return out


def renew_lease(queue_id: int, worker_id: str, lease_seconds: int = 300,
                db_path: str = DEFAULT_DB_PATH) -> bool:
    conn = get_connection(db_path)
    now = _now()
    cur = conn.execute(
        """
        UPDATE queue SET
            lease_until = datetime(?, ?),
            updated_at = ?
        WHERE id = ? AND status = 'processing' AND worker_id = ?
        """,
        (now, f"+{int(lease_seconds)} seconds", now, queue_id, worker_id),
    )
    return cur.rowcount > 0


def mark_done(queue_id: int, worker_id: Optional[str] = None,
              db_path: str = DEFAULT_DB_PATH) -> bool:
    conn = get_connection(db_path)
    now = _now()

    if worker_id is None:
        cur = conn.execute(
            """
            UPDATE queue SET
                status = 'done', lease_until = NULL, worker_id = NULL,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (now, now, queue_id),
        )
    else:
        cur = conn.execute(
            """
            UPDATE queue SET
                status = 'done', lease_until = NULL, worker_id = NULL,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND worker_id = ?
            """,
            (now, now, queue_id, worker_id),
        )
    return cur.rowcount > 0


def mark_failed(queue_id: int, error: str, worker_id: Optional[str] = None,
                retry: bool = False, max_attempts: int = 3,
                db_path: str = DEFAULT_DB_PATH) -> bool:
    conn = get_connection(db_path)
    status = "pending" if retry else "failed"
    now = _now()

    if retry:
        row = conn.execute(
            "SELECT attempts FROM queue "
            "WHERE id = ? AND status = 'processing'",
            (queue_id,),
        ).fetchone()
        if row is not None and row["attempts"] >= max_attempts:
            status = "failed"

    if worker_id is None:
        cur = conn.execute(
            """
            UPDATE queue SET
                status = ?, last_error = ?, worker_id = NULL, lease_until = NULL,
                finished_at = CASE WHEN ? = 'failed' THEN ? ELSE finished_at END,
                updated_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (status, error[:2000], status, now, now, queue_id),
        )
    else:
        cur = conn.execute(
            """
            UPDATE queue SET
                status = ?, last_error = ?, worker_id = NULL, lease_until = NULL,
                finished_at = CASE WHEN ? = 'failed' THEN ? ELSE finished_at END,
                updated_at = ?
            WHERE id = ? AND status = 'processing' AND worker_id = ?
            """,
            (status, error[:2000], status, now, now, queue_id, worker_id),
        )
    return cur.rowcount > 0


def requeue_expired(db_path: str = DEFAULT_DB_PATH,
                    max_attempts: int = 3) -> int:
    conn = get_connection(db_path)
    now = _now()
    cur = conn.execute(
        """
        UPDATE queue SET
            status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END,
            last_error = CASE WHEN attempts >= ?
                              THEN COALESCE(last_error, 'lease_expired_max_attempts')
                              ELSE last_error END,
            worker_id = NULL,
            lease_until = NULL,
            finished_at = CASE WHEN attempts >= ? THEN ? ELSE finished_at END,
            updated_at = ?
        WHERE status = 'processing'
          AND (lease_until IS NULL OR lease_until <= ?)
        """,
        (max_attempts, max_attempts, max_attempts, now, now, now),
    )
    return cur.rowcount


def requeue_stuck(db_path: str = DEFAULT_DB_PATH) -> int:
    """Compatibilidad con API anterior. Solo recupera leases expirados."""
    return requeue_expired(db_path)


def queue_stats(db_path: str = DEFAULT_DB_PATH) -> dict:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM queue GROUP BY status"
    ).fetchall()
    stats = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for row in rows:
        stats[row["status"]] = row["count"]
    return stats


# ============================================================================
# REVIEW QUEUE
# ============================================================================

def add_to_review(query: str, song_id: Optional[int], confidence: Optional[float],
                  evidence: list, candidates: list,
                  db_path: str = DEFAULT_DB_PATH) -> int:
    conn = get_connection(db_path)
    existing = conn.execute(
        "SELECT id FROM review_queue WHERE song_id IS ? AND status = 'pending' LIMIT 1",
        (song_id,),
    ).fetchone()
    if existing:
        return existing["id"]

    cur = conn.execute(
        """
        INSERT INTO review_queue (
            query, song_id, confidence, evidence_json, candidates_json,
            status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (query, song_id, confidence, _json_dumps(evidence),
         _json_dumps(candidates), _now()),
    )
    return int(cur.lastrowid)


def list_pending_reviews(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT * FROM review_queue
        WHERE status = 'pending'
        ORDER BY confidence ASC, created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_review(review_id: int, resolution: str = "resolved",
                   note: Optional[str] = None, db_path: str = DEFAULT_DB_PATH) -> None:
    if resolution not in {"resolved", "discarded"}:
        raise ValueError("resolution debe ser 'resolved' o 'discarded'")

    conn = get_connection(db_path)
    conn.execute(
        "UPDATE review_queue SET status = ?, resolution_note = ?, resolved_at = ? WHERE id = ?",
        (resolution, note, _now(), review_id),
    )


def count_pending_reviews(db_path: str = DEFAULT_DB_PATH) -> int:
    conn = get_connection(db_path)
    return conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'"
    ).fetchone()[0]


# ============================================================================
# FAILED QUERIES
# ============================================================================

def register_failure(query: str, error: str, db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    now = _now()
    conn.execute(
        """
        INSERT INTO failed_queries (query, error, attempts, first_seen_at, last_seen_at)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(query, error) DO UPDATE SET
            attempts = failed_queries.attempts + 1,
            last_seen_at = excluded.last_seen_at
        """,
        (query, error, now, now),
    )


def is_known_failure(query: str, error: str, max_attempts: int = 3,
                     db_path: str = DEFAULT_DB_PATH) -> bool:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT attempts FROM failed_queries WHERE query = ? AND error = ?",
        (query, error),
    ).fetchone()
    return bool(row and row["attempts"] >= max_attempts)


def count_failures(db_path: str = DEFAULT_DB_PATH) -> int:
    conn = get_connection(db_path)
    return conn.execute("SELECT COUNT(*) FROM failed_queries").fetchone()[0]


# ============================================================================
# MÉTRICAS
# ============================================================================

def record_stats(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)

    songs_total = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    songs_verified = conn.execute(
        "SELECT COUNT(*) FROM songs WHERE resolution_status = 'verified'"
    ).fetchone()[0]
    songs_ambiguous = conn.execute(
        "SELECT COUNT(*) FROM songs WHERE resolution_status = 'ambiguous'"
    ).fetchone()[0]

    queue = queue_stats(db_path)
    failures = count_failures(db_path)

    avg_seconds = conn.execute(
        """
        SELECT AVG((julianday(finished_at) - julianday(started_at)) * 86400.0)
        FROM queue
        WHERE status = 'done' AND started_at IS NOT NULL AND finished_at IS NOT NULL
        """
    ).fetchone()[0]

    conn.execute(
        """
        INSERT INTO crawl_stats (
            recorded_at, songs_total, songs_verified, songs_ambiguous,
            queue_pending, queue_done, failures_total, avg_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (_now(), songs_total, songs_verified, songs_ambiguous,
         queue["pending"], queue["done"], failures, avg_seconds),
    )


# ============================================================================
# MANTENIMIENTO
# ============================================================================

def check_integrity(db_path: str = DEFAULT_DB_PATH) -> tuple[bool, list[str]]:
    """Recoge TODAS las filas de PRAGMA integrity_check y devuelve la lista
    de problemas (vacía si todo está bien)."""
    conn = get_connection(db_path)
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    issues = [r[0] for r in rows if r[0] != "ok"]
    return (len(issues) == 0, issues)


def vacuum(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    if conn.in_transaction:
        raise RuntimeError("No se puede ejecutar VACUUM dentro de una transacción.")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")


# ============================================================================
# ESTADÍSTICAS
# ============================================================================

def database_stats(db_path: str = DEFAULT_DB_PATH) -> dict:
    create_database(db_path)
    conn = get_connection(db_path)

    songs = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    artists = conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    reviews = count_pending_reviews(db_path)
    failures = count_failures(db_path)

    return {
        "songs": songs,
        "artists": artists,
        "tags": tags,
        "pending_reviews": reviews,
        "failures": failures,
        "queue": queue_stats(db_path),
    }


# ============================================================================
# CLI
# ============================================================================

def _cmd_init(args):
    create_database(args.db)
    print(f"Base de datos inicializada: {args.db} (schema v{SCHEMA_VERSION})")


def _cmd_stats(args):
    stats = database_stats(args.db)
    print(f"Base de datos: {args.db}")
    print(f"  Songs           : {stats['songs']}")
    print(f"  Artists         : {stats['artists']}")
    print(f"  Tags            : {stats['tags']}")
    print(f"  Pending reviews : {stats['pending_reviews']}")
    print(f"  Failures        : {stats['failures']}")
    print(f"  Queue           : {stats['queue']}")


def _cmd_check(args):
    create_database(args.db)
    ok, issues = check_integrity(args.db)
    if ok:
        print("SQLite integrity check: OK")
    else:
        print(f"SQLite integrity check: {len(issues)} issue(s)")
        for issue in issues[:20]:
            print(f"  {issue}")


def _cmd_reset(args):
    if not args.yes:
        print("Este comando BORRARÁ toda la base. Usa --yes para confirmar.")
        return
    reset_database(args.db, confirm=True)
    print(f"Base recreada: {args.db}")


def _cmd_vacuum(args):
    vacuum(args.db)
    print("VACUUM completado.")


def main():
    parser = argparse.ArgumentParser(description="Persistencia del crawler musical")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Ruta de SQLite")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Inicializa la base").set_defaults(func=_cmd_init)
    sub.add_parser("stats", help="Muestra estadísticas").set_defaults(func=_cmd_stats)
    sub.add_parser("check", help="Comprueba integridad").set_defaults(func=_cmd_check)

    reset_parser = sub.add_parser("reset", help="Borra y recrea")
    reset_parser.add_argument("--yes", action="store_true", help="Confirmar borrado")
    reset_parser.set_defaults(func=_cmd_reset)

    sub.add_parser("vacuum", help="Optimiza la DB").set_defaults(func=_cmd_vacuum)

    args = parser.parse_args()
    try:
        args.func(args)
    finally:
        close_connection()


if __name__ == "__main__":
    main()