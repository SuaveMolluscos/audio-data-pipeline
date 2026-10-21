#!/usr/bin/env python3
"""
seeds.py — Semillas iniciales del crawler.

Conjunto curado de ~150 canciones que cubren:
    - Múltiples géneros (pop, rock, hip-hop, jazz, clásica, etc.)
    - Múltiples décadas (1960s → 2020s)
    - Múltiples idiomas (inglés, español, portugués, francés, etc.)
    - Múltiples niveles de popularidad (mainstream + nicho)

Diversidad importa: si todas las semillas son del mismo género/época,
el crawler se expande en una sola dirección y la DB queda sesgada.

CLI:
    python seeds.py list                    → todas las semillas por categoría
    python seeds.py stats                   → conteo por categoría
    python seeds.py enqueue                 → encolar TODAS en la cola
    python seeds.py enqueue --categories pop,rock
    python seeds.py enqueue --dry-run       → ver qué se encolaría, sin tocar la DB
    python seeds.py enqueue --priority 9.0
"""

from __future__ import annotations

import argparse
from typing import Optional

import db


# ============================================================================
# SEMILLAS POR CATEGORÍA
# ============================================================================
#
# Cada entrada es una tupla (título, artista).
# Puedes comentar/quitar canciones sin romper nada.
#

