"""SQLite: схема и все операции с данными DS Bot.

Все изменения, затрагивающие несколько таблиц, выполняются в одной транзакции.
История участников не удаляется: при выходе/исключении меняется только статус.
"""
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import config
from rank_system import DEFAULT_RANK, RANKS, evaluate, validate_scores

log = logging.getLogger(__name__)
SYSTEM = "Система"


def now() -> int:
    return int(time.time())


class CooldownError(Exception):
    """Повторная проверка ещё недоступна. remaining — секунд до доступности."""

    def __init__(self, remaining: int):
        super().__init__("cooldown")
        self.remaining = remaining


SCHEMA = """
CREATE TABLE IF NOT EXISTS ranks (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    level INTEGER NOT NULL UNIQUE,
    min_wins INTEGER NOT NULL,
    max_wins INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    full_name TEXT,
    roblox_name TEXT NOT NULL,
    roblox_display_name TEXT,
    roblox_id INTEGER NOT NULL UNIQUE,
    rank_code TEXT NOT NULL DEFAULT 'DSc' REFERENCES ranks(code),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'left', 'removed')),
    joined_at INTEGER NOT NULL,
    last_pvp_at INTEGER,
    next_check_at INTEGER,
    pvp_count INTEGER NOT NULL DEFAULT 0,
    last_wins INTEGER,
    left_at INTEGER
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE,
    username TEXT,
    position TEXT NOT NULL,
    added_by INTEGER,
    added_at INTEGER NOT NULL,
    CHECK (tg_id IS NOT NULL OR username IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS permissions (
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    permission TEXT NOT NULL,
    PRIMARY KEY (staff_id, permission)
);

CREATE TABLE IF NOT EXISTS rank_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'done', 'cancelled')),
    created_at INTEGER NOT NULL,
    accepted_by INTEGER,
    accepted_at INTEGER,
    finished_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_request
    ON rank_requests(user_id) WHERE status IN ('pending', 'accepted');

CREATE TABLE IF NOT EXISTS pvp_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL UNIQUE REFERENCES rank_requests(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    checker_tg_id INTEGER NOT NULL,
    checker_wins INTEGER NOT NULL,
    user_wins INTEGER NOT NULL,
    old_rank TEXT NOT NULL REFERENCES ranks(code),
    new_rank TEXT NOT NULL REFERENCES ranks(code),
    created_at INTEGER NOT NULL,
    CHECK (checker_wins BETWEEN 0 AND 10 AND user_wins BETWEEN 0 AND 10),
    CHECK ((checker_wins = 10 AND user_wins < 10) OR (user_wins = 10 AND checker_wins < 10))
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    actor_tg_id INTEGER,
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    target_user_id INTEGER REFERENCES users(id),
    details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts DESC);

CREATE TABLE IF NOT EXISTS messages (
    key TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    text_hash TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inactive_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    left_at INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    restored_at INTEGER
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        with self.lock:
            self.conn.executescript(SCHEMA)
            for code, title, level, low, high in RANKS:
                self.conn.execute(
                    "INSERT INTO ranks(code, title, level, min_wins, max_wins) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET title=excluded.title, level=excluded.level, "
                    "min_wins=excluded.min_wins, max_wins=excluded.max_wins",
                    (code, title, level, low, high),
                )
            self._migrate()

    def _migrate(self):
        """Добавляет новые колонки в уже существующую базу. Старые данные не трогаются."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "roblox_display_name" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN roblox_display_name TEXT")

    # ------------------------------------------------------------ низкий уровень
    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def rows(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def row(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def run(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).rowcount

    # ------------------------------------------------------------ настройки
    def get_setting(self, key, default=None):
        r = self.row("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.run(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_group_id(self):
        value = self.get_setting("group_id")
        if value:
            try:
                return int(value)
            except ValueError:
                pass
        return config.GROUP_ID

    # ------------------------------------------------------------ история
    def _log(self, c, actor_tg, actor_name, action, details="", target=None):
        c.execute(
            "INSERT INTO history(ts, actor_tg_id, actor_name, action, target_user_id, details) "
            "VALUES (?,?,?,?,?,?)",
            (now(), actor_tg, actor_name or SYSTEM, action, target, details),
        )

    def log(self, actor_tg, actor_name, action, details="", target=None):
        with self.tx() as c:
            self._log(c, actor_tg, actor_name, action, details, target)

    def history_count(self):
        return self.row("SELECT COUNT(*) FROM history")[0]

    def history_page(self, limit, offset):
        return self.rows("SELECT * FROM history ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))

    # ------------------------------------------------------------ личность (ник Telegram)
    def touch_identity(self, tg_id, username, full_name) -> bool:
        """Обновляет username; если работник был добавлен по @username — привязывает его ID."""
        resolved = False
        with self.tx() as c:
            c.execute("UPDATE users SET username=?, full_name=? WHERE tg_id=?",
                      (username, full_name, tg_id))
            if username:
                cur = c.execute(
                    "UPDATE staff SET tg_id=?, username=? WHERE tg_id IS NULL AND lower(username)=lower(?)",
                    (tg_id, username, username),
                )
                resolved = cur.rowcount > 0
                c.execute("UPDATE staff SET username=? WHERE tg_id=?", (username, tg_id))
        return resolved

    # ------------------------------------------------------------ участники
    def get_user(self, user_id):
        return self.row("SELECT * FROM users WHERE id=?", (user_id,))

    def get_user_by_tg(self, tg_id):
        return self.row("SELECT * FROM users WHERE tg_id=?", (tg_id,))

    def get_user_by_roblox(self, roblox_id):
        return self.row("SELECT * FROM users WHERE roblox_id=?", (roblox_id,))

    def get_user_by_username(self, username):
        return self.row("SELECT * FROM users WHERE lower(username)=lower(?)", (username,))

    def create_user(self, tg_id, username, full_name, roblox_id, roblox_name, actor_name=None,
                    display_name=None):
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO users(tg_id, username, full_name, roblox_name, roblox_display_name, "
                "roblox_id, rank_code, status, joined_at) VALUES (?,?,?,?,?,?,?, 'active', ?)",
                (tg_id, username, full_name, roblox_name, display_name, roblox_id, DEFAULT_RANK, now()),
            )
            uid = cur.lastrowid
            self._log(c, tg_id, actor_name or username or full_name, "member_add",
                      f"Roblox: {roblox_name} (ID {roblox_id}), владение подтверждено кодом", uid)
        return uid

    def update_roblox_display_name(self, user_id, display_name):
        """Тихо обновляет Display Name (он может меняться чаще, чем username)."""
        return self.run("UPDATE users SET roblox_display_name=? WHERE id=? "
                        "AND COALESCE(roblox_display_name, '') != ?",
                        (display_name, user_id, display_name or "")) > 0

    def update_roblox_name(self, user_id, new_name, details=""):
        with self.tx() as c:
            old = c.execute("SELECT roblox_name FROM users WHERE id=?", (user_id,)).fetchone()
            if not old or old["roblox_name"] == new_name:
                return False
            c.execute("UPDATE users SET roblox_name=? WHERE id=?", (new_name, user_id))
            self._log(c, None, SYSTEM, "roblox_rename",
                      f"{old['roblox_name']} → {new_name}. {details}".strip(), user_id)
        return True

    def set_member_status(self, user_id, status, actor_tg=None, actor_name=None, reason=""):
        """status: 'active' (восстановление), 'left' (вышел из группы) или 'removed' (исключён).
        Возвращает True, если статус действительно изменился."""
        with self.tx() as c:
            u = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not u or u["status"] == status:
                return False
            ts = now()
            if status == "active":
                c.execute("UPDATE users SET status='active', left_at=NULL WHERE id=?", (user_id,))
                c.execute("UPDATE inactive_users SET restored_at=? WHERE user_id=? AND restored_at IS NULL",
                          (ts, user_id))
                self._log(c, actor_tg, actor_name, "member_restore", reason, user_id)
            else:
                c.execute("UPDATE users SET status=?, left_at=? WHERE id=?", (status, ts, user_id))
                c.execute("INSERT INTO inactive_users(user_id, left_at, reason) VALUES (?,?,?)",
                          (user_id, ts, reason))
                c.execute("UPDATE rank_requests SET status='cancelled', finished_at=? "
                          "WHERE user_id=? AND status IN ('pending','accepted')", (ts, user_id))
                action = "member_left" if status == "left" else "member_remove"
                self._log(c, actor_tg, actor_name, action, reason, user_id)
        return True

    def search_users(self, query, limit=10):
        q = (query or "").strip().lstrip("@").lower()
        if not q:
            return []
        sql = ("SELECT * FROM users WHERE instr(lower(roblox_name), ?) > 0 "
               "OR instr(lower(COALESCE(username, '')), ?) > 0")
        params = [q, q]
        if q.isdigit():
            sql += " OR tg_id=? OR roblox_id=?"
            params += [int(q), int(q)]
        sql += " ORDER BY roblox_name COLLATE NOCASE LIMIT ?"
        params.append(limit)
        return self.rows(sql, params)

    def active_members(self, rank=None):
        sql = ("SELECT u.* FROM users u JOIN ranks r ON r.code=u.rank_code WHERE u.status='active'")
        params = []
        if rank:
            sql += " AND u.rank_code=?"
            params.append(rank)
        sql += " ORDER BY r.level DESC, u.roblox_name COLLATE NOCASE"
        return self.rows(sql, params)

    def count_active(self):
        return self.row("SELECT COUNT(*) FROM users WHERE status='active'")[0]

    def top_page(self, limit, offset):
        return self.rows(
            "SELECT u.*, r.level FROM users u JOIN ranks r ON r.code=u.rank_code "
            "WHERE u.status='active' "
            "ORDER BY r.level DESC, COALESCE(u.last_wins, -1) DESC, u.pvp_count DESC, u.joined_at ASC "
            "LIMIT ? OFFSET ?", (limit, offset))

    def user_matches(self, user_id, limit=5):
        return self.rows("SELECT * FROM pvp_matches WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (user_id, limit))

    def add_violation(self, user_id, text, actor_tg, actor_name):
        self.log(actor_tg, actor_name, "violation", text, user_id)

    def violations(self, user_id, limit=5):
        return self.rows("SELECT * FROM history WHERE target_user_id=? AND action='violation' "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit))

    # ------------------------------------------------------------ работники и права
    def get_staff(self, staff_id):
        return self.row("SELECT * FROM staff WHERE id=?", (staff_id,))

    def get_staff_by_tg(self, tg_id):
        return self.row("SELECT * FROM staff WHERE tg_id=?", (tg_id,))

    def find_staff_by_username(self, username):
        return self.row("SELECT * FROM staff WHERE lower(username)=lower(?)", (username,))

    def list_staff(self):
        return self.rows("SELECT s.*, u.roblox_name FROM staff s "
                         "LEFT JOIN users u ON u.tg_id = s.tg_id ORDER BY s.id")

    def get_perms(self, staff_id):
        return {r["permission"] for r in
                self.rows("SELECT permission FROM permissions WHERE staff_id=?", (staff_id,))}

    def perms_map(self):
        result = {}
        for r in self.rows("SELECT staff_id, permission FROM permissions"):
            result.setdefault(r["staff_id"], set()).add(r["permission"])
        return result

    def has_permission(self, tg_id, permission) -> bool:
        return self.row(
            "SELECT 1 FROM staff s JOIN permissions p ON p.staff_id=s.id "
            "WHERE s.tg_id=? AND p.permission=?", (tg_id, permission)) is not None

    def staff_tg_with_perms(self, permissions):
        marks = ",".join("?" for _ in permissions)
        return [r["tg_id"] for r in self.rows(
            f"SELECT DISTINCT s.tg_id FROM staff s JOIN permissions p ON p.staff_id=s.id "
            f"WHERE s.tg_id IS NOT NULL AND p.permission IN ({marks})", list(permissions))]

    def add_staff(self, tg_id, username, position, perms, actor_tg, actor_name, details):
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO staff(tg_id, username, position, added_by, added_at) VALUES (?,?,?,?,?)",
                (tg_id, username, position, actor_tg, now()))
            sid = cur.lastrowid
            c.executemany("INSERT INTO permissions(staff_id, permission) VALUES (?,?)",
                          [(sid, p) for p in perms])
            self._log(c, actor_tg, actor_name, "staff_add", details)
        return sid

    def set_staff_position(self, staff_id, position, perms, actor_tg, actor_name, details):
        with self.tx() as c:
            c.execute("UPDATE staff SET position=? WHERE id=?", (position, staff_id))
            c.execute("DELETE FROM permissions WHERE staff_id=?", (staff_id,))
            c.executemany("INSERT INTO permissions(staff_id, permission) VALUES (?,?)",
                          [(staff_id, p) for p in perms])
            self._log(c, actor_tg, actor_name, "staff_position", details)

    def set_perms(self, staff_id, perms, actor_tg, actor_name, details):
        with self.tx() as c:
            c.execute("DELETE FROM permissions WHERE staff_id=?", (staff_id,))
            c.executemany("INSERT INTO permissions(staff_id, permission) VALUES (?,?)",
                          [(staff_id, p) for p in perms])
            self._log(c, actor_tg, actor_name, "perms_change", details)

    def remove_staff(self, staff_id, actor_tg, actor_name, details):
        with self.tx() as c:
            c.execute("DELETE FROM staff WHERE id=?", (staff_id,))
            self._log(c, actor_tg, actor_name, "staff_remove", details)

    # ------------------------------------------------------------ заявки и PvP
    def create_request(self, user_id):
        with self.tx() as c:
            u = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not u or u["status"] != "active":
                raise ValueError("Ты не состоишь в активном составе клана.")
            ts = now()
            if u["next_check_at"] and u["next_check_at"] > ts:
                raise CooldownError(u["next_check_at"] - ts)
            try:
                cur = c.execute("INSERT INTO rank_requests(user_id, status, created_at) "
                                "VALUES (?, 'pending', ?)", (user_id, ts))
            except sqlite3.IntegrityError:
                raise ValueError("У тебя уже есть активная заявка на проверку.")
            rid = cur.lastrowid
            self._log(c, u["tg_id"], u["username"] or u["roblox_name"], "request_create",
                      f"Заявка #{rid}", user_id)
        return rid

    _REQUEST_SQL = ("SELECT r.*, u.tg_id AS user_tg, u.username AS user_username, "
                    "u.roblox_name, u.rank_code, u.last_pvp_at
