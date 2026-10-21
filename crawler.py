#!/usr/bin/env python3
"""
crawler.py — Orquestador del crawling musical.

Toma items de la cola (db.py), los procesa (analyzer.py) y expande el
grafo encolando nuevos items. Gestiona workers, leases, métricas y
apagado limpio.

Regla de guardado (importante):

    SI el analyzer devolvió ALGO de data → se guarda.
    SI data es None (no se recolectó absolutamente nada) → no se guarda,
    se registra como fallo.

    Cuando el analyzer no pudo resolver identidad pero sí devolvió data
    (por ejemplo, identity_not_resolved con candidatos de iTunes), el
    crawler genera una identidad provisional (local:hash) y marca el
    registro como requiring review, para que un humano lo resuelva luego.

Item types soportados:
    song    → analiza la canción y guarda el resultado
    artist  → expande las grabaciones del artista y las encola como songs
    tag     → expande los top tracks del tag y los encola como songs

CLI:
    python crawler.py
    python crawler.py --seed "Billie Jean|Michael Jackson"
    python crawler.py --workers 4
"""

from __future__ import annotations

import argparse
import os
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import threading
import time
import urllib.parse
from typing import Optional

import db
from analyzer import (
    analyze_song,
    http_get_json,
    USER_AGENT,
    LASTFM_API_KEY,
    _fallback_fingerprint,   # reutilizamos el hash del analyzer para consistencia
)


# ============================================================================
# CONFIGURACIÓN
# ============================================================================

DEFAULT_DB_PATH = db.DEFAULT_DB_PATH
DEFAULT_WORKERS = 1
DEFAULT_LEASE_SECONDS = 600
STATS_INTERVAL_SECONDS = 300
EXPIRED_REQUEUE_INTERVAL_SECONDS = 60
EXIT_WHEN_IDLE_SECONDS = 15

# Prioridades (mayor = antes)
PRIORITY_SEED = 10.0
PRIORITY_ARTIST_EXPANSION = 8.0
PRIORITY_TAG_EXPANSION = 5.0

# Reglas de expansión
MIN_TAG_WEIGHT_TO_EXPAND = 20.0
MAX_DEPTH = 5
ARTIST_RECORDINGS_LIMIT = 100
TAG_TOP_TRACKS_LIMIT = 100


# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("crawler")


def _setup_logging(level: str = "INFO", log_file: Optional[str] = "crawler.log") -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_crawler_handler", False):
            logger.removeHandler(handler)
            handler.close()

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        "%H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch._crawler_handler = True
    logger.addHandler(ch)

    if log_file:
        try:
            fh = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            fh._crawler_handler = True
            logger.addHandler(fh)
        except OSError:
            pass


# ============================================================================
# PARADA LIMPIA
# ============================================================================

stop_event = threading.Event()


def _handle_signal(signum, frame):
    logger.info("Señal %s recibida; parando limpio...", signum)
    stop_event.set()


# ============================================================================
# IDENTIDAD PROVISIONAL
# ============================================================================

def _ensure_identity_for_save(result: dict) -> dict:
    """
    Si el analyzer devolvió data pero no pudo resolver identidad,
    sintetiza una identidad provisional para poder guardar el registro.

    Regla del crawler: TODO lo recolectado se persiste. Solo se descarta
    si no hay absolutamente nada de data.
    """
    data = result.get("data")
    if not data:
        return result

    existing_identity = data.get("identity") or {}
    if existing_identity.get("canonical_id"):
        return result  # ya tiene identidad, nada que hacer

    candidates = data.get("itunes_candidates") or []
    query = data.get("original_query") or ""

    # Intentar extraer metadatos del mejor candidato disponible
    if candidates:
        best = candidates[0] or {}
        title = best.get("title") or query or "(desconocido)"
        artist = best.get("artist") or ""
        duration = best.get("duration_sec")

        # Aprovechar los metadatos del candidato (best-effort).
        # Si data ya tenía un valor, no lo pisamos.
        for k in ("album", "genre", "year", "country",
                  "itunes_id", "deezer_id", "duration_sec"):
            if not data.get(k) and best.get(k) is not None:
                data[k] = best.get(k)
    else:
        title = query or "(desconocido)"
        artist = ""
        duration = None

    canonical_id = _fallback_fingerprint(title, artist, duration)

    data["identity"] = {
        "canonical_id": canonical_id,
        "canonical_id_type": "local_fingerprint",
        "provisional_identity": True,
        "resolution_status": result.get("error") or "unresolved",
        "resolution_confidence": 0.0,
        "evidence": (data.get("identity_match") or {}).get("evidence") or [],
        "scores": {},
    }

    if not data.get("title"):
        data["title"] = title
    if not data.get("artist"):
        data["artist"] = artist or "(desconocido)"

    result = dict(result)
    result["data"] = data
    # Forzar review para que un humano lo verifique
    result["requires_review"] = True
    return result


