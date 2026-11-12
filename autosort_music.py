#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoSort Music — переименование музыкальных файлов по тегам.

По умолчанию ничего не меняет: показывает предпросмотр.
Добавьте --apply, чтобы переименовать файлы.

Примеры:
    python autosort_music.py "D:\\Music"
    python autosort_music.py "D:\\Music" --apply
    python autosort_music.py "D:\\Music" --format "{artist} - {title}"
    python autosort_music.py "D:\\Music" --undo
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Formatter
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from mutagen import File as MutagenFile
except ImportError:  # pragma: no cover
    sys.exit(
        "Не найдена библиотека mutagen.\n"
        "Установите её командой:  pip install mutagen"
    )


AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".m4b", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".aiff", ".aif", ".ape", ".wv", ".dsf",
}

LOG_NAME = "_autosort_log.csv"
USER_AGENT = "AutoSortMusic/1.0 ( https://musicbrainz.org ; local utility )"
MB_URL = "https://musicbrainz.org/ws/2/recording/"
ITUNES_URL = "https://itunes.apple.com/search"

# Слова, которые почти всегда являются мусором в имени файла / теге.
JUNK_WORDS = (
    r"official\s*(music\s*)?video|official\s*audio|official\s*lyric\s*video|"
    r"lyrics?(\s*video)?|visuali[sz]er|audio|video|hq|hd|4k|8k|"
    r"\d{2,3}\s*kbps|320|vbr|explicit|clean|"
    r"free\s*download|download|new\s*song\s*\d{4}|premiere"
)
JUNK_BRACKETS_RE = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]*?(?:" + JUNK_WORDS + r")[^\(\)\[\]\{\}]*?[\)\]\}]", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
TRACK_PREFIX_RE = re.compile(r"^\s*(?:\[\s*)?\d{1,3}(?:\s*[\)\]]|\s*[-._)]\s+|\s+)(?=\S)")
MULTI_SPACE_RE = re.compile(r"\s+")
INVALID_CHARS = '<>:"/\\|?*'
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if os.name == "nt":
        # В старом conhost escape-коды печатаются как мусор.
        return any(os.environ.get(key) for key in
                   ("WT_SESSION", "ANSICON", "ConEmuANSI", "TERM_PROGRAM"))
    return True


USE_COLOR = _use_color()


def color(text: str, code: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


@dataclass
class TrackMeta:
    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    year: str = ""
    track: str = ""
    duration: float | None = None


def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


def _clean_meta(meta: TrackMeta) -> TrackMeta:
    for field in ("title", "artist", "album", "albumartist"):
        value = getattr(meta, field)
        value = JUNK_BRACKETS_RE.sub("", value)
        value = MULTI_SPACE_RE.sub(" ", value).strip(" -–—_.")
        setattr(meta, field, value)
    meta.year = re.sub(r"\D", "", meta.year)[:4]
    meta.track = re.sub(r"^0*(\d+)", r"\1", meta.track.split("/")[0].strip())
    meta.track = meta.track if meta.track.isdigit() else ""
    return meta


def read_meta(path: Path) -> tuple[TrackMeta, bool, bool]:
    """Читает теги. Возвращает (метаданные, были_ли_теги, файл_читается)."""
    meta = TrackMeta()
    audio = None
    for easy in (True, False):
        try:
            audio = MutagenFile(path, easy=easy)
            if audio is not None:
                break
        except Exception:
            audio = None

    if audio is None:
        return meta, False, False

    try:
        if audio.info is not None:
            meta.duration = float(audio.info.length) or None
    except Exception:
        pass

    tags = getattr(audio, "tags", None)
    if not tags:
        return meta, False, True

    def get(*keys: str) -> str:
        for key in keys:
            for candidate in (key, key.lower(), key.upper(), key.title()):
                try:
                    if candidate in tags:
                        return _first(tags[candidate])
                except Exception:
                    continue
        return ""

    meta.title = get("title", "TIT2", "\xa9nam")
    meta.artist = get("artist", "TPE1", "\xa9ART")
    meta.album = get("album", "TALB", "\xa9alb")
    meta.albumartist = get("albumartist", "album artist", "TPE2", "aART")
    meta.year = get("date", "year", "TDRC", "TYER", "\xa9day")
    meta.track = get("tracknumber", "track", "TRCK", "trkn")
    return _clean_meta(meta), bool(meta.title or meta.artist or meta.album), True


def guess_from_filename(stem: str) -> tuple[str, str]:
    """Пытается вытащить «исполнителя» и «название» из имени файла."""
    text = stem.replace("_", " ")
    text = URL_RE.sub(" ", text)
    text = JUNK_BRACKETS_RE.sub(" ", text)
    text = MULTI_SPACE_RE.sub(" ", text).strip(" -–—_.")
    text = TRACK_PREFIX_RE.sub("", text)
    text = MULTI_SPACE_RE.sub(" ", text).strip(" -–—_.")

    parts = re.split(r"\s+[-–—]\s+", text, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    return "", text.strip()


# --------------------------------------------------------------------------
# Онлайн-поиск: MusicBrainz, затем iTunes
# --------------------------------------------------------------------------

LUCENE_SPECIAL_RE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
NORM_BRACKETS_RE = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]*[\)\]\}]")
NORM_PUNCT_RE = re.compile(r"[^\w\s]|_")


