"""Проверка аккаунтов через официальный Roblox API (users.roblox.com, thumbnails.roblox.com)."""
import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
USERS_BY_NAME_URL = "https://users.roblox.com/v1/usernames/users"
USER_BY_ID_URL = "https://users.roblox.com/v1/users/{}"
HEADSHOT_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"
PROFILE_URL = "https://www.roblox.com/users/{}/profile"

# Код подтверждения — фраза из обычных английских слов. Roblox закрашивает «####»
# случайные наборы букв и цифр (принимает их за личные данные), а нормальные слова пропускает.
# Только безобидные слова: природа, еда, предметы, цвета. Без «опасных» слов и цифр.
CODE_WORDS = [
    "apple", "river", "stone", "cloud", "forest", "garden", "silver", "golden", "orange", "purple",
    "yellow", "green", "blue", "white", "brown", "pink", "lemon", "banana", "cherry", "grape",
    "peach", "melon", "carrot", "potato", "tomato", "bread", "butter", "honey", "sugar", "candy",
    "cookie", "pizza", "salad", "pepper", "coffee", "water", "milk", "juice", "ocean", "island",
    "mountain", "valley", "desert", "meadow", "field", "hill", "lake", "pond", "beach", "sand",
    "wave", "storm", "rain", "snow", "wind", "sun", "moon", "star", "sky", "dawn",
    "sunset", "rainbow", "thunder", "breeze", "flower", "tulip", "rose", "daisy", "maple", "birch",
    "willow", "cedar", "bamboo", "grass", "leaf", "branch", "acorn", "moss", "fern", "pebble",
    "rabbit", "turtle", "dolphin", "panda", "koala", "tiger", "zebra", "horse", "sheep", "duck",
    "swan", "eagle", "falcon", "robin", "owl", "fox", "bear", "otter", "seal", "whale",
    "table", "chair", "window", "door", "lamp", "clock", "mirror", "pillow", "blanket", "basket",
    "bottle", "bucket", "candle", "ladder", "bridge", "castle", "tower", "tunnel", "road", "train",
    "boat", "kite", "ball", "drum", "piano", "guitar", "violin", "flute", "puzzle", "marble",
    "ribbon", "button", "pocket", "jacket", "hat", "scarf", "boots", "gloves", "umbrella", "compass",
    "anchor", "lantern", "feather", "crystal", "diamond", "copper", "iron", "velvet", "cotton", "paper",
    "pencil", "notebook", "letter", "story", "music", "dance", "smile", "dream", "garden", "harbor",
]
CODE_WORDS = sorted(set(CODE_WORDS))
CODE_LENGTH = 4  # слов в фразе: 150+ слов ^ 4 — это миллиарды комбинаций


class RobloxError(Exception):
    """Временная ошибка Roblox API (сеть, лимиты, сбой сервера). Данные при этом не трогаем."""


@dataclass
class RobloxUser:
    id: int
    name: str                      # настоящий username (для входа в аккаунт)
    display_name: str = ""         # отображаемое имя (Display Name)
    description: str = ""          # описание профиля («Обо мне»)
    is_banned: bool = False

    @property
    def profile_url(self) -> str:
        return profile_url(self.id)


def profile_url(user_id: int) -> str:
    return PROFILE_URL.format(int(user_id))


def valid_nick_format(nick: str) -> bool:
    return bool(USERNAME_RE.fullmatch(nick or ""))


def generate_code() -> str:
    """Временная фраза для описания профиля, например: apple river stone cloud."""
    rng = secrets.SystemRandom()
    return " ".join(rng.sample(CODE_WORDS, CODE_LENGTH))


def _normalize(text: Optional[str]) -> str:
    """Оставляет только буквы и цифры в нижнем регистре, всё остальное — один пробел.
    Так фраза находится, даже если человек добавил запятые, переносы строк или лишние пробелы."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def code_in_description(code: str, description: Optional[str]) -> bool:
    """Ищет фразу целиком (слова подряд) в описании профиля. Регистр и знаки препинания не важны."""
    wanted = _normalize(code)
    if not wanted:
        return False
    return f" {wanted} " in f" {_normalize(description)} "


async def _request(method: str, url: str, **kwargs):
    timeout = aiohttp.ClientTimeout(total=10)
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status in (400, 404):
                        return None
                    if resp.status == 429 or resp.status >= 500:
                        last_error = RobloxError(f"HTTP {resp.status}")
                        retry_after = resp.headers.get("Retry-After", "")
                        delay = int(retry_after) if retry_after.isdigit() else 1.5 * (attempt + 1)
                        await asyncio.sleep(min(delay, 5))
                        continue
                    if resp.status != 200:
                        raise RobloxError(f"HTTP {resp.status}")
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            last_error = RobloxError(str(exc) or exc.__class__.__name__)
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last_error or RobloxError("Roblox API недоступен")


async def get_user_by_name(name: str) -> Optional[RobloxUser]:
    """Ищет аккаунт по username. Возвращает None, если такого аккаунта нет.
    Roblox сам возвращает настоящий регистр username и Display Name."""
    data = await _request(
        "POST", USERS_BY_NAME_URL,
        json={"usernames": [name], "excludeBannedUsers": False},
    )
    items = (data or {}).get("data") or []
    if not items:
        return None
    item = items[0]
    return RobloxUser(id=int(item["id"]), name=str(item["name"]),
                      display_name=str(item.get("displayName") or item["name"]))


async def get_user_by_id(user_id: int) -> Optional[RobloxUser]:
    """Полные данные по постоянному User ID (включая описание профиля)."""
    data = await _request("GET", USER_BY_ID_URL.format(int(user_id)))
    if not data or "name" not in data:
        return None
    return RobloxUser(id=int(data["id"]), name=str(data["name"]),
                      display_name=str(data.get("displayName") or data["name"]),
                      description=str(data.get("description") or ""),
                      is_banned=bool(data.get("isBanned")))


async def get_avatar_url(user_id: int) -> Optional[str]:
    """Ссылка на официальную аватарку (headshot 420x420) или None, если её нет/не готова.
    Ошибки аватарки не критичны, поэтому наружу не пробрасываются."""
    for attempt in range(2):
        try:
            data = await _request("GET", HEADSHOT_URL, params={
                "userIds": int(user_id), "size": "420x420",
                "format": "Png", "isCircular": "false"})
        except RobloxError:
            return None
        items = (data or {}).get("data") or []
        if items and items[0].get("state") == "Completed" and items[0].get("imageUrl"):
            return str(items[0]["imageUrl"])
        if items and items[0].get("state") == "Pending" and attempt == 0:
            await asyncio.sleep(1.5)  # Roblox ещё рисует картинку
            continue
        return None
    return None


async def download_image(url: str, limit: int = 5_000_000) -> Optional[bytes]:
    """Скачивает картинку, чтобы отправить в Telegram файлом, если ссылку он не принял."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                body = await resp.content.read(limit + 1)
                return body if 0 < len(body) <= limit else None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
