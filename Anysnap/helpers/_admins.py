from functools import wraps

from pyrogram import StopPropagation, enums, types
from pyrogram.errors import (
    ChatSendPlainForbidden,
    ChatWriteForbidden,
)

from Anysnap import app, db, lang


def admin_check(func):
    @wraps(func)
    async def wrapper(
        _,
        update: types.Message | types.CallbackQuery,
        *args,
        **kwargs,
    ):
        async def reply(text):
            if isinstance(update, types.Message):
                try:
                    return await update.reply_text(text)
                except (
                    ChatSendPlainForbidden,
                    ChatWriteForbidden,
                ):
                    return
            else:
                try:
                    return await update.answer(
                        text,
                        show_alert=True,
                    )
                except Exception:
                    return

        if not update.from_user:
            return

        chat_id = (
            update.chat.id
            if isinstance(update, types.Message)
            else update.message.chat.id
        )

        user_id = update.from_user.id

        # =====================================================
        # SUDO
        # =====================================================

        if user_id in app.sudoers:
            return await func(
                _,
                update,
                *args,
                **kwargs,
            )

        # =====================================================
        # DATABASE ADMINS
        # =====================================================

        admins = await db.get_admins(
            chat_id
        )

        if user_id in admins:
            return await func(
                _,
                update,
                *args,
                **kwargs,
            )

        # =====================================================
        # NO PERMISSION
        # =====================================================

        _lang = await lang.get_lang(
            chat_id
        )

        return await reply(
            _lang["user_no_perms"]
        )

    return wrapper


def can_manage_vc(func):
    @wraps(func)
    async def wrapper(
        _,
        update: types.Message | types.CallbackQuery,
        *args,
        **kwargs,
    ):
        if not update.from_user:
            return

        # =====================================================
        # GET CHAT ID
        # =====================================================

        if isinstance(update, types.Message):
            chat_id = update.chat.id
        else:
            if not update.message:
                return

            chat_id = update.message.chat.id

        user_id = update.from_user.id

        # =====================================================
        # SUDO
        # =====================================================

        if user_id in app.sudoers:
            return await func(
                _,
                update,
                *args,
                **kwargs,
            )

        # =====================================================
        # AUTHORIZED USER
        # =====================================================

        if await db.is_auth(
            chat_id,
            user_id,
        ):
            return await func(
                _,
                update,
                *args,
                **kwargs,
            )

        # =====================================================
        # DATABASE ADMIN
        # =====================================================

        admins = await db.get_admins(
            chat_id
        )

        if user_id in admins:
            return await func(
                _,
                update,
                *args,
                **kwargs,
            )

        # =====================================================
        # ACTUAL TELEGRAM ADMIN
        #
        # This fixes the problem where a Telegram admin
        # was rejected because their ID wasn't in DB cache.
        # =====================================================

        try:
            member = await app.get_chat_member(
                chat_id,
                user_id,
            )

            if member.status in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]:
                return await func(
                    _,
                    update,
                    *args,
                    **kwargs,
                )

        except Exception:
            pass

        # =====================================================
        # NO PERMISSION
        #
        # IMPORTANT:
        # Message has no .lang attribute.
        # We get language through db/lang instead.
        # =====================================================

        _lang = await lang.get_lang(
            chat_id
        )

        if isinstance(
            update,
            types.Message,
        ):
            try:
                return await update.reply_text(
                    _lang["user_no_perms"]
                )

            except (
                ChatSendPlainForbidden,
                ChatWriteForbidden,
            ):
                return

        try:
            return await update.answer(
                _lang["user_no_perms"],
                show_alert=True,
            )

        except Exception:
            return

    return wrapper


async def can_manage_vc_channel(
    chat_id: int,
    user_id: int,
) -> bool:
    """Check if user can manage VC in channel mode."""

    # =====================================================
    # SUDO
    # =====================================================

    if user_id in app.sudoers:
        return True

    # =====================================================
    # AUTHORIZED USER
    # =====================================================

    if await db.is_auth(
        chat_id,
        user_id,
    ):
        return True

    # =====================================================
    # DATABASE ADMIN
    # =====================================================

    admins = await db.get_admins(
        chat_id
    )

    if user_id in admins:
        return True

    # =====================================================
    # ACTUAL TELEGRAM ADMIN
    # =====================================================

    try:
        member = await app.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]

    except Exception:
        return False


async def is_admin(
    chat_id: int,
    user_id: int,
) -> bool:

    # =====================================================
    # DATABASE ADMIN
    # =====================================================

    if user_id in await db.get_admins(
        chat_id
    ):
        return True

    # =====================================================
    # TELEGRAM ADMIN
    # =====================================================

    try:
        member = await app.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]

    except Exception:
        raise StopPropagation


async def reload_admins(
    chat_id: int,
) -> list[int]:

    try:
        admins = [
            admin
            async for admin in app.get_chat_members(
                chat_id,
                filter=enums.ChatMembersFilter.ADMINISTRATORS,
            )
            if not admin.user.is_bot
        ]

        return [
            admin.user.id
            for admin in admins
        ]

    except Exception:
        return []


async def is_admin_callback(
    query: types.CallbackQuery,
) -> bool:

    if not query.from_user:
        return False

    if not query.message:
        return False

    user_id = query.from_user.id
    chat_id = query.message.chat.id

    # =====================================================
    # SUDO
    # =====================================================

    if user_id in app.sudoers:
        return True

    # =====================================================
    # DATABASE ADMIN
    # =====================================================

    admins = await db.get_admins(
        chat_id
    )

    if user_id in admins:
        return True

    # =====================================================
    # ACTUAL TELEGRAM ADMIN
    # =====================================================

    try:
        member = await app.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]

    except Exception:
        return False