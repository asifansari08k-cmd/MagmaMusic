import os
import re
import time
import asyncio
import sqlite3
from typing import Any, Dict, Optional

import yt_dlp

from Anysnap import logger


# =========================================================
# CONFIGURATION
# =========================================================

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
CACHE_EXPIRE_HOURS = float(os.getenv("CACHE_EXPIRE_HOURS", "24"))
MAX_VIDEO_QUALITY = os.getenv("MAX_VIDEO_QUALITY", "720")

# cookies.txt is in the project root
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")

# SQLite cache
DB_FILE = os.getenv("DB_FILE", "cache.db")

# Make sure download directory exists
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# =========================================================
# DATABASE & CACHE
# =========================================================

def init_db():
    """Initialize SQLite cache database."""
    try:
        with sqlite3.connect(DB_FILE, timeout=15.0) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT,
                    title TEXT,
                    file_name TEXT,
                    file_path TEXT,
                    file_type TEXT,
                    file_size INTEGER,
                    duration INTEGER,
                    created_time REAL,
                    thumbnail TEXT,
                    UNIQUE(video_id, file_type)
                )
                """
            )
            conn.commit()

        logger.info("✅ Downloader SQLite cache initialized.")

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")


def get_cached_metadata(
    video_id: str,
    file_type: str,
) -> Optional[Dict[str, Any]]:
    """Return cached metadata if the file still exists."""
    try:
        with sqlite3.connect(DB_FILE, timeout=15.0) as conn:
            conn.row_factory = sqlite3.Row

            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM downloads
                WHERE video_id = ?
                AND file_type = ?
                """,
                (video_id, file_type),
            )

            row = cur.fetchone()

            if not row:
                return None

            file_path = row["file_path"]

            if (
                os.path.isfile(file_path)
                and os.path.getsize(file_path) > 0
            ):
                return dict(row)

            logger.warning(
                f"Cached file missing: {row['file_name']}"
            )

            cur.execute(
                "DELETE FROM downloads WHERE id = ?",
                (row["id"],),
            )

            conn.commit()

    except Exception as e:
        logger.error(
            f"❌ Error accessing downloader cache: {e}"
        )

    return None


def save_cached_metadata(
    data: Dict[str, Any],
    file_type: str,
):
    """Save downloaded file metadata to SQLite."""
    try:
        with sqlite3.connect(DB_FILE, timeout=15.0) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO downloads
                (
                    video_id,
                    title,
                    file_name,
                    file_path,
                    file_type,
                    file_size,
                    duration,
                    created_time,
                    thumbnail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["videoId"],
                    data["title"],
                    data["filename"],
                    data["path"],
                    file_type,
                    data["filesize"],
                    data["duration"],
                    time.time(),
                    data["thumbnail"],
                ),
            )

            conn.commit()

    except Exception as e:
        logger.error(
            f"❌ Error saving downloader cache: {e}"
        )


def find_legacy_cached_file(
    video_id: str,
    ext: str,
) -> Optional[str]:
    """Find older downloaded files not present in SQLite."""
    if not video_id:
        return None

    suffix = f"_{video_id}.{ext}"

    try:
        with os.scandir(DOWNLOAD_DIR) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.endswith(suffix):
                    return entry.name

    except Exception as e:
        logger.error(
            f"❌ Error scanning {DOWNLOAD_DIR}: {e}"
        )

    return None


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

# Initialize when this module is imported.
init_db()


# =========================================================
# VIDEO ID
# =========================================================

def extract_video_id(url: str) -> Optional[str]:
    """Extract the 11-character YouTube video ID."""
    if not url:
        return None

    if re.match(r"^[0-9A-Za-z_-]{11}$", url):
        return url

    pattern = (
        r"(?:youtu\.be\/|v=|\/shorts\/|"
        r"\/embed\/|\/v\/)"
        r"([0-9A-Za-z_-]{11})"
    )

    match = re.search(pattern, url)

    if match:
        return match.group(1)

    match = re.search(
        r"[0-9A-Za-z_-]{11}",
        url,
    )

    return match.group(0) if match else None


# =========================================================
# YT-DLP BASE OPTIONS
# =========================================================

def get_base_ydl_opts() -> Dict[str, Any]:
    """Create common yt-dlp configuration."""

    opts = {
        "outtmpl": os.path.join(
            DOWNLOAD_DIR,
            "%(title).150s_%(id)s.%(ext)s",
        ),

        "restrictfilenames": True,
        "noplaylist": True,

        "quiet": False,
        "no_warnings": False,

        "retries": 10,
        "fragment_retries": 10,

        "socket_timeout": 30,

        # Resume interrupted downloads
        "continuedl": True,

        # YouTube JS challenge support
        "js_runtimes": {
            "node": {},
        },

        "remote_components": [
            "ejs:github",
        ],
    }

    # Load cookies.txt if available
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

        logger.info(
            f"🍪 Loaded YouTube cookies: {COOKIES_FILE}"
        )
    else:
        logger.warning(
            f"⚠️ cookies.txt not found: {COOKIES_FILE}"
        )

    return opts


