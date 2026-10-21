#!/usr/bin/env python3
"""
main.py — Centro de control del crawler musical.

Interfaz de terminal interactiva que:
    - Configura parámetros (workers, depth, prioridades, etc.)
    - Encola semillas manualmente o desde seeds.py
    - Lanza el crawler con un dashboard en vivo
    - Consulta la DB mientras corre
    - Permite parar limpio sin perder trabajo

Uso:
    python main.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Optional

import db
import seeds as seeds_module

# Importamos el crawler completo para poder reusar sus funciones
import crawler


# ============================================================================
# ESTADO GLOBAL DE LA INTERFAZ
# ============================================================================

class Session:
    """Parámetros de la sesión actual. Todo editable desde el menú."""

    def __init__(self):
        self.db_path: str = db.DEFAULT_DB_PATH
        self.workers: int = 1
        self.lease_seconds: int = 600
        self.log_level: str = "INFO"
        self.max_depth: int = 8
        self.min_tag_weight: float = 0.0
        self.priority_seed: float = 10.0
        self.priority_artist: float = 8.0
        self.priority_tag: float = 5.0
        self.show_live_stats: bool = True
        self.live_stats_interval: int = 5

    def apply_to_crawler(self) -> None:
        """Empuja los parámetros al módulo crawler antes de arrancar."""
        crawler.MAX_DEPTH = self.max_depth
        crawler.MIN_TAG_WEIGHT_TO_EXPAND = self.min_tag_weight
        crawler.PRIORITY_SEED = self.priority_seed
        crawler.PRIORITY_ARTIST_EXPANSION = self.priority_artist
        crawler.PRIORITY_TAG_EXPANSION = self.priority_tag
        crawler.DEFAULT_LEASE_SECONDS = self.lease_seconds


SESSION = Session()


# ============================================================================
# UI HELPERS
# ============================================================================

def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _hr(char: str = "─", n: int = 78) -> str:
    return char * n


def _banner():
    print("""
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║   🎵  MUSIC CRAWLER — Centro de Control                                  ║
║                                                                          ║
║   Orquesta seeds → analyzer → db y muestra todo en vivo                 ║
║                                                                          ║
╚══════════════════════════════════════════════════════════════════════════╝
""")


def _pause(msg: str = "Pulsa Enter para continuar..."):
    input(f"\n  {msg}")


def _prompt_int(label: str, default: int, minv: int = 0,
                maxv: int = 10000) -> int:
    raw = input(f"  {label} [{default}]: ").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return max(minv, min(maxv, v))
    except ValueError:
        print(f"  ⚠️  Valor inválido, uso default = {default}")
        return default


def _prompt_float(label: str, default: float, minv: float = 0.0,
                  maxv: float = 100.0) -> float:
    raw = input(f"  {label} [{default}]: ").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return max(minv, min(maxv, v))
    except ValueError:
        print(f"  ⚠️  Valor inválido, uso default = {default}")
        return default


def _prompt_choice(label: str, choices: list[str], default: str) -> str:
    print(f"  {label}:")
    for i, c in enumerate(choices, 1):
        mark = "→" if c == default else " "
        print(f"    {mark} {i}) {c}")
    raw = input(f"  Elige [1-{len(choices)}] (Enter = {default}): ").strip()
    if not raw:
        return default
    try:
        i = int(raw) - 1
        if 0 <= i < len(choices):
            return choices[i]
    except ValueError:
        pass
    return default


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    d = "s" if default else "n"
    raw = input(f"  {label} [s/n] ({d}): ").strip().lower()
    if not raw:
        return default
    return raw in ("s", "si", "sí", "y", "yes", "1")


# ============================================================================
# DASHBOARD EN VIVO
# ============================================================================

class LiveDashboard:
    """Imprime stats cada N segundos mientras el crawler corre."""

    def __init__(self, interval: int = 5):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_time = time.time()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                stats = db.database_stats(db_path=SESSION.db_path)
                q = stats["queue"]
                elapsed = int(time.time() - self._start_time)
                mm, ss = divmod(elapsed, 60)
                hh, mm = divmod(mm, 60)
                elapsed_str = f"{hh:02d}:{mm:02d}:{ss:02d}"

                print(
                    f"\r  ⏱  {elapsed_str}  │  "
                    f"songs={stats['songs']:<6} "
                    f"artists={stats['artists']:<5} "
                    f"tags={stats['tags']:<5}  │  "
                    f"cola: P={q['pending']:<6} "
                    f"R={q['processing']:<3} "
                    f"D={q['done']:<6} "
                    f"F={q['failed']:<5}  │  "
                    f"reviews={stats['pending_reviews']}",
                    end="",
                    flush=True,
                )
            except Exception:
                pass

    def start(self):
        self._start_time = time.time()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="dashboard", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        print()  # nueva línea tras la barra de estado


# ============================================================================
# MENÚ: CONFIGURACIÓN
# ============================================================================

def menu_config():
    _clear()
    print(f"\n{_hr('═')}")
    print("  ⚙️  CONFIGURACIÓN ACTUAL")
    print(f"{_hr('═')}\n")
    print(f"  Base de datos      : {SESSION.db_path}")
    print(f"  Workers            : {SESSION.workers}")
    print(f"  Lease (seg)        : {SESSION.lease_seconds}")
    print(f"  Log level          : {SESSION.log_level}")
    print(f"  MAX_DEPTH          : {SESSION.max_depth}")
    print(f"  MIN_TAG_WEIGHT     : {SESSION.min_tag_weight}")
    print(f"  Prioridad seed     : {SESSION.priority_seed}")
    print(f"  Prioridad artist   : {SESSION.priority_artist}")
    print(f"  Prioridad tag      : {SESSION.priority_tag}")
    print(f"  Live stats         : {SESSION.show_live_stats}")
    print(f"  Interval stats (s) : {SESSION.live_stats_interval}\n")

    print(f"  {_hr('·')}")
    print("  ¿Qué quieres modificar?")
    print("    1) Workers")
    print("    2) Lease seconds")
    print("    3) MAX_DEPTH")
    print("    4) MIN_TAG_WEIGHT (umbral de tags a expandir)")
    print("    5) Prioridades (seed/artist/tag)")
    print("    6) Live stats on/off e interval")
    print("    7) Log level")
    print("    8) Ruta de la DB")
    print("    0) Volver")

    op = input("\n  Opción: ").strip()

    if op == "1":
        SESSION.workers = _prompt_int("Workers (1-8)", SESSION.workers, 1, 8)
    elif op == "2":
        SESSION.lease_seconds = _prompt_int(
            "Lease seconds", SESSION.lease_seconds, 60, 3600)
    elif op == "3":
        SESSION.max_depth = _prompt_int(
            "MAX_DEPTH (saltos de expansión)", SESSION.max_depth, 1, 20)
    elif op == "4":
        SESSION.min_tag_weight = _prompt_float(
            "MIN_TAG_WEIGHT (0 = expandir todos)",
            SESSION.min_tag_weight, 0.0, 1000.0)
    elif op == "5":
        SESSION.priority_seed = _prompt_float(
            "Prioridad seed", SESSION.priority_seed, 0.0, 100.0)
        SESSION.priority_artist = _prompt_float(
            "Prioridad artist", SESSION.priority_artist, 0.0, 100.0)
        SESSION.priority_tag = _prompt_float(
            "Prioridad tag", SESSION.priority_tag, 0.0, 100.0)
    elif op == "6":
        SESSION.show_live_stats = _prompt_yes_no(
            "Mostrar stats en vivo", SESSION.show_live_stats)
        if SESSION.show_live_stats:
            SESSION.live_stats_interval = _prompt_int(
                "Intervalo (seg)", SESSION.live_stats_interval, 1, 60)
    elif op == "7":
        SESSION.log_level = _prompt_choice(
            "Log level",
            ["DEBUG", "INFO", "WARNING", "ERROR"],
            SESSION.log_level,
        )
    elif op == "8":
        raw = input(f"  Ruta de la DB [{SESSION.db_path}]: ").strip()
        if raw:
            SESSION.db_path = raw

    print("\n  ✅ Configuración actualizada.")


# ============================================================================
# MENÚ: SEEDS
# ============================================================================

def menu_seeds():
    while True:
        _clear()
        print(f"\n{_hr('═')}")
        print("  🌱  GESTIÓN DE SEMILLAS")
        print(f"{_hr('═')}\n")

        # Estado de la cola
        try:
            db.create_database(db_path=SESSION.db_path)
            qs = db.queue_stats(db_path=SESSION.db_path)
            print(f"  Cola actual: {qs}\n")
        except Exception as e:
            print(f"  ⚠️  No se pudo leer la cola: {e}\n")

        print("    1) Encolar TODAS las categorías de seeds.py")
        print("    2) Encolar categorías específicas")
        print("    3) Añadir una semilla manual \"Título|Artista\"")
        print("    4) Ver categorías disponibles")
        print("    5) Ver los próximos items de la cola")
        print("    0) Volver")

        op = input("\n  Opción: ").strip()

        if op == "1":
            n = _enqueue_all_categories()
            print(f"\n  ✅ Encoladas {n} semillas.")
            _pause()

        elif op == "2":
            n = _enqueue_specific_categories()
            print(f"\n  ✅ Encoladas {n} semillas.")
            _pause()

        elif op == "3":
            raw = input("  Semilla (\"Título|Artista\" o \"Título\"): ").strip()
            if raw:
                _enqueue_manual(raw)
            _pause()

        elif op == "4":
            _clear()
            print(f"\n{_hr('═')}")
            print("  Categorías disponibles")
            print(f"{_hr('═')}\n")
            for cat, items in seeds_module.SEEDS_BY_CATEGORY.items():
                print(f"  {cat:<18} ({len(items)} semillas)")
            _pause()

        elif op == "5":
            _clear()
            _print_queue_top(30)
            _pause()

        elif op == "0":
            return


def _enqueue_all_categories() -> int:
    db.create_database(db_path=SESSION.db_path)
    return seeds_module.enqueue(
        categories=None,
        priority=SESSION.priority_seed,
        db_path=SESSION.db_path,
    )


def _enqueue_specific_categories() -> int:
    cats = list(seeds_module.SEEDS_BY_CATEGORY.keys())
    print(f"\n  Categorías disponibles: {', '.join(cats)}")
    raw = input("  Escribe las categorías separadas por coma: ").strip()
    if not raw:
        return 0
    selected = [c.strip() for c in raw.split(",") if c.strip()]
    db.create_database(db_path=SESSION.db_path)
    return seeds_module.enqueue(
        categories=selected,
        priority=SESSION.priority_seed,
        db_path=SESSION.db_path,
    )


def _enqueue_manual(raw: str):
    if "|" in raw:
        title, artist = raw.split("|", 1)
        title, artist = title.strip(), artist.strip()
    else:
        title, artist = raw.strip(), ""
    if not title:
        return
    dedupe = f"song:{db._normalize(artist)}|{db._normalize(title)}"
    db.enqueue(
        item_type="song",
        payload={"title": title, "artist": artist},
        priority=SESSION.priority_seed,
        source="manual",
        depth=0,
        dedupe_key=dedupe,
        db_path=SESSION.db_path,
    )
    print(f"  ✅ Encolada: {title} — {artist}")


def _print_queue_top(limit: int = 20):
    conn = db.get_connection(db_path=SESSION.db_path)
    rows = conn.execute(
        """
        SELECT id, item_type, priority, depth, source, payload_json
        FROM queue
        WHERE status = 'pending'
        ORDER BY priority DESC, created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        print("  (cola vacía)")
        return

    print(f"\n  {_hr('·')}")
    print(f"  Próximos {len(rows)} items pendientes:")
    print(f"  {_hr('·')}")
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
            summary = p.get("title") or p.get("name") or p.get("mbid") or "?"
            if p.get("artist"):
                summary += f" — {p['artist']}"
        except Exception:
            summary = str(r["payload_json"])[:40]
        print(f"    #{r['id']:<5} {r['item_type']:<8} "
              f"p={r['priority']:<5} d={r['depth']} "
              f"[{r['source']:<18}] {summary[:40]}")