SEEDS_BY_CATEGORY: dict[str, list[tuple[str, str]]] = {

    # ─── POP MAINSTREAM (US/UK) ──────────────────────────────────────────
    "pop": [
        ("Billie Jean", "Michael Jackson"),
        ("Like a Prayer", "Madonna"),
        ("Shape of You", "Ed Sheeran"),
        ("Blinding Lights", "The Weeknd"),
        ("Rolling in the Deep", "Adele"),
        ("Bad Guy", "Billie Eilish"),
        ("Uptown Funk", "Mark Ronson"),
        ("Shake It Off", "Taylor Swift"),
        ("Anti-Hero", "Taylor Swift"),
        ("As It Was", "Harry Styles"),
        ("Levitating", "Dua Lipa"),
        ("Flowers", "Miley Cyrus"),
    ],

    # ─── ROCK CLÁSICO Y ALTERNATIVO ──────────────────────────────────────
    "rock": [
        ("Bohemian Rhapsody", "Queen"),
        ("Stairway to Heaven", "Led Zeppelin"),
        ("Smells Like Teen Spirit", "Nirvana"),
        ("Hotel California", "Eagles"),
        ("Imagine", "John Lennon"),
        ("Hey Jude", "The Beatles"),
        ("Sweet Child O' Mine", "Guns N' Roses"),
        ("Wonderwall", "Oasis"),
        ("Seven Nation Army", "The White Stripes"),
        ("Do I Wanna Know?", "Arctic Monkeys"),
        ("Mr. Brightside", "The Killers"),
        ("Creep", "Radiohead"),
        ("Everlong", "Foo Fighters"),
        ("Losing My Religion", "R.E.M."),
    ],

    # ─── HIP-HOP / RAP ───────────────────────────────────────────────────
    "hip_hop": [
        ("Lose Yourself", "Eminem"),
        ("God's Plan", "Drake"),
        ("Sicko Mode", "Travis Scott"),
        ("HUMBLE.", "Kendrick Lamar"),
        ("Alright", "Kendrick Lamar"),
        ("Juicy", "The Notorious B.I.G."),
        ("N.Y. State of Mind", "Nas"),
        ("Ms. Jackson", "OutKast"),
        ("Still D.R.E.", "Dr. Dre"),
        ("Empire State of Mind", "Jay-Z"),
    ],

    # ─── R&B / SOUL ──────────────────────────────────────────────────────
    "rnb_soul": [
        ("Superstition", "Stevie Wonder"),
        ("Ain't No Mountain High Enough", "Marvin Gaye"),
        ("Say My Name", "Destiny's Child"),
        ("No Scrubs", "TLC"),
        ("Redbone", "Childish Gambino"),
        ("Kill Bill", "SZA"),
        ("Adorn", "Miguel"),
        ("Ordinary People", "John Legend"),
        ("Let's Stay Together", "Al Green"),
        ("What's Going On", "Marvin Gaye"),
    ],

    # ─── ELECTRÓNICA ─────────────────────────────────────────────────────
    "electronic": [
        ("Strobe", "deadmau5"),
        ("One More Time", "Daft Punk"),
        ("Get Lucky", "Daft Punk"),
        ("Titanium", "David Guetta"),
        ("Wake Me Up", "Avicii"),
        ("Levels", "Avicii"),
        ("Animals", "Martin Garrix"),
        ("Around the World", "Daft Punk"),
        ("Sandstorm", "Darude"),
        ("Windowlicker", "Aphex Twin"),
    ],

    # ─── LATINO / ESPAÑOL ────────────────────────────────────────────────
    "latin": [
        ("No Te Vayas", "Cristian Castro"),
        ("La Incondicional", "Luis Miguel"),
        ("Hasta la Raíz", "Natalia Lafourcade"),
        ("Color Esperanza", "Diego Torres"),
        ("La Bamba", "Ritchie Valens"),
        ("Livin' la Vida Loca", "Ricky Martin"),
        ("Despacito", "Luis Fonsi"),
        ("Bailando", "Enrique Iglesias"),
        ("Todo de Ti", "Rauw Alejandro"),
        ("Tití Me Preguntó", "Bad Bunny"),
        ("Me Porto Bonito", "Bad Bunny"),
        ("Tusa", "Karol G"),
        ("PROVENZA", "Karol G"),
        ("Ojos Así", "Shakira"),
        ("Hips Don't Lie", "Shakira"),
        ("La Tortura", "Shakira"),
    ],

    # ─── BRASIL / PORTUGUÉS ──────────────────────────────────────────────
    "brazilian": [
        ("Figa de Guiné", "Alcione"),
        ("Garota de Ipanema", "João Gilberto"),
        ("Aquarela do Brasil", "Ary Barroso"),
        ("Mas Que Nada", "Jorge Ben Jor"),
        ("Construção", "Chico Buarque"),
        ("País Tropical", "Jorge Ben Jor"),
        ("Ai Se Eu Te Pego", "Michel Teló"),
        ("Chega de Saudade", "João Gilberto"),
        ("Aquele Abraço", "Gilberto Gil"),
        ("Carinhoso", "Pixinguinha"),
    ],

    # ─── EUROPEO (FR/DE/IT) ──────────────────────────────────────────────
    "european": [
        ("La Vie en Rose", "Édith Piaf"),
        ("Ne Me Quitte Pas", "Jacques Brel"),
        ("Dernière Danse", "Indila"),
        ("Papaoutai", "Stromae"),
        ("Formidable", "Stromae"),
        ("99 Luftballons", "Nena"),
        ("Du Hast", "Rammstein"),
        ("Atemlos durch die Nacht", "Helene Fischer"),
        ("Nel Blu Dipinto Di Blu", "Domenico Modugno"),
        ("Con Te Partirò", "Andrea Bocelli"),
    ],

    # ─── ASIÁTICO (JP/KR) ────────────────────────────────────────────────
    "asian": [
        ("Plastic Love", "Mariya Takeuchi"),
        ("Rydeen", "Yellow Magic Orchestra"),
        ("Lemon", "Kenshi Yonezu"),
        ("Pretender", "Official HIGE DANDism"),
        ("Gangnam Style", "PSY"),
        ("Dynamite", "BTS"),
        ("How You Like That", "BLACKPINK"),
        ("Idol", "Yoasobi"),
        ("Stay With Me", "Miki Matsubara"),
        ("Marigold", "Aimyon"),
    ],

    # ─── JAZZ ────────────────────────────────────────────────────────────
    "jazz": [
        ("Take Five", "Dave Brubeck"),
        ("So What", "Miles Davis"),
        ("My Favorite Things", "John Coltrane"),
        ("What a Wonderful World", "Louis Armstrong"),
        ("Fly Me to the Moon", "Frank Sinatra"),
        ("Round Midnight", "Thelonious Monk"),
        ("Autumn Leaves", "Bill Evans"),
        ("A Love Supreme", "John Coltrane"),
        ("Summertime", "Ella Fitzgerald"),
        ("Strange Fruit", "Billie Holiday"),
    ],

    # ─── CLÁSICA ─────────────────────────────────────────────────────────
    "classical": [
        ("Claro de Luna", "Ludwig van Beethoven"),
        ("Las Cuatro Estaciones", "Antonio Vivaldi"),
        ("Boléro", "Maurice Ravel"),
        ("Nocturno Op. 9 No. 2", "Frédéric Chopin"),
        ("El Cascanueces", "Pyotr Ilyich Tchaikovsky"),
        ("Sinfonía No. 5", "Ludwig van Beethoven"),
        ("Réquiem", "Wolfgang Amadeus Mozart"),
        ("El Barbero de Sevilla", "Gioachino Rossini"),
    ],

    # ─── REGGAE ──────────────────────────────────────────────────────────
    "reggae": [
        ("No Woman No Cry", "Bob Marley"),
        ("Three Little Birds", "Bob Marley"),
        ("Is This Love", "Bob Marley"),
        ("Redemption Song", "Bob Marley"),
        ("Buffalo Soldier", "Bob Marley"),
        ("54-46 Was My Number", "Toots & The Maytals"),
        ("Rivers of Babylon", "Boney M."),
        ("Bad Boys", "Inner Circle"),
    ],

    # ─── METAL ───────────────────────────────────────────────────────────
    "metal": [
        ("Master of Puppets", "Metallica"),
        ("Paranoid", "Black Sabbath"),
        ("Ace of Spades", "Motörhead"),
        ("Chop Suey!", "System of a Down"),
        ("Enter Sandman", "Metallica"),
        ("Iron Man", "Black Sabbath"),
        ("The Trooper", "Iron Maiden"),
        ("Fear of the Dark", "Iron Maiden"),
    ],

    # ─── FOLK / COUNTRY ──────────────────────────────────────────────────
    "folk_country": [
        ("The Sound of Silence", "Simon & Garfunkel"),
        ("Blowin' in the Wind", "Bob Dylan"),
        ("Jolene", "Dolly Parton"),
        ("Ring of Fire", "Johnny Cash"),
        ("Hurt", "Johnny Cash"),
        ("Take Me Home, Country Roads", "John Denver"),
        ("The Times They Are A-Changin'", "Bob Dylan"),
        ("Wagon Wheel", "Old Crow Medicine Show"),
    ],

    # ─── AFRICANO ────────────────────────────────────────────────────────
    "african": [
        ("Yeke Yeke", "Mory Kanté"),
        ("Pata Pata", "Miriam Makeba"),
        ("Soul Makossa", "Manu Dibango"),
        ("Jerusalema", "Master KG"),
        ("Water", "Tyla"),
        ("Mbube", "Solomon Linda"),
        ("Malaika", "Fadhili William"),
    ],
}