# ============================================================================
# EXPANSIÓN
# ============================================================================

def _priority_for_depth(base: float, depth: int) -> float:
    return max(0.5, base - depth * 1.5)


def _enqueue_artist(artist_mbid: str, depth: int, db_path: str) -> None:
    if not artist_mbid:
        return
    db.enqueue(
        item_type="artist",
        payload={"mbid": artist_mbid},
        priority=_priority_for_depth(PRIORITY_ARTIST_EXPANSION, depth),
        source="artist_expansion",
        depth=depth,
        dedupe_key=f"artist:{artist_mbid}",
        dedupe_completed=True,
        db_path=db_path,
    )


def _enqueue_tag(tag_name: str, weight: float, depth: int, db_path: str) -> None:
    if not tag_name or weight < MIN_TAG_WEIGHT_TO_EXPAND:
        return
    db.enqueue(
        item_type="tag",
        payload={"name": tag_name},
        priority=_priority_for_depth(PRIORITY_TAG_EXPANSION, depth),
        source="tag_expansion",
        depth=depth,
        dedupe_key=f"tag:{db._normalize(tag_name)}",
        dedupe_completed=True,
        db_path=db_path,
    )


def _enqueue_song(title: str, artist: str, depth: int,
                  source: str, db_path: str) -> None:
    if not title:
        return
    dedupe = f"song:{db._normalize(artist)}|{db._normalize(title)}"
    db.enqueue(
        item_type="song",
        payload={"title": title, "artist": artist},
        priority=_priority_for_depth(PRIORITY_SEED, depth),
        source=source,
        depth=depth,
        dedupe_key=dedupe,
        dedupe_completed=True,
        db_path=db_path,
    )


def _expand_from_song(result: dict, item: dict, db_path: str) -> None:
    """Desde un análisis exitoso, encola artista principal + tags fuertes."""
    data = result.get("data") or {}
    depth = (item.get("depth") or 0) + 1

    if depth > MAX_DEPTH:
        logger.debug("depth %d > MAX_DEPTH, no se expande", depth)
        return

    if data.get("artist_mbid"):
        _enqueue_artist(data["artist_mbid"], depth, db_path)

    for tag in data.get("tags") or []:
        if isinstance(tag, dict):
            _enqueue_tag(
                tag.get("name"),
                tag.get("weight") or 0.0,
                depth,
                db_path,
            )


# ============================================================================
# FETCHERS (usados por los procesadores de artist/tag)
# ============================================================================

def _fetch_artist_recordings(
    artist_mbid: str, limit: int = 100
) -> Optional[list[dict]]:
    params = urllib.parse.urlencode({
        "artist": artist_mbid,
        "limit": limit,
        "fmt": "json",
    })
    url = f"https://musicbrainz.org/ws/2/recording?{params}"
    data = http_get_json(url, "musicbrainz",
                         headers={"User-Agent": USER_AGENT})
    if data is None:
        return None

    out: list[dict] = []
    for rec in data.get("recordings", []):
        title = rec.get("title")
        if not title:
            continue
        credits = rec.get("artist-credit") or []
        artist_name = ""
        if credits and isinstance(credits[0], dict):
            artist_name = (
                credits[0].get("name")
                or (credits[0].get("artist") or {}).get("name")
                or ""
            )
        out.append({"title": title, "artist": artist_name})
    return out


def _fetch_tag_top_tracks(tag: str, limit: int = 100) -> Optional[list[dict]]:
    if not LASTFM_API_KEY:
        logger.debug("Last.fm sin API key: no se expande tag %r", tag)
        return []

    params = urllib.parse.urlencode({
        "method": "tag.getTopTracks",
        "tag": tag,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "limit": limit,
    })
    url = f"https://ws.audioscrobbler.com/2.0/?{params}"
    data = http_get_json(url, "lastfm")
    if data is None:
        return None

    tracks = (data.get("tracks") or {}).get("track") or []
    out: list[dict] = []
    for t in tracks:
        title = t.get("name")
        artist = (t.get("artist") or {}).get("name")
        if title and artist:
            out.append({"title": title, "artist": artist})
    return out