# ============================================================================
# MENÚ: LANZAR CRAWLER
# ============================================================================

def menu_run():
    _clear()
    print(f"\n{_hr('═')}")
    print("  🚀  LANZAR CRAWLER")
    print(f"{_hr('═')}\n")

    try:
        db.create_database(db_path=SESSION.db_path)
        qs = db.queue_stats(db_path=SESSION.db_path)
    except Exception as e:
        print(f"  ❌ Error preparando DB: {e}")
        _pause()
        return

    print("  Configuración a usar:")
    print(f"    DB              : {SESSION.db_path}")
    print(f"    Workers         : {SESSION.workers}")
    print(f"    Lease (seg)     : {SESSION.lease_seconds}")
    print(f"    Log level       : {SESSION.log_level}")
    print(f"    MAX_DEPTH       : {SESSION.max_depth}")
    print(f"    MIN_TAG_WEIGHT  : {SESSION.min_tag_weight}")
    print(f"    Live stats      : {SESSION.show_live_stats} "
          f"(cada {SESSION.live_stats_interval}s)")
    print(f"\n  Cola antes de arrancar: {qs}\n")

    if qs["pending"] == 0:
        print("  ⚠️  La cola está vacía. Encola semillas antes de arrancar.")
        _pause()
        return

    if not _prompt_yes_no("¿Arrancar ahora?", True):
        return

    SESSION.apply_to_crawler()

    # Logging del crawler
    crawler._setup_logging(SESSION.log_level, log_file="crawler.log")

    # Dashboard
    dashboard: Optional[LiveDashboard] = None
    if SESSION.show_live_stats:
        dashboard = LiveDashboard(interval=SESSION.live_stats_interval)
        dashboard.start()

    # Arrancar crawler en un hilo para poder manejar Ctrl+C aquí también
    stop = threading.Event()

    def _run_crawler():
        try:
            crawler.run(
                db_path=SESSION.db_path,
                workers=SESSION.workers,
                lease_seconds=SESSION.lease_seconds,
                seeds=None,   # ya encoladas
            )
        except Exception as e:
            print(f"\n  ❌ Error en el crawler: {e}")
        finally:
            stop.set()

    t = threading.Thread(target=_run_crawler, name="crawler-main")
    t.start()

    print("\n  ▶  Crawler corriendo. Pulsa Ctrl+C para pararlo limpiamente.\n")

    try:
        while t.is_alive():
            t.join(timeout=1.0)
    except KeyboardInterrupt:
        print("\n\n  ⏹  Ctrl+C recibido. Esperando parada limpia...")
        crawler.stop_event.set()
        t.join(timeout=60.0)

    if dashboard:
        dashboard.stop()

    print("\n  ✅ Crawler detenido.\n")
    _show_final_summary()
    _pause()