# ============================================================================
# HELPERS
# ============================================================================

def _normalize_categories(categories: Optional[list[str]]) -> list[str]:
    """Filtra y valida la lista de categorías solicitadas."""
    if not categories:
        return list(SEEDS_BY_CATEGORY.keys())
    valid = set(SEEDS_BY_CATEGORY.keys())
    requested = [c.strip().lower() for c in categories if c.strip()]
    unknown = [c for c in requested if c not in valid]
    if unknown:
        print(f"⚠️  Categorías desconocidas: {unknown}")
        print(f"    Válidas: {sorted(valid)}")
    return [c for c in requested if c in valid]


def get_seeds(categories: Optional[list[str]] = None) -> list[tuple[str, str]]:
    """Devuelve todas las semillas de las categorías indicadas (o todas)."""
    cats = _normalize_categories(categories)
    out: list[tuple[str, str]] = []
    for c in cats:
        out.extend(SEEDS_BY_CATEGORY[c])
    return out


def enqueue(
    categories: Optional[list[str]] = None,
    priority: float = 10.0,
    source: str = "seed",
    db_path: str = db.DEFAULT_DB_PATH,
    dry_run: bool = False,
) -> int:
    """
    Encola las semillas en la cola del crawler.

    Idempotente: si una semilla ya está pendiente o en proceso,
    el índice UNIQUE de `dedupe_key` en `queue` evita duplicarla.
    Devuelve cuántas se encolaron nuevas.
    """
    seeds = get_seeds(categories)

    if dry_run:
        for title, artist in seeds:
            print(f"  [DRY-RUN] {title} | {artist}")
        return len(seeds)

    enqueued = 0
    for title, artist in seeds:
        dedupe = f"song:{db._normalize(artist)}|{db._normalize(title)}"
        qid = db.enqueue(
            item_type="song",
            payload={"title": title, "artist": artist},
            priority=priority,
            source=source,
            depth=0,
            dedupe_key=dedupe,
            db_path=db_path,
        )
        if qid is not None:
            enqueued += 1

    return enqueued