# =========================================================
# YOUTUBE EXTRACTION WITH FALLBACK
# =========================================================

def extract_youtube_with_fallback(
    url: str,
    opts: Dict[str, Any],
    download: bool = True,
) -> Dict[str, Any]:
    """
    Try normal YouTube extraction first.
    If it fails, retry using web_embedded.
    """

    strategies = [
        (
            "default",
            {},
        ),
        (
            "default-web_embedded",
            {
                "youtube": {
                    "player_client": [
                        "default",
                        "web_embedded",
                    ],
                },
            },
        ),
    ]

    last_error = None

    for name, extractor_args in strategies:
        try:
            attempt_opts = dict(opts)

            if extractor_args:
                attempt_opts["extractor_args"] = extractor_args

            logger.info(
                f"▶️ YouTube extraction strategy: {name}"
            )

            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                info = ydl.extract_info(
                    url,
                    download=download,
                )

            logger.info(
                f"✅ YouTube extraction successful: {name}"
            )

            return info

        except yt_dlp.utils.DownloadError as e:
            last_error = e

            logger.warning(
                f"❌ Strategy failed: {name} | {e}"
            )

    raise RuntimeError(
        f"All YouTube extraction strategies failed: "
        f"{last_error}"
    )


# =========================================================
# AUDIO DOWNLOAD
# =========================================================

def download_audio_sync(
    url: str,
) -> str:
    """Download YouTube audio and return local file path."""

    video_id = extract_video_id(url)

    # -----------------------------------------------------
    # DATABASE CACHE
    # -----------------------------------------------------

    if video_id:
        cached_data = get_cached_metadata(
            video_id,
            "mp3",
        )

        if cached_data:
            logger.info(
                f"⚡ Audio cache hit: {video_id}"
            )

            return cached_data["file_path"]

        # -------------------------------------------------
        # LEGACY CACHE
        # -------------------------------------------------

        legacy_file = find_legacy_cached_file(
            video_id,
            "mp3",
        )

        if legacy_file:
            path = os.path.join(
                DOWNLOAD_DIR,
                legacy_file,
            )

            if (
                os.path.isfile(path)
                and os.path.getsize(path) > 0
            ):
                logger.info(
                    f"⚡ Legacy audio cache hit: {video_id}"
                )

                data = {
                    "videoId": video_id,
                    "title": legacy_file[
                        :-len(f"_{video_id}.mp3")
                    ],
                    "filename": legacy_file,
                    "path": path,
                    "filesize": os.path.getsize(path),
                    "duration": 0,
                    "thumbnail": (
                        f"https://i.ytimg.com/vi/"
                        f"{video_id}/hqdefault.jpg"
                    ),
                }

                save_cached_metadata(
                    data,
                    "mp3",
                )

                return path

    # -----------------------------------------------------
    # DOWNLOAD
    # -----------------------------------------------------

    logger.info(
        f"🎵 Starting audio download: {url}"
    )

    opts = get_base_ydl_opts()

    opts.update(
        {
            "format": (
                "140/"
                "ba[ext=m4a]/"
                "bestaudio/best"
            ),

            "writethumbnail": False,

            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                },
            ],

            "concurrent_fragment_downloads": 15,

            "http_chunk_size": 10485760,

            "nocheckcertificate": True,

            "noprogress": True,
            "quiet": True,
            "no_warnings": True,

            "updatetime": False,
            "clean_infojson": False,

            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 15,

            "postprocessor_args": [
                "-threads",
                "0",
                "-vn",
                "-sn",
            ],
        }
    )

    try:
        info = extract_youtube_with_fallback(
            url,
            opts,
            download=True,
        )

        # Prepare final filename
        with yt_dlp.YoutubeDL(opts) as ydl:
            filename = ydl.prepare_filename(info)

        base_path, _ = os.path.splitext(filename)

        final_path = f"{base_path}.mp3"

        if (
            not os.path.isfile(final_path)
            or os.path.getsize(final_path) == 0
        ):
            raise RuntimeError(
                "Downloaded audio file is missing or empty."
            )

        logger.info(
            f"✅ Audio downloaded: {final_path}"
        )

        response_data = {
            "videoId": info.get("id") or video_id,
            "title": info.get("title", ""),
            "filename": os.path.basename(final_path),
            "path": final_path,
            "filesize": os.path.getsize(final_path),
            "duration": info.get("duration", 0) or 0,
            "thumbnail": info.get("thumbnail", ""),
        }

        save_cached_metadata(
            response_data,
            "mp3",
        )

        return final_path

    except yt_dlp.utils.DownloadError as e:
        logger.error(
            f"❌ yt-dlp audio error: {e}"
        )

        raise RuntimeError(
            f"Audio download failed: {e}"
        )

    except Exception as e:
        logger.error(
            f"❌ Audio download error: {e}",
            exc_info=True,
        )

        raise RuntimeError(
            f"Audio download failed: {e}"
        )


