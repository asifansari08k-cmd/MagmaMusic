from pyrogram import filters, types

from Anysnap import app, db
from Anysnap.helpers import can_manage_vc


# ============================================================
# AUTOPLAY / CAUTOPLAY
# ============================================================

@app.on_message(
    filters.command(
        ["autoplay", "cautoplay"]
    )
    & filters.group
    & ~app.bl_users
)
@can_manage_vc
async def _autoplay(
    _,
    m: types.Message,
):

    # ========================================================
    # DELETE COMMAND
    # ========================================================

    try:

        await m.delete()

    except Exception:

        pass

    # ========================================================
    # COMMAND
    # ========================================================

    command = (
        m.command[0].lower()
        if m.command
        else "autoplay"
    )

    is_channel = (
        command == "cautoplay"
    )

    group_id = m.chat.id

    # ========================================================
    # GET CHANNEL MODE
    # ========================================================

    try:

        channel_id = await db.get_cmode(
            group_id
        )

    except Exception:

        channel_id = None

    # ========================================================
    # SELECT AUTOPLAY CHAT
    # ========================================================

    # /cautoplay
    #
    # Autoplay setting belongs to the
    # actual channel where playback happens.
    #
    if is_channel:

        if channel_id is None:

            return await m.reply_text(
                "<blockquote>"
                "❌ <b>Channel play is not enabled.</b>\n\n"
                "First enable channel playback using "
                "<code>/channelplay</code>."
                "</blockquote>"
            )

        autoplay_chat_id = channel_id

    # /autoplay
    #
    # If channel mode is active, use the
    # linked channel automatically.
    #
    else:

        autoplay_chat_id = (
            channel_id
            if channel_id is not None
            else group_id
        )

    # ========================================================
    # NO ARGUMENT
    # ========================================================

    if (
        not m.command
        or len(m.command) < 2
    ):

        if is_channel:

            return await m.reply_text(
                "<blockquote>"
                "🎵 <b>Channel Autoplay</b>\n\n"
                "Use:\n"
                "• <code>/cautoplay on</code>\n"
                "• <code>/cautoplay off</code>"
                "</blockquote>"
            )

        return await m.reply_text(
            "<blockquote>"
            "🎵 <b>Autoplay Settings</b>\n\n"
            "Use:\n"
            "• <code>/autoplay on</code>\n"
            "• <code>/autoplay off</code>\n\n"
            "For channel playback:\n"
            "• <code>/cautoplay on</code>\n"
            "• <code>/cautoplay off</code>"
            "</blockquote>"
        )

    # ========================================================
    # ACTION
    # ========================================================

    action = (
        m.command[1]
        .strip()
        .lower()
    )

    if action not in (
        "on",
        "off",
    ):

        if is_channel:

            return await m.reply_text(
                "<blockquote>"
                "❌ <b>Invalid option.</b>\n\n"
                "Use:\n"
                "• <code>/cautoplay on</code>\n"
                "• <code>/cautoplay off</code>"
                "</blockquote>"
            )

        return await m.reply_text(
            "<blockquote>"
            "❌ <b>Invalid option.</b>\n\n"
            "Use:\n"
            "• <code>/autoplay on</code>\n"
            "• <code>/autoplay off</code>"
            "</blockquote>"
        )

    # ========================================================
    # ENABLE / DISABLE
    # ========================================================

    enabled = (
        action == "on"
    )

    # ========================================================
    # SAVE TO MONGODB
    # ========================================================

    try:

        await db.set_autoplay(
            autoplay_chat_id,
            enabled,
        )

    except Exception as e:

        return await m.reply_text(
            "<blockquote>"
            "❌ <b>Failed to update autoplay.</b>\n\n"
            f"<code>{str(e)[:150]}</code>"
            "</blockquote>"
        )

    # ========================================================
    # VERIFY DATABASE
    # ========================================================

    try:

        current_status = (
            await db.get_autoplay(
                autoplay_chat_id
            )
        )

    except Exception:

        current_status = enabled

    # ========================================================
    # SUCCESS MESSAGE
    # ========================================================

    if current_status:

        if is_channel:

            text = (
                "<blockquote>"
                "🎵 <b>Channel Autoplay: ON</b>\n\n"
                "When the channel queue ends, "
                "I will automatically search for "
                "another song and continue playback."
                "</blockquote>"
            )

        else:

            text = (
                "<blockquote>"
                "🎵 <b>Autoplay: ON</b>\n\n"
                "When the queue ends, "
                "I will automatically search for "
                "another song and continue playback."
                "</blockquote>"
            )

    else:

        if is_channel:

            text = (
                "<blockquote>"
                "⏹ <b>Channel Autoplay: OFF</b>\n\n"
                "Autoplay has been disabled for "
                "the linked channel."
                "</blockquote>"
            )

        else:

            text = (
                "<blockquote>"
                "⏹ <b>Autoplay: OFF</b>\n\n"
                "Autoplay has been disabled."
                "</blockquote>"
            )

    # ========================================================
    # SEND RESULT
    # ========================================================

    try:

        await m.reply_text(
            text
        )

    except Exception:

        pass