# ============================================================================
# PROCESADORES POR TIPO
# ============================================================================

def _process_song(item: dict, db_path: str) -> str:
    if stop_event.is_set():
        return "stopped"

    payload = item.get("payload") or {}
    title = payload.get("title")
    artist = payload.get("artist")
    query = payload.get("query")

    if not any([title, artist, query]):
        return "failed:empty_payload"

    logger.info("song → %s — %s", title or query, artist or "")

    try:
        if query and not title:
            result = analyze_song(free_query=query)
        else:
            result = analyze_song(title=title, artist=artist)
    except Exception as exc:
        logger.exception("Error analizando: %s", exc)
        return f"exception:{type(exc).__name__}"

    # --------------------------------------------------------------
    # REGLA: guardar TODO lo que se haya recolectado.
    # Solo se descarta si `data` es None (absolutamente nada).
    # --------------------------------------------------------------
    if not result.get("data"):
        error = result.get("error") or "unknown"
        if error == "itunes_unavailable":
            logger.warning("iTunes temporalmente no disponible para %r", title or query)
            return "retry:itunes_unavailable"

        q = f"{title or ''} {artist or ''}".strip() or (query or "")
        if q and not db.is_known_failure(q, error, db_path=db_path):
            db.register_failure(q, error, db_path=db_path)
        logger.info("song descartada (sin data): %s — %s", q, error)
        return f"failed:{error}"

    # Si no hay identidad resuelta, generamos una provisional.
    had_identity = bool((result.get("data") or {}).get("identity", {}).get("canonical_id"))
    result = _ensure_identity_for_save(result)

    if not had_identity:
        provisional = (result.get("data") or {}).get("identity", {}).get("canonical_id")
        logger.info("identidad provisional generada: %s", provisional)

    song_id = db.insert_from_analysis(result, db_path=db_path)

    if not song_id:
        logger.warning("insert_from_analysis devolvió None para %r",
                       title or query)
        return "failed:insert_failed"

    # Solo expandimos desde matches seguros
    if result.get("ok") and not result.get("requires_review"):
        _expand_from_song(result, item, db_path)

    if result.get("ok"):
        return "ok"
    if result.get("requires_review"):
        return "review"
    return "saved_unresolved"


def _process_artist(item: dict, db_path: str) -> str:
    if stop_event.is_set():
        return "stopped"

    payload = item.get("payload") or {}
    mbid = payload.get("mbid")
    if not mbid:
        return "failed:empty_payload"

    logger.info("artist → %s", mbid)
    recordings = _fetch_artist_recordings(mbid, ARTIST_RECORDINGS_LIMIT)
    if recordings is None:
        return "retry:musicbrainz_unavailable"
    if not recordings:
        return "no_recordings"

    depth = (item.get("depth") or 0) + 1
    enqueued = 0
    for r in recordings:
        if stop_event.is_set():
            break
        if not r.get("title"):
            continue
        _enqueue_song(
            title=r["title"],
            artist=r.get("artist") or "",
            depth=depth,
            source="artist_expansion",
            db_path=db_path,
        )
        enqueued += 1

    logger.info("artist %s → %d canciones encoladas", mbid[:8], enqueued)
    return f"ok:{enqueued}"


def _process_tag(item: dict, db_path: str) -> str:
    if stop_event.is_set():
        return "stopped"

    payload = item.get("payload") or {}
    name = payload.get("name")
    if not name:
        return "failed:empty_payload"

    logger.info("tag → %s", name)
    tracks = _fetch_tag_top_tracks(name, TAG_TOP_TRACKS_LIMIT)
    if tracks is None:
        return "retry:lastfm_unavailable"
    if not tracks:
        return "no_tracks"

    depth = (item.get("depth") or 0) + 1
    enqueued = 0
    for t in tracks:
        if stop_event.is_set():
            break
        _enqueue_song(
            title=t["title"],
            artist=t["artist"],
            depth=depth,
            source="tag_expansion",
            db_path=db_path,
        )
        enqueued += 1

    logger.info("tag %s → %d canciones encoladas", name, enqueued)
    return f"ok:{enqueued}"


_DISPATCH = {
    "song": _process_song,
    "artist": _process_artist,
    "tag": _process_tag,
}


def _process_item(item: dict, db_path: str) -> str:
    handler = _DISPATCH.get(item.get("item_type"))
    if handler is None:
        logger.warning("Tipo desconocido: %r", item.get("item_type"))
        return f"unknown_type:{item.get('item_type')}"
    return handler(item, db_path)


