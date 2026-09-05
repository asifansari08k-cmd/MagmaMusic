import re
import asyncio
from dataclasses import replace
from typing import Union

import aiohttp
from pyrogram import enums, types
from py_yt import VideosSearch, Playlist

from Anysnap import logger
from Anysnap.helpers import Track, utils
from Anysnap.core.downloader import (
    download_audio,
    download_video,
)


class YouTube:

    def __init__(self):
        """Initialize Anysnap YouTube handler."""

        self.base = (
            "https://www.youtube.com/watch?v="
        )

        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|live/|embed/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )

        # ----------------------------------------------------
        # Search cache
        # ----------------------------------------------------

        self.search_cache = {}

        logger.info("=" * 50)
        logger.info(
            "📹 Anysnap YouTube Handler Initialized"
        )
        logger.info(
            "⚡ Mode: Direct yt-dlp Downloader"
        )
        logger.info(
            "🤖 Mode: Related/Up-Next Autoplay"
        )
        logger.info("=" * 50)

    # ========================================================
    # VALIDATE YOUTUBE URL
    # ========================================================

    def valid(
        self,
        url: str,
    ) -> bool:

        """Check if URL is a valid YouTube URL."""

        if not url:
            return False

        return bool(
            re.match(
                self.regex,
                url,
            )
        )

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
            messages.append(
                message_1.reply_to_message
            )

        for message in messages:

            text = (
                message.text
                or message.caption
                or ""
            )

            # ------------------------------------------------
            # Normal URL entity
            # ------------------------------------------------

            if message.entities:

                for entity in message.entities:

                    if (
                        entity.type
                        == enums.MessageEntityType.URL
                    ):

                        link = text[
                            entity.offset:
                            entity.offset
                            + entity.length
                        ]

                        break

            # ------------------------------------------------
            # Text link
            # ------------------------------------------------

            if (
                not link
                and message.caption_entities
            ):

                for entity in (
                    message.caption_entities
                ):

                    if (
                        entity.type
                        == enums.MessageEntityType.TEXT_LINK
                    ):

                        link = entity.url

                        break

            if link:
                break

        if link:

            return (
                link
                .split("&si")[0]
                .split("?si")[0]
            )

        return None

    # ========================================================
    # CREATE TRACK FROM SEARCH DATA
    # ========================================================

    def _track_from_data(
        self,
        data: dict,
        m_id: int = 0,
        truncate_title: bool = False,
    ) -> Track | None:

        try:

            video_id = data.get(
                "id"
            )

            if not video_id:
                return None

            # ------------------------------------------------
            # Duration
            # ------------------------------------------------

            duration = data.get(
                "duration"
            )

            is_live = (
                duration is None
                or str(duration).upper()
                == "LIVE"
            )

            if is_live:

                duration_text = "LIVE"
                duration_sec = 0

            else:

                duration_text = (
                    duration
                    or "0:00"
                )

                try:

                    duration_sec = (
                        utils.to_seconds(
                            duration_text
                        )
                    )

                except Exception:

                    duration_sec = 0

            # ------------------------------------------------
            # Channel
            # ------------------------------------------------

            channel = data.get(
                "channel",
                {},
            )

            if isinstance(
                channel,
                dict,
            ):

                channel_name = (
                    channel.get(
                        "name",
                        "",
                    )
                    or ""
                )

            else:

                channel_name = str(
                    channel
                    or ""
                )

            # ------------------------------------------------
            # Title
            # ------------------------------------------------

            title = (
                data.get(
                    "title",
                    "Unknown",
                )
                or "Unknown"
            )

            if truncate_title:

                title = title[:25]

            # ------------------------------------------------
            # Thumbnail
            # ------------------------------------------------

            thumbnails = data.get(
                "thumbnails",
                [],
            )

            thumbnail_url = ""

            if thumbnails:

                try:

                    thumbnail_url = (
                        thumbnails[-1]
                        .get(
                            "url",
                            "",
                        )
                        .split("?")[0]
                    )

                except Exception:

                    thumbnail_url = ""

            # ------------------------------------------------
            # URL
            # ------------------------------------------------

            video_url = (
                data.get(
                    "link"
                )
                or (
                    self.base
                    + video_id
                )
            )

            # ------------------------------------------------
            # Views
            # ------------------------------------------------

            view_count = ""

            view_data = data.get(
                "viewCount",
                {},
            )

            if isinstance(
                view_data,
                dict,
            ):

                view_count = (
                    view_data.get(
                        "short",
                        "",
                    )
                    or ""
                )

            elif view_data:

                view_count = str(
                    view_data
                )

            # ------------------------------------------------
            # Track
            # ------------------------------------------------

            track = Track(
                id=video_id,
                channel_name=channel_name,
                duration=duration_text,
                duration_sec=duration_sec,
                message_id=m_id,
                title=title,
                thumbnail=thumbnail_url,
                url=video_url,
                view_count=view_count,
                is_live=is_live,
            )

            track.file_path = None
            track.time = 0
            track.video = False

            return track

        except Exception as e:

            logger.debug(
                f"Track conversion failed: {e}"
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
        Returns first/best result.
        """

        cache_key = query.strip()

        current_time = (
            asyncio.get_running_loop().time()
        )

        # ----------------------------------------------------
        # CACHE
        # ----------------------------------------------------

        if cache_key in self.search_cache:

            cached_result, cache_timestamp = (
                self.search_cache[
                    cache_key
                ]
            )

            if (
                current_time
                - cache_timestamp
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
                cache_key,
                limit=1,
            )

            results = await _search.next()

            if (
                results
                and results.get("result")
            ):

                data = (
                    results["result"][0]
                )

                track = (
                    self._track_from_data(
                        data,
                        m_id=m_id,
                        truncate_title=True,
                    )
                )

                if not track:
                    return None

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

                if (
                    len(
                        self.search_cache
                    )
                    > 100
                ):

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
    # MULTI SEARCH
    # ========================================================

    async def search_all(
        self,
        query: str,
        m_id: int = 0,
        limit: int = 5,
        exclude_ids: set[str] | None = None,
    ) -> list[Track]:

        """
        Search multiple YouTube results.

        Used as autoplay fallback.

        Titles are NOT truncated here.
        """

        try:

            query = (
                query
                or ""
            ).strip()

            if not query:
                return []

            logger.info(
                f"🔎 YouTube multi-search: "
                f"query={query}, "
                f"limit={limit}"
            )

            excluded = set(
                exclude_ids or set()
            )

            _search = VideosSearch(
                query,
                limit=limit,
            )

            results = await _search.next()

            if not results:
                return []

            raw_results = results.get(
                "result",
                [],
            )

            if not raw_results:
                return []

            tracks = []
            seen_ids: set[str] = set()

            for data in raw_results:

                try:

                    video_id = data.get(
                        "id"
                    )

                    if not video_id:
                        continue

                    if video_id in excluded:
                        continue

                    if video_id in seen_ids:
                        continue

                    seen_ids.add(
                        video_id
                    )

                    track = (
                        self._track_from_data(
                            data,
                            m_id=m_id,
                            truncate_title=False,
                        )
                    )

                    if not track:
                        continue

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
                f"returned {len(tracks)} "
                f"unique results"
            )

            return tracks

        except Exception as e:

            logger.warning(
                f"⚠️ Multi-search failed "
                f"for '{query}': {e}"
            )

            return []

    # ========================================================
    # YOUTUBE RELATED / UP NEXT
    # ========================================================

    async def related(
        self,
        video_id: str,
        limit: int = 10,
    ) -> list[Track]:

        """
        Get YouTube Related / Up Next videos.

        Used by autoplay.
        """

        if not video_id:
            return []

        video_id = str(
            video_id
        ).strip()

        if not video_id:
            return []

        logger.info(
            f"🤖 Fetching YouTube Related / "
            f"Up Next: {video_id}"
        )

        watch_url = (
            self.base
            + video_id
        )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
            "Accept-Language":
                "en-US,en;q=0.9",
            "Accept":
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8",
        }

        timeout = aiohttp.ClientTimeout(
            total=20
        )

        try:

            async with aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
            ) as session:

                # ============================================
                # WATCH PAGE
                # ============================================

                async with session.get(
                    watch_url,
                    allow_redirects=True,
                ) as response:

                    if response.status != 200:

                        logger.warning(
                            f"🤖 YouTube watch page "
                            f"returned {response.status}"
                        )

                        return []

                    webpage = (
                        await response.text()
                    )

                # ============================================
                # API KEY
                # ============================================

                api_key_match = re.search(
                    r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"',
                    webpage,
                )

                if not api_key_match:

                    logger.warning(
                        "🤖 INNERTUBE_API_KEY "
                        "not found"
                    )

                    return []

                api_key = (
                    api_key_match.group(1)
                )

                # ============================================
                # CLIENT VERSION
                # ============================================

                client_version_match = re.search(
                    r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"',
                    webpage,
                )

                client_version = (
                    client_version_match.group(1)
                    if client_version_match
                    else "2.20260904.01.00"
                )

                # ============================================
                # NEXT ENDPOINT
                # ============================================

                next_url = (
                    "https://www.youtube.com/"
                    "youtubei/v1/next?key="
                    + api_key
                )

                payload = {
                    "context": {
                        "client": {
                            "hl": "en",
                            "gl": "IN",
                            "clientName": "WEB",
                            "clientVersion":
                                client_version,
                        }
                    },
                    "videoId":
                        video_id,
                }

                async with session.post(
                    next_url,
                    json=payload,
                    headers={
                        "Content-Type":
                            "application/json",
                        "Origin":
                            "https://www.youtube.com",
                        "Referer":
                            watch_url,
                    },
                ) as response:

                    if response.status != 200:

                        logger.warning(
                            f"🤖 YouTube /next "
                            f"returned {response.status}"
                        )

                        return []

                    data = (
                        await response.json(
                            content_type=None
                        )
                    )

        except asyncio.TimeoutError:

            logger.warning(
                f"⏱️ YouTube Related "
                f"request timed out: "
                f"{video_id}"
            )

            return []

        except Exception as e:

            logger.warning(
                f"🤖 Related request "
                f"failed for {video_id}: {e}"
            )

            return []

        # ====================================================
        # FIND SECONDARY RESULTS
        # ====================================================

        secondary = []

        try:

            contents = (
                data.get(
                    "contents",
                    {}
                )
            )

            two_column = (
                contents.get(
                    "twoColumnWatchNextResults",
                    {}
                )
            )

            secondary_data = (
                two_column.get(
                    "secondaryResults",
                    {}
                )
            )

            if isinstance(
                secondary_data,
                dict,
            ):

                nested = (
                    secondary_data.get(
                        "secondaryResults",
                        {}
                    )
                )

                if isinstance(
                    nested,
                    dict,
                ):

                    secondary = (
                        nested.get(
                            "results",
                            []
                        )
                        or []
                    )

                if not secondary:

                    secondary = (
                        secondary_data.get(
                            "results",
                            []
                        )
                        or []
                    )

        except Exception as e:

            logger.debug(
                f"🤖 Related structure "
                f"parse failed: {e}"
            )

        # ====================================================
        # FALLBACK RECURSIVE SEARCH
        # ====================================================

        if not secondary:

            def find_video_renderers(
                obj,
            ):

                found = []

                if isinstance(
                    obj,
                    dict,
                ):

                    if (
                        "compactVideoRenderer"
                        in obj
                    ):

                        found.append(
                            obj
                        )

                    elif (
                        "videoRenderer"
                        in obj
                    ):

                        found.append(
                            obj
                        )

                    elif (
                        "compactAutoplayRenderer"
                        in obj
                    ):

                        found.append(
                            obj
                        )

                    for value in obj.values():

                        found.extend(
                            find_video_renderers(
                                value
                            )
                        )

                elif isinstance(
                    obj,
                    list,
                ):

                    for value in obj:

                        found.extend(
                            find_video_renderers(
                                value
                            )
                        )

                return found

            secondary = (
                find_video_renderers(
                    data
                )
            )

        if not secondary:

            logger.warning(
                f"🤖 No YouTube Related / "
                f"Up Next results found "
                f"for {video_id}"
            )

            return []

        # ====================================================
        # CONVERT RESULTS
        # ====================================================

        tracks = []
        seen_ids = set()

        for item in secondary:

            if len(tracks) >= limit:
                break

            try:

                if not isinstance(
                    item,
                    dict,
                ):
                    continue

                renderer = (
                    item.get(
                        "compactVideoRenderer"
                    )
                    or item.get(
                        "videoRenderer"
                    )
                    or item.get(
                        "compactAutoplayRenderer"
                    )
                )

                if not renderer:
                    continue

                related_id = (
                    renderer.get(
                        "videoId"
                    )
                )

                if not related_id:
                    continue

                # ------------------------------------------------
                # CURRENT VIDEO
                # ------------------------------------------------

                if related_id == video_id:
                    continue

                # ------------------------------------------------
                # DUPLICATE
                # ------------------------------------------------

                if related_id in seen_ids:
                    continue

                seen_ids.add(
                    related_id
                )

                # ------------------------------------------------
                # TITLE
                # ------------------------------------------------

                title_data = (
                    renderer.get(
                        "title",
                        {}
                    )
                    or {}
                )

                title = ""

                if title_data.get(
                    "simpleText"
                ):

                    title = (
                        title_data[
                            "simpleText"
                        ]
                    )

                elif title_data.get(
                    "runs"
                ):

                    title = "".join(
                        run.get(
                            "text",
                            "",
                        )
                        for run in title_data[
                            "runs"
                        ]
                    )

                title = (
                    title
                    or ""
                ).strip()

                if not title:
                    continue

                # ------------------------------------------------
                # DURATION
                # ------------------------------------------------

                duration_text = ""

                length_data = (
                    renderer.get(
                        "lengthText",
                        {}
                    )
                    or {}
                )

                if length_data.get(
                    "simpleText"
                ):

                    duration_text = (
                        length_data[
                            "simpleText"
                        ]
                    )

                # ------------------------------------------------
                # LIVE
                # ------------------------------------------------

                is_live = False

                badges = (
                    renderer.get(
                        "badges",
                        []
                    )
                    or []
                )

                for badge in badges:

                    badge_renderer = (
                        badge.get(
                            "metadataBadgeRenderer",
                            {}
                        )
                        or {}
                    )

                    badge_label = (
                        badge_renderer.get(
                            "label",
                            ""
                        )
                        or ""
                    )

                    if (
                        "LIVE"
                        in badge_label.upper()
                    ):

                        is_live = True
                        break

                if (
                    duration_text
                    and duration_text.upper()
                    == "LIVE"
                ):

                    is_live = True

                if is_live:

                    duration_text = "LIVE"
                    duration_sec = 0

                else:

                    duration_text = (
                        duration_text
                        or "0:00"
                    )

                    duration_sec = (
                        self._duration_to_seconds(
                            duration_text
                        )
                    )

                # ------------------------------------------------
                # CHANNEL
                # ------------------------------------------------

                channel_name = ""

                byline = (
                    renderer.get(
                        "shortBylineText",
                        {}
                    )
                    or renderer.get(
                        "longBylineText",
                        {}
                    )
                    or {}
                )

                runs = (
                    byline.get(
                        "runs",
                        []
                    )
                    or []
                )

                if runs:

                    channel_name = (
                        runs[0].get(
                            "text",
                            ""
                        )
                        or ""
                    )

                # ------------------------------------------------
                # THUMBNAIL
                # ------------------------------------------------

                thumbnail_url = ""

                thumbnail_data = (
                    renderer.get(
                        "thumbnail",
                        {}
                    )
                    or {}
                )

                thumbnails = (
                    thumbnail_data.get(
                        "thumbnails",
                        []
                    )
                    or []
                )

                if thumbnails:

                    thumbnail_url = (
                        thumbnails[-1]
                        .get(
                            "url",
                            "",
                        )
                        or ""
                    ).split("?")[0]

                # ------------------------------------------------
                # VIEW COUNT
                # ------------------------------------------------

                view_count = ""

                view_data = (
                    renderer.get(
                        "viewCountText",
                        {}
                    )
                    or {}
                )

                if view_data.get(
                    "simpleText"
                ):

                    view_count = (
                        view_data[
                            "simpleText"
                        ]
                    )

                elif view_data.get(
                    "runs"
                ):

                    view_count = "".join(
                        run.get(
                            "text",
                            "",
                        )
                        for run in view_data[
                            "runs"
                        ]
                    )

                # ------------------------------------------------
                # CREATE TRACK
                # ------------------------------------------------

                track = Track(
                    id=related_id,
                    channel_name=channel_name,
                    duration=duration_text,
                    duration_sec=duration_sec,
                    message_id=0,
                    title=title,
                    thumbnail=thumbnail_url,
                    url=(
                        self.base
                        + related_id
                    ),
                    view_count=view_count,
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
                    f"🤖 Related item "
                    f"skipped: {e}"
                )

                continue

        logger.info(
            f"🤖 YouTube Related returned "
            f"{len(tracks)} candidates for "
            f"{video_id}"
        )

        return tracks

    # ========================================================
    # DURATION HELPER
    # ========================================================

    def _duration_to_seconds(
        self,
        value: str | None,
    ) -> int:

        if not value:
            return 0

        try:

            value = str(
                value
            ).strip()

            if not value:
                return 0

            parts = [
                int(x)
                for x in value.split(":")
            ]

            if len(parts) == 3:

                return (
                    parts[0] * 3600
                    + parts[1] * 60
                    + parts[2]
                )

            if len(parts) == 2:

                return (
                    parts[0] * 60
                    + parts[1]
                )

            if len(parts) == 1:
                return parts[0]

        except Exception:
            pass

        return 0

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

            for data in plist[
                "videos"
            ][:limit]:

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

                    duration = (
                        data.get(
                            "duration",
                            "0:00",
                        )
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

                        duration=duration,

                        duration_sec=(
                            self._duration_to_seconds(
                                duration
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

                    track.file_path = None
                    track.time = 0
                    track.video = False

                    tracks.append(
                        track
                    )

                except Exception as e:

                    logger.debug(
                        f"Playlist item skipped: "
                        f"{e}"
                    )

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
        using local yt-dlp downloader.

        Returns:
            Local filesystem path.
        """

        try:

            if not video_id:
                return None

            youtube_url = (
                self.base
                + video_id
            )

            logger.info(
                f"🚀 Starting direct "
                f"YouTube download: "
                f"{video_id}"
            )

            # ------------------------------------------------
            # VIDEO
            # ------------------------------------------------

            if video:

                file_path = (
                    await download_video(
                        youtube_url
                    )
                )

            # ------------------------------------------------
            # AUDIO
            # ------------------------------------------------

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
                f"no file for: "
                f"{video_id}"
            )

            return None

        except Exception as e:

            logger.error(
                f"❌ YouTube download failed "
                f"for {video_id}: {e}",
                exc_info=True,
            )

            return None