# =========================================================
# VIDEO DOWNLOAD
# =========================================================

def download_video_sync(
    url: str,
) -> str:
    """Download YouTube video and return local file path."""

    video_id = extract_video_id(url)

    # -----------------------------------------------------
    # DATABASE CACHE
    # -----------------------------------------------------

    if video_id:
        cached_data = get_cached_metadata(
            video_id,
            "mp4",
        )

        if cached_data:
            logger.info(
                f"⚡ Video cache hit: {video_id}"
            )

            return cached_data["file_path"]

        # -------------------------------------------------
        # LEGACY CACHE
        # -------------------------------------------------

        legacy_file = find_legacy_cached_file(
            video_id,
            "mp4",
        )

        if legacy_file:
            path = os.path.join(
                DOWNLOAD_DIR,
                legacy_file,
            )

            if (
                os.path.isfile(path)
                and os.path.getsize(path) > 0
            ):
                logger.info(
                    f"⚡ Legacy video cache hit: {video_id}"
                )

                data = {
                    "videoId": video_id,
                    "title": legacy_file[
                        :-len(f"_{video_id}.mp4")
                    ],
                    "filename": legacy_file,
                    "path": path,
                    "filesize": os.path.getsize(path),
                    "duration": 0,
                    "thumbnail": (
                        f"https://i.ytimg.com/vi/"
                        f"{video_id}/hqdefault.jpg"
                    ),
                }

                save_cached_metadata(
                    data,
                    "mp4",
                )

                return path

    # -----------------------------------------------------
    # DOWNLOAD
    # -----------------------------------------------------

    logger.info(
        f"🎬 Starting video download: {url}"
    )

    opts = get_base_ydl_opts()

    opts.update(
        {
            "format": (
                f"bv*[height<={MAX_VIDEO_QUALITY}]"
                f"[ext=mp4]+"
                f"ba[ext=m4a]/"
                f"b[height<={MAX_VIDEO_QUALITY}]"
                f"[ext=mp4]/best"
            ),

            "merge_output_format": "mp4",

            "writethumbnail": False,
            "embedthumbnail": False,

            "concurrent_fragment_downloads": 15,

            "http_chunk_size": 10485760,

            "nocheckcertificate": True,

            "noprogress": True,
            "quiet": True,
            "no_warnings": True,

            "updatetime": False,
            "clean_infojson": False,

            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 15,

            "postprocessor_args": [
                "-threads",
                "0",
            ],
        }
    )

    try:
        info = extract_youtube_with_fallback(
            url,
            opts,
            download=True,
        )

        with yt_dlp.YoutubeDL(opts) as ydl:
            filename = ydl.prepare_filename(info)

        base_path, _ = os.path.splitext(filename)

        final_path = f"{base_path}.mp4"

        # Check possible output extensions
        for ext in [
            ".mp4",
            ".webm",
            ".mkv",
        ]:
            test_path = f"{base_path}{ext}"

            if (
                os.path.isfile(test_path)
                and os.path.getsize(test_path) > 0
            ):
                final_path = test_path
                break

        if not (
            os.path.isfile(final_path)
            and os.path.getsize(final_path) > 0
        ):
            raise RuntimeError(
                "Downloaded video file not found or empty."
            )

        logger.info(
            f"✅ Video downloaded: {final_path}"
        )

        response_data = {
            "videoId": info.get("id") or video_id,
            "title": info.get("title", ""),
            "filename": os.path.basename(final_path),
            "path": final_path,
            "filesize": os.path.getsize(final_path),
            "duration": info.get("duration", 0) or 0,
            "thumbnail": info.get("thumbnail", ""),
        }

        save_cached_metadata(
            response_data,
            "mp4",
        )

        return final_path

    except yt_dlp.utils.DownloadError as e:
        logger.error(
            f"❌ yt-dlp video error: {e}"
        )

        raise RuntimeError(
            f"Video download failed: {e}"
        )

    except Exception as e:
        logger.error(
            f"❌ Video download error: {e}",
            exc_info=True,
        )

        raise RuntimeError(
            f"Video download failed: {e}"
        )


# =========================================================
# ASYNC WRAPPERS
# =========================================================

async def download_audio(
    url: str,
) -> Optional[str]:
    """
    Async wrapper for audio downloader.

    Returns local filesystem path.
    """

    try:
        return await asyncio.to_thread(
            download_audio_sync,
            url,
        )

    except Exception as e:
        logger.error(
            f"❌ Async audio download failed: {e}"
        )

        return None


async def download_video(
    url: str,
) -> Optional[str]:
    """
    Async wrapper for video downloader.

    Returns local filesystem path.
    """

    try:
        return await asyncio.to_thread(
            download_video_sync,
            url,
        )

    except Exception as e:
        logger.error(
            f"❌ Async video download failed: {e}"
        )

        return None