def lucene_escape(text: str) -> str:
    return LUCENE_SPECIAL_RE.sub(r"\\\1", text)


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).casefold()
    text = NORM_BRACKETS_RE.sub(" ", text)
    text = NORM_PUNCT_RE.sub(" ", text)
    return MULTI_SPACE_RE.sub(" ", text).strip()


def default_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "autosort_music" / "online_cache.json"


class OnlineLookup:
    """Ищет трек в MusicBrainz, при неудаче — в iTunes Search API."""

    def __init__(self, min_score: int = 85, tolerance: float = 10.0,
                 timeout: float = 15.0, cache_path: Path | None = None):
        self.min_score = min_score
        self.tolerance = tolerance
        self.timeout = timeout
        self.cache_path = cache_path or default_cache_path()
        self.cache: dict[str, list[dict]] = {}
        self._last_request = 0.0
        self.requests = 0
        self.errors = 0
        self.provider = ""
        self.failures = {"MusicBrainz": 0, "iTunes": 0}
        self._down: set[str] = set()
        self._error_shown: set[str] = set()
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            with self.cache_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.cache = data
        except (OSError, ValueError):
            self.cache = {}

    def save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("w", encoding="utf-8") as fh:
                json.dump(self.cache, fh, ensure_ascii=False)
        except OSError:
            pass

    def _wait(self, interval: float) -> None:
        delay = interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

    def _get_json(self, url: str, provider: str, interval: float) -> dict | None:
        if provider in self._down:
            return None
        last_error = ""
        for attempt in range(3):
            self._wait(interval)
            try:
                request = Request(url, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                })
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                self.requests += 1
                self.failures[provider] = 0
                return payload
            except HTTPError as exc:
                self.requests += 1
                last_error = f"HTTP {exc.code}"
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                self.requests += 1
                last_error = str(getattr(exc, "reason", exc))
                time.sleep(1.5 * (attempt + 1))
        self.errors += 1
        self.failures[provider] += 1
        if self.failures[provider] >= 2:
            self._down.add(provider)
        if provider not in self._error_shown:
            self._error_shown.add(provider)
            extra = " — поиск здесь отключён до конца запуска" if provider in self._down else ""
            print(color(f"  ! {provider} недоступен ({last_error}){extra}", "33"))
        return None

    # ---- MusicBrainz -----------------------------------------------------

    def _mb_query(self, query: str) -> list[dict]:
        key = "mb:" + query.lower()
        if key in self.cache:
            return self.cache[key]

        params = urlencode({"query": query, "fmt": "json", "limit": 5})
        payload = self._get_json(f"{MB_URL}?{params}", "MusicBrainz", 1.1)
        if payload is None:
            return []
        candidates = [self._mb_simplify(r) for r in payload.get("recordings", [])]
        self.cache[key] = candidates
        return candidates

    @staticmethod
    def _mb_simplify(recording: dict) -> dict:
        credits = recording.get("artist-credit") or []
        artist = "".join(
            f"{credit.get('name', '')}{credit.get('joinphrase', '') or ''}"
            for credit in credits
            if isinstance(credit, dict)
        ).strip()
        releases = recording.get("releases") or []
        release = releases[0] if releases else {}
        date = recording.get("first-release-date") or release.get("date") or ""
        return {
            "artist": artist,
            "title": (recording.get("title") or "").strip(),
            "length": recording.get("length"),
            "album": (release.get("title") or "").strip(),
            "year": str(date)[:4],
            "score": int(recording.get("score") or 0),
        }

    def _lookup_mb(self, title: str, artist: str,
                   duration: float | None) -> dict | None:
        query = f'recording:"{lucene_escape(title)}"' if title else ""
        if artist:
            query = (query + " AND " if query else "") + f'artist:"{lucene_escape(artist)}"'
        candidates = [c for c in self._mb_query(query) if c["title"]]

        match = self._pick_scored(candidates, duration)
        if match is None and artist and title:
            # Иногда исполнитель записан иначе — пробуем только название.
            fallback = f'recording:"{lucene_escape(title)}"'
            candidates = [c for c in self._mb_query(fallback) if c["title"]]
            match = self._pick_scored(candidates, duration)
        return match

    def _pick_scored(self, candidates: list[dict], duration: float | None) -> dict | None:
        usable = []
        for candidate in candidates:
            score = candidate["score"]
            if score < self.min_score:
                continue
            diff = None
            if duration and candidate.get("length"):
                diff = abs(candidate["length"] / 1000.0 - duration)
            usable.append((score, diff, candidate))

        def by_tolerance(tolerance: float, min_score: int = 0):
            return [
                item for item in usable
                if item[0] >= min_score and (item[1] is None or item[1] <= tolerance)
            ]

        accepted = by_tolerance(self.tolerance)
        if not accepted:
            accepted = by_tolerance(self.tolerance * 2.5, max(self.min_score, 95))
        if not accepted:
            return None
        accepted.sort(key=lambda item: (-item[0], item[1] if item[1] is not None else 9999))
        return accepted[0][2]

    # ---- iTunes ----------------------------------------------------------

    def _itunes_query(self, term: str) -> list[dict]:
        key = "itunes:" + term.lower()
        if key in self.cache:
            return self.cache[key]

        params = urlencode({"term": term, "entity": "song", "limit": 8})
        payload = self._get_json(f"{ITUNES_URL}?{params}", "iTunes", 2.0)
        if payload is None:
            return []
        candidates = []
        for item in payload.get("results", []):
            if item.get("kind") not in (None, "song"):
                continue
            date = str(item.get("releaseDate") or "")
            candidates.append({
                "artist": (item.get("artistName") or "").strip(),
                "title": (item.get("trackName") or "").strip(),
                "length": item.get("trackTimeMillis"),
                "album": (item.get("collectionName") or "").strip(),
                "year": date[:4],
                "score": 0,
            })
        self.cache[key] = candidates
        return candidates

    def _lookup_itunes(self, title: str, artist: str,
                       duration: float | None) -> dict | None:
        term = " ".join(part for part in (artist, title) if part)
        if not term:
            return None
        norm_title = normalize_for_match(title)
        norm_artist = normalize_for_match(artist)
        best: tuple[int, float, dict] | None = None

        for candidate in self._itunes_query(term):
            if not candidate["title"]:
                continue
            if normalize_for_match(candidate["title"]) != norm_title:
                continue

            score = 100
            if norm_artist:
                candidate_artist = normalize_for_match(candidate["artist"])
                similar = (
                    candidate_artist == norm_artist
                    or norm_artist in candidate_artist
                    or candidate_artist in norm_artist
                )
                if not similar:
                    score = 70
            if score < self.min_score:
                continue

            diff = 9999.0
            if duration and candidate.get("length"):
                diff = abs(candidate["length"] / 1000.0 - duration)
                if diff > self.tolerance * 2.5:
                    continue
            key = (score, -diff)
            if best is None or key > (best[0], -best[1]):
                best = (score, diff, candidate)

        if best is None:
            return None
        score, _, candidate = best
        result = dict(candidate)
        result["score"] = score
        return result

    # ---- общий вход ------------------------------------------------------

    def lookup(self, title: str, artist: str, duration: float | None) -> dict | None:
        if not title:
            return None
        match = self._lookup_mb(title, artist, duration)
        if match:
            self.provider = "MusicBrainz"
            return match
        match = self._lookup_itunes(title, artist, duration)
        if match:
            self.provider = "iTunes"
        return match



