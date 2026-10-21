#!/usr/bin/env python3
"""Interactive SSH monitor for the music crawler service."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

import db


@dataclass
class MonitorConfig:
    db_path: str = db.DEFAULT_DB_PATH
    service_name: str = "music-crawler.service"
    env_file: str = "/etc/music-crawler/music-crawler.env"
    runtime_env_file: str = "/etc/music-crawler/runtime.env"
    backup_dir: str = "/var/lib/music-crawler/backups"
    refresh_seconds: int = 5
    log_lines: int = 40
    use_color: bool = True


class ServiceMonitor:
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.colors = {
            "reset": "\033[0m",
            "green": "\033[32m",
            "yellow": "\033[33m",
            "red": "\033[31m",
            "cyan": "\033[36m",
        }
        if (
            not self.config.use_color
            or not sys.stdout.isatty()
            or os.environ.get("TERM") == "dumb"
        ):
            self.colors = {key: "" for key in self.colors}

    def _color(self, value: str, color: str) -> str:
        return f"{self.colors[color]}{value}{self.colors['reset']}"

    @staticmethod
    def _configure_terminal_encoding() -> None:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is None:
                continue
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    def _clear(self) -> None:
        print("\033[2J\033[H", end="")

    def _pause(self) -> None:
        input("\nPulsa Enter para volver...")

    def _keyboard_available(self) -> bool:
        return (
            os.name != "nt"
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        )

    @contextmanager
    def _raw_keyboard(self):
        import termios
        import tty

        file_descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(file_descriptor)
        try:
            tty.setcbreak(file_descriptor)
            yield
        finally:
            termios.tcsetattr(file_descriptor, termios.TCSADRAIN, previous)

    def _read_key(self) -> str:
        character = sys.stdin.read(1)
        if character != "\x1b":
            if character in {"\r", "\n"}:
                return "enter"
            return character.lower()

        sequence = sys.stdin.read(2)
        return {
            "[A": "up",
            "[B": "down",
            "[C": "right",
            "[D": "left",
        }.get(sequence, "escape")

    def _select_menu(self, title: str, options: list[str]) -> Optional[int]:
        selected = 0
        while True:
            self._clear()
            print(self._color(title, "cyan"))
            print("=" * 72)
            print(
                "Flechas: mover   Enter: seleccionar   "
                "a: agregar semilla   q: salir   b: buscar"
            )
            print()
            for index, option in enumerate(options):
                marker = ">" if index == selected else " "
                color = "green" if index == selected else "reset"
                print(self._color(f"{marker} {index + 1:02d}  {option}", color))

            with self._raw_keyboard():
                key = self._read_key()
            if key == "up":
                selected = (selected - 1) % len(options)
            elif key == "down":
                selected = (selected + 1) % len(options)
            elif key == "enter":
                return selected
            elif key in {"q", "escape"}:
                return None
            elif key == "b":
                return -1
            elif key == "a":
                return -2

    def _read_browser_key(self) -> str:
        if not self._keyboard_available():
            return input("\n[n] siguiente [p] anterior [b] buscar [q] salir: ").strip().lower()
        print("\nFlechas izquierda/derecha: página   b: buscar   q: salir", end="", flush=True)
        with self._raw_keyboard():
            key = self._read_key()
        if key == "right":
            return "n"
        if key == "left":
            return "p"
        return key

    def snapshot(self) -> dict:
        stats = db.database_stats(self.config.db_path)
        connection = db.get_connection(self.config.db_path)
        recent = connection.execute(
            """
            SELECT id, item_type, status, attempts, last_error, updated_at
            FROM queue
            ORDER BY updated_at DESC, id DESC
            LIMIT 8
            """
        ).fetchall()
        return {
            "stats": stats,
            "recent_queue": [dict(row) for row in recent],
            "latest_song": connection.execute(
                "SELECT MAX(updated_at) FROM songs"
            ).fetchone()[0],
            "latest_analysis": connection.execute(
                "SELECT MAX(analyzed_at) FROM song_analysis_versions"
            ).fetchone()[0],
        }

    def service_state(self) -> str:
        if not shutil.which("systemctl"):
            return "systemctl no disponible"
        result = subprocess.run(
            ["systemctl", "is-active", self.config.service_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or result.stderr.strip() or "unknown"

    def health_report(self) -> str:
        state = self.service_state()
        service_label = self._color(
            "OK" if state == "active" else state.upper(),
            "green" if state == "active" else "red",
        )
        try:
            db_ok, issues = db.check_integrity(self.config.db_path)
            database_label = self._color(
                "OK" if db_ok else f"ERROR ({len(issues)})",
                "green" if db_ok else "red",
            )
        except Exception as exc:
            database_label = self._color(f"ERROR: {exc}", "red")

        try:
            total, used, free = shutil.disk_usage(self.config.db_path)
            disk_label = (
                f"{free / (1024 ** 3):.1f} GB libres / "
                f"{total / (1024 ** 3):.1f} GB"
            )
        except OSError as exc:
            disk_label = f"no disponible ({exc})"

        backups = self._backup_files()
        latest_backup = backups[0] if backups else None
        backup_label = latest_backup or "ninguno"

        return "\n".join(
            [
                "SALUD DEL SISTEMA",
                "-" * 50,
                f"Servicio       : {service_label}",
                f"Base de datos  : {database_label}",
                f"Disco          : {disk_label}",
                f"Ultimo backup  : {backup_label}",
                f"Backups         : {len(backups)}",
            ]
        )

    def _backup_files(self) -> list[str]:
        try:
            files = [
                entry for entry in os.listdir(self.config.backup_dir)
                if entry.startswith("tracks-") and entry.endswith(".sqlite")
            ]
        except OSError:
            return []
        return sorted(files, reverse=True)

    def backup_report(self) -> str:
        backups = self._backup_files()
        lines = [
            "BACKUPS",
            "-" * 50,
            f"Directorio: {self.config.backup_dir}",
            f"Cantidad   : {len(backups)}",
        ]
        if not backups:
            lines.append("No hay backups disponibles.")
            return "\n".join(lines)
        lines.append("")
        lines.extend(backups[:20])
        return "\n".join(lines)

    def failed_report(self) -> str:
        connection = db.get_connection(self.config.db_path)
        rows = connection.execute(
            """
            SELECT id, item_type, attempts, last_error, updated_at
            FROM queue
            WHERE status = 'failed'
            ORDER BY updated_at DESC, id DESC
            LIMIT 30
            """
        ).fetchall()
        lines = ["TRABAJOS FALLIDOS", "-" * 72]
        if not rows:
            lines.append(self._color("No hay trabajos fallidos.", "green"))
            return "\n".join(lines)
        for row in rows:
            error = (row["last_error"] or "sin detalle")[:45]
            lines.append(
                f"#{row['id']:<5} {row['item_type']:<8} "
                f"intentos={row['attempts']:<2} {row['updated_at']} {error}"
            )
        return "\n".join(lines)

    def queue_report(self) -> str:
        connection = db.get_connection(self.config.db_path)
        rows = connection.execute(
            """
            SELECT id, item_type, priority, depth, status, payload_json, updated_at
            FROM queue
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 40
            """
        ).fetchall()
        lines = ["COLA PENDIENTE", "-" * 88]
        if not rows:
            lines.append("La cola está vacía.")
            return "\n".join(lines)
        for row in rows:
            payload = json.loads(row["payload_json"])
            title = payload.get("title") or payload.get("name") or "?"
            artist = payload.get("artist")
            label = f"{title} - {artist}" if artist else title
            lines.append(
                f"#{row['id']:<5} p={row['priority']:<5} d={row['depth']:<2} "
                f"{row['item_type']:<7} {label[:55]}"
            )
        return "\n".join(lines)

    def browse_queue(self) -> None:
        page_size = 25
        search = input(
            "Buscar artista o canción (Enter para mostrar toda la cola): "
        ).strip()
        show_all_statuses = False
        page = 0

        def _fetch() -> list:
            connection = db.get_connection(self.config.db_path)
            if show_all_statuses:
                query = (
                    "SELECT id, item_type, status, priority, depth, "
                    "payload_json, updated_at FROM queue "
                    "ORDER BY priority DESC, created_at ASC"
                )
            else:
                query = (
                    "SELECT id, item_type, status, priority, depth, "
                    "payload_json, updated_at FROM queue "
                    "WHERE status = 'pending' "
                    "ORDER BY priority DESC, created_at ASC"
                )
            all_rows = connection.execute(query).fetchall()
            normalized_search = db._normalize(search)
            result = []
            for row in all_rows:
                payload = json.loads(row["payload_json"])
                searchable = db._normalize(" ".join(
                    str(payload.get(field) or "")
                    for field in ("title", "artist", "name", "mbid")
                ))
                if not normalized_search or normalized_search in searchable:
                    result.append(row)
            return result

        rows = _fetch()

        while True:
            total_pages = max(1, (len(rows) + page_size - 1) // page_size)
            page = min(page, total_pages - 1)
            start = page * page_size
            visible = rows[start:start + page_size]

            header = "COLA (todos los estados)" if show_all_statuses else "COLA PENDIENTE"
            print(f"\n{header}")
            print("-" * 88)
            print(
                f"Filtro: {search or '(ninguno)'} | "
                f"Resultados: {len(rows)} | Página {page + 1}/{total_pages}"
            )
            if not visible:
                print("No hay tareas que coincidan.")
            connection = db.get_connection(self.config.db_path)
            for row in visible:
                payload = json.loads(row["payload_json"])
                if row["item_type"] == "artist":
                    artist_mbid = payload.get("mbid") or ""
                    artist_row = connection.execute(
                        "SELECT name FROM artists WHERE artist_mbid = ? LIMIT 1",
                        (artist_mbid,),
                    ).fetchone()
                    label = artist_row["name"] if artist_row else f"mbid:{artist_mbid}"
                else:
                    title = payload.get("title") or payload.get("name") or "?"
                    artist = payload.get("artist")
                    label = f"{title} - {artist}" if artist else title
                status_label = f" [{row['status']}]" if show_all_statuses else ""
                print(
                    f"#{row['id']:<5} p={row['priority']:<5} d={row['depth']:<2} "
                    f"{row['item_type']:<7}{status_label} {label[:55]}"
                )

            print(
                "\nFlechas: página   b: buscar   e: pendientes/todos   "
                "r: refrescar   q: salir"
            )
            command = self._read_browser_key()
            if command == "n" and page + 1 < total_pages:
                page += 1
            elif command == "p" and page > 0:
                page -= 1
            elif command == "b":
                search = input("Nuevo artista o canción (Enter=toda): ").strip()
                page = 0
                rows = _fetch()
            elif command == "e":
                show_all_statuses = not show_all_statuses
                page = 0
                rows = _fetch()
            elif command == "r":
                rows = _fetch()
            elif command == "q" or not command:
                return

    def _write_runtime_config(self, workers: int, lease_seconds: int) -> None:
        parent = os.path.dirname(self.config.runtime_env_file)
        os.makedirs(parent, exist_ok=True)
        temporary_path = f"{self.config.runtime_env_file}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as runtime_file:
            runtime_file.write(f"CRAWLER_WORKERS={workers}\n")
            runtime_file.write(f"CRAWLER_LEASE_SECONDS={lease_seconds}\n")
        os.replace(temporary_path, self.config.runtime_env_file)

    def runtime_configuration(self) -> dict[str, int]:
        values = self._read_environment_file(self.config.runtime_env_file)
        try:
            workers = int(values.get("CRAWLER_WORKERS", "1"))
        except ValueError:
            workers = 1
        try:
            lease_seconds = int(values.get("CRAWLER_LEASE_SECONDS", "600"))
        except ValueError:
            lease_seconds = 600
        return {
            "workers": max(1, min(32, workers)),
            "lease_seconds": max(60, min(3600, lease_seconds)),
        }

    def change_runtime_configuration(self) -> None:
        current = self.runtime_configuration()
        print(f"Workers actuales: {current['workers']}")
        print(f"Lease actual: {current['lease_seconds']} segundos")
        try:
            workers = int(input("Workers (1-32): ").strip() or current["workers"])
            lease_seconds = int(
                input("Lease en segundos (60-3600): ").strip()
                or current["lease_seconds"]
            )
        except ValueError:
            print("Valores inválidos.")
            self._pause()
            return
        if not 1 <= workers <= 32 or not 60 <= lease_seconds <= 3600:
            print("Workers debe estar entre 1 y 32; lease entre 60 y 3600.")
            self._pause()
            return
        self._write_runtime_config(workers, lease_seconds)
        print("Configuración guardada.")
        if input("¿Reiniciar servicio ahora? [s/N]: ").strip().lower() in {"s", "si", "sí", "y", "yes"}:
            print(self.restart_service())
        self._pause()

    def change_queue_priority(self) -> None:
        try:
            queue_id = int(input("ID pendiente: ").strip())
            priority = float(input("Nueva prioridad: ").strip())
        except ValueError:
            print("Valores inválidos.")
            self._pause()
            return
        connection = db.get_connection(self.config.db_path)
        cursor = connection.execute(
            "UPDATE queue SET priority = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (priority, db._now(), queue_id),
        )
        print("Prioridad actualizada." if cursor.rowcount else "No existe una tarea pendiente con ese ID.")
        self._pause()

    def edit_pending_seed(self) -> None:
        try:
            queue_id = int(input("ID pendiente: ").strip())
        except ValueError:
            print("ID inválido.")
            self._pause()
            return
        connection = db.get_connection(self.config.db_path)
        row = connection.execute(
            "SELECT item_type, payload_json FROM queue "
            "WHERE id = ? AND status = 'pending'",
            (queue_id,),
        ).fetchone()
        if not row:
            print("Solo puedes editar tareas pendientes existentes.")
            self._pause()
            return
        if row["item_type"] != "song":
            print("Solo puedes editar tareas pendientes de tipo canción.")
            self._pause()
            return
        payload = json.loads(row["payload_json"])
        title = input(f"Título [{payload.get('title', '')}]: ").strip() or payload.get("title", "")
        artist = input(f"Artista [{payload.get('artist', '')}]: ").strip() or payload.get("artist", "")
        dedupe_key = f"song:{db._normalize(artist)}|{db._normalize(title)}"
        conflict = connection.execute(
            "SELECT id FROM queue WHERE dedupe_key = ? AND id != ? "
            "AND status IN ('pending', 'processing')",
            (dedupe_key, queue_id),
        ).fetchone()
        if conflict:
            print(f"Ya existe otra tarea activa con ese contenido: #{conflict['id']}")
        else:
            connection.execute(
                "UPDATE queue SET payload_json = ?, dedupe_key = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (json.dumps({"title": title, "artist": artist}, ensure_ascii=False), dedupe_key, db._now(), queue_id),
            )
            print("Tarea actualizada.")
        self._pause()

    def cancel_pending(self) -> None:
        try:
            queue_id = int(input("ID pendiente a cancelar: ").strip())
        except ValueError:
            print("ID inválido.")
            self._pause()
            return
        if input("¿Eliminar esta tarea? [s/N]: ").strip().lower() not in {"s", "si", "sí", "y", "yes"}:
            return
        connection = db.get_connection(self.config.db_path)
        cursor = connection.execute(
            "DELETE FROM queue WHERE id = ? AND status = 'pending'",
            (queue_id,),
        )
        print("Tarea eliminada." if cursor.rowcount else "No existe una tarea pendiente con ese ID.")
        self._pause()

    def export_database(self) -> None:
        default_name = datetime.now(timezone.utc).strftime(
            "tracks-export-%Y%m%dT%H%M%SZ.sqlite"
        )
        destination = input(
            f"Ruta de exportación [{self.config.backup_dir}/{default_name}]: "
        ).strip()
        destination = destination or os.path.join(
            self.config.backup_dir, default_name
        )
        parent = os.path.dirname(os.path.abspath(destination))
        try:
            os.makedirs(parent, exist_ok=True)
            source = db.get_connection(self.config.db_path)
            exported = sqlite3.connect(destination)
            try:
                source.backup(exported)
            finally:
                exported.close()
            print(f"Base exportada correctamente: {destination}")
        except (OSError, sqlite3.Error) as exc:
            print(f"No se pudo exportar la base: {exc}")
        self._pause()

    def export_songs_csv(self) -> None:
        default_name = datetime.now(timezone.utc).strftime(
            "songs-export-%Y%m%dT%H%M%SZ.csv"
        )
        destination = input(
            f"Ruta de exportación [{self.config.backup_dir}/{default_name}]: "
        ).strip()
        destination = destination or os.path.join(
            self.config.backup_dir, default_name
        )
        columns = [
            "id", "canonical_id", "title", "artist_display", "album",
            "genre", "year", "duration_sec", "mbid", "artist_mbid",
            "isrc", "resolution_status", "resolution_confidence",
            "tempo_mean", "key_name", "deezer_bpm", "created_at",
            "updated_at",
        ]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
            connection = db.get_connection(self.config.db_path)
            rows = connection.execute(
                "SELECT " + ", ".join(columns) + " FROM songs ORDER BY id"
            ).fetchall()
            with open(destination, "w", newline="", encoding="utf-8-sig") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=columns)
                writer.writeheader()
                writer.writerows({column: row[column] for column in columns} for row in rows)
            print(f"CSV exportado correctamente: {destination} ({len(rows)} canciones)")
        except (OSError, sqlite3.Error) as exc:
            print(f"No se pudo exportar el CSV: {exc}")
        self._pause()

    def _run_system_command(self, command: list[str]) -> str:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        output = (result.stdout or result.stderr).strip()
        return output or f"Comando terminado con código {result.returncode}."

    def recent_logs(self) -> str:
        if not shutil.which("journalctl"):
            return "journalctl no disponible."
        return self._run_system_command(
            [
                "journalctl",
                "-u",
                self.config.service_name,
                "-n",
                str(self.config.log_lines),
                "--no-pager",
            ]
        )

    def follow_logs(self) -> None:
        if not shutil.which("journalctl"):
            print("journalctl no disponible.")
            input("\nPulsa Enter para volver...")
            return

        process = subprocess.Popen(
            ["journalctl", "-u", self.config.service_name, "-f", "-n", "20"],
            stdout=None,
            stderr=None,
        )
        try:
            process.wait()
        except KeyboardInterrupt:
            process.terminate()
            process.wait(timeout=5)

    def database_report(self) -> str:
        connection = db.get_connection(self.config.db_path)
        tables = [
            "songs",
            "song_analysis_versions",
            "artists",
            "tags",
            "queue",
            "review_queue",
            "failed_queries",
        ]
        counts = []
        for table in tables:
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            counts.append(f"{table:<25} {count:>10}")

        songs = connection.execute(
            """
            SELECT id, title, artist_display, resolution_status, updated_at
            FROM songs
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()
        recent_songs = [
            f"#{row['id']:<5} {(row['title'] or '')[:30]:<30} "
            f"{(row['artist_display'] or '')[:22]:<22} "
            f"{row['resolution_status'] or 'unknown':<10} {row['updated_at']}"
            for row in songs
        ]
        return "\n".join(
            [
                "TABLAS",
                "-" * 40,
                *counts,
                "",
                "ULTIMAS CANCIONES",
                "-" * 72,
                *recent_songs,
            ]
        )

    def browse_database(self) -> None:
        """Explora canciones guardadas por páginas y permite buscar título/artista."""
        page_size = 25
        search = input(
            "Buscar artista o canción (Enter para mostrar toda la base): "
        ).strip()
        page = 0

        while True:
            connection = db.get_connection(self.config.db_path)
            rows = connection.execute(
                """
                SELECT id, title, artist_display, resolution_status, updated_at
                FROM songs
                ORDER BY id DESC
                """
            ).fetchall()
            normalized_search = db._normalize(search)
            filtered = [
                row for row in rows
                if not normalized_search
                or normalized_search in db._normalize(
                    f"{row['title'] or ''} {row['artist_display'] or ''}"
                )
            ]

            total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
            page = min(page, total_pages - 1)
            start = page * page_size
            visible = filtered[start:start + page_size]

            print("\nCANCIONES EN LA BASE DE DATOS")
            print("-" * 88)
            print(
                f"Filtro: {search or '(ninguno)'} | "
                f"Resultados: {len(filtered)} | Página {page + 1}/{total_pages}"
            )
            if not visible:
                print("No hay canciones que coincidan.")
            for row in visible:
                print(
                    f"#{row['id']:<5} {(row['title'] or '')[:35]:<35} "
                    f"{(row['artist_display'] or '')[:27]:<27} "
                    f"{row['resolution_status'] or 'unknown':<10} "
                    f"{row['updated_at']}"
                )

            command = self._read_browser_key()
            if command == "n" and page + 1 < total_pages:
                page += 1
            elif command == "p" and page > 0:
                page -= 1
            elif command == "b":
                search = input("Nuevo artista o canción (Enter=toda): ").strip()
                page = 0
            elif command == "q" or not command:
                return

    def song_detail(self) -> None:
        raw_id = input("ID de la canción: ").strip()
        if not raw_id.isdigit():
            print("ID inválido.")
            self._pause()
            return

        song_id = int(raw_id)
        song = db.get_song(song_id, db_path=self.config.db_path)
        if not song:
            print(f"No existe la canción #{song_id}.")
            self._pause()
            return

        print(f"\nDETALLE COMPLETO DE LA CANCION #{song_id}")
        print("=" * 88)
        json_fields = {
            "features_json",
            "completeness_json",
            "provenance_json",
            "raw_json",
        }
        for key, value in song.items():
            if key in json_fields:
                continue
            print(f"{key:<28}: {value}")

        for field in (
            "features_json",
            "completeness_json",
            "provenance_json",
            "raw_json",
        ):
            print(f"\n{field}")
            print("-" * 88)
            value = song.get(field)
            if not value:
                print("(sin datos)")
                continue
            try:
                parsed = json.loads(value)
                print(json.dumps(parsed, ensure_ascii=False, indent=2, default=str))
            except (TypeError, json.JSONDecodeError):
                print(value)

        connection = db.get_connection(self.config.db_path)
        versions = connection.execute(
            """
            SELECT id, analyzed_at, analyzer_version, ok, requires_review,
                   resolution_status, resolution_confidence, original_query
            FROM song_analysis_versions
            WHERE song_id = ?
            ORDER BY analyzed_at DESC, id DESC
            """,
            (song_id,),
        ).fetchall()
        print("\nHISTORIAL DE ANALISIS")
        print("-" * 88)
        if not versions:
            print("(sin historial)")
        for version in versions:
            print(
                f"#{version['id']} {version['analyzed_at']} "
                f"status={version['resolution_status']} "
                f"confidence={version['resolution_confidence']} "
                f"review={version['requires_review']} "
                f"version={version['analyzer_version']}"
            )
        self._pause()

    def configuration_report(self) -> str:
        environment = self._read_environment_file()
        api_configured = bool(
            environment.get("LASTFM_API_KEY", os.environ.get("LASTFM_API_KEY", "")).strip()
        )
        user_agent = environment.get(
            "MUSIC_ANALYZER_USER_AGENT",
            os.environ.get("MUSIC_ANALYZER_USER_AGENT", "default"),
        )
        analyzer_log = environment.get(
            "ANALYZER_LOG_FILE",
            os.environ.get("ANALYZER_LOG_FILE", "analyzer.log"),
        )
        return "\n".join(
            [
                "CONFIGURACION DEL MONITOR",
                "-" * 40,
                f"DB path              : {self.config.db_path}",
                f"Servicio             : {self.config.service_name}",
                f"Refresh              : {self.config.refresh_seconds}s",
                f"Log lines            : {self.config.log_lines}",
                f"Environment file     : {self.config.env_file}",
                f"Runtime config       : {self.config.runtime_env_file}",
                f"Workers              : {self.runtime_configuration()['workers']}",
                f"Lease                : {self.runtime_configuration()['lease_seconds']}s",
                f"Last.fm API key     : {'configurada' if api_configured else 'no configurada'}",
                f"User-Agent          : {user_agent}",
                f"Analyzer log        : {analyzer_log}",
            ]
        )

    def _read_environment_file(self, path: Optional[str] = None) -> dict[str, str]:
        values: dict[str, str] = {}
        try:
            with open(path or self.config.env_file, encoding="utf-8") as environment_file:
                for raw_line in environment_file:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
        except OSError:
            pass
        return values

    def service_unit(self) -> str:
        if not shutil.which("systemctl"):
            return "systemctl no disponible."
        return self._run_system_command(
            ["systemctl", "cat", self.config.service_name]
        )

    def add_seed(self) -> None:
        title = input("Título de la canción: ").strip()
        if not title:
            print("El título no puede estar vacío.")
            input("Pulsa Enter para volver...")
            return

        artist = input("Artista (opcional): ").strip()
        normalized_artist = db._normalize(artist)
        normalized_title = db._normalize(title)
        dedupe_key = f"song:{normalized_artist}|{normalized_title}"
        connection = db.get_connection(self.config.db_path)
        existing = connection.execute(
            """
            SELECT id FROM queue
            WHERE dedupe_key = ? AND status IN ('pending', 'processing')
            LIMIT 1
            """,
            (dedupe_key,),
        ).fetchone()

        if existing:
            print(f"La canción ya está pendiente o en proceso: #{existing['id']}")
            input("Pulsa Enter para volver...")
            return

        queue_id = db.enqueue(
            item_type="song",
            payload={"title": title, "artist": artist},
            priority=10.0,
            source="manual_monitor",
            depth=0,
            dedupe_key=dedupe_key,
            db_path=self.config.db_path,
        )
        print(f"Semilla añadida a la cola: #{queue_id} - {title} - {artist}")
        input("Pulsa Enter para volver...")

    def service_status(self) -> str:
        if not shutil.which("systemctl"):
            return "systemctl no disponible."
        return self._run_system_command(
            ["systemctl", "status", self.config.service_name, "--no-pager"]
        )

    def restart_service(self) -> str:
        if not shutil.which("systemctl"):
            return "systemctl no disponible."
        return self._run_system_command(
            ["sudo", "systemctl", "restart", self.config.service_name]
        )

    def render(self) -> None:
        self._clear()
        snapshot = self.snapshot()
        stats = snapshot["stats"]
        queue = stats["queue"]

        state = self.service_state()
        state_label = self._color(
            "ACTIVO" if state == "active" else state.upper(),
            "green" if state == "active" else "red",
        )
        alerts = []
        if queue["failed"]:
            alerts.append(self._color(f"{queue['failed']} fallidos", "red"))
        if stats["pending_reviews"]:
            alerts.append(self._color(f"{stats['pending_reviews']} reviews", "yellow"))

        print(self._color("MUSIC CRAWLER", "cyan"))
        print("=" * 72)
        print(f"Servicio: {state_label}    DB: {self.config.db_path}")
        print("-" * 72)
        print(
            f"Canciones {stats['songs']:<6}  Artistas {stats['artists']:<6} "
            f"Tags {stats['tags']:<6}  En analisis {queue['processing']:<3}"
        )
        print(
            f"Cola: {queue['pending']} pendientes | {queue['done']} hechas | "
            f"{queue['failed']} fallidas"
        )
        print(
            f"Ultima cancion: {snapshot['latest_song'] or 'sin datos'} | "
            f"Ultimo analisis: {snapshot['latest_analysis'] or 'sin datos'}"
        )
        print(f"Alertas: {' | '.join(alerts) if alerts else 'ninguna'}")
        print("\nACTIVIDAD RECIENTE")
        print("-" * 72)
        for item in snapshot["recent_queue"]:
            error = f" error={item['last_error'][:35]}" if item["last_error"] else ""
            print(
                f"#{item['id']:<5} {item['item_type']:<8} "
                f"{item['status']:<10} attempts={item['attempts']:<2} "
                f"{item['updated_at']}{error}"
            )

    def live(self) -> None:
        try:
            while True:
                self.render()
                print(f"\nActualizando cada {self.config.refresh_seconds}s. Ctrl+C para volver.")
                time.sleep(self.config.refresh_seconds)
        except KeyboardInterrupt:
            return

    def summary(self) -> str:
        snapshot = self.snapshot()
        stats = snapshot["stats"]
        queue = stats["queue"]
        return "\n".join(
            [
                f"service={self.service_state()}",
                f"songs={stats['songs']}",
                f"artists={stats['artists']}",
                f"tags={stats['tags']}",
                f"pending={queue['pending']}",
                f"processing={queue['processing']}",
                f"done={queue['done']}",
                f"failed={queue['failed']}",
                f"reviews={stats['pending_reviews']}",
                f"last_song={snapshot['latest_song'] or ''}",
                f"last_analysis={snapshot['latest_analysis'] or ''}",
            ]
        )

    def _keyboard_menu(self) -> None:
        options = [
            "Actualizar panel",
            "Monitor en vivo",
            "Seguir logs en vivo",
            "Ver logs recientes",
            "Explorar base de datos",
            "Ver detalle completo de canción",
            "Salud del sistema",
            "Ver configuración",
            "Ver trabajos fallidos",
            "Ver backups",
            "Estado detallado del servicio",
            "Cambiar workers y lease",
            "Añadir semilla manual",
            "Ver cola pendiente",
            "Editar tarea pendiente",
            "Cambiar prioridad",
            "Cancelar tarea pendiente",
            "Reiniciar servicio",
            "Exportar base SQLite",
            "Exportar canciones CSV",
        ]

        while True:
            try:
                self.render()
            except Exception as exc:
                print(f"\nError leyendo la base: {exc}")
            selected = self._select_menu("MENU PRINCIPAL", options)
            if selected is None:
                return
            if selected == -1:
                self.browse_database()
                continue
            if selected == -2:
                self.add_seed()
                continue
            if selected == 0:
                continue
            if selected == 1:
                self.live()
            elif selected == 2:
                self.follow_logs()
            elif selected == 3:
                print("\n" + self.recent_logs())
                self._pause()
            elif selected == 4:
                self.browse_database()
            elif selected == 5:
                self.song_detail()
            elif selected == 6:
                print("\n" + self.health_report())
                self._pause()
            elif selected == 7:
                print("\n" + self.configuration_report())
                print("\nDEFINICION SYSTEMD")
                print("-" * 40)
                print(self.service_unit())
                self._pause()
            elif selected == 8:
                print("\n" + self.failed_report())
                self._pause()
            elif selected == 9:
                print("\n" + self.backup_report())
                self._pause()
            elif selected == 10:
                print("\n" + self.service_status())
                self._pause()
            elif selected == 11:
                self.change_runtime_configuration()
            elif selected == 12:
                self.add_seed()
            elif selected == 13:
                self.browse_queue()
            elif selected == 14:
                self.edit_pending_seed()
            elif selected == 15:
                self.change_queue_priority()
            elif selected == 16:
                self.cancel_pending()
            elif selected == 17:
                confirm = input("¿Reiniciar el servicio? [s/N]: ").strip().lower()
                if confirm in {"s", "si", "sí", "y", "yes"}:
                    print("\n" + self.restart_service())
                self._pause()
            elif selected == 18:
                self.export_database()
            elif selected == 19:
                self.export_songs_csv()

    def menu(self) -> None:
        if self._keyboard_available():
            self._keyboard_menu()
        else:
            self._legacy_menu()

    def _legacy_menu(self) -> None:
        while True:
            try:
                self.render()
            except Exception as exc:
                print(f"\nError leyendo la base: {exc}")

            print("\nMENU PRINCIPAL")
            print("  1  Actualizar panel")
            print("  2  Monitor en vivo")
            print("  3  Seguir logs en vivo")
            print("  4  Ver logs recientes")
            print("  5  Explorar base de datos")
            print("  6  Ver detalle completo de canción")
            print("  7  Salud del sistema")
            print("  8  Ver configuracion")
            print("  9  Ver trabajos fallidos")
            print("  10 Ver backups")
            print("  11 Estado detallado del servicio")
            print("  12 Cambiar workers y lease")
            print("  13 Añadir semilla manual")
            print("  14 Ver cola pendiente")
            print("  15 Editar tarea pendiente")
            print("  16 Cambiar prioridad")
            print("  17 Cancelar tarea pendiente")
            print("  18 Reiniciar servicio")
            print("  19 Exportar base SQLite")
            print("  20 Exportar canciones CSV")
            print("  0  Salir")

            try:
                option = input("\nOpción: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if option == "1":
                continue
            if option == "2":
                self.live()
                continue
            if option == "3":
                self.follow_logs()
                continue
            if option == "4":
                print("\n" + self.recent_logs())
                self._pause()
                continue
            if option == "5":
                self.browse_database()
                continue
            if option == "6":
                self.song_detail()
                continue
            if option == "7":
                print("\n" + self.health_report())
                self._pause()
                continue
            if option == "8":
                print("\n" + self.configuration_report())
                print("\nDEFINICION SYSTEMD")
                print("-" * 40)
                print(self.service_unit())
                self._pause()
                continue
            if option == "9":
                print("\n" + self.failed_report())
                self._pause()
                continue
            if option == "10":
                print("\n" + self.backup_report())
                self._pause()
                continue
            if option == "11":
                print("\n" + self.service_status())
                self._pause()
                continue
            if option == "12":
                self.change_runtime_configuration()
                continue
            if option == "13":
                self.add_seed()
                continue
            if option == "14":
                self.browse_queue()
                continue
            if option == "15":
                self.edit_pending_seed()
                continue
            if option == "16":
                self.change_queue_priority()
                continue
            if option == "17":
                self.cancel_pending()
                continue
            if option == "18":
                confirm = input("¿Reiniciar el servicio? [s/N]: ").strip().lower()
                if confirm in {"s", "si", "sí", "y", "yes"}:
                    print("\n" + self.restart_service())
                self._pause()
                continue
            if option == "19":
                self.export_database()
                continue
            if option == "20":
                self.export_songs_csv()
                continue
            if option == "0":
                return


def main() -> None:
    ServiceMonitor._configure_terminal_encoding()
    parser = argparse.ArgumentParser(description="Monitor SSH del crawler musical")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH, help="Ruta de SQLite")
    parser.add_argument(
        "--service",
        default="music-crawler.service",
        help="Nombre de la unidad systemd",
    )
    parser.add_argument(
        "--env-file",
        default="/etc/music-crawler/music-crawler.env",
        help="Archivo EnvironmentFile de systemd",
    )
    parser.add_argument(
        "--backup-dir",
        default="/var/lib/music-crawler/backups",
        help="Directorio de backups SQLite",
    )
    parser.add_argument(
        "--runtime-env-file",
        default="/etc/music-crawler/runtime.env",
        help="Configuración operativa de workers y lease",
    )
    parser.add_argument("--refresh", type=int, default=5, help="Intervalo en segundos")
    parser.add_argument("--log-lines", type=int, default=40, help="Cantidad de logs")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Imprime un resumen y termina, útil para scripts",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Desactiva colores y control de pantalla",
    )
    args = parser.parse_args()

    config = MonitorConfig(
        db_path=args.db,
        service_name=args.service,
        env_file=args.env_file,
        backup_dir=args.backup_dir,
        runtime_env_file=args.runtime_env_file,
        refresh_seconds=max(1, args.refresh),
        log_lines=max(1, min(args.log_lines, 500)),
        use_color=not args.no_color,
    )
    try:
        monitor = ServiceMonitor(config)
        if args.summary:
            print(monitor.summary())
        else:
            monitor.menu()
    finally:
        db.close_connection()


if __name__ == "__main__":
    main()