def _show_final_summary():
    try:
        stats = db.database_stats(db_path=SESSION.db_path)
        print(f"  {_hr('═')}")
        print("  RESUMEN FINAL")
        print(f"  {_hr('═')}")
        print(f"    Songs           : {stats['songs']}")
        print(f"    Artists         : {stats['artists']}")
        print(f"    Tags            : {stats['tags']}")
        print(f"    Pending reviews : {stats['pending_reviews']}")
        print(f"    Failures        : {stats['failures']}")
        print(f"    Queue           : {stats['queue']}")
        print(f"  {_hr('═')}")
    except Exception as e:
        print(f"  ⚠️  No se pudieron leer stats: {e}")


# ============================================================================
# MENÚ: CONSULTAR DB
# ============================================================================

def menu_inspect():
    while True:
        _clear()
        print(f"\n{_hr('═')}")
        print("  🔍  CONSULTAR BASE DE DATOS")
        print(f"{_hr('═')}\n")

        try:
            stats = db.database_stats(db_path=SESSION.db_path)
            q = stats["queue"]
            print(f"  Canciones: {stats['songs']}  "
                  f"Artistas: {stats['artists']}  "
                  f"Tags: {stats['tags']}\n")
            print(f"  Cola: P={q['pending']} R={q['processing']} "
                  f"D={q['done']} F={q['failed']}\n")
        except Exception as e:
            print(f"  ⚠️  {e}\n")

        print("    1) Ver últimas canciones")
        print("    2) Ver detalle de una canción (con JSON)")
        print("    3) Ver tags más frecuentes")
        print("    4) Ver cola completa")
        print("    5) Ver reviews pendientes")
        print("    6) Ver fallos")
        print("    7) SQL libre")
        print("    0) Volver")

        op = input("\n  Opción: ").strip()

        if op == "1":
            _clear()
            _list_songs(30)
            _pause()
        elif op == "2":
            raw = input("  ID de la canción: ").strip()
            if raw.isdigit():
                _clear()
                _show_song(int(raw))
                _pause()
        elif op == "3":
            _clear()
            _list_top_tags(30)
            _pause()
        elif op == "4":
            _clear()
            _print_queue_top(50)
            _pause()
        elif op == "5":
            _clear()
            _list_reviews()
            _pause()
        elif op == "6":
            _clear()
            _list_failures()
            _pause()
        elif op == "7":
            raw = input("  SQL: ").strip()
            if raw:
                _clear()
                _run_sql(raw)
                _pause()
        elif op == "0":
            return