# --------------------------------------------------------------------------
# Формирование имён
# --------------------------------------------------------------------------

class SafeDict(dict):
    def __missing__(self, key):  # noqa: D105
        return ""


ALLOWED_FIELDS = {"title", "artist", "album", "albumartist", "year", "track"}


def validate_template(template: str) -> str:
    """Возвращает текст ошибки или пустую строку."""
    try:
        fields = [field for _, field, _, _ in Formatter().parse(template) if field]
    except ValueError as exc:
        return f"некорректный шаблон: {exc}"
    unknown = sorted({
        field.split(".")[0].split("[")[0] for field in fields
    } - ALLOWED_FIELDS)
    if unknown:
        allowed = ", ".join("{" + name + "}" for name in sorted(ALLOWED_FIELDS))
        return "неизвестные поля: " + ", ".join("{" + u + "}" for u in unknown) + \
               f"\nДоступны: {allowed}"
    return ""


def sanitize_name(name: str, ext: str, folder: Path) -> str:
    name = unicodedata.normalize("NFC", name)
    name = "".join(" " if ch in INVALID_CHARS else ch for ch in name)
    name = "".join(ch for ch in name if ord(ch) >= 32 and ord(ch) != 127)
    name = MULTI_SPACE_RE.sub(" ", name).strip(" .")
    if not name:
        name = "track"
    if name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name

    # Ограничение длины: Windows не любит пути длиннее ~260 символов.
    budget = max(30, 240 - len(str(folder)) - 1 - len(ext))
    if len(name) > budget:
        name = name[:budget].rstrip(" .")
    return name[:120].rstrip(" .") + ext


