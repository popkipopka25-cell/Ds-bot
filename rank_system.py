"""PvP-ранги DS: таблица, расчёт и проверка результатов матча.

Модуль не зависит от базы данных и Telegram, поэтому его легко проверять отдельно.
Должности администрации (staff_system) и PvP-ранги — две независимые системы.
"""
import re

MATCH_TARGET = 10  # матч идёт до 10 побед

# (код, название, уровень, мин. побед участника, макс. побед участника)
RANKS = [
    ("DSa", "Advanced", 4, 8, 10),
    ("DSe", "Expert", 3, 6, 7),
    ("DSf", "Skillful", 2, 4, 5),
    ("DSc", "Casual", 1, 0, 3),
]
DEFAULT_RANK = "DSc"

_INFO = {code: (title, level) for code, title, level, _lo, _hi in RANKS}


def rank_title(code: str) -> str:
    return _INFO[code][0]


def rank_level(code: str) -> int:
    return _INFO[code][1]


def rank_label(code: str) -> str:
    return f"{code} — {_INFO[code][0]}"


def rank_for_wins(wins: int) -> str:
    """Ранг определяется ТОЛЬКО по количеству побед участника."""
    if not isinstance(wins, int) or wins < 0 or wins > MATCH_TARGET:
        raise ValueError("Количество побед должно быть от 0 до 10.")
    for code, _title, _level, low, high in RANKS:
        if low <= wins <= high:
            return code
    raise ValueError("Ранг не найден.")  # недостижимо при корректной таблице


def parse_score(text: str) -> int:
    value = (text or "").strip()
    if re.fullmatch(r"[-−–]\s*\d+", value):
        raise ValueError("Число не может быть отрицательным.")
    if not re.fullmatch(r"\d{1,3}", value):
        raise ValueError("Введи целое число от 0 до 10.")
    number = int(value)
    if number > MATCH_TARGET:
        raise ValueError("Нельзя ввести больше 10 побед.")
    return number


def validate_scores(checker_wins, user_wins) -> None:
    """Проверка результата матча до 10 побед. Бросает ValueError с понятным текстом."""
    if checker_wins is None or user_wins is None:
        raise ValueError("Результат не может быть пустым.")
    if not isinstance(checker_wins, int) or not isinstance(user_wins, int):
        raise ValueError("Победы должны быть целыми числами.")
    if checker_wins < 0 or user_wins < 0:
        raise ValueError("Число побед не может быть отрицательным.")
    if checker_wins > MATCH_TARGET or user_wins > MATCH_TARGET:
        raise ValueError("Нельзя ввести больше 10 побед.")
    if checker_wins == MATCH_TARGET and user_wins == MATCH_TARGET:
        raise ValueError("Обе стороны не могут иметь по 10 побед.")
    if checker_wins < MATCH_TARGET and user_wins < MATCH_TARGET:
        raise ValueError("Матч идёт до 10 побед: у одной из сторон должно быть ровно 10.")


def evaluate(old_rank: str, user_wins: int):
    """Возвращает (новый ранг, 'up' | 'down' | 'same')."""
    new_rank = rank_for_wins(user_wins)
    old_level, new_level = rank_level(old_rank), rank_level(new_rank)
    if new_level > old_level:
        return new_rank, "up"
    if new_level < old_level:
        return new_rank, "down"
    return new_rank, "same"


def rank_table_text() -> str:
    lines = []
    for code, title, _level, low, high in RANKS:
        wins = f"{low}–{high}" if low != high else str(low)
        lines.append(f"• <b>{code} — {title}</b>: {wins} побед участника")
    return "\n".join(lines)
