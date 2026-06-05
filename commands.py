from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllChatAdministrators,
)


async def setup_commands(bot: Bot):
    # Команды в личке (видны всем, кто открыл бота в ЛС)
    private_cmds = [
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="menu", description="Управление каналами и группами"),
        BotCommand(command="reactall", description="Реакции на старые посты канала"),
    ]

    # Команды для админов в группах/супергруппах (видны только администраторам чата)
    admin_cmds = [
        BotCommand(command="newtopic", description="Создать тему"),
        BotCommand(command="topics", description="Список созданных тем"),
        BotCommand(command="deltopic", description="Удалить тему по id"),
    ]

    await bot.set_my_commands(private_cmds, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeAllChatAdministrators())
