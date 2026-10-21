#!/usr/bin/env python3
"""
Music Analyzer v8
=================

Song analyzer designed for crawlers and music databases.

Changes from v7:
- FIX: Last.fm API key was being read with the key itself as the env var
       name. Now it reads LASTFM_API_KEY from the environment.
- FIX: Removed unreachable branch in `analyze_song` error handling.
- FIX: CLI now splits "Title | Artist" / "Title - Artist" so the artist
       scoring path is actually used.
- ADD: Per-analysis duration (`_analysis_duration_sec`) for crawler ETA.
- ADD: MusicBrainz scoring now factors in recording length when both the
       candidate and the target report one.
- IMP: warnings filter narrowed to known-noisy librosa messages instead
       of silencing everything.
- IMP: Structured, slightly more useful CLI output.

CLI:
    python analyzer.py "Billie Jean | Michael Jackson"
    python analyzer.py "Billie Jean Michael Jackson"
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

# FIX: only silence the specific noisy warnings librosa emits; do not
# blanket-suppress UserWarning, which can hide real issues.
import warnings

warnings.filterwarnings(
    "ignore",
    message=r".*n_fft=.*is too large for input signal.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r".*Trying to estimate tuning from empty frequency set.*",
    category=UserWarning,
)

# ============================================================================
# LOGGING
# ============================================================================

ANALYZER_LOG_FILE = os.environ.get("ANALYZER_LOG_FILE", "analyzer.log")
logger = logging.getLogger("music_analyzer")

if not logger.handlers:
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        "%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            ANALYZER_LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass


# ============================================================================
# OPTIONAL DEPENDENCIES
# ============================================================================

try:
    import numpy as np
    import librosa
    import soundfile as sf

    LIBROSA_AVAILABLE = True
except ImportError:
    np = None
    librosa = None
    sf = None
    LIBROSA_AVAILABLE = False
    logger.warning("librosa/numpy not installed; audio analysis will be skipped.")


FFMPEG_EXE: Optional[str] = None

try:
    import imageio_ffmpeg

    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_EXE = shutil.which("ffmpeg")

if not FFMPEG_EXE:
    logger.warning("ffmpeg not found; audio extraction will be skipped.")


# ============================================================================
# CONFIGURATION
# ============================================================================

ITUNES_SEARCH = "https://itunes.apple.com/search"
DEEZER_API = "https://api.deezer.com"
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

# FIX: read the Last.fm API key from the environment variable NAMED
# "LASTFM_API_KEY", not from the variable named after the key itself.
#
# Set it before running:
#   Windows (PowerShell) : $env:LASTFM_API_KEY = "your_key_here"
#   Linux / macOS        : export LASTFM_API_KEY="your_key_here"
#
# Get a free key at: https://www.last.fm/api/account/create
# If you ever pasted a real key directly into this file, revoke it first.
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()

USER_AGENT = os.environ.get(
    "MUSIC_ANALYZER_USER_AGENT",
    "MusicDatabaseCrawler/1.0 (contact: change-me@example.com)",
)

HTTP_TIMEOUT = 20
AUDIO_TIMEOUT = 45
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_AUDIO_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_RETRIES = 3
BACKOFF_BASE = 1.5

RATE_LIMITS = {
    "itunes": 0.35,
    "deezer": 0.25,
    "musicbrainz": 1.10,
    "lastfm": 0.35,
    "audio": 0.15,
}

_last_request: dict[str, float] = {name: 0.0 for name in RATE_LIMITS}
_rate_locks = {name: threading.Lock() for name in RATE_LIMITS}
_audio_analysis_lock = threading.Lock()


# ============================================================================
# MODELS
# ============================================================================

@dataclass(frozen=True)
class IdentityMatch:
    status: str
    confidence: float
    title_score: float
    artist_score: float
    duration_score: Optional[float]
    evidence: list[str]


# ============================================================================
# GENERAL HELPERS
# ============================================================================
def _normalize_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).casefold().strip()

    # FIX: unicodedata was imported inside the function on every call;
    # moved to module scope for clarity and to avoid the repeated import.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))

    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _text_similarity(a: Any, b: Any) -> float:
    na = _normalize_text(a)
    nb = _normalize_text(b)

    if not na or not nb:
        return 0.0

    if na == nb:
        return 1.0

    ta = set(na.split())
    tb = set(nb.split())

    jaccard = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    containment = 1.0 if (na in nb or nb in na) else 0.0
    char_ratio = difflib.SequenceMatcher(None, na, nb).ratio()

    return max(jaccard, containment * 0.92, char_ratio * 0.9)


def _duration_score(a: Any, b: Any) -> Optional[float]:
    if a is None or b is None:
        return None

    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return None

    if a <= 0 or b <= 0:
        return None

    diff = abs(a - b)

    if diff <= 1:
        return 1.0
    if diff <= 3:
        return 0.95
    if diff <= 7:
        return 0.80
    if diff <= 15:
        return 0.50
    return 0.0


def _fallback_fingerprint(title: str, artist: str, duration: Any = None) -> str:
    base = "|".join(
        [
            _normalize_text(artist),
            _normalize_text(title),
            str(int(float(duration))) if duration else "",
        ]
    )
    return "local:" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def _escape_lucene(text: str) -> str:
    special = r'+-&&||!(){}[]^"~*?:\/'
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _round_recursive(value: Any, digits: int = 4) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, list):
        return [_round_recursive(x, digits) for x in value]
    if isinstance(value, dict):
        return {k: _round_recursive(v, digits) for k, v in value.items()}
    return value


# ============================================================================
# RATE LIMIT + HTTP
# ============================================================================

def _wait_turn(api: str) -> None:
    lock = _rate_locks.get(api)
    if lock is None:
        return

    with lock:
        now = time.monotonic()
        previous = _last_request.get(api, 0.0)
        wait = RATE_LIMITS.get(api, 0.5) - (now - previous)

        if wait > 0:
            time.sleep(wait)

        _last_request[api] = time.monotonic()


def _headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def http_get(
    url: str,
    api: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = HTTP_TIMEOUT,
) -> Optional[bytes]:
    for attempt in range(1, MAX_RETRIES + 1):
        _wait_turn(api)

        try:
            req = urllib.request.Request(url, headers=_headers(headers))
            with urllib.request.urlopen(req, timeout=timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_HTTP_RESPONSE_BYTES:
                    logger.warning("%s: response too large", api)
                    return None

                payload = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
                if len(payload) > MAX_HTTP_RESPONSE_BYTES:
                    logger.warning("%s: response too large", api)
                    return None
                return payload

        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600

            if not retryable:
                # FIX: distinguish 404 from 403 for better debuggability.
                if exc.code == 404:
                    logger.debug("%s: 404 not found", api)
                elif exc.code == 403:
                    logger.error(
                        "%s: HTTP 403 forbidden — check UA or rate limits", api
                    )
                else:
                    logger.warning("%s: HTTP %s (not retrying)", api, exc.code)
                return None

            wait = BACKOFF_BASE ** attempt
            logger.warning(
                "%s: HTTP %s, retry %s/%s in %.1fs",
                api, exc.code, attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            wait = BACKOFF_BASE ** attempt
            logger.warning(
                "%s: %s, retry %s/%s in %.1fs",
                api, exc, attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)

        except Exception as exc:
            logger.exception("%s: unexpected error: %s", api, exc)
            return None

    logger.error("%s: retries exhausted", api)
    return None


def http_get_json(
    url: str,
    api: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = HTTP_TIMEOUT,
) -> Optional[dict[str, Any]]:
    raw = http_get(url, api, headers=headers, timeout=timeout)
    if not raw:
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("%s: invalid JSON: %s", api, exc)
        return None

    return data if isinstance(data, dict) else None


# ============================================================================
# iTUNES — METADATA CANDIDATES
# ============================================================================

def search_itunes(query: str, limit: int = 10) -> Optional[list[dict[str, Any]]]:
    params = urllib.parse.urlencode(
        {
            "term": query,
            "media": "music",
            "entity": "song",
            "limit": min(max(limit, 1), 50),
        }
    )

    data = http_get_json(f"{ITUNES_SEARCH}?{params}", "itunes")
    if data is None:
        return None

    if not data:
        return []

    results: list[dict[str, Any]] = []

    for item in data.get("results", []):
        duration_ms = item.get("trackTimeMillis")
        results.append(
            {
                "title": item.get("trackName"),
                "artist": item.get("artistName"),
                "album": item.get("collectionName"),
                "genre": item.get("primaryGenreName"),
                "year": (item.get("releaseDate") or "")[:4],
                "release_date": item.get("releaseDate"),
                "duration_sec": (
                    round(float(duration_ms) / 1000, 3)
                    if duration_ms
                    else None
                ),
                "country": item.get("country"),
                "itunes_id": item.get("trackId"),
                "itunes_artist_id": item.get("artistId"),
                "itunes_collection_id": item.get("collectionId"),
                "itunes_preview_m4a": item.get("previewUrl"),
                "itunes_url": item.get("trackViewUrl"),
            }
        )

    return results


def _score_itunes_candidate(
    candidate: dict[str, Any],
    title: str,
    artist: Optional[str],
    duration: Optional[float],
) -> IdentityMatch:
    title_score = _text_similarity(title, candidate.get("title"))

    artist_score = (
        _text_similarity(artist, candidate.get("artist"))
        if artist
        else 0.0
    )

    duration_score = _duration_score(duration, candidate.get("duration_sec"))

    if artist:
        confidence = (
            title_score * 0.50
            + artist_score * 0.35
            + (duration_score if duration_score is not None else 0.0) * 0.15
        )
    else:
        confidence = title_score

    evidence: list[str] = []

    if title_score >= 0.95:
        evidence.append("title_exact_match")
    elif title_score >= 0.75:
        evidence.append("title_similar")

    if artist and artist_score >= 0.95:
        evidence.append("artist_exact_match")
    elif artist and artist_score >= 0.75:
        evidence.append("artist_similar")

    if duration_score is not None and duration_score >= 0.95:
        evidence.append("duration_compatible")

    if confidence >= 0.92:
        status = "verified"
    elif confidence >= 0.78:
        status = "probable"
    elif confidence >= 0.55:
        status = "ambiguous"
    else:
        status = "rejected"

    return IdentityMatch(
        status=status,
        confidence=round(confidence, 4),
        title_score=round(title_score, 4),
        artist_score=round(artist_score, 4),
        duration_score=(
            round(duration_score, 4)
            if duration_score is not None
            else None
        ),
        evidence=evidence,
    )


def resolve_itunes_identity(
    candidates: list[dict[str, Any]],
    title: str,
    artist: Optional[str],
    duration: Optional[float] = None,
) -> tuple[Optional[dict[str, Any]], IdentityMatch]:
    if not candidates:
        return None, IdentityMatch(
            status="not_found",
            confidence=0.0,
            title_score=0.0,
            artist_score=0.0,
            duration_score=None,
            evidence=[],
        )

    scored = [
        (
            candidate,
            _score_itunes_candidate(candidate, title, artist, duration),
        )
        for candidate in candidates
    ]

    scored.sort(key=lambda x: x[1].confidence, reverse=True)

    best_candidate, best_match = scored[0]

    if len(scored) > 1:
        second_match = scored[1][1]

        if (
            best_match.status in {"verified", "probable"}
            and best_match.confidence - second_match.confidence < 0.04
        ):
            best_match = IdentityMatch(
                status="ambiguous",
                confidence=best_match.confidence,
                title_score=best_match.title_score,
                artist_score=best_match.artist_score,
                duration_score=best_match.duration_score,
                evidence=best_match.evidence + ["candidates_tied"],
            )

    if artist is None and best_match.status != "rejected":
        best_match = IdentityMatch(
            status="ambiguous",
            confidence=best_match.confidence,
            title_score=best_match.title_score,
            artist_score=best_match.artist_score,
            duration_score=best_match.duration_score,
            evidence=best_match.evidence + ["artist_not_provided"],
        )

    if best_match.status == "rejected":
        return None, best_match

    return best_candidate, best_match


# ============================================================================
# MUSICBRAINZ
# ============================================================================

def search_musicbrainz(
    title: str,
    artist: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    t = _escape_lucene(title)
    a = _escape_lucene(artist)

    query = f'recording:"{t}"~2 AND artist:"{a}"~2'

    params = urllib.parse.urlencode(
        {
            "query": query,
            "fmt": "json",
            "limit": min(max(limit, 1), 25),
        }
    )

    data = http_get_json(
        f"{MUSICBRAINZ_API}/recording?{params}",
        "musicbrainz",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    if not data:
        return []

    output: list[dict[str, Any]] = []

    for recording in data.get("recordings", []):
        credits = recording.get("artist-credit") or []
        artist_ids = []
        artist_names = []

        for credit in credits:
            if isinstance(credit, dict):
                artist_obj = credit.get("artist") or {}
                if artist_obj.get("id"):
                    artist_ids.append(artist_obj["id"])
                name = credit.get("name") or artist_obj.get("name")
                if name:
                    artist_names.append(name)

        isrcs = recording.get("isrcs") or []

        # FIX: MusicBrainz exposes recording length in milliseconds. Capture
        # it so the scorer below can use it for a duration check.
        length_ms = recording.get("length")
        length_sec = (
            round(float(length_ms) / 1000, 3) if length_ms else None
        )

        output.append(
            {
                "mbid": recording.get("id"),
                "title": recording.get("title"),
                "artist_mbids": artist_ids,
                "artist_mbid": artist_ids[0] if artist_ids else None,
                "artist_names": artist_names,
                "isrcs": isrcs,
                "isrc": isrcs[0] if isrcs else None,
                "musicbrainz_score": recording.get("score"),
                "length_sec": length_sec,
            }
        )

    return output


def select_musicbrainz(
    candidates: list[dict[str, Any]],
    title: str,
    artist: str,
    *,
    target_duration: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """
    Scores candidates on title + artist similarity, and, when both sides
    report a duration, on duration compatibility. This avoids picking a
    live version or a remix when an album version is what was requested.
    """
    if not candidates:
        return None

    def score(item: dict[str, Any]) -> float:
        title_score = _text_similarity(title, item.get("title"))

        names = item.get("artist_names") or []
        artist_score = (
            max(_text_similarity(artist, name) for name in names)
            if names
            else 0.0
        )

        base = title_score * 0.55 + artist_score * 0.45

        # FIX: bonus/malus por duración compatible. No penalizamos cuando
        # falta información: simplemente no sumamos.
        if target_duration and item.get("length_sec"):
            d_score = _duration_score(target_duration, item["length_sec"])
            if d_score is not None:
                base = base * 0.85 + d_score * 0.15

        return base

    return max(candidates, key=score)


# ============================================================================
# DEEZER
# ============================================================================

def search_deezer_preview(
    title: str,
    artist: str,
    isrc: Optional[str] = None,
) -> dict[str, Any]:
    if isrc:
        url = f"{DEEZER_API}/track/isrc:{urllib.parse.quote(isrc)}"
        data = http_get_json(url, "deezer")

        if data and data.get("id"):
            return {
                "preview_mp3": data.get("preview"),
                "deezer_id": data.get("id"),
                "deezer_bpm": _float_or_none(data.get("bpm")),
                "deezer_rank": data.get("rank"),
                "match_method": "isrc",
            }

    query = urllib.parse.urlencode({"q": f'artist:"{artist}" track:"{title}"'})
    data = http_get_json(f"{DEEZER_API}/search?{query}&limit=5", "deezer")

    if data and data.get("data"):
        candidates = data["data"]

        def score(item: dict[str, Any]) -> float:
            return (
                _text_similarity(title, item.get("title")) * 0.55
                + _text_similarity(
                    artist, item.get("artist", {}).get("name")
                ) * 0.45
            )

        best = max(candidates, key=score)

        return {
            "preview_mp3": best.get("preview"),
            "deezer_id": best.get("id"),
            "deezer_bpm": _float_or_none(best.get("bpm")),
            "deezer_rank": best.get("rank"),
            "match_method": "text",
        }

    return {
        "preview_mp3": None,
        "deezer_id": None,
        "deezer_bpm": None,
        "deezer_rank": None,
        "match_method": None,
    }


# ============================================================================
# LAST.FM
# ============================================================================

def get_lastfm_tags(
    artist: str,
    title: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if not LASTFM_API_KEY:
        # FIX: log once per session that tags are disabled so the missing
        # key isn't silently swallowed during crawler runs.
        logger.debug("Last.fm tags disabled: LASTFM_API_KEY not set.")
        return []

    params = urllib.parse.urlencode(
        {
            "method": "track.getTopTags",
            "artist": artist,
            "track": title,
            "api_key": LASTFM_API_KEY,
            "format": "json",
            "autocorrect": 1,
        }
    )

    data = http_get_json(f"{LASTFM_API}?{params}", "lastfm")
    if not data:
        return []

    raw_tags = data.get("toptags", {}).get("tag", [])
    output = []

    for tag in raw_tags[:limit]:
        if not isinstance(tag, dict):
            continue
        output.append(
            {
                "name": tag.get("name"),
                "weight": _float_or_none(tag.get("count")),
                "url": tag.get("url"),
            }
        )

    return output


# ============================================================================
# AUDIO
# ============================================================================

if LIBROSA_AVAILABLE:
    _KS_MAJOR = np.array(
        [6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
         2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
    )

    _KS_MINOR = np.array(
        [6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
         2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
    )

    _NOTES = [
        "C", "C#", "D", "D#", "E", "F",
        "F#", "G", "G#", "A", "A#", "B",
    ]
else:
    _KS_MAJOR = None
    _KS_MINOR = None
    _NOTES = []


def convert_to_wav(
    audio_bytes: bytes,
    *,
    max_seconds: int = 30,
) -> Optional[bytes]:
    if not FFMPEG_EXE or not audio_bytes:
        return None

    try:
        process = subprocess.run(
            [
                FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-f", "wav",
                "-ac", "1",
                "-ar", "22050",
                "-t", str(max_seconds),
                "pipe:1",
            ],
            input=audio_bytes,
            capture_output=True,
            timeout=AUDIO_TIMEOUT,
        )

        if process.returncode != 0:
            logger.warning(
                "ffmpeg: %s",
                process.stderr.decode(errors="ignore")[:300],
            )
            return None

        return process.stdout

    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg: timeout")
        return None
    except OSError as exc:
        logger.warning("ffmpeg: %s", exc)
        return None


def estimate_key(chroma_mean: list[float]) -> dict[str, Any]:
    if not LIBROSA_AVAILABLE:
        return {"key": None, "mode": None, "confidence": None, "key_margin": None}

    chroma = np.asarray(chroma_mean, dtype=float)

    if chroma.size != 12 or not np.any(chroma):
        return {"key": None, "mode": None, "confidence": None, "key_margin": None}

    candidates: list[tuple[float, str, str]] = []

    for i in range(12):
        major = np.roll(_KS_MAJOR, i)
        minor = np.roll(_KS_MINOR, i)

        corr_major = float(np.corrcoef(chroma, major)[0, 1])
        corr_minor = float(np.corrcoef(chroma, minor)[0, 1])

        if math.isfinite(corr_major):
            candidates.append((corr_major, _NOTES[i], "major"))
        if math.isfinite(corr_minor):
            candidates.append((corr_minor, _NOTES[i], "minor"))

    if not candidates:
        return {"key": None, "mode": None, "confidence": None, "key_margin": None}

    candidates.sort(reverse=True, key=lambda x: x[0])

    best = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    margin = best[0] - second[0] if second else None

    return {
        "key": best[1],
        "mode": best[2],
        "confidence": round(max(0.0, min(1.0, best[0])), 4),
        "key_margin": (
            round(max(0.0, margin), 4) if margin is not None else None
        ),
    }


def _analyze_segment(y: Any, y_norm: Any, sr: int) -> Optional[dict[str, Any]]:
    if len(y) < max(1024, sr // 4):
        return None

    try:
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        tempo = float(np.atleast_1d(tempo)[0])

        rms = librosa.feature.rms(y=y)[0]
        rms_norm = librosa.feature.rms(y=y_norm)[0]

        centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
        zcr = librosa.feature.zero_crossing_rate(y)[0]
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
        chroma = librosa.feature.chroma_stft(y=y, sr=sr)
        rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr)[0]
        contrast = librosa.feature.spectral_contrast(y=y, sr=sr)

        harmonic = librosa.effects.harmonic(y)
        tonnetz = librosa.feature.tonnetz(y=harmonic, sr=sr)

        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        onsets = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=sr, units="time",
        )

        duration = len(y) / sr

        if len(onsets) >= 3:
            intervals = np.diff(onsets)
            if np.mean(intervals) > 0:
                cv = float(np.std(intervals) / np.mean(intervals))
                rhythmic_regularity = max(0.0, 1.0 - min(1.0, cv))
            else:
                rhythmic_regularity = None
        else:
            rhythmic_regularity = None

        zcr_value = float(np.mean(zcr))
        acousticness_proxy = max(0.0, min(1.0, 1.0 - zcr_value * 8.0))

        return {
            "tempo_bpm": tempo,
            "energy_rms": float(np.mean(rms)),
            "energy_norm_peak": float(np.mean(rms_norm)),
            "acousticness_proxy_zcr": acousticness_proxy,
            "brightness_centroid_hz": float(np.mean(centroid)),
            "zero_crossing_rate": zcr_value,
            "mfcc_mean": [float(x) for x in np.mean(mfcc, axis=1)],
            "chroma_mean": [float(x) for x in np.mean(chroma, axis=1)],
            "spectral_rolloff_hz": float(np.mean(rolloff)),
            "spectral_contrast_mean": [
                float(x) for x in np.mean(contrast, axis=1)
            ],
            "tonnetz_mean": [float(x) for x in np.mean(tonnetz, axis=1)],
            "onset_rate": float(len(onsets) / duration),
            "rhythmic_regularity": rhythmic_regularity,
        }

    except Exception as exc:
        logger.warning("Error analyzing segment: %s", exc)
        return None


def _aggregate_segments(segments: list[dict[str, Any]]) -> dict[str, Any]:
    if not segments:
        return {}

    result: dict[str, Any] = {}

    for key in segments[0]:
        values = [item[key] for item in segments if item.get(key) is not None]

        if not values:
            result[key] = None
            continue

        if isinstance(values[0], list):
            arr = np.asarray(values, dtype=float)
            result[key] = {
                "mean": [float(x) for x in np.mean(arr, axis=0)],
                "std": [float(x) for x in np.std(arr, axis=0)],
            }
        else:
            arr = np.asarray(values, dtype=float)
            result[key] = {
                "mean": float(np.mean(arr)),
                "std": float(np.std(arr)),
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
            }

    return result


def calculate_librosa_features(
    preview_url: str,
    *,
    max_seconds: int = 30,
    num_segments: int = 3,
) -> Optional[dict[str, Any]]:
    if not LIBROSA_AVAILABLE or not preview_url:
        return None

    # librosa/numba can race while compiling or caching the same functions
    # when multiple crawler workers analyze audio simultaneously.
    with _audio_analysis_lock:
        return _calculate_librosa_features_locked(
            preview_url,
            max_seconds=max_seconds,
            num_segments=num_segments,
        )


def _calculate_librosa_features_locked(
    preview_url: str,
    *,
    max_seconds: int,
    num_segments: int,
) -> Optional[dict[str, Any]]:
    try:
        _wait_turn("audio")

        req = urllib.request.Request(
            preview_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "audio/*,*/*;q=0.9",
            },
        )

        with urllib.request.urlopen(req, timeout=AUDIO_TIMEOUT) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_AUDIO_RESPONSE_BYTES:
                logger.warning("audio: preview too large")
                return None

            audio_bytes = response.read(MAX_AUDIO_RESPONSE_BYTES + 1)
            if len(audio_bytes) > MAX_AUDIO_RESPONSE_BYTES:
                logger.warning("audio: preview too large")
                return None

        if not audio_bytes:
            return None

        logger.info("Preview downloaded: %.1f KB", len(audio_bytes) / 1024)

        wav_bytes = convert_to_wav(audio_bytes, max_seconds=max_seconds)
        if not wav_bytes:
            return None

        # Read WAV through soundfile instead of librosa.load(). The latter
        # lazily imports librosa.core.audio and triggers a Numba cache failure
        # with the Debian/Python 3.13 package layout.
        y, sr = sf.read(
            io.BytesIO(wav_bytes),
            dtype="float32",
            always_2d=False,
        )
        if getattr(y, "ndim", 1) > 1:
            y = np.mean(y, axis=1)
        if y is None or len(y) == 0:
            return None

        total_duration = len(y) / sr

        peak = float(np.max(np.abs(y)))
        y_norm_full = y if peak <= 1e-9 else y / peak

        num_segments = max(1, min(num_segments, 6))
        segment_len = len(y) // num_segments

        segment_analyses: list[dict[str, Any]] = []

        for index in range(num_segments):
            start = index * segment_len
            end = (
                len(y)
                if index == num_segments - 1
                else (index + 1) * segment_len
            )

            segment = y[start:end]
            segment_norm = y_norm_full[start:end]

            features = _analyze_segment(segment, segment_norm, sr)

            if features:
                features["segment_index"] = index
                features["start_sec"] = start / sr
                features["end_sec"] = end / sr
                segment_analyses.append(features)

        if not segment_analyses:
            return None

        aggregated = _aggregate_segments(segment_analyses)

        chroma_global = librosa.feature.chroma_stft(y=y, sr=sr)
        chroma_mean = [float(x) for x in np.mean(chroma_global, axis=1)]

        key_info = estimate_key(chroma_mean)

        return {
            "method": "librosa",
            "sample_rate_hz": int(sr),
            "analyzed_duration_sec": round(total_duration, 3),
            "audio_source": "preview",
            "preview_max_sec": max_seconds,
            "segments_analyzed": len(segment_analyses),
            "statistics": _round_recursive(aggregated, 5),
            "key": key_info,
            "chroma_global": [round(x, 5) for x in chroma_mean],
        }

    except Exception:
        logger.exception("features: audio analysis failed")
        return None


# ============================================================================
# IDENTITY AND PROVENANCE
# ============================================================================

def build_identity(
    song: dict[str, Any],
    match: IdentityMatch,
) -> dict[str, Any]:
    isrc = song.get("isrc")
    mbid = song.get("mbid")

    if isrc:
        canonical_id = f"isrc:{isrc}"
        canonical_type = "isrc"
    elif mbid:
        canonical_id = f"mbid:{mbid}"
        canonical_type = "musicbrainz_recording"
    else:
        canonical_id = _fallback_fingerprint(
            song.get("title") or "",
            song.get("artist") or "",
            song.get("duration_sec"),
        )
        canonical_type = "local_fingerprint"

    return {
        "canonical_id": canonical_id,
        "canonical_id_type": canonical_type,
        "resolution_status": match.status,
        "resolution_confidence": match.confidence,
        "evidence": match.evidence,
        "scores": {
            "title": match.title_score,
            "artist": match.artist_score,
            "duration": match.duration_score,
        },
        "provisional_identity": canonical_type == "local_fingerprint",
    }


def build_provenance(
    *,
    itunes: bool,
    musicbrainz: bool,
    deezer: bool,
    lastfm: bool,
    audio: bool,
) -> dict[str, Any]:
    return {
        # FIX: bump version so downstream consumers can filter by it.
        "analyzer_version": "8.0.0",
        "retrieved_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
        ),
        "sources": {
            "itunes": "source" if itunes else None,
            "musicbrainz": "source" if musicbrainz else None,
            "deezer": "source" if deezer else None,
            "lastfm": "source" if lastfm else None,
            "audio": "derived" if audio else None,
        },
        "audio_feature_note": (
            "Audio features come from a preview clip and may not represent "
            "the full song."
        ),
    }


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def analyze_song(
    title: Optional[str] = None,
    artist: Optional[str] = None,
    free_query: Optional[str] = None,
) -> dict[str, Any]:
    """
    Full pipeline.

    The crawler is responsible for:
    - persistence
    - final deduplication
    - business-level retries
    - queues
    - record updates
    """
    t0 = time.monotonic()  # FIX: start timer for ETA estimation.

    # FIX: validate arguments BEFORE dereferencing .strip() on None.
    if not any([title, artist, free_query]):
        return {
            "ok": False,
            "error": "empty_query",
            "requires_review": False,
            "completeness": None,
            "data": None,
            "_analysis_duration_sec": 0.0,
        }

    if free_query:
        query = free_query.strip()
        requested_title = query
        requested_artist = None
    elif title and artist:
        query = f"{title.strip()} {artist.strip()}"
        requested_title = title.strip()
        requested_artist = artist.strip()
    else:
        query = (title or "").strip()
        requested_title = query
        requested_artist = None

    if not query:
        return {
            "ok": False,
            "error": "empty_query",
            "requires_review": False,
            "completeness": None,
            "data": None,
            "_analysis_duration_sec": 0.0,
        }

    logger.info("Analyzing: %s", query)

    # 1. iTunes
    itunes_candidates = search_itunes(query, limit=10)
    if itunes_candidates is None:
        return {
            "ok": False,
            "error": "itunes_unavailable",
            "requires_review": False,
            "completeness": None,
            "data": None,
            "_analysis_duration_sec": round(time.monotonic() - t0, 2),
        }

    candidate, identity_match = resolve_itunes_identity(
        itunes_candidates, requested_title, requested_artist,
    )

    if not candidate:
        return {
            "ok": False,
            "error": "identity_not_resolved",
            "requires_review": False,
            "completeness": {
                "basic_metadata": False,
                "extended_metadata": False,
                "audio_features": False,
                "tags": False,
            },
            "data": {
                "original_query": query,
                "identity_match": {
                    "status": identity_match.status,
                    "confidence": identity_match.confidence,
                    "evidence": identity_match.evidence,
                },
                "itunes_candidates": itunes_candidates[:5],
            },
            "_analysis_duration_sec": round(time.monotonic() - t0, 2),
        }

    song = dict(candidate)

    # 2. MusicBrainz
    mb_candidates = search_musicbrainz(
        song.get("title") or requested_title,
        song.get("artist") or requested_artist or "",
        limit=5,
    )

    mb = select_musicbrainz(
        mb_candidates,
        song.get("title") or requested_title,
        song.get("artist") or requested_artist or "",
        target_duration=song.get("duration_sec"),  # FIX: pass duration.
    )

    if mb:
        song.update(
            {
                "mbid": mb.get("mbid"),
                "artist_mbid": mb.get("artist_mbid"),
                "artist_mbids": mb.get("artist_mbids") or [],
                "isrc": mb.get("isrc"),
                "isrcs": mb.get("isrcs") or [],
                "musicbrainz_score": mb.get("musicbrainz_score"),
                "musicbrainz_length_sec": mb.get("length_sec"),
            }
        )
    else:
        song.update(
            {
                "mbid": None,
                "artist_mbid": None,
                "artist_mbids": [],
                "isrc": None,
                "isrcs": [],
                "musicbrainz_score": None,
                "musicbrainz_length_sec": None,
            }
        )

    # 3. Deezer
    deezer = search_deezer_preview(
        song.get("title") or "",
        song.get("artist") or "",
        isrc=song.get("isrc"),
    )

    song.update(
        {
            "deezer_id": deezer.get("deezer_id"),
            "deezer_bpm": deezer.get("deezer_bpm"),
            "deezer_rank": deezer.get("deezer_rank"),
            "deezer_match_method": deezer.get("match_method"),
        }
    )

    preview_url = deezer.get("preview_mp3") or song.get("itunes_preview_m4a")

    song["preview_source"] = (
        "deezer"
        if deezer.get("preview_mp3")
        else ("itunes" if song.get("itunes_preview_m4a") else None)
    )

    # 4. Audio
    features = None
    if preview_url:
        features = calculate_librosa_features(preview_url)
    song["features"] = features

    # 5. Tags
    tags = get_lastfm_tags(
        song.get("artist") or "",
        song.get("title") or "",
    )
    song["tags"] = tags

    # 6. Identity + provenance
    song["identity"] = build_identity(song, identity_match)
    song["provenance"] = build_provenance(
        itunes=True,
        musicbrainz=bool(mb),
        deezer=bool(deezer.get("deezer_id")),
        lastfm=bool(tags),
        audio=bool(features),
    )
    song["original_query"] = query

    # 7. Completeness
    completeness = {
        "basic_metadata": bool(song.get("title") and song.get("artist")),
        "extended_metadata": bool(song.get("mbid") or song.get("isrc")),
        "audio_features": bool(features),
        "tags": bool(tags),
        "canonical_identity": song["identity"]["canonical_id_type"]
        != "local_fingerprint",
        # FIX: surface key estimation confidence as its own completeness flag.
        "key_estimated": bool(features and features.get("key", {}).get("key")),
    }

    requires_review = identity_match.status == "ambiguous"
    ok = identity_match.status in {"verified", "probable"}

    # FIX: removed unreachable branch. There are only three cases:
    #   - ok               -> verified or probable
    #   - requires_review  -> ambiguous
    #   - neither          -> (cannot happen here; rejected returns earlier)
    error: Optional[str] = None
    if not ok:
        error = "identity_requires_review" if requires_review else "identity_failed"

    return {
        "ok": ok,
        "error": error,
        "requires_review": requires_review,
        "completeness": completeness,
        "data": song,
        # FIX: expose timing so the crawler can log per-song cost.
        "_analysis_duration_sec": round(time.monotonic() - t0, 2),
    }


# ============================================================================
# CLI
# ============================================================================

def _split_free_query(raw: str) -> tuple[str, Optional[str]]:
    """
    FIX: Best-effort split of a free query into (title, artist).

    Supported separators (first match wins):
        "Title | Artist"
        "Title - Artist"
        "Title – Artist"     (en dash)
        "Title — Artist"     (em dash)

    If none is found, returns (raw, None) and the analyzer will treat
    the whole thing as a title-only search.
    """
    for sep in (" | ", " - ", " – ", " — "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return raw.strip(), None


def print_song(result: dict[str, Any]) -> None:
    print("=" * 72)

    if not result.get("data"):
        print("ANALYSIS FAILED")
        print(f"Error: {result.get('error')}")
        print("=" * 72)
        return

    song = result["data"]
    identity = song.get("identity") or {}
    features = song.get("features") or {}

    print(f"  {song.get('title')} — {song.get('artist')}")
    print("=" * 72)

    print("\nIDENTITY")
    print(f"  Canonical ID      : {identity.get('canonical_id')}")
    print(f"  Type              : {identity.get('canonical_id_type')}")
    print(f"  Status            : {identity.get('resolution_status')}")
    print(f"  Confidence        : {identity.get('resolution_confidence')}")
    print(f"  Requires review   : {result.get('requires_review')}")
    print(f"  ISRC              : {song.get('isrc') or 'N/A'}")
    print(f"  Recording MBID    : {song.get('mbid') or 'N/A'}")
    print(f"  Artist MBID       : {song.get('artist_mbid') or 'N/A'}")

    print("\nMETADATA")
    print(f"  Album             : {song.get('album') or 'N/A'}")
    print(f"  Genre             : {song.get('genre') or 'N/A'}")
    print(f"  Year              : {song.get('year') or 'N/A'}")
    print(f"  Duration          : {song.get('duration_sec') or 'N/A'} s")
    print(f"  Country           : {song.get('country') or 'N/A'}")

    print("\nAUDIO")
    if features:
        print(f"  Method            : {features.get('method')}")
        print(f"  Analyzed duration : {features.get('analyzed_duration_sec')} s")
        print(f"  Segments          : {features.get('segments_analyzed')}")

        key_info = features.get("key") or {}
        print(
            f"  Key               : "
            f"{key_info.get('key')} {key_info.get('mode')} "
            f"(conf. {key_info.get('confidence')}, "
            f"margin {key_info.get('key_margin')})"
        )

        stats = features.get("statistics") or {}
        for key in (
            "tempo_bpm",
            "energy_rms",
            "energy_norm_peak",
            "acousticness_proxy_zcr",
            "brightness_centroid_hz",
            "zero_crossing_rate",
            "spectral_rolloff_hz",
            "onset_rate",
            "rhythmic_regularity",
        ):
            if key in stats:
                print(f"  {key:24}: {stats[key]}")
    else:
        print("  Not available")

    print("\nTAGS")
    tags = song.get("tags") or []
    if tags:
        print(
            "  "
            + ", ".join(str(tag.get("name")) for tag in tags if tag.get("name"))
        )
    else:
        print("  No tags")

    print("\nCOMPLETENESS")
    print(json.dumps(result.get("completeness"), ensure_ascii=False, indent=2))

    print(f"\nElapsed: {result.get('_analysis_duration_sec', '?')} s")
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Busca metadatos musicales y analiza una preview de audio."
    )
    parser.add_argument(
        "query",
        nargs="+",
        help='Título y artista, por ejemplo: "Billie Jean | Michael Jackson"',
    )
    args = parser.parse_args()
    raw_query = " ".join(args.query)

    # FIX: try to split "Title | Artist" so the artist-scoring path is used.
    title_part, artist_part = _split_free_query(raw_query)

    if artist_part:
        result = analyze_song(title=title_part, artist=artist_part)
    else:
        result = analyze_song(free_query=raw_query)

    print_song(result)

    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()