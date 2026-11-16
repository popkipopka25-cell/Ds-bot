"""Проверка аккаунтов через официальный Roblox API (users.roblox.com)."""
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
USERS_BY_NAME_URL = "https://users.roblox.com/v1/usernames/users"
USER_BY_ID_URL = "https://users.roblox.com/v1/users/{}"


class RobloxError(Exception):
    """Временная ошибка Roblox API (сеть, лимиты, сбой сервера). Данные при этом не трогаем."""


@dataclass
class RobloxUser:
    id: int
    name: str


def valid_nick_format(nick: str) -> bool:
    return bool(USERNAME_RE.fullmatch(nick or ""))


def ends_with_ds(name: str) -> bool:
    """Ник должен заканчиваться на DS (регистр не важен)."""
    return (name or "").lower().endswith("ds")


async def _request(method: str, url: str, **kwargs):
    timeout = aiohttp.ClientTimeout(total=10)
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status == 404:
                        return None
                    if resp.status == 429 or resp.status >= 500:
                        last_error = RobloxError(f"HTTP {resp.status}")
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if resp.status != 200:
                        raise RobloxError(f"HTTP {resp.status}")
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = RobloxError(str(exc) or exc.__class__.__name__)
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last_error or RobloxError("Roblox API недоступен")


async def get_user_by_name(name: str) -> Optional[RobloxUser]:
    """Возвращает аккаунт по нику или None, если такого аккаунта нет."""
    data = await _request(
        "POST", USERS_BY_NAME_URL,
        json={"usernames": [name], "excludeBannedUsers": False},
    )
    items = (data or {}).get("data") or []
    if not items:
        return None
    item = items[0]
    return RobloxUser(id=int(item["id"]), name=str(item["name"]))


async def get_user_by_id(user_id: int) -> Optional[RobloxUser]:
    data = await _request("GET", USER_BY_ID_URL.format(int(user_id)))
    if not data or "name" not in data:
        return None
    return RobloxUser(id=int(data["id"]), name=str(data["name"]))
