from pyrogram import filters, types

from Anysnap import app, db
from Anysnap.helpers import can_manage_vc


@app.on_message(
    filters.command(["autoplay", "cautoplay"])
    & filters.group
    & ~app.bl_users
)
@can_manage_vc
async def _autoplay(_, m: types.Message):

    try:
        await m.delete()
    except Exception:
        pass

    command = m.command[0].lower()
    is_channel = command == "cautoplay"

    chat_id = m.chat.id
    channel_id = await db.get_cmode(m.chat.id)

    # /cautoplay
    if is_channel:
        if channel_id is None:
            return await m.reply_text(
                "<blockquote>"
                "❌ <b>Channel play is not enabled.</b>\n\n"
                "Use <code>/channelplay</code> to enable it first."
                "</blockquote>"
            )

        chat_id = channel_id

    # /autoplay when channel mode is enabled
    elif channel_id is not None:
        chat_id = channel_id

    # No argument
    if len(m.command) < 2:
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

    action = m.command[1].lower()

    if action not in ("on", "off"):
        return await m.reply_text(
            "<blockquote>"
            "❌ <b>Invalid option.</b>\n\n"
            "Use <code>/autoplay on</code> "
            "or <code>/autoplay off</code>."
            "</blockquote>"
        )

    enabled = action == "on"

    await db.set_autoplay(chat_id, enabled)

    if enabled:
        text = (
            "<blockquote>"
            "🎵 <b>Autoplay: ON</b>\n\n"
            "When the queue ends, I will automatically "
            "search for another similar song and play it."
            "</blockquote>"
        )
    else:
        text = (
            "<blockquote>"
            "⏹ <b>Autoplay: OFF</b>\n\n"
            "Autoplay has been disabled. "
            "Playback will stop when the queue ends."
            "</blockquote>"
        )

    await m.reply_text(text)