def compose_name(template: str, meta: TrackMeta) -> str:
    fields = SafeDict(
        title=meta.title,
        artist=meta.artist,
        album=meta.album,
        albumartist=meta.albumartist,
        year=meta.year,
        track=(meta.track.zfill(2) if meta.track else ""),
    )
    try:
        name = template.format_map(fields)
    except (KeyError, IndexError, ValueError):
        name = "{artist} - {title}".format_map(fields)

    name = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", name)
    name = re.sub(r"(?:\s*[-–—]\s*){2,}", " - ", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"^[\s\-–—_.]+", "", name)
    name = re.sub(r"[\s\-–—_.]+$", "", name)
    if not name.strip():
        name = meta.title or meta.artist or "track"
    return name.strip()


@dataclass
class Plan:
    source: Path
    target: Path | None
    kind: str          # rename | skip
    reason: str = ""
    origin: str = ""   # tags | online | filename
    score: int = 0
    provider: str = ""
    artist: str = ""
    title: str = ""
    album: str = ""


def unique_target(folder: Path, base: str, ext: str, taken: set[str]) -> Path:
    candidate = base + ext
    index = 2
    while candidate.lower() in taken or (folder / candidate).exists():
        candidate = f"{base} ({index}){ext}"
        index += 1
    taken.add(candidate.lower())
    return folder / candidate


def is_suffixed_variant(base: str, stem: str) -> bool:
    """«Song (2)» — это уже результат разрешения конфликта имён, не трогаем."""
    return re.fullmatch(re.escape(base) + r" \(\d+\)", stem) is not None


def collect_files(folder: Path, recursive: bool) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.glob("*")
    files = []
    for path in iterator:
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(path)
    return sorted(files, key=lambda p: str(p).lower())


