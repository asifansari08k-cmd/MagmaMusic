import os
import re
import time
import asyncio
import sqlite3
import threading
from typing import Any, Dict, Optional

import yt_dlp

from Anysnap import logger


# =========================================================
# CONFIGURATION
# =========================================================

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
CACHE_EXPIRE_HOURS = float(os.getenv("CACHE_EXPIRE_HOURS", "24"))
MAX_VIDEO_QUALITY = os.getenv("MAX_VIDEO_QUALITY", "720")

COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")
DB_FILE = os.getenv("DB_FILE", "cache.db")

# Extreme but practical settings.
CONCURRENT_FRAGMENTS = int(
    os.getenv("CONCURRENT_FRAGMENTS", "64")
)

HTTP_CHUNK_SIZE = int(
    os.getenv("HTTP_CHUNK_SIZE", str(100 * 1024 * 1024))
)

SOCKET_TIMEOUT = int(
    os.getenv("SOCKET_TIMEOUT", "20")
)

NO_PROGRESS_TIMEOUT = int(
    os.getenv("NO_PROGRESS_TIMEOUT", "120")
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# =========================================================
# PROGRESS WATCH
# =========================================================

class ProgressTracker:
    """
    Tracks real download progress.

    This is NOT a total download timeout.
    A large file can take as long as necessary.

    Timeout happens only when absolutely no progress
    has been seen for NO_PROGRESS_TIMEOUT seconds.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.last_progress = time.monotonic()
        self.downloaded = 0

    def hook(self, data: Dict[str, Any]):
        status = data.get("status")

        if status == "downloading":
            with self.lock:
                self.last_progress = time.monotonic()

                downloaded = data.get("downloaded_bytes")
                if downloaded is not None:
                    self.downloaded = downloaded

        elif status == "finished":
            with self.lock:
                self.last_progress = time.monotonic()

    def stalled(self) -> bool:
        with self.lock:
            return (
                time.monotonic() - self.last_progress
                > NO_PROGRESS_TIMEOUT
            )


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
        logger.error(
            f"❌ Database initialization failed: {e}",
            exc_info=True,
        )


def get_cached_metadata(
    video_id: str,
    file_type: str,
) -> Optional[Dict[str, Any]]:

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
            created_time = float(row["created_time"] or 0)

            # -------------------------------------------------
            # CACHE EXPIRY
            # -------------------------------------------------

            cache_age = time.time() - created_time
            cache_expire_seconds = CACHE_EXPIRE_HOURS * 3600

            if cache_age > cache_expire_seconds:
                logger.info(
                    f"🕒 Cache expired: {video_id} | {file_type}"
                )

                cur.execute(
                    "DELETE FROM downloads WHERE id = ?",
                    (row["id"],),
                )

                conn.commit()

                if file_path and os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(
                            f"🗑️ Removed expired file: {file_path}"
                        )
                    except OSError as e:
                        logger.warning(
                            f"⚠️ Could not remove expired file: {e}"
                        )

                return None

            # -------------------------------------------------
            # FILE VALIDATION
            # -------------------------------------------------

            if (
                file_path
                and os.path.isfile(file_path)
                and os.path.getsize(file_path) > 0
            ):
                return dict(row)

            logger.warning(
                f"⚠️ Cached file missing or empty: "
                f"{row['file_name']}"
            )

            cur.execute(
                "DELETE FROM downloads WHERE id = ?",
                (row["id"],),
            )

            conn.commit()

    except Exception as e:
        logger.error(
            f"❌ Error accessing downloader cache: {e}",
            exc_info=True,
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
            f"❌ Error saving downloader cache: {e}",
            exc_info=True,
        )


def find_legacy_cached_file(
    video_id: str,
    ext: str,
) -> Optional[str]:

    if not video_id:
        return None

    suffix = f"_{video_id}.{ext}"

    try:
        with os.scandir(DOWNLOAD_DIR) as entries:
            for entry in entries:
                if (
                    entry.is_file()
                    and entry.name.endswith(suffix)
                    and os.path.getsize(entry.path) > 0
                ):
                    return entry.name

    except Exception as e:
        logger.error(
            f"❌ Error scanning {DOWNLOAD_DIR}: {e}",
            exc_info=True,
        )

    return None


init_db()


# =========================================================
# VIDEO ID
# =========================================================

def extract_video_id(url: str) -> Optional[str]:

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
    """Common high-speed yt-dlp configuration."""

    opts = {
        "outtmpl": os.path.join(
            DOWNLOAD_DIR,
            "%(title).150s_%(id)s.%(ext)s",
        ),

        "restrictfilenames": True,
        "noplaylist": True,

        # -------------------------------------------------
        # RETRIES / NETWORK
        # -------------------------------------------------

        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,

        "socket_timeout": SOCKET_TIMEOUT,

        "continuedl": True,
        "nopart": False,

        # -------------------------------------------------
        # EXTREME DOWNLOAD SPEED
        # -------------------------------------------------

        "concurrent_fragment_downloads":
            CONCURRENT_FRAGMENTS,

        "http_chunk_size":
            HTTP_CHUNK_SIZE,

        # -------------------------------------------------
        # YOUTUBE JS CHALLENGE
        # -------------------------------------------------

        "js_runtimes": {
            "node": {},
        },

        "remote_components": [
            "ejs:github",
        ],

        # -------------------------------------------------
        # REDUCE LOGGING OVERHEAD
        # -------------------------------------------------

        "quiet": True,
        "no_warnings": True,

        # -------------------------------------------------
        # CONNECTION
        # -------------------------------------------------

        "nocheckcertificate": True,

        "updatetime": False,
        "clean_infojson": False,
    }

    # -----------------------------------------------------
    # COOKIES
    # -----------------------------------------------------

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
        "All YouTube extraction strategies failed: "
        f"{last_error}"
    )


# =========================================================
# AUDIO DOWNLOAD
# =========================================================

def download_audio_sync(url: str) -> str:

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
        f"🎵 Starting EXTREME audio download: {url}"
    )

    opts = get_base_ydl_opts()

    tracker = ProgressTracker()

    opts.update(
        {
            # -------------------------------------------------
            # 192 KBPS AUDIO — UNCHANGED
            # -------------------------------------------------

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

            # -------------------------------------------------
            # EXTREME SPEED
            # -------------------------------------------------

            "concurrent_fragment_downloads":
                CONCURRENT_FRAGMENTS,

            "http_chunk_size":
                HTTP_CHUNK_SIZE,

            # -------------------------------------------------
            # RETRIES
            # -------------------------------------------------

            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": SOCKET_TIMEOUT,

            # -------------------------------------------------
            # PROGRESS
            # -------------------------------------------------

            "progress_hooks": [
                tracker.hook,
            ],

            "noprogress": True,

            # -------------------------------------------------
            # FFMPEG
            # -------------------------------------------------

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

        # -------------------------------------------------
        # PREPARE OUTPUT
        # -------------------------------------------------

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

def download_video_sync(url: str) -> str:

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
        f"🎬 Starting EXTREME video download: {url}"
    )

    opts = get_base_ydl_opts()

    tracker = ProgressTracker()

    opts.update(
        {
            # -------------------------------------------------
            # MAX 720P — UNCHANGED
            # -------------------------------------------------

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

            # -------------------------------------------------
            # EXTREME SPEED
            # -------------------------------------------------

            "concurrent_fragment_downloads":
                CONCURRENT_FRAGMENTS,

            "http_chunk_size":
                HTTP_CHUNK_SIZE,

            # -------------------------------------------------
            # RETRIES
            # -------------------------------------------------

            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": SOCKET_TIMEOUT,

            # -------------------------------------------------
            # PROGRESS
            # -------------------------------------------------

            "progress_hooks": [
                tracker.hook,
            ],

            "noprogress": True,

            # -------------------------------------------------
            # FFMPEG
            # -------------------------------------------------

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

        # -------------------------------------------------
        # PREPARE OUTPUT
        # -------------------------------------------------

        with yt_dlp.YoutubeDL(opts) as ydl:
            filename = ydl.prepare_filename(info)

        base_path, _ = os.path.splitext(filename)

        final_path = f"{base_path}.mp4"

        # -------------------------------------------------
        # CHECK POSSIBLE OUTPUT EXTENSIONS
        # -------------------------------------------------

        for ext in (
            ".mp4",
            ".webm",
            ".mkv",
        ):

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

    try:

        return await asyncio.to_thread(
            download_audio_sync,
            url,
        )

    except asyncio.CancelledError:

        logger.warning(
            "⚠️ Audio asyncio task cancelled."
        )

        raise

    except Exception as e:

        logger.error(
            f"❌ Async audio download failed: {e}",
            exc_info=True,
        )

        return None


async def download_video(
    url: str,
) -> Optional[str]:

    try:

        return await asyncio.to_thread(
            download_video_sync,
            url,
        )

    except asyncio.CancelledError:

        logger.warning(
            "⚠️ Video asyncio task cancelled."
        )

        raise

    except Exception as e:

        logger.error(
            f"❌ Async video download failed: {e}",
            exc_info=True,
        )

        return None