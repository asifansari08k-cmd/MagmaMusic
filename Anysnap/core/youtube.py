import re
import asyncio
from dataclasses import replace
from typing import Union

from pyrogram import enums, types
from py_yt import VideosSearch, Playlist

from Anysnap import logger
from Anysnap.helpers import Track, utils
from Anysnap.core.downloader import download_audio, download_video


class YouTube:
    def __init__(self):
        """Initialize Anysnap YouTube handler."""
        self.base = "https://www.youtube.com/watch?v="

        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|live/|embed/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )

        self.search_cache = {}

        logger.info("=" * 50)
        logger.info("📹 Anysnap YouTube Handler Initialized")
        logger.info("⚡ Mode: Direct yt-dlp Downloader")
        logger.info("=" * 50)

    # ========================================================
    # VALIDATE YOUTUBE URL
    # ========================================================

    def valid(self, url: str) -> bool:
        """Check if URL is a valid YouTube URL."""
        return bool(re.match(self.regex, url))

    # ========================================================
    # EXTRACT YOUTUBE URL
    # ========================================================

    def url(
        self,
        message_1: types.Message,
    ) -> Union[str, None]:
        """Extract YouTube URL from message."""

        messages = [message_1]
        link = None

        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)

        for message in messages:

            text = (
                message.text
                or message.caption
                or ""
            )

            if message.entities:

                for entity in message.entities:

                    if (
                        entity.type
                        == enums.MessageEntityType.URL
                    ):
                        link = text[
                            entity.offset:
                            entity.offset + entity.length
                        ]
                        break

            if message.caption_entities:

                for entity in message.caption_entities:

                    if (
                        entity.type
                        == enums.MessageEntityType.TEXT_LINK
                    ):
                        link = entity.url
                        break

        if link:

            return (
                link
                .split("&si")[0]
                .split("?si")[0]
            )

        return None

    # ========================================================
    # NORMAL SEARCH
    # ========================================================

    async def search(
        self,
        query: str,
        m_id: int,
    ) -> Track | None:
        """
        Normal YouTube search.

        Used by normal /play command.
        Returns only the first/best result.
        """

        cache_key = query
        current_time = (
            asyncio.get_running_loop().time()
        )

        # ----------------------------------------------------
        # CACHE
        # ----------------------------------------------------

        if cache_key in self.search_cache:

            cached_result, cache_timestamp = (
                self.search_cache[cache_key]
            )

            if (
                current_time - cache_timestamp
                < 600
            ):

                fresh = replace(
                    cached_result
                )

                fresh.message_id = m_id
                fresh.file_path = None
                fresh.user = None
                fresh.time = 0
                fresh.video = False

                return fresh

        # ----------------------------------------------------
        # SEARCH
        # ----------------------------------------------------

        try:

            _search = VideosSearch(
                query,
                limit=1,
            )

            results = await _search.next()

            if (
                results
                and results.get("result")
            ):

                data = results["result"][0]

                duration = data.get(
                    "duration"
                )

                is_live = (
                    duration is None
                    or duration == "LIVE"
                )

                track = Track(
                    id=data.get("id"),

                    channel_name=data.get(
                        "channel",
                        {},
                    ).get(
                        "name"
                    ),

                    duration=(
                        duration
                        if not is_live
                        else "LIVE"
                    ),

                    duration_sec=(
                        0
                        if is_live
                        else utils.to_seconds(
                            duration
                        )
                    ),

                    message_id=m_id,

                    title=data.get(
                        "title",
                        "",
                    )[:25],

                    thumbnail=(
                        data.get(
                            "thumbnails",
                            [{}],
                        )[-1]
                        .get(
                            "url",
                            "",
                        )
                        .split("?")[0]
                    ),

                    url=data.get(
                        "link"
                    ),

                    view_count=data.get(
                        "viewCount",
                        {},
                    ).get(
                        "short"
                    ),

                    is_live=is_live,
                )

                # ------------------------------------------------
                # SAVE CACHE
                # ------------------------------------------------

                self.search_cache[
                    cache_key
                ] = (
                    track,
                    current_time,
                )

                # ------------------------------------------------
                # LIMIT CACHE
                # ------------------------------------------------

                if len(
                    self.search_cache
                ) > 100:

                    oldest_key = min(
                        self.search_cache.keys(),
                        key=lambda k:
                        self.search_cache[k][1],
                    )

                    del self.search_cache[
                        oldest_key
                    ]

                return replace(
                    track
                )

        except Exception as e:

            logger.warning(
                f"⚠️ Search failed for "
                f"'{query}': {e}"
            )

        return None

    # ========================================================
    # AUTOPLAY SEARCH
    # ========================================================

    async def search_all(
        self,
        query: str,
        m_id: int = 0,
        limit: int = 5,
        exclude_ids: set[str] | None = None,
    ) -> list[Track]:
        """
        Search multiple YouTube results for autoplay.

        Duplicate video IDs are removed automatically.

        exclude_ids:
            Video IDs that must NOT be returned.
            This can contain the currently playing video
            and recently played videos.
        """

        try:

            logger.info(
                f"🔎 YouTube multi-search: "
                f"query={query}, limit={limit}"
            )

            # ------------------------------------------------
            # NORMALIZE EXCLUDE IDS
            # ------------------------------------------------

            excluded = set(
                exclude_ids or set()
            )

            # ------------------------------------------------
            # SEARCH
            # ------------------------------------------------

            _search = VideosSearch(
                query,
                limit=limit,
            )

            results = await _search.next()

            if not results:

                logger.warning(
                    f"⚠️ No search response "
                    f"for '{query}'"
                )

                return []

            raw_results = results.get(
                "result",
                [],
            )

            if not raw_results:

                logger.warning(
                    f"⚠️ No search results "
                    f"for '{query}'"
                )

                return []

            tracks = []

            # ------------------------------------------------
            # DUPLICATE PROTECTION
            # ------------------------------------------------

            seen_ids: set[str] = set()

            for data in raw_results:

                try:

                    video_id = data.get(
                        "id"
                    )

                    if not video_id:
                        continue

                    # ----------------------------------------
                    # SKIP CURRENT / RECENTLY PLAYED
                    # ----------------------------------------

                    if video_id in excluded:

                        logger.debug(
                            f"⏭️ Skipping excluded "
                            f"video: {video_id}"
                        )

                        continue

                    # ----------------------------------------
                    # SKIP DUPLICATE SEARCH RESULT
                    # ----------------------------------------

                    if video_id in seen_ids:

                        logger.debug(
                            f"⏭️ Skipping duplicate "
                            f"search result: {video_id}"
                        )

                        continue

                    seen_ids.add(
                        video_id
                    )

                    # ----------------------------------------
                    # VIDEO INFO
                    # ----------------------------------------

                    duration = data.get(
                        "duration"
                    )

                    is_live = (
                        duration is None
                        or duration == "LIVE"
                    )

                    thumbnails = data.get(
                        "thumbnails",
                        [],
                    )

                    thumbnail_url = ""

                    if thumbnails:

                        thumbnail_url = (
                            thumbnails[-1]
                            .get(
                                "url",
                                "",
                            )
                            .split("?")[0]
                        )

                    # ----------------------------------------
                    # TRACK
                    # ----------------------------------------

                    track = Track(
                        id=video_id,

                        channel_name=data.get(
                            "channel",
                            {},
                        ).get(
                            "name",
                            "",
                        ),

                        duration=(
                            duration
                            if not is_live
                            else "LIVE"
                        ),

                        duration_sec=(
                            0
                            if is_live
                            else utils.to_seconds(
                                duration
                            )
                        ),

                        message_id=m_id,

                        title=data.get(
                            "title",
                            "Unknown",
                        )[:25],

                        thumbnail=thumbnail_url,

                        url=data.get(
                            "link",
                            self.base + video_id,
                        ),

                        view_count=data.get(
                            "viewCount",
                            {},
                        ).get(
                            "short",
                            "",
                        ),

                        is_live=is_live,
                    )

                    track.file_path = None
                    track.time = 0
                    track.video = False

                    tracks.append(
                        track
                    )

                except Exception as e:

                    logger.debug(
                        f"Skipping invalid "
                        f"search result: {e}"
                    )

                    continue

            logger.info(
                f"🔎 YouTube multi-search "
                f"returned {len(tracks)} unique results"
            )

            return tracks

        except Exception as e:

            logger.warning(
                f"⚠️ Multi-search failed "
                f"for '{query}': {e}"
            )

            return []

    # ========================================================
    # PLAYLIST
    # ========================================================

    async def playlist(
        self,
        limit: int,
        user: str,
        url: str,
    ) -> list[Track]:
        """Extract tracks from a YouTube playlist."""

        try:

            plist = await Playlist.get(
                url
            )

            tracks = []

            if (
                not plist
                or "videos" not in plist
                or not plist["videos"]
            ):
                return []

            for data in plist["videos"][:limit]:

                try:

                    thumbnails = data.get(
                        "thumbnails",
                        [],
                    )

                    thumbnail_url = (
                        thumbnails[-1]
                        .get(
                            "url",
                            "",
                        )
                        .split("?")[0]
                        if thumbnails
                        else ""
                    )

                    link = (
                        data.get(
                            "link",
                            "",
                        )
                        .split("&list=")[0]
                    )

                    track = Track(
                        id=data.get(
                            "id",
                            "",
                        ),

                        channel_name=data.get(
                            "channel",
                            {},
                        ).get(
                            "name",
                            "",
                        ),

                        duration=data.get(
                            "duration",
                            "0:00",
                        ),

                        duration_sec=utils.to_seconds(
                            data.get(
                                "duration",
                                "0:00",
                            )
                        ),

                        title=data.get(
                            "title",
                            "Unknown",
                        )[:25],

                        thumbnail=thumbnail_url,

                        url=link,

                        user=user,

                        view_count="",
                    )

                    tracks.append(
                        track
                    )

                except Exception:

                    continue

            return tracks

        except Exception as e:

            logger.error(
                f"Playlist error: {e}"
            )

            return []

    # ========================================================
    # DOWNLOAD
    # ========================================================

    async def download(
        self,
        video_id: str,
        is_live: bool = False,
        video: bool = False,
    ) -> str | None:
        """
        Download YouTube media directly
        using the local downloader.

        Returns:
            Local filesystem path.
        """

        try:

            youtube_url = (
                self.base + video_id
            )

            logger.info(
                f"🚀 Starting direct "
                f"YouTube download: {video_id}"
            )

            if video:

                file_path = (
                    await download_video(
                        youtube_url
                    )
                )

            else:

                file_path = (
                    await download_audio(
                        youtube_url
                    )
                )

            if file_path:

                logger.info(
                    f"✅ Download completed: "
                    f"{file_path}"
                )

                return file_path

            logger.error(
                f"❌ Downloader returned "
                f"no file for: {video_id}"
            )

            return None

        except Exception as e:

            logger.error(
                f"❌ YouTube download failed "
                f"for {video_id}: {e}",
                exc_info=True,
            )

            return None