def build_plan(files: list[Path], args, online: OnlineLookup | None) -> list[Plan]:
    plans: list[Plan] = []
    taken_by_dir: dict[Path, set[str]] = {}
    for index, path in enumerate(files, 1):
        folder = path.parent
        taken = taken_by_dir.get(folder)
        if taken is None:
            try:
                taken = {p.name.lower() for p in folder.iterdir()}
            except OSError:
                taken = set()
            taken_by_dir[folder] = taken

        meta, _has_tags, readable = read_meta(path)
        if not readable:
            plans.append(Plan(path, None, "skip", "не удалось прочитать аудиофайл"))
            continue
        stem_artist, stem_title = guess_from_filename(path.stem)

        title = meta.title or stem_title
        artist = meta.artist or stem_artist
        origin = "tags" if meta.title else "filename"
        score = 0
        provider = ""

        # Онлайн-поиск нужен, только если не хватает названия или исполнителя.
        needs_online = online is not None and (not meta.title or not meta.artist)
        if needs_online:
            print(color(f"  … ищу онлайн: {path.name}", "90"))
            match = online.lookup(title, artist, meta.duration)
            if match:
                meta.title = meta.title or match["title"]
                meta.artist = meta.artist or match["artist"]
                meta.album = meta.album or match["album"]
                meta.year = meta.year or match["year"]
                origin = "online"
                score = match["score"]
                provider = online.provider
            elif not meta.title:
                plans.append(Plan(path, None, "skip",
                                  "нет тегов и не найдено онлайн", origin))
                continue

        if not meta.title:
            plans.append(Plan(path, None, "skip", "не удалось определить название", origin))
            continue

        ext = path.suffix
        base = compose_name(args.format, meta)
        safe = sanitize_name(base, ext, folder)
        safe_base = safe[: -len(ext)] if safe.lower().endswith(ext.lower()) else safe
        target = folder / safe

        if target.name == path.name or is_suffixed_variant(safe_base, path.stem):
            plans.append(Plan(path, target, "skip", "имя уже правильное", origin, score,
                              provider, meta.artist, meta.title, meta.album))
            continue

        if target.name.lower() == path.name.lower():
            # Отличается только регистр — переименовываем через временное имя.
            taken.add(target.name.lower())
            plans.append(Plan(path, target, "rename", "", origin, score,
                              provider, meta.artist, meta.title, meta.album))
            continue

        target = unique_target(folder, safe_base, ext, taken)
        plans.append(Plan(path, target, "rename", "", origin, score,
                          provider, meta.artist, meta.title, meta.album))
    return plans


# --------------------------------------------------------------------------
# Журнал и переименование
# --------------------------------------------------------------------------

LOG_FIELDS = ["run_id", "timestamp", "action", "old_path", "new_path",
              "origin", "score", "artist", "title", "album", "error"]


def append_log(log_path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    new_file = not log_path.exists() or log_path.stat().st_size == 0
    try:
        with log_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
            if new_file:
                # BOM нужен Excel для корректной кириллицы.
                fh.write("\ufeff")
                writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in LOG_FIELDS})
    except OSError as exc:
        print(color(f"  ! Не удалось записать журнал: {exc}", "31"))


def apply_plan(plan: Plan, log_path: Path, rows: list[dict]) -> str:
    source, target = plan.source, plan.target
    assert target is not None
    try:
        if target.exists() and target.name.lower() != source.name.lower():
            raise FileExistsError(f"файл уже существует: {target.name}")
        if source.name != target.name and source.name.lower() == target.name.lower():
            temp = source.with_name(source.name + ".autosort-tmp")
            source.rename(temp)
            try:
                temp.rename(target)
            except OSError:
                try:
                    temp.rename(source)
                except OSError:
                    pass
                raise
        else:
            source.rename(target)
        rows.append({
            "run_id": RUN_ID,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": "rename",
            "old_path": str(source),
            "new_path": str(target),
            "origin": plan.origin,
            "score": plan.score,
            "artist": plan.artist,
            "title": plan.title,
            "album": plan.album,
        })
        return "ok"
    except OSError as exc:
        rows.append({
            "run_id": RUN_ID,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": "error",
            "old_path": str(source),
            "new_path": str(target),
            "origin": plan.origin,
            "error": str(exc),
        })
        plan.reason = str(exc)
        return "error"