def _list_songs(limit: int = 30):
    conn = db.get_connection(db_path=SESSION.db_path)
    rows = conn.execute(
        """
        SELECT id, title, artist_display, genre, year,
               resolution_status, tempo_mean, key_name
        FROM songs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        print("  (sin canciones)")
        return

    print(f"\n  {_hr('·', 100)}")
    print(f"  {'ID':<5} {'Título':<38} {'Artista':<24} "
          f"{'Status':<10} {'BPM':<7} {'Key'}")
    print(f"  {_hr('·', 100)}")
    for r in rows:
        bpm = f"{r['tempo_mean']:.1f}" if r['tempo_mean'] else "?"
        print(f"  {r['id']:<5} "
              f"{(r['title'] or '')[:37]:<38} "
              f"{(r['artist_display'] or '')[:23]:<24} "
              f"{(r['resolution_status'] or ''):<10} "
              f"{bpm:<7} {r['key_name'] or ''}")


def _show_song(song_id: int):
    song = db.get_song(song_id, db_path=SESSION.db_path)
    if not song:
        print(f"  No existe #{song_id}")
        return

    print(f"\n  {_hr('═')}")
    print(f"  CANCIÓN #{song_id}")
    print(f"  {_hr('═')}\n")

    for k, v in song.items():
        if k.endswith("_json"):
            continue
        print(f"    {k.ljust(28)} : {v}")

    for field in ("features_json", "completeness_json",
                  "provenance_json", "raw_json"):
        raw = song.get(field)
        if not raw:
            continue
        print(f"\n  ▶ {field}")
        print(f"  {_hr('·')}")
        try:
            data = json.loads(raw)
            pretty = json.dumps(data, indent=2, ensure_ascii=False, default=str)
            for line in pretty.splitlines():
                print(f"    {line}")
        except Exception:
            print(f"    {raw[:300]}...")


def _list_top_tags(limit: int = 30):
    tags = db.get_top_tags(limit, db_path=SESSION.db_path)
    if not tags:
        print("  (sin tags)")
        return
    print(f"\n  Top {len(tags)} tags:")
    for t in tags:
        print(f"    {t['songs_count']:>5}  {t['name']}")


def _list_reviews():
    reviews = db.list_pending_reviews(50, db_path=SESSION.db_path)
    if not reviews:
        print("  (sin reviews pendientes)")
        return
    print(f"\n  {len(reviews)} reviews pendientes:\n")
    for r in reviews:
        print(f"    #{r['id']:<5} conf={r['confidence'] or 0:.3f}  "
              f"query={r['query']!r}")


def _list_failures():
    conn = db.get_connection(db_path=SESSION.db_path)
    rows = conn.execute(
        """
        SELECT query, error, attempts, last_seen_at
        FROM failed_queries
        ORDER BY last_seen_at DESC
        LIMIT 50
        """
    ).fetchall()
    if not rows:
        print("  (sin fallos)")
        return
    print(f"\n  {len(rows)} fallos:\n")
    for r in rows:
        print(f"    att={r['attempts']:<3} {r['error']:<25} "
              f"{r['query'][:50]!r}")


def _run_sql(query: str):
    normalized_query = query.lstrip().casefold()
    if not (normalized_query.startswith("select ")
            or normalized_query.startswith("select\n")
            or normalized_query.startswith("explain ")
            or normalized_query.startswith("pragma ")):
        print("  ❌ Solo se permiten consultas SELECT, EXPLAIN o PRAGMA desde este menú.")
        return

    try:
        conn = db.get_connection(db_path=SESSION.db_path)
        rows = conn.execute(query).fetchall()
    except Exception as e:
        print(f"  ❌ Error SQL: {e}")
        return
    if not rows:
        print("  (sin resultados)")
        return
    print(f"\n  {len(rows)} filas:\n")
    for r in rows:
        d = dict(r)
        for k, v in list(d.items()):
            if isinstance(v, str) and len(v) > 200:
                d[k] = v[:200] + "…"
        print(f"  {d}")


# ============================================================================
# MENÚ: MANTENIMIENTO
# ============================================================================

def menu_maintenance():
    while True:
        _clear()
        print(f"\n{_hr('═')}")
        print("  🔧  MANTENIMIENTO")
        print(f"{_hr('═')}\n")
        print("    1) Ver integridad de la DB")
        print("    2) Vacuum (compactar)")
        print("    3) Recuperar leases expirados")
        print("    4) Borrar la cola")
        print("    5) Reset COMPLETO (borra todo)")
        print("    0) Volver")

        op = input("\n  Opción: ").strip()

        if op == "1":
            _clear()
            ok, issues = db.check_integrity(db_path=SESSION.db_path)
            if ok:
                print("\n  ✅ Integridad OK")
            else:
                print(f"\n  ⚠️  {len(issues)} problemas:")
                for i in issues[:20]:
                    print(f"    {i}")
            _pause()

        elif op == "2":
            print("\n  Compactando (puede tardar)...")
            db.vacuum(db_path=SESSION.db_path)
            print("  ✅ Vacuum completado.")
            _pause()

        elif op == "3":
            n = db.requeue_expired(db_path=SESSION.db_path)
            print(f"\n  ✅ Recuperados {n} items.")
            _pause()

        elif op == "4":
            if _prompt_yes_no("¿Borrar TODA la cola?", False):
                conn = db.get_connection(db_path=SESSION.db_path)
                cur = conn.execute("DELETE FROM queue")
                print(f"\n  ✅ Borrados {cur.rowcount} items.")
            _pause()

        elif op == "5":
            if _prompt_yes_no("¿RESET COMPLETO? Se borra TODO", False):
                if _prompt_yes_no("¿Estás seguro?", False):
                    db.reset_database(db_path=SESSION.db_path, confirm=True)
                    print("\n  ✅ Base recreada.")
            _pause()

        elif op == "0":
            return


# ============================================================================
# MENÚ PRINCIPAL
# ============================================================================

def main_menu():
    while True:
        _clear()
        _banner()

        try:
            stats = db.database_stats(db_path=SESSION.db_path)
            q = stats["queue"]
            print("  📊 Estado actual")
            print(f"     DB     : {SESSION.db_path}")
            print(f"     Songs  : {stats['songs']:<6}  "
                  f"Artists: {stats['artists']:<5}  "
                  f"Tags: {stats['tags']:<5}")
            print(f"     Cola   : P={q['pending']:<6} R={q['processing']:<3} "
                  f"D={q['done']:<6} F={q['failed']:<5}")
            print(f"     Reviews: {stats['pending_reviews']:<5}  "
                  f"Failures: {stats['failures']}")
        except Exception as e:
            print(f"  ⚠️  No se pudo leer la DB: {e}")

        print(f"\n  {_hr('═')}")
        print("  MENÚ PRINCIPAL")
        print(f"  {_hr('═')}\n")
        print("    1) 🚀  Lanzar crawler")
        print("    2) 🌱  Gestionar semillas")
        print("    3) 🔍  Consultar base de datos")
        print("    4) ⚙️   Configuración")
        print("    5) 🔧  Mantenimiento")
        print("    6) 🚪  Salir")

        op = input("\n  Opción: ").strip()

        if op == "1":
            menu_run()
        elif op == "2":
            menu_seeds()
        elif op == "3":
            menu_inspect()
        elif op == "4":
            menu_config()
        elif op == "5":
            menu_maintenance()
        elif op == "6":
            print("\n  👋 Hasta luego.\n")
            break


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    try:
        db.create_database(db_path=SESSION.db_path)
    except Exception as e:
        print(f"❌ No se pudo inicializar la DB: {e}")
        sys.exit(1)

    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n\n  ⏹  Interrumpido. Cerrando limpiamente...\n")
    finally:
        try:
            db.close_connection()
        except Exception:
            pass


if __name__ == "__main__":
    main()