# ============================================================================
# WORKER
# ============================================================================

def _renew_item_lease(
    queue_id: int,
    worker_id: str,
    lease_seconds: int,
    db_path: str,
    stop: threading.Event,
) -> None:
    interval = max(0.5, min(30.0, lease_seconds / 3))
    while not stop.wait(interval):
        try:
            if not db.renew_lease(
                queue_id,
                worker_id,
                lease_seconds=lease_seconds,
                db_path=db_path,
            ):
                logger.warning(
                    "[%s] no se pudo renovar el lease del item %s",
                    worker_id,
                    queue_id,
                )
                return
        except Exception:
            logger.exception(
                "[%s] error renovando el lease del item %s",
                worker_id,
                queue_id,
            )

def worker(worker_id: str, db_path: str,
           lease_seconds: int = DEFAULT_LEASE_SECONDS,
           keep_alive: bool = False) -> None:
    logger.info("[%s] arrancado", worker_id)
    idle_seconds = 0.0

    while not stop_event.is_set():
        item = db.pop_next(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            db_path=db_path,
        )

        if item is None:
            if keep_alive:
                stop_event.wait(1.0)
                continue

            idle_seconds += 1.0
            if idle_seconds >= EXIT_WHEN_IDLE_SECONDS:
                logger.info("[%s] cola vacía tras %.0fs, saliendo",
                            worker_id, idle_seconds)
                break
            time.sleep(1.0)
            continue

        idle_seconds = 0.0

        lease_stop = threading.Event()
        lease_thread = threading.Thread(
            target=_renew_item_lease,
            args=(item["id"], worker_id, lease_seconds, db_path, lease_stop),
            name=f"{worker_id}-lease",
            daemon=True,
        )
        lease_thread.start()

        try:
            status = _process_item(item, db_path)
        except Exception as exc:
            logger.exception("[%s] excepción en item %s: %s",
                             worker_id, item.get("id"), exc)
            status = f"exception:{type(exc).__name__}"
        finally:
            lease_stop.set()
            lease_thread.join(timeout=2.0)

        if stop_event.is_set():
            db.mark_failed(item["id"], "stopped", worker_id=worker_id,
                           retry=True, db_path=db_path)
            break

        if status.startswith("retry:"):
            db.mark_failed(
                item["id"], status, worker_id=worker_id,
                retry=True, db_path=db_path,
            )
        elif (
            status.startswith("failed")
            or status.startswith("exception")
            or status.startswith("empty_payload")
            or status.startswith("insert_failed")
        ):
            db.mark_failed(item["id"], status, worker_id=worker_id,
                           db_path=db_path)
        else:
            db.mark_done(item["id"], worker_id=worker_id, db_path=db_path)

    logger.info("[%s] detenido", worker_id)


# ============================================================================
# MANTENIMIENTO
# ============================================================================

def _maintenance_loop(db_path: str, interval: int = STATS_INTERVAL_SECONDS) -> None:
    last_expired_check = time.monotonic()
    last_stats = last_expired_check
    wake_interval = min(interval, EXPIRED_REQUEUE_INTERVAL_SECONDS)

    while not stop_event.wait(wake_interval):
        now = time.monotonic()

        if now - last_expired_check >= EXPIRED_REQUEUE_INTERVAL_SECONDS:
            try:
                recovered = db.requeue_expired(db_path=db_path)
                if recovered:
                    logger.info("Leases expirados recuperados: %d", recovered)
            except Exception:
                logger.exception("Error recuperando leases")
            last_expired_check = now

        if now - last_stats < interval:
            continue

        try:
            db.record_stats(db_path=db_path)
            stats = db.database_stats(db_path=db_path)
            logger.info(
                "Métricas | songs=%d artists=%d tags=%d reviews=%d "
                "failures=%d cola=%s",
                stats["songs"], stats["artists"], stats["tags"],
                stats["pending_reviews"], stats["failures"], stats["queue"],
            )
        except Exception:
            logger.exception("Error registrando stats")
        last_stats = now

    try:
        db.record_stats(db_path=db_path)
    except Exception:
        pass


# ============================================================================
# ARRANQUE
# ============================================================================