def undo(folder: Path, run_id: str | None) -> int:
    log_path = folder / LOG_NAME
    if not log_path.exists():
        print(color(f"Журнал не найден: {log_path}", "33"))
        return 1
    with log_path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    renames = [r for r in rows if r.get("action") == "rename" and r.get("new_path")]
    if not renames:
        print("В журнале нет переименований.")
        return 0

    if not run_id or run_id == "last":
        target_run = renames[-1]["run_id"]
    else:
        target_run = run_id
    selected = [r for r in renames if r.get("run_id") == target_run]
    if not selected:
        print(color(f"Запуск «{target_run}» не найден в журнале.", "33"))
        return 1

    print(f"Откат запуска {target_run}: файлов {len(selected)}")
    undone = 0
    undo_rows = []
    for row in reversed(selected):
        current = Path(row["new_path"])
        original = Path(row["old_path"])
        if not current.exists():
            print(color(f"  - пропуск (нет файла): {current.name}", "33"))
            continue
        if original.exists():
            print(color(f"  - пропуск (имя занято): {original.name}", "33"))
            continue
        try:
            current.rename(original)
        except OSError as exc:
            print(color(f"  ! ошибка: {current.name}: {exc}", "31"))
            continue
        undone += 1
        print(f"  {current.name}  ->  {original.name}")
        undo_rows.append({
            "run_id": run_id if run_id and run_id != "last" else target_run,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": "undo",
            "old_path": str(current),
            "new_path": str(original),
        })
    append_log(log_path, undo_rows)
    print(color(f"Возвращено имён: {undone}", "32"))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Переименовывает музыкальные файлы в «Исполнитель - Название».",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            '  python autosort_music.py "D:\\Music"            # предпросмотр\n'
            '  python autosort_music.py "D:\\Music" --apply    # переименовать\n'
            '  python autosort_music.py "D:\\Music" --undo     # отменить последний запуск\n'
        ),
    )
    parser.add_argument("folder", nargs="?", default=".", help="папка с музыкой")
    parser.add_argument("--apply", action="store_true",
                        help="действительно переименовать (без флага — только предпросмотр)")
    parser.add_argument("--format", default="{artist} - {title}",
                        help="шаблон имени, по умолчанию: {artist} - {title}; "
                             "доступны {title} {artist} {album} {albumartist} {year} {track}")
    parser.add_argument("--no-online", action="store_true",
                        help="не искать недостающие названия в интернете")
    parser.add_argument("--min-score", type=int, default=85,
                        help="минимальное совпадение при онлайн-поиске, 0-100 (по умолчанию 85)")
    parser.add_argument("--tolerance", type=float, default=10.0,
                        help="допустимая разница длительности в секундах (по умолчанию 10)")
    parser.add_argument("--no-recursive", action="store_true",
                        help="не заходить во вложенные папки")
    parser.add_argument("--undo", nargs="?", const="last", default=None, metavar="RUN_ID",
                        help="отменить переименования (последний запуск или указанный RUN_ID)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(color(f"Папка не найдена: {folder}", "31"))
        return 2

    if args.undo:
        return undo(folder, args.undo)

    template_error = validate_template(args.format)
    if template_error:
        print(color(f"Ошибка в --format: {template_error}", "31"))
        return 2

    files = collect_files(folder, recursive=not args.no_recursive)
    if not files:
        print(f"В папке нет музыкальных файлов: {folder}")
        print("Поддерживаются: " + ", ".join(sorted(AUDIO_EXTENSIONS)))
        return 0

    online = None
    if not args.no_online:
        online = OnlineLookup(min_score=args.min_score, tolerance=args.tolerance)

    print(f"Папка: {folder}")
    print(f"Найдено файлов: {len(files)}")
    print("Режим: " + ("ПЕРЕИМЕНОВАНИЕ" if args.apply else "предпросмотр (ничего не меняется)"))
    print()

    plans = build_plan(files, args, online)

    if online is not None:
        online.save_cache()

    log_path = folder / LOG_NAME
    log_rows: list[dict] = []
    renamed = skipped = errors = 0

    for index, plan in enumerate(plans, 1):
        prefix = f"[{index}/{len(plans)}]"
        name = plan.source.name
        if plan.kind == "skip":
            skipped += 1
            if plan.reason == "имя уже правильное" and not args.apply:
                print(f"{prefix} = {name}")
            else:
                print(f"{prefix} {color('-', '33')} {name}")
                print(f"        {color(plan.reason, '90')}")
            continue

        if plan.origin == "online":
            origin = f"{plan.provider or 'онлайн'} {plan.score}"
        else:
            origin = {"tags": "теги", "filename": "имя файла"}.get(plan.origin, plan.origin)
        print(f"{prefix} {color('→', '32')} {name}")
        print(f"        {color(plan.target.name, '36')}  ({origin})")

        if args.apply:
            result = apply_plan(plan, log_path, log_rows)
            if result == "ok":
                renamed += 1
            else:
                errors += 1
                print(color(f"        ошибка: {plan.reason}", "31"))
        else:
            renamed += 1

    print()
    if args.apply:
        append_log(log_path, log_rows)
        print(color(f"Переименовано: {renamed}   пропущено: {skipped}   ошибок: {errors}", "32"))
        if renamed:
            print(f"Журнал: {log_path}  (откат: --undo)")
    else:
        print(f"Будет переименовано: {renamed}   пропущено: {skipped}")
        if renamed:
            print()
            print(color("Это предпросмотр. Чтобы применить: добавьте --apply", "33"))
    if online is not None and online.requests:
        print(f"Онлайн-запросов: {online.requests}")
    return 0


RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        raise SystemExit(130)