# ============================================================================
# CLI
# ============================================================================

def _cmd_list(args):
    cats = _normalize_categories(args.categories)
    total = 0
    for cat in cats:
        seeds = SEEDS_BY_CATEGORY[cat]
        print(f"\n▶ {cat.upper()} ({len(seeds)})")
        print("  " + "─" * 70)
        for title, artist in seeds:
            print(f"    {title:<45} — {artist}")
        total += len(seeds)
    print(f"\nTotal: {total} semillas en {len(cats)} categorías")


def _cmd_stats(args):
    print(f"\n{'Categoría':<18} {'Semillas':>10}")
    print("─" * 30)
    total = 0
    for cat, seeds in SEEDS_BY_CATEGORY.items():
        print(f"{cat:<18} {len(seeds):>10}")
        total += len(seeds)
    print("─" * 30)
    print(f"{'TOTAL':<18} {total:>10}")


def _cmd_enqueue(args):
    db.create_database(db_path=args.db)
    before = db.queue_stats(db_path=args.db)

    enqueued = enqueue(
        categories=args.categories,
        priority=args.priority,
        source=args.source,
        db_path=args.db,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"\n[DRY-RUN] Se encolarían {enqueued} semillas.")
        print("Quita --dry-run para hacerlo de verdad.")
        return

    after = db.queue_stats(db_path=args.db)
    print(f"\nSemillas solicitadas : {enqueued}")
    print(f"Cola antes           : {before}")
    print(f"Cola después         : {after}")
    print("\nAhora puedes lanzar: python crawler.py --workers 1")


def main():
    parser = argparse.ArgumentParser(description="Semillas del crawler musical")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH,
                        help="Ruta de la base SQLite")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Lista las semillas por categoría")
    p_list.add_argument("--categories", nargs="*", default=None,
                        help="Filtrar por categorías (default: todas)")
    p_list.set_defaults(func=_cmd_list)

    p_stats = sub.add_parser("stats", help="Conteo por categoría")
    p_stats.set_defaults(func=_cmd_stats)

    p_enq = sub.add_parser("enqueue", help="Encolar semillas en la cola")
    p_enq.add_argument("--categories", nargs="*", default=None,
                       help="Filtrar por categorías (default: todas)")
    p_enq.add_argument("--priority", type=float, default=10.0,
                       help="Prioridad de las semillas (default: 10.0)")
    p_enq.add_argument("--source", default="seed",
                       help="Etiqueta de origen (default: 'seed')")
    p_enq.add_argument("--dry-run", action="store_true",
                       help="Solo muestra qué se encolaría, sin tocar la DB")
    p_enq.set_defaults(func=_cmd_enqueue)

    args = parser.parse_args()

    try:
        args.func(args)
    finally:
        db.close_connection()


if __name__ == "__main__":
    main()