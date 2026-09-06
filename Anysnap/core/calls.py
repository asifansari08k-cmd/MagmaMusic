import asyncio
import logging
import re

from difflib import SequenceMatcher

from ntgcalls import ConnectionNotFound, TelegramServerError
from pyrogram import enums, errors
from pyrogram.types import InputMediaPhoto, Message
from pytgcalls import PyTgCalls, exceptions, types
from pytgcalls.pytgcalls_session import PyTgCallsSession

from Anysnap import (
    app,
    config,
    db,
    lang,
    logger,
    preload,
    queue,
    userbot,
    yt,
)
from Anysnap.helpers import Media, Track, buttons, thumb


# ============================================================
# SUPPRESS HARMLESS PYTGCALLS ERRORS
# ============================================================

class PyTgCallsErrorFilter(logging.Filter):

    def filter(self, record):

        message = record.getMessage()

        if "UpdateGroupCall" in message:
            return False

        if (
            "Connection with chat id" in message
            and "not found" in message
        ):
            return False

        return True


logging.getLogger(
    "pyrogram.dispatcher"
).addFilter(
    PyTgCallsErrorFilter()
)


# ============================================================
# TG CALL
# ============================================================

class TgCall(PyTgCalls):

    def __init__(self):

        self.clients = []

        # Prevent duplicate play_next calls
        self._play_next_locks = {}
        
        # Track active download tasks to allow independent /skip cancellation
        self._download_tasks = {}

        # Prevent duplicate StreamEnded events
        self._stream_end_cache = {}

        # Currently playing track for autoplay
        self._autoplay_current = {}

        # ====================================================
        # AUTOPLAY HISTORY
        # ====================================================

        # chat_id -> recently played YouTube IDs
        self._autoplay_history = {}
        self._autoplay_history_limit = 10

        # ====================================================
        # AUTOPLAY TITLE HISTORY
        # ====================================================

        self._autoplay_title_history = {}
        self._autoplay_title_history_limit = 10

    # ========================================================
    # AUTOPLAY STATUS
    # ========================================================

    async def _get_autoplay_status(
        self,
        chat_id: int,
    ) -> bool:

        try:
            if await db.get_autoplay(chat_id):
                return True
        except Exception as e:
            logger.warning(f"Autoplay direct check failed for {chat_id}: {e}")

        try:
            chat = await app.get_chat(chat_id)
            if chat.type == enums.ChatType.CHANNEL:
                group_id = await db.get_group_for_channel(chat_id)
                if group_id:
                    if await db.get_autoplay(group_id):
                        return True
        except Exception:
            pass

        try:
            channel_id = await db.get_cmode(chat_id)
            if channel_id:
                if await db.get_autoplay(channel_id):
                    return True
        except Exception:
            pass

        return False

    # ========================================================
    # AUTOPLAY CURRENT TRACK
    # ========================================================

    async def _get_autoplay_current(
        self,
        chat_id: int,
    ):

        media = self._autoplay_current.get(chat_id)
        if media:
            return media

        try:
            chat = await app.get_chat(chat_id)
            if chat.type == enums.ChatType.CHANNEL:
                group_id = await db.get_group_for_channel(chat_id)
                if group_id:
                    return self._autoplay_current.get(group_id)
        except Exception:
            pass

        try:
            channel_id = await db.get_cmode(chat_id)
            if channel_id:
                return self._autoplay_current.get(channel_id)
        except Exception:
            pass

        return None

    # ========================================================
    # AUTOPLAY ID HISTORY
    # ========================================================

    def _get_autoplay_history(
        self,
        chat_id: int,
    ) -> set[str]:
        history = self._autoplay_history.get(chat_id, [])
        return set(history)

    def _remember_autoplay_track(
        self,
        chat_id: int,
        video_id: str | None,
    ) -> None:

        if not video_id:
            return

        history = self._autoplay_history.setdefault(chat_id, [])

        if video_id in history:
            history.remove(video_id)

        history.append(video_id)

        if len(history) > self._autoplay_history_limit:
            del history[:-self._autoplay_history_limit]

    # ========================================================
    # AUTOPLAY TITLE NORMALIZATION
    # ========================================================

    def _normalize_autoplay_title(
        self,
        title: str | None,
    ) -> str:
        
        if not title:
            return ""

        title = str(title).lower()

        title = re.sub(r'\([^)]*\)', ' ', title)
        title = re.sub(r'\[[^\]]*\]', ' ', title)
        title = re.sub(r'\{[^}]*\}', ' ', title)

        title = re.sub(r'[^a-z0-9\s]', ' ', title)

        removable = {
            "official", "music", "video", "audio", "lyrics", "lyric",
            "visualizer", "topic", "hd", "4k", "full", "song", "bass",
            "boosted", "remix", "lofi", "slowed", "reverb", "ft", "feat",
            "featuring", "prod", "with", "ultra", "deep", "dj", "mix",
            "mashup", "cover", "status", "8d", "trend", "trending", "viral",
            "by", "and"
        }

        words = title.split()
        filtered_words = [w for w in words if w not in removable and len(w) > 1]

        filtered_words.sort()
        return " ".join(filtered_words)

    # ========================================================
    # AUTOPLAY TITLE HISTORY
    # ========================================================

    def _get_autoplay_title_history(
        self,
        chat_id: int,
    ) -> set[str]:
        return set(self._autoplay_title_history.get(chat_id, []))

    def _remember_autoplay_title(
        self,
        chat_id: int,
        title: str | None,
    ) -> None:

        normalized = self._normalize_autoplay_title(title)
        if not normalized:
            return

        history = self._autoplay_title_history.setdefault(chat_id, [])

        if normalized in history:
            history.remove(normalized)

        history.append(normalized)

        if len(history) > self._autoplay_title_history_limit:
            del history[:-self._autoplay_title_history_limit]

    # ========================================================
    # SAME SONG DETECTION 
    # ========================================================

    def _is_same_autoplay_song(
        self,
        title: str | None,
        recent_titles: set[str],
    ) -> bool:

        normalized = self._normalize_autoplay_title(title)
        if not normalized:
            return False

        current_words = set(normalized.split())
        if not current_words:
            return False

        if normalized in recent_titles:
            return True

        for old_title in recent_titles:
            old_words = set(old_title.split())
            if not old_words:
                continue
            
            common_words = current_words.intersection(old_words)
            min_len = min(len(current_words), len(old_words))
            
            if min_len > 0:
                match_percentage = len(common_words) / min_len
                if match_percentage >= 0.70:
                    return True

        return False

    # ========================================================
    # CLEAR AUTOPLAY HISTORY
    # ========================================================

    def _clear_autoplay_history(
        self,
        chat_id: int,
    ) -> None:
        self._autoplay_history.pop(chat_id, None)
        self._autoplay_title_history.pop(chat_id, None)

    # ========================================================
    # EDIT/SEND MEDIA
    # ========================================================

    async def _edit_media_with_retry(self, message, media_obj, reply_markup):
        try:
            return await message.edit_media(media=media_obj, reply_markup=reply_markup)
        except errors.FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
            try:
                return await message.edit_media(media=media_obj, reply_markup=reply_markup)
            except Exception:
                return None
        except Exception:
            return None

    async def _send_photo_with_retry(self, chat_id, photo, caption, reply_markup):
        try:
            return await app.send_photo(chat_id=chat_id, photo=photo, caption=caption, reply_markup=reply_markup)
        except errors.FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
            try:
                return await app.send_photo(chat_id=chat_id, photo=photo, caption=caption, reply_markup=reply_markup)
            except Exception:
                return None
        except Exception:
            return None

    # ========================================================
    # PAUSE / RESUME / STOP
    # ========================================================

    async def pause(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        try:
            await client.pause(chat_id)
            await db.playing(chat_id, paused=True)
            return True
        except Exception:
            await db.playing(chat_id, paused=False)
            return False

    async def resume(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        try:
            await client.resume(chat_id)
            await db.playing(chat_id, paused=False)
            return True
        except Exception:
            return False

    async def stop(self, chat_id: int) -> None:
        client = await db.get_assistant(chat_id)
        
        # Free lock explicitly via task cancellation if stuck
        task = self._download_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

        try:
            await preload.cancel_preload(chat_id)
        except Exception:
            pass

        try:
            queue.clear(chat_id)
            await db.remove_call(chat_id)
        except Exception:
            pass

        self._autoplay_current.pop(chat_id, None)
        self._clear_autoplay_history(chat_id)

        try:
            chat = await app.get_chat(chat_id)
            if chat.type == enums.ChatType.CHANNEL:
                group_id = await db.get_group_for_channel(chat_id)
                if group_id:
                    self._autoplay_current.pop(group_id, None)
                    self._clear_autoplay_history(group_id)
            else:
                channel_id = await db.get_cmode(chat_id)
                if channel_id:
                    self._autoplay_current.pop(channel_id, None)
                    self._clear_autoplay_history(channel_id)
        except Exception:
            pass

        try:
            await client.leave_call(chat_id, close=False)
            await asyncio.sleep(0.5)
        except Exception:
            pass

    # ========================================================
    # PLAY MEDIA
    # ========================================================

    async def play_media(self, chat_id: int, message: Message | None, media: Media | Track, seek_time: int = 0, message_chat_id: int = None) -> None:
        client = await db.get_assistant(chat_id)
        _lang = await lang.get_lang(chat_id)
        target_chat_for_messages = message_chat_id if message_chat_id else chat_id

        if config.THUMB_GEN and isinstance(media, Track):
            _thumb = await thumb.generate(media)
        else:
            _thumb = config.DEFAULT_THUMB

        if not media.file_path:
            if message:
                return await message.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            return

        try:
            chat = await app.get_chat(chat_id)
            if chat.type not in [enums.ChatType.SUPERGROUP, enums.ChatType.GROUP, enums.ChatType.CHANNEL]:
                if message:
                    await message.edit_text("❌ Can only play in groups/channels.")
                return

            if chat.type == enums.ChatType.CHANNEL:
                userbot_client = await db.get_client(chat_id)
                if not userbot_client:
                    if message:
                        await message.edit_text("❌ No assistant available.")
                    return
                try:
                    assistant_member = await app.get_chat_member(chat_id, userbot_client.me.id)
                    if assistant_member.status == enums.ChatMemberStatus.BANNED:
                        await db.set_cmode(chat_id, None)
                        if message:
                            await message.edit_text("❌ Assistant is banned in this channel.")
                        return
                except errors.RPCError as e:
                    if "CHANNEL_INVALID" in str(e) or "USER_NOT_PARTICIPANT" in str(e):
                        if message:
                            await message.edit_text(f"❌ Assistant not in channel.")
                        await db.set_cmode(chat_id, None)
                        return

        except errors.RPCError:
            await db.set_cmode(chat_id, None)
            return

        ffmpeg_params = f"-ss {seek_time} -probesize 10M -analyzeduration 5M -rtbufsize 5M -fflags +genpts+igndts" if seek_time > 1 else "-probesize 10M -analyzeduration 5M -rtbufsize 5M -fflags +genpts+igndts -sync ext"

        is_video = getattr(media, "video", False)
        video_flags = types.MediaStream.Flags.AUTO_DETECT if is_video else types.MediaStream.Flags.IGNORE

        stream = types.MediaStream(
            media_path=media.file_path,
            audio_parameters=types.AudioQuality.STUDIO,
            audio_flags=types.MediaStream.Flags.REQUIRED,
            video_flags=video_flags,
            ffmpeg_parameters=ffmpeg_params,
        )

        try:
            if await client.get_call(chat_id):
                await client.leave_call(chat_id, close=False)
        except Exception:
            pass

        max_retries, retry_delay = 3, 1

        try:
            for attempt in range(max_retries):
                try:
                    await client.play(chat_id=chat_id, stream=stream, config=types.GroupCallConfig(auto_start=True))
                    break
                except Exception:
                    if attempt < max_retries - 1:
                        try:
                            await client.leave_call(chat_id, close=False)
                            await asyncio.sleep(retry_delay)
                        except Exception:
                            pass
                        continue
                    raise

            media.time = seek_time if seek_time else 1

            if not seek_time:
                await db.add_call(chat_id)
                self._autoplay_current[chat_id] = media
                self._remember_autoplay_track(chat_id, getattr(media, "id", None))
                self._remember_autoplay_title(chat_id, getattr(media, "title", None))

                try:
                    chat_obj = await app.get_chat(chat_id)
                    if chat_obj.type == enums.ChatType.CHANNEL:
                        group_id = await db.get_group_for_channel(chat_id)
                        if group_id:
                            self._autoplay_current[group_id] = media
                    else:
                        channel_id = await db.get_cmode(chat_id)
                        if channel_id:
                            self._autoplay_current[channel_id] = media
                except Exception:
                    pass

                owner_name = getattr(config, "OWNER_NAME", config.BOT_NAME)
                owner_link = getattr(config, "OWNER_LINK", "https://t.me/ANYSNAP")

                text = _lang["play_media"].format(media.url, media.title, media.duration, media.user, owner_name, owner_link)

                if not media.is_live and media.duration_sec:
                    import time as time_module
                    played, duration, bar_length = media.time, media.duration_sec, 12
                    percentage = min((played / duration) * 100, 100) if duration != 0 else 0
                    filled = int(round(bar_length * percentage / 100))
                    timer_bar = "—" * filled + "●" + "—" * (bar_length - filled)
                    
                    time_fmt = "%H:%M:%S" if duration >= 3600 else "%M:%S"
                    played_time = time_module.strftime(time_fmt, time_module.gmtime(played))
                    total_time = time_module.strftime(time_fmt, time_module.gmtime(duration))
                    
                    timer_text = f"{played_time} {timer_bar} {total_time}"
                    keyboard = buttons.controls(chat_id, timer=timer_text)
                else:
                    keyboard = buttons.controls(chat_id)

                if message:
                    try:
                        await message.delete()
                    except Exception:
                        pass

                sent_photo = await self._send_photo_with_retry(chat_id=target_chat_for_messages, photo=_thumb, caption=text, reply_markup=keyboard)
                if sent_photo:
                    media.message_id = sent_photo.id

                try:
                    asyncio.create_task(preload.start_preload(chat_id, count=2))
                except Exception:
                    pass

        except Exception as e:
            await self.stop(chat_id)

    # ========================================================
    # REPLAY / SEEK
    # ========================================================

    async def replay(self, chat_id: int) -> None:
        try:
            if not await db.get_call(chat_id): return
            media = queue.get_current(chat_id) or await self._get_autoplay_current(chat_id)
            if not media: return
            msg = await app.send_message(chat_id=chat_id, text="Replaying...")
            await self.play_media(chat_id, msg, media)
        except Exception:
            pass

    async def seek_stream(self, chat_id: int, seconds: int) -> bool:
        try:
            if not await db.get_call(chat_id): return False
            media = queue.get_current(chat_id) or await self._get_autoplay_current(chat_id)
            if not media or media.is_live: return False
            media.time = seconds
            await self.play_media(chat_id, None, media, seek_time=seconds)
            return True
        except Exception:
            return False

    # ========================================================
    # PLAY NEXT + AUTOPLAY (WITH SKIP INDEPENDENCE)
    # ========================================================

    async def play_next(self, chat_id: int) -> None:

        if chat_id not in self._play_next_locks:
            self._play_next_locks[chat_id] = asyncio.Lock()
        lock = self._play_next_locks[chat_id]

        # ----------------------------------------------------------------
        # SKIP INDEPENDENCE FIX:
        # If lock is locked, we don't just return. We cancel any ongoing 
        # download task (which frees the lock), then wait to acquire it.
        # ----------------------------------------------------------------
        if lock.locked():
            task = self._download_tasks.get(chat_id)
            if task and not task.done():
                logger.info(f"Cancelling stuck/ongoing download for {chat_id} due to /skip or fast play_next")
                task.cancel()
            else:
                return

        async with lock:
            try:
                if not await db.get_call(chat_id):
                    return

                message_chat_id = None
                try:
                    chat = await app.get_chat(chat_id)
                    if chat.type == enums.ChatType.CHANNEL:
                        group_id = await db.get_group_for_channel(chat_id)
                        if group_id:
                            message_chat_id = group_id
                except Exception:
                    pass

                target_chat = message_chat_id if message_chat_id else chat_id
                loop_mode = await db.get_loop(chat_id)

                if loop_mode == 1:
                    media = queue.get_current(chat_id) or await self._get_autoplay_current(chat_id)
                    if media:
                        _lang = await lang.get_lang(chat_id)
                        try:
                            msg = await app.send_message(chat_id=target_chat, text=_lang["play_again"])
                            await self.play_media(chat_id, msg, media, message_chat_id=message_chat_id)
                        except Exception:
                            pass
                        return

                media = queue.get_next(chat_id)

                if not media and loop_mode == 10:
                    all_items = queue.get_all(chat_id)
                    if all_items:
                        first_track = all_items[0]
                        try:
                            msg = await app.send_message(chat_id=target_chat, text="🔁 Looping queue...")
                            if not first_track.file_path:
                                is_live = getattr(first_track, "is_live", False)
                                
                                # Wrapped Download Call
                                dl_task = asyncio.create_task(yt.download(first_track.id, is_live=is_live, video=getattr(first_track, "video", False)))
                                self._download_tasks[chat_id] = dl_task
                                try:
                                    first_track.file_path = await dl_task
                                except asyncio.CancelledError:
                                    logger.info("Loop download cancelled via Skip")
                                    return
                                finally:
                                    self._download_tasks.pop(chat_id, None)
                                    
                            first_track.message_id = msg.id
                            await self.play_media(chat_id, msg, first_track, message_chat_id=message_chat_id)
                        except Exception:
                            pass
                    return

                if media:
                    try:
                        if media.message_id:
                            delete_chat_id = target_chat if message_chat_id else chat_id
                            await app.delete_messages(chat_id=delete_chat_id, message_ids=media.message_id, revoke=True)
                            media.message_id = 0
                    except Exception:
                        pass

                if not media:
                    autoplay_enabled = await self._get_autoplay_status(chat_id)
                    
                    if autoplay_enabled:
                        try:
                            source_media = await self._get_autoplay_current(chat_id)
                            
                            if source_media:
                                search_query = getattr(source_media, "title", "").strip()
                                current_id = getattr(source_media, "id", None)
                                current_title = getattr(source_media, "title", "")

                                recent_ids = self._get_autoplay_history(chat_id)
                                if current_id:
                                    recent_ids.add(current_id)

                                recent_titles = self._get_autoplay_title_history(chat_id)
                                current_normalized = self._normalize_autoplay_title(current_title)
                                if current_normalized:
                                    recent_titles.add(current_normalized)

                                candidates = []
                                if search_query:
                                    if "-" in search_query:
                                        base_name = search_query.split("-")[0].strip()
                                    elif "|" in search_query:
                                        base_name = search_query.split("|")[0].strip()
                                    else:
                                        norm_sq = self._normalize_autoplay_title(search_query)
                                        sq_words = norm_sq.split()
                                        base_name = " ".join(sq_words[:2]) if sq_words else search_query

                                    search_queries = [
                                        f"{base_name} hit songs",
                                        f"songs similar to {base_name}",
                                        f"{base_name} popular tracks",
                                    ]

                                    seen_search_ids = set()
                                    
                                    try:
                                        if hasattr(yt, "get_related") and current_id:
                                            api_results = await yt.get_related(current_id, limit=7)
                                            if api_results:
                                                for track in api_results:
                                                    track_id = getattr(track, "id", None)
                                                    if track_id and track_id not in seen_search_ids:
                                                        seen_search_ids.add(track_id)
                                                        candidates.append(track)
                                    except Exception:
                                        pass

                                    if not candidates:
                                        for query in search_queries:
                                            try:
                                                results = await yt.search_all(query, m_id=0, limit=6, exclude_ids=recent_ids)
                                                for track in results:
                                                    track_id = getattr(track, "id", None)
                                                    if not track_id or track_id in seen_search_ids:
                                                        continue
                                                    seen_search_ids.add(track_id)
                                                    candidates.append(track)
                                            except Exception:
                                                pass

                                next_track = None
                                for track in candidates:
                                    track_id = getattr(track, "id", None)
                                    track_title = getattr(track, "title", "")

                                    if not track_id or track_id in recent_ids or getattr(track, "is_live", False):
                                        continue

                                    if self._is_same_autoplay_song(track_title, recent_titles):
                                        continue

                                    next_track = track
                                    break

                                if next_track:
                                    is_live = getattr(next_track, "is_live", False)
                                    
                                    # Wrapped Autoplay Download Call
                                    dl_task = asyncio.create_task(yt.download(next_track.id, is_live=is_live, video=getattr(next_track, "video", False)))
                                    self._download_tasks[chat_id] = dl_task
                                    try:
                                        next_track.file_path = await dl_task
                                    except asyncio.CancelledError:
                                        logger.info("Autoplay download cancelled via Skip")
                                        return
                                    finally:
                                        self._download_tasks.pop(chat_id, None)

                                    if next_track.file_path:
                                        queue.force_add(chat_id, next_track)
                                        media = next_track

                        except Exception as e:
                            logger.error(f"Autoplay failed for {chat_id}: {e}")

                if not media:
                    if config.AUTO_END:
                        _lang = await lang.get_lang(chat_id)
                        try:
                            await app.send_message(chat_id=target_chat, text=_lang.get("auto_end", "✅ Queue finished. Stream ended automatically."))
                        except Exception:
                            pass
                    return await self.stop(chat_id)

                _lang = await lang.get_lang(chat_id)
                msg = None

                if not media.file_path:
                    is_live = getattr(media, "is_live", False)
                    
                    # Wrapped Regular Next Track Download Call
                    dl_task = asyncio.create_task(yt.download(media.id, is_live=is_live, video=getattr(media, "video", False)))
                    self._download_tasks[chat_id] = dl_task
                    try:
                        media.file_path = await dl_task
                    except asyncio.CancelledError:
                        logger.info("Next track download cancelled via Skip")
                        return
                    finally:
                        self._download_tasks.pop(chat_id, None)

                    if not media.file_path:
                        await self.stop(chat_id)
                        return

                try:
                    msg = await app.send_message(chat_id=target_chat, text=_lang["play_next"])
                except Exception:
                    msg = None

                media.message_id = msg.id if msg else 0
                await self.play_media(chat_id, msg, media, message_chat_id=message_chat_id)

                try:
                    asyncio.create_task(preload.start_preload(chat_id, count=2))
                except Exception:
                    pass

            except Exception as e:
                logger.error(f"Error in play_next for {chat_id}: {e}")
                try:
                    await self.stop(chat_id)
                except Exception:
                    pass

    # ========================================================
    # PING
    # ========================================================

    async def ping(self) -> float:
        pings = [client.ping for client in self.clients]
        return round(sum(pings) / len(pings), 2) if pings else 0.0

    # ========================================================
    # PYTGCALLS EVENTS
    # ========================================================

    async def decorators(self, client: PyTgCalls) -> None:
        @client.on_update()
        async def update_handler(_, update: types.Update) -> None:
            if isinstance(update, types.StreamEnded):
                chat_id = update.chat_id
                current_time = asyncio.get_event_loop().time()
                if chat_id in self._stream_end_cache and current_time - self._stream_end_cache[chat_id] < 2.0:
                    return
                self._stream_end_cache[chat_id] = current_time
                self._stream_end_cache = {cid: ts for cid, ts in self._stream_end_cache.items() if (current_time - ts < 5.0)}
                await self.play_next(chat_id)

            elif isinstance(update, types.ChatUpdate):
                if update.status in [types.ChatUpdate.Status.KICKED, types.ChatUpdate.Status.LEFT_GROUP, types.ChatUpdate.Status.CLOSED_VOICE_CHAT]:
                    await self.stop(update.chat_id)

    # ========================================================
    # BOOT
    # ========================================================

    async def boot(self) -> None:
        PyTgCallsSession.notice_displayed = True
        for ub in userbot.clients:
            client = PyTgCalls(ub, cache_duration=100)
            await client.start()
            self.clients.append(client)
            await self.decorators(client)
        logger.info("📞 PyTgCalls client(s) started.")