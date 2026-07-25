from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
)

from config import ADMIN_ID


async def setup_commands(bot: Bot):
    # Команды в личке для обычных пользователей
    private_cmds = [
        BotCommand(command="start", description="Открыть меню"),
    ]

    # Полный набор — только владельцу бота
    owner_cmds = [
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="menu", description="Управление каналами и группами"),
        BotCommand(command="reactall", description="Реакции на старые посты канала"),
        BotCommand(command="backup", description="Резервная копия базы"),
    ]

    # Команды для админов в группах/супергруппах (видны только администраторам чата)
    admin_cmds = [
        BotCommand(command="newtopic", description="Создать тему"),
        BotCommand(command="topics", description="Список созданных тем"),
        BotCommand(command="deltopic", description="Удалить тему по id"),
        BotCommand(command="warn", description="Предупреждение (ответом на сообщение)"),
        BotCommand(command="unwarn", description="Снять одно предупреждение"),
        BotCommand(command="warns", description="Сколько предупреждений у пользователя"),
        BotCommand(command="resetwarns", description="Сбросить предупреждения"),
    ]

    await bot.set_my_commands(private_cmds, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeAllChatAdministrators())
    try:
        await bot.set_my_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception:
        pass  # админ ещё не открывал бота в ЛС
