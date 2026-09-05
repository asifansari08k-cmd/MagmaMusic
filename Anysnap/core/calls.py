import asyncio
import logging

from ntgcalls import ConnectionNotFound, TelegramServerError
from pyrogram import enums, errors
from pyrogram.errors import MessageIdInvalid
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

        if "UpdateGroupCall" in record.getMessage():
            return False

        if (
            "Connection with chat id" in record.getMessage()
            and "not found" in record.getMessage()
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

        # Prevent duplicate StreamEnded events
        self._stream_end_cache = {}

    # ========================================================
    # EDIT MEDIA WITH RETRY
    # ========================================================

    async def _edit_media_with_retry(
        self,
        message: Message,
        media_obj: InputMediaPhoto,
        reply_markup,
    ):

        try:

            return await message.edit_media(
                media=media_obj,
                reply_markup=reply_markup,
            )

        except errors.FloodWait as fw:

            await asyncio.sleep(
                fw.value + 1
            )

            try:

                return await message.edit_media(
                    media=media_obj,
                    reply_markup=reply_markup,
                )

            except Exception:

                return None

        except errors.MessageNotModified:

            return None

        except Exception:

            return None

    # ========================================================
    # SEND PHOTO WITH RETRY
    # ========================================================

    async def _send_photo_with_retry(
        self,
        chat_id: int,
        photo,
        caption: str,
        reply_markup,
    ):

        try:

            return await app.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                reply_markup=reply_markup,
            )

        except errors.FloodWait as fw:

            await asyncio.sleep(
                fw.value + 1
            )

            try:

                return await app.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    reply_markup=reply_markup,
                )

            except Exception:

                return None

        except Exception:

            return None

    # ========================================================
    # PAUSE
    # ========================================================

    async def pause(
        self,
        chat_id: int,
    ) -> bool:

        client = await db.get_assistant(
            chat_id
        )

        try:

            await client.pause(
                chat_id
            )

            await db.playing(
                chat_id,
                paused=True,
            )

            return True

        except (
            ConnectionNotFound,
            exceptions.NotInCallError,
        ):

            await db.playing(
                chat_id,
                paused=False,
            )

            await db.remove_call(
                chat_id
            )

            queue.clear(
                chat_id
            )

            logger.warning(
                f"Pause requested but assistant "
                f"not in call for {chat_id}, syncing state"
            )

            return False

        except Exception as e:

            await db.playing(
                chat_id,
                paused=False,
            )

            logger.error(
                f"Pause failed for {chat_id}: {e}"
            )

            return False

    # ========================================================
    # RESUME
    # ========================================================

    async def resume(
        self,
        chat_id: int,
    ) -> bool:

        client = await db.get_assistant(
            chat_id
        )

        try:

            await client.resume(
                chat_id
            )

            await db.playing(
                chat_id,
                paused=False,
            )

            return True

        except (
            ConnectionNotFound,
            exceptions.NotInCallError,
        ):

            await db.playing(
                chat_id,
                paused=False,
            )

            await db.remove_call(
                chat_id
            )

            queue.clear(
                chat_id
            )

            logger.warning(
                f"Resume requested but assistant "
                f"not in call for {chat_id}, syncing state"
            )

            return False

        except Exception as e:

            logger.error(
                f"Resume failed for {chat_id}: {e}"
            )

            return False

    # ========================================================
    # STOP
    # ========================================================

    async def stop(
        self,
        chat_id: int,
    ) -> None:

        client = await db.get_assistant(
            chat_id
        )

        try:

            await preload.cancel_preload(
                chat_id
            )

        except Exception as e:

            logger.debug(
                f"Error cancelling preload "
                f"for {chat_id}: {e}"
            )

        try:

            queue.clear(
                chat_id
            )

            await db.remove_call(
                chat_id
            )

        except Exception as e:

            logger.warning(
                f"Error clearing queue/call "
                f"for {chat_id}: {e}"
            )

        try:

            await client.leave_call(
                chat_id,
                close=False,
            )

            await asyncio.sleep(
                0.5
            )

        except (
            ConnectionNotFound,
            exceptions.NotInCallError,
        ):

            pass

        except Exception as e:

            error_msg = str(e).lower()

            if not any(
                ignore in error_msg
                for ignore in [
                    "not in a call",
                    "not in the group call",
                    "groupcall_forbidden",
                    "no active group call",
                    "call was already stopped",
                    "call already disconnected",
                ]
            ):

                logger.warning(
                    f"Error leaving call "
                    f"for {chat_id}: {e}"
                )

    # ========================================================
    # PLAY MEDIA
    # ========================================================

    async def play_media(
        self,
        chat_id: int,
        message: Message | None,
        media: Media | Track,
        seek_time: int = 0,
        message_chat_id: int = None,
    ) -> None:

        client = await db.get_assistant(
            chat_id
        )

        _lang = await lang.get_lang(
            chat_id
        )

        target_chat_for_messages = (
            message_chat_id
            if message_chat_id
            else chat_id
        )

        # ----------------------------------------------------
        # THUMBNAIL
        # ----------------------------------------------------

        if (
            config.THUMB_GEN
            and isinstance(media, Track)
        ):

            _thumb = await thumb.generate(
                media
            )

        else:

            _thumb = config.DEFAULT_THUMB

        # ----------------------------------------------------
        # FILE CHECK
        # ----------------------------------------------------

        if not media.file_path:

            if message:

                return await message.edit_text(
                    _lang[
                        "error_no_file"
                    ].format(
                        config.SUPPORT_CHAT
                    )
                )

            logger.error(
                f"No file path for media "
                f"in {chat_id}"
            )

            return

        try:

            chat = await app.get_chat(
                chat_id
            )

            # ------------------------------------------------
            # CHAT TYPE
            # ------------------------------------------------

            if chat.type not in [
                enums.ChatType.SUPERGROUP,
                enums.ChatType.GROUP,
                enums.ChatType.CHANNEL,
            ]:

                logger.error(
                    f"Invalid chat type for "
                    f"{chat_id}: {chat.type}"
                )

                if message:

                    await message.edit_text(
                        "❌ Can only play in groups/channels."
                    )

                return

            # ------------------------------------------------
            # CHANNEL SUPPORT
            # ------------------------------------------------

            if chat.type == enums.ChatType.CHANNEL:

                userbot_client = await db.get_client(
                    chat_id
                )

                if not userbot_client:

                    logger.error(
                        f"No userbot client available "
                        f"for {chat_id}"
                    )

                    if message:

                        await message.edit_text(
                            "❌ No assistant available."
                        )

                    return

                try:

                    assistant_member = (
                        await app.get_chat_member(
                            chat_id,
                            userbot_client.me.id,
                        )
                    )

                    if (
                        assistant_member.status
                        == enums.ChatMemberStatus.BANNED
                    ):

                        logger.error(
                            f"Assistant banned in "
                            f"channel {chat_id}"
                        )

                        await db.set_cmode(
                            chat_id,
                            None,
                        )

                        if message:

                            await message.edit_text(
                                "❌ Assistant is banned in this channel."
                            )

                        return

                except errors.RPCError as e:

                    if (
                        "CHANNEL_INVALID"
                        in str(e)
                        or "USER_NOT_PARTICIPANT"
                        in str(e)
                    ):

                        logger.error(
                            f"Assistant not in channel "
                            f"{chat_id}: {e}"
                        )

                        if message:

                            await message.edit_text(
                                "❌ <b>Assistant not in channel!</b>\n\n"
                                f"<blockquote>Please add "
                                f"@{userbot_client.me.username} "
                                "to the channel as admin with "
                                "voice chat permissions.</blockquote>"
                            )

                        await db.set_cmode(
                            chat_id,
                            None,
                        )

                        return

        except errors.RPCError as e:

            if "CHANNEL_INVALID" in str(e):

                logger.error(
                    f"Invalid channel "
                    f"{chat_id}: {e}"
                )

                if message:

                    await message.edit_text(
                        "❌ Invalid channel. "
                        "Disabling channel play."
                    )

                await db.set_cmode(
                    chat_id,
                    None,
                )

                return

            raise

        # ====================================================
        # FFMPEG PARAMETERS
        # ====================================================

        if seek_time > 1:

            ffmpeg_params = (
                f"-ss {seek_time} "
                "-probesize 10M "
                "-analyzeduration 5M "
                "-rtbufsize 5M "
                "-fflags +genpts+igndts"
            )

        else:

            ffmpeg_params = (
                "-probesize 10M "
                "-analyzeduration 5M "
                "-rtbufsize 5M "
                "-fflags +genpts+igndts "
                "-sync ext"
            )

        # ====================================================
        # VIDEO / AUDIO
        # ====================================================

        is_video = getattr(
            media,
            "video",
            False,
        )

        video_flags = (
            types.MediaStream.Flags.AUTO_DETECT
            if is_video
            else types.MediaStream.Flags.IGNORE
        )

        stream = types.MediaStream(
            media_path=media.file_path,
            audio_parameters=types.AudioQuality.STUDIO,
            audio_flags=types.MediaStream.Flags.REQUIRED,
            video_flags=video_flags,
            ffmpeg_parameters=ffmpeg_params,
        )

        # ====================================================
        # CHECK EXISTING CALL
        # ====================================================

        try:

            call = await client.get_call(
                chat_id
            )

            if call:

                logger.debug(
                    f"Already connected to {chat_id}, "
                    "leaving before reconnecting..."
                )

                await client.leave_call(
                    chat_id,
                    close=False,
                )

        except (
            ConnectionNotFound,
            exceptions.NotInCallError,
        ):

            pass

        except Exception as e:

            logger.debug(
                f"Error checking connection state "
                f"for {chat_id}: {e}"
            )

        # ====================================================
        # PLAY RETRIES
        # ====================================================

        max_retries = 3
        retry_delay = 1

        try:

            for attempt in range(
                max_retries
            ):

                try:

                    await client.play(
                        chat_id=chat_id,
                        stream=stream,
                        config=types.GroupCallConfig(
                            auto_start=True
                        ),
                    )

                    break

                except (
                    exceptions.NoActiveGroupCall,
                    errors.RPCError,
                ) as e:

                    error_msg = str(e)

                    if (
                        "GROUPCALL_INVALID"
                        in error_msg
                        or "GROUPCALL"
                        in error_msg
                        or isinstance(
                            e,
                            exceptions.NoActiveGroupCall,
                        )
                    ):

                        if (
                            attempt
                            < max_retries - 1
                        ):

                            logger.debug(
                                f"Group call transitioning "
                                f"for {chat_id}, "
                                f"retrying in "
                                f"{retry_delay}s..."
                            )

                            await asyncio.sleep(
                                retry_delay
                            )

                            continue

                        raise

                    raise

                except Exception as e:

                    error_msg = str(e).lower()

                    if (
                        "cannot be initialized "
                        "more than once"
                        in error_msg
                        or "connection"
                        in error_msg
                    ):

                        if (
                            attempt
                            < max_retries - 1
                        ):

                            logger.debug(
                                f"Connection error "
                                f"for {chat_id}, "
                                "leaving and retrying..."
                            )

                            try:

                                await client.leave_call(
                                    chat_id,
                                    close=False,
                                )

                                await asyncio.sleep(
                                    retry_delay
                                )

                            except Exception:

                                pass

                            continue

                        raise

                    raise

            # =================================================
            # SET MEDIA TIME
            # =================================================

            if seek_time:

                media.time = seek_time

            else:

                media.time = 1

            # =================================================
            # NORMAL PLAYBACK
            # =================================================

            if not seek_time:

                await db.add_call(
                    chat_id
                )

                owner_name = getattr(
                    config,
                    "OWNER_NAME",
                    config.BOT_NAME,
                )

                owner_link = getattr(
                    config,
                    "OWNER_LINK",
                    "https://t.me/ANYSNAP",
                )

                text = _lang[
                    "play_media"
                ].format(
                    media.url,
                    media.title,
                    media.duration,
                    media.user,
                    owner_name,
                    owner_link,
                )

                # =============================================
                # PROGRESS BAR
                # =============================================

                if (
                    not media.is_live
                    and media.duration_sec
                ):

                    import time as time_module

                    played = media.time
                    duration = media.duration_sec

                    bar_length = 12

                    percentage = (
                        min(
                            (played / duration)
                            * 100,
                            100,
                        )
                        if duration != 0
                        else 0
                    )

                    filled = int(
                        round(
                            bar_length
                            * percentage
                            / 100
                        )
                    )

                    timer_bar = (
                        "—" * filled
                        + "●"
                        + "—" * (
                            bar_length
                            - filled
                        )
                    )

                    if duration >= 3600:

                        played_time = (
                            time_module.strftime(
                                "%H:%M:%S",
                                time_module.gmtime(
                                    played
                                ),
                            )
                        )

                        total_time = (
                            time_module.strftime(
                                "%H:%M:%S",
                                time_module.gmtime(
                                    duration
                                ),
                            )
                        )

                    else:

                        played_time = (
                            time_module.strftime(
                                "%M:%S",
                                time_module.gmtime(
                                    played
                                ),
                            )
                        )

                        total_time = (
                            time_module.strftime(
                                "%M:%S",
                                time_module.gmtime(
                                    duration
                                ),
                            )
                        )

                    timer_text = (
                        f"{played_time} "
                        f"{timer_bar} "
                        f"{total_time}"
                    )

                    keyboard = buttons.controls(
                        chat_id,
                        timer=timer_text,
                    )

                else:

                    keyboard = buttons.controls(
                        chat_id
                    )

                # =============================================
                # DELETE REQUEST MESSAGE
                # =============================================

                if message:

                    try:

                        await message.delete()

                    except Exception:

                        pass

                # =============================================
                # SEND PLAYING MESSAGE
                # =============================================

                sent_photo = (
                    await self._send_photo_with_retry(
                        chat_id=target_chat_for_messages,
                        photo=_thumb,
                        caption=text,
                        reply_markup=keyboard,
                    )
                )

                if sent_photo:

                    media.message_id = (
                        sent_photo.id
                    )

                # =============================================
                # PRELOAD
                # =============================================

                try:

                    asyncio.create_task(
                        preload.start_preload(
                            chat_id,
                            count=2,
                        )
                    )

                except Exception as e:

                    logger.debug(
                        f"Error starting preload "
                        f"for {chat_id}: {e}"
                    )

        # ====================================================
        # FILE NOT FOUND
        # ====================================================

        except FileNotFoundError:

            if message:

                try:

                    await message.edit_text(
                        _lang[
                            "error_no_file"
                        ].format(
                            config.SUPPORT_CHAT
                        )
                    )

                except Exception:

                    pass

            await self.play_next(
                chat_id
            )

        # ====================================================
        # NO ACTIVE GROUP CALL
        # ====================================================

        except exceptions.NoActiveGroupCall:

            await self.stop(
                chat_id
            )

            if message:

                try:

                    await message.edit_text(
                        _lang[
                            "error_vc_disabled"
                        ]
                    )

                except Exception:

                    pass

        # ====================================================
        # RPC ERROR
        # ====================================================

        except errors.RPCError as e:

            error_str = str(e)

            if any(
                x in error_str
                for x in [
                    "CHAT_ADMIN_REQUIRED",
                    "phone.CreateGroupCall",
                    "GROUPCALL_FORBIDDEN",
                    "GROUPCALL_CREATE_FORBIDDEN",
                    "VOICE_MESSAGES_FORBIDDEN",
                ]
            ):

                await self.stop(
                    chat_id
                )

                if message:

                    try:

                        await message.edit_text(
                            _lang[
                                "error_vc_disabled"
                            ]
                        )

                    except Exception:

                        pass

            elif (
                "GROUPCALL_INVALID"
                in error_str
                or "GROUPCALL"
                in error_str
            ):

                await self.stop(
                    chat_id
                )

                if message:

                    try:

                        await message.edit_text(
                            _lang[
                                "error_no_call"
                            ]
                        )

                    except Exception:

                        pass

            else:

                logger.error(
                    f"RPC error in play_media "
                    f"for {chat_id}: {e}"
                )

                await self.stop(
                    chat_id
                )

        # ====================================================
        # NO AUDIO SOURCE
        # ====================================================

        except exceptions.NoAudioSourceFound:

            if message:

                try:

                    await message.edit_text(
                        _lang[
                            "error_no_audio"
                        ]
                    )

                except Exception:

                    pass

            await self.play_next(
                chat_id
            )

        # ====================================================
        # CONNECTION ERROR
        # ====================================================

        except (
            ConnectionNotFound,
            TelegramServerError,
        ):

            await self.stop(
                chat_id
            )

            if message:

                try:

                    await message.edit_text(
                        _lang[
                            "error_tg_server"
                        ]
                    )

                except Exception:

                    pass

        # ====================================================
        # TIMEOUT
        # ====================================================

        except TimeoutError as e:

            logger.warning(
                f"⏱️ Timeout joining voice chat "
                f"{chat_id}: {str(e)}"
            )

            await self.stop(
                chat_id
            )

            if message:

                try:

                    await message.edit_text(
                        "⏱️ <b>Connection timed out!</b>\n\n"
                        "<blockquote>Failed to join voice chat. "
                        "Please check your network and try again."
                        "</blockquote>"
                    )

                except Exception:

                    pass

            await asyncio.sleep(
                2
            )

            await self.play_next(
                chat_id
            )

        # ====================================================
        # UNKNOWN ERROR
        # ====================================================

        except Exception as e:

            logger.error(
                f"Unexpected error in play_media "
                f"for {chat_id}: {e}",
                exc_info=True,
            )

            await self.stop(
                chat_id
            )

            if message:

                try:

                    await message.edit_text(
                        f"❌ Playback error: "
                        f"{str(e)[:100]}"
                    )

                except Exception:

                    pass

    # ========================================================
    # REPLAY
    # ========================================================

    async def replay(
        self,
        chat_id: int,
    ) -> None:

        try:

            if not await db.get_call(
                chat_id
            ):

                return

            message_chat_id = None

            try:

                chat = await app.get_chat(
                    chat_id
                )

                if (
                    chat.type
                    == enums.ChatType.CHANNEL
                ):

                    group_id = (
                        await db.get_group_for_channel(
                            chat_id
                        )
                    )

                    if group_id:

                        message_chat_id = (
                            group_id
                        )

            except Exception:

                pass

            media = queue.get_current(
                chat_id
            )

            if not media:

                return

            _lang = await lang.get_lang(
                chat_id
            )

            target_chat = (
                message_chat_id
                if message_chat_id
                else chat_id
            )

            msg = await app.send_message(
                chat_id=target_chat,
                text=_lang[
                    "play_again"
                ],
            )

            await self.play_media(
                chat_id,
                msg,
                media,
                message_chat_id=message_chat_id,
            )

        except Exception as e:

            logger.error(
                f"Error in replay for "
                f"{chat_id}: {e}",
                exc_info=True,
            )

    # ========================================================
    # SEEK
    # ========================================================

    async def seek_stream(
        self,
        chat_id: int,
        seconds: int,
    ) -> bool:

        try:

            if not await db.get_call(
                chat_id
            ):

                return False

            media = queue.get_current(
                chat_id
            )

            if (
                not media
                or media.is_live
            ):

                return False

            client = await db.get_assistant(
                chat_id
            )

            _lang = await lang.get_lang(
                chat_id
            )

            message_chat_id = None

            try:

                chat = await app.get_chat(
                    chat_id
                )

                if (
                    chat.type
                    == enums.ChatType.CHANNEL
                ):

                    group_id = (
                        await db.get_group_for_channel(
                            chat_id
                        )
                    )

                    if group_id:

                        message_chat_id = (
                            group_id
                        )

            except Exception:

                pass

            media.time = seconds

            target_chat = (
                message_chat_id
                if message_chat_id
                else chat_id
            )

            try:

                msg = await app.get_messages(
                    target_chat,
                    media.message_id,
                )

            except Exception:

                msg = None

            if not msg:

                msg = await app.send_message(
                    chat_id=target_chat,
                    text=_lang[
                        "seeking"
                    ],
                )

            await self.play_media(
                chat_id,
                msg,
                media,
                seek_time=seconds,
                message_chat_id=message_chat_id,
            )

            return True

        except Exception as e:

            logger.warning(
                f"Seek stream failed "
                f"for {chat_id}: {e}"
            )

            return False

    # ========================================================
    # PLAY NEXT + AUTOPLAY
    # ========================================================

    async def play_next(
        self,
        chat_id: int,
    ) -> None:

        # ----------------------------------------------------
        # CREATE LOCK
        # ----------------------------------------------------

        if (
            chat_id
            not in self._play_next_locks
        ):

            self._play_next_locks[
                chat_id
            ] = asyncio.Lock()

        lock = self._play_next_locks[
            chat_id
        ]

        # ----------------------------------------------------
        # PREVENT DUPLICATE CALL
        # ----------------------------------------------------

        if lock.locked():

            logger.debug(
                f"play_next already running "
                f"for {chat_id}"
            )

            return

        async with lock:

            try:

                # =============================================
                # CHECK ACTIVE CALL
                # =============================================

                if not await db.get_call(
                    chat_id
                ):

                    logger.debug(
                        f"No active call for {chat_id}"
                    )

                    return

                # =============================================
                # CHANNEL → LINKED GROUP
                # =============================================

                message_chat_id = None

                try:

                    chat = await app.get_chat(
                        chat_id
                    )

                    if (
                        chat.type
                        == enums.ChatType.CHANNEL
                    ):

                        group_id = (
                            await db.get_group_for_channel(
                                chat_id
                            )
                        )

                        if group_id:

                            message_chat_id = (
                                group_id
                            )

                except Exception:

                    pass

                target_chat = (
                    message_chat_id
                    if message_chat_id
                    else chat_id
                )

                # =============================================
                # LOOP MODE
                # =============================================

                loop_mode = await db.get_loop(
                    chat_id
                )

                # =============================================
                # LOOP CURRENT SONG
                # =============================================

                if loop_mode == 1:

                    media = queue.get_current(
                        chat_id
                    )

                    if media:

                        _lang = await lang.get_lang(
                            chat_id
                        )

                        try:

                            msg = await app.send_message(
                                chat_id=target_chat,
                                text=_lang[
                                    "play_again"
                                ],
                            )

                            await self.play_media(
                                chat_id,
                                msg,
                                media,
                                message_chat_id=message_chat_id,
                            )

                        except errors.ChannelPrivate:

                            try:

                                await self.leave_call(
                                    chat_id
                                )

                            except Exception:

                                pass

                            await db.rm_chat(
                                chat_id
                            )

                        return

                # =============================================
                # IMPORTANT:
                # SAVE CURRENT BEFORE get_next()
                #
                # queue.get_next() removes current.
                # So current song must be captured first.
                # =============================================

                current_media = queue.get_current(
                    chat_id
                )

                # =============================================
                # GET NEXT QUEUED SONG
                # =============================================

                media = queue.get_next(
                    chat_id
                )

                # =============================================
                # LOOP WHOLE QUEUE
                # =============================================

                if (
                    not media
                    and loop_mode == 10
                ):

                    all_items = queue.get_all(
                        chat_id
                    )

                    if all_items:

                        first_track = (
                            all_items[0]
                        )

                        _lang = await lang.get_lang(
                            chat_id
                        )

                        try:

                            msg = await app.send_message(
                                chat_id=target_chat,
                                text="🔁 Looping queue...",
                            )

                            if not first_track.file_path:

                                is_live = getattr(
                                    first_track,
                                    "is_live",
                                    False,
                                )

                                first_track.file_path = (
                                    await yt.download(
                                        first_track.id,
                                        is_live=is_live,
                                        video=getattr(
                                            first_track,
                                            "video",
                                            False,
                                        ),
                                    )
                                )

                            first_track.message_id = (
                                msg.id
                            )

                            await self.play_media(
                                chat_id,
                                msg,
                                first_track,
                                message_chat_id=message_chat_id,
                            )

                        except errors.ChannelPrivate:

                            await self.leave_call(
                                chat_id
                            )

                            await db.rm_chat(
                                chat_id
                            )

                    return

                # =============================================
                # DELETE OLD QUEUE MESSAGE
                # =============================================

                try:

                    if (
                        media
                        and media.message_id
                    ):

                        await app.delete_messages(
                            chat_id=chat_id,
                            message_ids=media.message_id,
                            revoke=True,
                        )

                        media.message_id = 0

                except Exception:

                    pass

                # =================================================
                # 🤖 AUTOPLAY
                # =================================================

                if not media:

                    autoplay_enabled = (
                        await db.get_autoplay(
                            chat_id
                        )
                    )

                    if autoplay_enabled:

                        try:

                            logger.info(
                                f"🤖 Autoplay triggered "
                                f"for {chat_id}"
                            )

                            # -------------------------------------
                            # current_media was saved BEFORE
                            # queue.get_next()
                            # -------------------------------------

                            source_media = (
                                current_media
                            )

                            if source_media:

                                search_query = getattr(
                                    source_media,
                                    "title",
                                    "",
                                )

                                search_query = (
                                    search_query
                                    .strip()
                                )

                                current_id = getattr(
                                    source_media,
                                    "id",
                                    None,
                                )

                                # ---------------------------------
                                # SEARCH
                                # ---------------------------------

                                if search_query:

                                    logger.info(
                                        f"🤖 Autoplay searching: "
                                        f"{search_query}"
                                    )

                                    results = (
                                        await yt.search(
                                            search_query
                                        )
                                    )

                                    next_track = None

                                    # -----------------------------
                                    # SELECT DIFFERENT VIDEO
                                    # -----------------------------

                                    if results:

                                        for track in results:

                                            track_id = getattr(
                                                track,
                                                "id",
                                                None,
                                            )

                                            if (
                                                track_id
                                                and track_id
                                                != current_id
                                            ):

                                                next_track = (
                                                    track
                                                )

                                                break

                                    # -----------------------------
                                    # FOUND
                                    # -----------------------------

                                    if next_track:

                                        # Put it in queue so queue
                                        # state remains correct.
                                        queue.add(
                                            chat_id,
                                            next_track,
                                        )

                                        # IMPORTANT:
                                        #
                                        # DO NOT call get_next()
                                        # here.
                                        #
                                        # get_next() would remove
                                        # the newly added song.
                                        #
                                        media = next_track

                                        logger.info(
                                            "🤖 Autoplay selected: "
                                            f"{getattr(next_track, 'title', 'Unknown')}"
                                        )

                                    else:

                                        logger.warning(
                                            f"🤖 Autoplay found "
                                            f"no different track "
                                            f"for {chat_id}"
                                        )

                                else:

                                    logger.warning(
                                        f"🤖 Current track has "
                                        f"no title for {chat_id}"
                                    )

                            else:

                                logger.warning(
                                    f"🤖 Current track unavailable "
                                    f"for {chat_id}"
                                )

                        except Exception as e:

                            logger.error(
                                f"❌ Autoplay failed "
                                f"for {chat_id}: {e}",
                                exc_info=True,
                            )

                # =================================================
                # QUEUE EMPTY + AUTOPLAY FAILED/OFF
                # =================================================

                if not media:

                    if config.AUTO_END:

                        _lang = await lang.get_lang(
                            chat_id
                        )

                        try:

                            await app.send_message(
                                chat_id=target_chat,
                                text=_lang.get(
                                    "auto_end",
                                    "✅ Queue finished. "
                                    "Stream ended automatically.",
                                ),
                            )

                        except Exception:

                            pass

                    return await self.stop(
                        chat_id
                    )

                # =================================================
                # LANGUAGE
                # =================================================

                _lang = await lang.get_lang(
                    chat_id
                )

                msg = None

                # =================================================
                # DOWNLOAD NEXT TRACK
                # =================================================

                if not media.file_path:

                    is_live = getattr(
                        media,
                        "is_live",
                        False,
                    )

                    media.file_path = (
                        await yt.download(
                            media.id,
                            is_live=is_live,
                            video=getattr(
                                media,
                                "video",
                                False,
                            ),
                        )
                    )

                    if not media.file_path:

                        logger.error(
                            f"❌ Failed to download "
                            f"next track {media.id}"
                        )

                        await self.stop(
                            chat_id
                        )

                        return

                # =================================================
                # SEND PLAY NEXT MESSAGE
                # =================================================

                try:

                    msg = await app.send_message(
                        chat_id=target_chat,
                        text=_lang[
                            "play_next"
                        ],
                    )

                except errors.FloodWait:

                    msg = None

                except errors.ChannelPrivate:

                    await self.leave_call(
                        chat_id
                    )

                    await db.rm_chat(
                        chat_id
                    )

                    return

                except Exception:

                    msg = None

                # =================================================
                # MESSAGE ID
                # =================================================

                media.message_id = (
                    msg.id
                    if msg
                    else 0
                )

                # =================================================
                # START PLAYBACK
                # =================================================

                if msg:

                    await self.play_media(
                        chat_id,
                        msg,
                        media,
                        message_chat_id=message_chat_id,
                    )

                else:

                    await self.play_media(
                        chat_id,
                        None,
                        media,
                        message_chat_id=message_chat_id,
                    )

                # =================================================
                # PRELOAD
                # =================================================

                try:

                    asyncio.create_task(
                        preload.start_preload(
                            chat_id,
                            count=2,
                        )
                    )

                except Exception:

                    pass

            # ====================================================
            # PLAY NEXT ERROR
            # ====================================================

            except Exception as e:

                logger.error(
                    f"Error in play_next "
                    f"for {chat_id}: {e}",
                    exc_info=True,
                )

                try:

                    await self.stop(
                        chat_id
                    )

                except Exception:

                    pass

    # ========================================================
    # PING
    # ========================================================

    async def ping(
        self,
    ) -> float:

        pings = [
            client.ping
            for client in self.clients
        ]

        if not pings:

            return 0.0

        return round(
            sum(pings) / len(pings),
            2,
        )

    # ========================================================
    # PYTGCALLS EVENTS
    # ========================================================

    async def decorators(
        self,
        client: PyTgCalls,
    ) -> None:

        # IMPORTANT:
        #
        # Only register the handler on the client passed
        # to this function.
        #
        # Do NOT loop self.clients here because boot()
        # already calls decorators() for every client.
        # Otherwise duplicate handlers can be registered.

        @client.on_update()
        async def update_handler(
            _,
            update: types.Update,
        ) -> None:

            # =================================================
            # STREAM ENDED
            # =================================================

            if isinstance(
                update,
                types.StreamEnded,
            ):

                chat_id = (
                    update.chat_id
                )

                # ---------------------------------------------
                # AUDIO + VIDEO
                # ---------------------------------------------
                #
                # Don't restrict this to AUDIO.
                # Autoplay should continue regardless of
                # whether the current stream is audio/video.
                # ---------------------------------------------

                current_time = (
                    asyncio.get_event_loop().time()
                )

                # ---------------------------------------------
                # DUPLICATE EVENT PROTECTION
                # ---------------------------------------------

                if (
                    chat_id
                    in self._stream_end_cache
                ):

                    if (
                        current_time
                        - self._stream_end_cache[
                            chat_id
                        ]
                        < 2.0
                    ):

                        return

                self._stream_end_cache[
                    chat_id
                ] = current_time

                # ---------------------------------------------
                # CLEAN OLD CACHE
                # ---------------------------------------------

                self._stream_end_cache = {
                    cid: timestamp
                    for cid, timestamp
                    in self._stream_end_cache.items()
                    if current_time - timestamp < 5.0
                }

                logger.info(
                    f"🎵 Stream ended: "
                    f"chat={chat_id}, "
                    f"type={getattr(update, 'stream_type', 'unknown')}"
                )

                # ---------------------------------------------
                # QUEUE / AUTOPLAY
                # ---------------------------------------------

                await self.play_next(
                    chat_id
                )

            # =================================================
            # CHAT UPDATE
            # =================================================

            elif isinstance(
                update,
                types.ChatUpdate,
            ):

                if update.status in [
                    types.ChatUpdate.Status.KICKED,
                    types.ChatUpdate.Status.LEFT_GROUP,
                    types.ChatUpdate.Status.CLOSED_VOICE_CHAT,
                ]:

                    await self.stop(
                        update.chat_id
                    )

    # ========================================================
    # BOOT
    # ========================================================

    async def boot(
        self,
    ) -> None:

        PyTgCallsSession.notice_displayed = True

        for ub in userbot.clients:

            client = PyTgCalls(
                ub,
                cache_duration=100,
            )

            await client.start()

            self.clients.append(
                client
            )

            await self.decorators(
                client
            )

        logger.info(
            "📞 PyTgCalls client(s) started."
        )