def _parse_seed_arg(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    if "|" in raw:
        title, artist = raw.split("|", 1)
        title, artist = title.strip(), artist.strip()
        if title:
            return {"title": title, "artist": artist or None}
    return {"title": raw.strip(), "artist": None}


def _enqueue_seeds(seeds: list[str], db_path: str) -> int:
    n = 0
    for raw in seeds:
        payload = _parse_seed_arg(raw)
        if not payload:
            continue
        title = payload["title"]
        artist = payload.get("artist") or ""
        dedupe = f"song:{db._normalize(artist)}|{db._normalize(title)}"
        queue_id = db.enqueue(
            item_type="song",
            payload=payload,
            priority=PRIORITY_SEED,
            source="seed",
            depth=0,
            dedupe_key=dedupe,
            db_path=db_path,
        )
        if queue_id is not None:
            n += 1
    return n


def _print_final_stats(db_path: str) -> None:
    stats = db.database_stats(db_path=db_path)
    logger.info(
        "FINAL | songs=%d artists=%d tags=%d reviews=%d failures=%d cola=%s",
        stats["songs"], stats["artists"], stats["tags"],
        stats["pending_reviews"], stats["failures"], stats["queue"],
    )


def run(db_path: str = DEFAULT_DB_PATH,
        workers: int = DEFAULT_WORKERS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    seeds: Optional[list[str]] = None,
    keep_alive: bool = False) -> int:

    if not 1 <= workers <= 32:
        raise ValueError("workers debe estar entre 1 y 32")
    if not 60 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds debe estar entre 60 y 3600")

    stop_event.clear()
    db.create_database(db_path=db_path)

    recovered = db.requeue_expired(db_path=db_path)
    if recovered:
        logger.info("Recuperados %d items huérfanos al arrancar", recovered)

    if seeds:
        n = _enqueue_seeds(seeds, db_path)
        logger.info("Semillas encoladas: %d", n)

    initial = db.queue_stats(db_path=db_path)
    logger.info("Cola inicial: %s", initial)

    if initial["pending"] == 0 and not keep_alive:
        logger.warning(
            "Cola vacía. Usa --seed o crea seeds.py para poblarla."
        )
        return 0

    if keep_alive and initial["pending"] == 0:
        logger.info("Cola vacía; modo keep-alive activo, esperando trabajo.")

    try:
        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_signal)
    except ValueError:
        logger.warning("No se pudieron registrar señales (¿no es main thread?)")

    maint = threading.Thread(
        target=_maintenance_loop,
        args=(db_path,),
        name="maintenance",
        daemon=True,
    )
    maint.start()

    threads: list[threading.Thread] = []
    try:
        for i in range(workers):
            wid = f"w-{i + 1}"
            t = threading.Thread(
                target=worker,
                args=(wid, db_path, lease_seconds, keep_alive),
                name=wid,
            )
            t.start()
            threads.append(t)

        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        logger.info("Ctrl+C detectado; parando workers...")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=30.0)
        maint.join(timeout=5.0)

    _print_final_stats(db_path)
    logger.info("Crawler detenido.")
    return 0


# ============================================================================
# CLI
# ============================================================================

def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        logger.warning("%s inválido; usando %s", name, default)
        return default
    return max(minimum, min(maximum, value))


def _bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("debe ser un entero") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"debe estar entre {minimum} y {maximum}"
            )
        return parsed

    return parse


def main():
    parser = argparse.ArgumentParser(description="Crawler musical")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help="Ruta de la base SQLite")
    parser.add_argument(
        "--workers", type=_bounded_int(1, 32),
        default=_env_int("CRAWLER_WORKERS", DEFAULT_WORKERS, 1, 32),
                        help="Número de workers en paralelo (default: 1)")
    parser.add_argument(
        "--lease-seconds", type=_bounded_int(60, 3600),
        default=_env_int(
            "CRAWLER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS, 60, 3600
        ),
                        help="Duración del lease de cada item (seg)")
    parser.add_argument("--seed", action="append", default=[],
                        help='Encolar "Título|Artista" antes de arrancar '
                             '(se puede repetir)')
    parser.add_argument("--keep-alive", action="store_true",
                        help="Mantiene workers activos aunque la cola quede vacía")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-file", default="crawler.log",
                        help="Archivo de log (vacío para desactivar)")

    args = parser.parse_args()
    _setup_logging(args.log_level, args.log_file or None)

    try:
        rc = run(
            db_path=args.db,
            workers=args.workers,
            lease_seconds=args.lease_seconds,
            seeds=args.seed,
            keep_alive=args.keep_alive,
        )
    except Exception:
        logger.exception("Error fatal en el crawler")
        rc = 1

    db.close_connection()
    sys.exit(rc)


if __name__ == "__main__":
    main()