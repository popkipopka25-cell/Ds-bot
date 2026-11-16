"""PostgreSQL (Neon): схема и все операции с данными DS Bot.

Все изменения, затрагивающие несколько таблиц, выполняются в одной транзакции.
История участников не удаляется: при выходе/исключении меняется только статус.
Данные лежат в Neon, поэтому переживают перезапуск Render, обновление кода и отключение бота.
"""
import json
import logging
import sqlite3  # нужен только как базовый класс DuplicateError (совместимость с members_system.py)
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg import errors as pgerrors
from psycopg_pool import ConnectionPool, PoolTimeout

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


class DuplicateError(sqlite3.IntegrityError):
    """Нарушение уникальности (Telegram ID или Roblox ID уже есть в базе).

    Наследуется от sqlite3.IntegrityError, чтобы существующий код
    `except sqlite3.IntegrityError` в members_system.py продолжал работать без правок.
    """


class Row(dict):
    """Строка результата: доступ и по имени колонки (r["id"]), и по номеру (r[0])."""
    __slots__ = ()

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []

    def make(values):
        return Row(zip(names, values))

    return make


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS ranks (
        code TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        level INTEGER NOT NULL UNIQUE,
        min_wins INTEGER NOT NULL,
        max_wins INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id BIGSERIAL PRIMARY KEY,
        tg_id BIGINT NOT NULL UNIQUE,
        username TEXT,
        full_name TEXT,
        roblox_name TEXT NOT NULL,
        roblox_id BIGINT NOT NULL UNIQUE,
        rank_code TEXT NOT NULL DEFAULT 'DSc' REFERENCES ranks(code),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'left', 'removed')),
        joined_at BIGINT NOT NULL,
        last_pvp_at BIGINT,
        next_check_at BIGINT,
        pvp_count INTEGER NOT NULL DEFAULT 0,
        last_wins INTEGER,
        left_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS staff (
        id BIGSERIAL PRIMARY KEY,
        tg_id BIGINT UNIQUE,
        username TEXT,
        "position" TEXT NOT NULL,
        added_by BIGINT,
        added_at BIGINT NOT NULL,
        CHECK (tg_id IS NOT NULL OR username IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS permissions (
        staff_id BIGINT NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        permission TEXT NOT NULL,
        PRIMARY KEY (staff_id, permission)
    )""",
    """CREATE TABLE IF NOT EXISTS rank_requests (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id),
        status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'done', 'cancelled')),
        created_at BIGINT NOT NULL,
        accepted_by BIGINT,
        accepted_at BIGINT,
        finished_at BIGINT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_open_request
        ON rank_requests(user_id) WHERE status IN ('pending', 'accepted')""",
    """CREATE TABLE IF NOT EXISTS pvp_matches (
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT NOT NULL UNIQUE REFERENCES rank_requests(id),
        user_id BIGINT NOT NULL REFERENCES users(id),
        checker_tg_id BIGINT NOT NULL,
        checker_wins INTEGER NOT NULL,
        user_wins INTEGER NOT NULL,
        old_rank TEXT NOT NULL REFERENCES ranks(code),
        new_rank TEXT NOT NULL REFERENCES ranks(code),
        created_at BIGINT NOT NULL,
        CHECK (checker_wins BETWEEN 0 AND 10 AND user_wins BETWEEN 0 AND 10),
        CHECK ((checker_wins = 10 AND user_wins < 10) OR (user_wins = 10 AND checker_wins < 10))
    )""",
    """CREATE TABLE IF NOT EXISTS history (
        id BIGSERIAL PRIMARY KEY,
        ts BIGINT NOT NULL,
        actor_tg_id BIGINT,
        actor_name TEXT NOT NULL,
        action TEXT NOT NULL,
        target_user_id BIGINT REFERENCES users(id),
        details TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts DESC)",
    """CREATE TABLE IF NOT EXISTS messages (
        key TEXT PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        text_hash TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS inactive_users (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id),
        left_at BIGINT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        restored_at BIGINT
    )""",
]

# Порядок важен из-за внешних ключей. Используется для резервной копии и переноса.
TABLES = ["ranks", "users", "staff", "permissions", "rank_requests", "pvp_matches",
          "history", "messages", "settings", "inactive_users"]

_TRANSIENT = (psycopg.OperationalError, PoolTimeout)


class Database:
    def __init__(self):
        self._pool = None
        self._init_lock = threading.Lock()
        self._identity_cache = {}

    # ------------------------------------------------------------ подключение
    def connect(self) -> None:
        """Открывает пул подключений к Neon и создаёт таблицы (если их ещё нет)."""
        with self._init_lock:
            if self._pool is not None:
                return
            if not config.DATABASE_URL:
                raise RuntimeError("Не задана переменная DATABASE_URL (строка подключения Neon).")
            pool = ConnectionPool(
                conninfo=config.DATABASE_URL,
                min_size=1,
                max_size=5,
                kwargs={
                    "autocommit": True,
                    "row_factory": _row_factory,
                    "prepare_threshold": None,   # совместимо и с pooled-адресом Neon (PgBouncer)
                    "connect_timeout": 15,
                },
                check=ConnectionPool.check_connection,  # мёртвые соединения (Neon засыпает) отбрасываются
                max_idle=240,
                max_lifetime=1800,
                timeout=45,                              # запас на «пробуждение» Neon
                open=False,
                name="ds-pool",
            )
            pool.open(wait=True, timeout=60)
            self._pool = pool
        self._init_schema()
        log.info("База данных Neon подключена, схема проверена.")

    def close(self) -> None:
        with self._init_lock:
            if self._pool is not None:
                try:
                    self._pool.close()
                finally:
                    self._pool = None

    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            self.connect()
        return self._pool

    def _init_schema(self) -> None:
        with self.pool.connection() as conn:
            with conn.transaction():
                for statement in SCHEMA:
                    conn.execute(statement)
                for code, title, level, low, high in RANKS:
                    conn.execute(
                        "INSERT INTO ranks(code, title, level, min_wins, max_wins) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT(code) DO UPDATE SET title=excluded.title, level=excluded.level, "
                        "min_wins=excluded.min_wins, max_wins=excluded.max_wins",
                        (code, title, level, low, high),
                    )

    # ------------------------------------------------------------ низкий уровень
    @contextmanager
    def tx(self):
        """Транзакция: всё или ничего. Возвращает соединение (c.execute(...) → курсор)."""
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    @staticmethod
    def _retry(func):
        for attempt in (1, 2):
            try:
                return func()
            except _TRANSIENT as exc:
                if attempt == 2:
                    raise
                log.warning("Временная ошибка базы (%s), повторяю запрос", exc.__class__.__name__)
                time.sleep(1.5)

    def rows(self, sql, params=()):
        def go():
            with self.pool.connection() as conn:
                return conn.execute(sql, params).fetchall()
        return self._retry(go)

    def row(self, sql, params=()):
        def go():
            with self.pool.connection() as conn:
                return conn.execute(sql, params).fetchone()
        return self._retry(go)

    def run(self, sql, params=()):
        def go():
            with self.pool.connection() as conn:
                return conn.execute(sql, params).rowcount
        return self._retry(go)

    # ------------------------------------------------------------ настройки
    def get_setting(self, key, default=None):
        r = self.row("SELECT value FROM settings WHERE key=%s", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.run(
            "INSERT INTO settings(key, value) VALUES (%s, %s) "
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
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (now(), actor_tg, actor_name or SYSTEM, action, target, details),
        )

    def log(self, actor_tg, actor_name, action, details="", target=None):
        with self.tx() as c:
            self._log(c, actor_tg, actor_name, action, details, target)

    def history_count(self):
        return self.row("SELECT COUNT(*) FROM history")[0]

    def history_page(self, limit, offset):
        return self.rows("SELECT * FROM history ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset))

    # ------------------------------------------------------------ личность (ник Telegram)
    def touch_identity(self, tg_id, username, full_name) -> bool:
        """Обновляет username; если работник был добавлен по @username — привязывает его ID.

        Если ник и имя не менялись с прошлого обращения, база не трогается (экономит запросы к Neon).
        """
        key = (username, full_name)
        if self._identity_cache.get(tg_id) == key:
            return False
        resolved = False
        with self.tx() as c:
            c.execute(
                "UPDATE users SET username=%s, full_name=%s WHERE tg_id=%s "
                "AND (username IS DISTINCT FROM %s OR full_name IS DISTINCT FROM %s)",
                (username, full_name, tg_id, username, full_name))
            if username:
                cur = c.execute(
                    "UPDATE staff SET tg_id=%s, username=%s WHERE tg_id IS NULL AND lower(username)=lower(%s)",
                    (tg_id, username, username))
                resolved = cur.rowcount > 0
                c.execute("UPDATE staff SET username=%s WHERE tg_id=%s AND username IS DISTINCT FROM %s",
                          (username, tg_id, username))
        if len(self._identity_cache) > 5000:
            self._identity_cache.clear()
        self._identity_cache[tg_id] = key
        return resolved

    # ------------------------------------------------------------ участники
    def get_user(self, user_id):
        return self.row("SELECT * FROM users WHERE id=%s", (user_id,))

    def get_user_by_tg(self, tg_id):
        return self.row("SELECT * FROM users WHERE tg_id=%s", (tg_id,))

    def get_user_by_roblox(self, roblox_id):
        return self.row("SELECT * FROM users WHERE roblox_id=%s", (roblox_id,))

    def get_user_by_username(self, username):
        return self.row("SELECT * FROM users WHERE lower(username)=lower(%s)", (username,))

    def create_user(self, tg_id, username, full_name, roblox_id, roblox_name, actor_name=None):
        try:
            with self.tx() as c:
                cur = c.execute(
                    "INSERT INTO users(tg_id, username, full_name, roblox_name, roblox_id, rank_code, "
                    "status, joined_at) VALUES (%s,%s,%s,%s,%s,%s, 'active', %s) RETURNING id",
                    (tg_id, username, full_name, roblox_name, roblox_id, DEFAULT_RANK, now()),
                )
                uid = cur.fetchone()["id"]
                self._log(c, tg_id, actor_name or username or full_name, "member_add",
                          f"Roblox: {roblox_name} (ID {roblox_id})", uid)
        except pgerrors.UniqueViolation as exc:
            raise DuplicateError(str(exc)) from exc
        return uid

    def update_roblox_name(self, user_id, new_name, details=""):
        with self.tx() as c:
            old = c.execute("SELECT roblox_name FROM users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
            if not old or old["roblox_name"] == new_name:
                return False
            c.execute("UPDATE users SET roblox_name=%s WHERE id=%s", (new_name, user_id))
            self._log(c, None, SYSTEM, "roblox_rename",
                      f"{old['roblox_name']} → {new_name}. {details}".strip(), user_id)
        return True

    def set_member_status(self, user_id, status, actor_tg=None, actor_name=None, reason=""):
        """status: 'active' (восстановление), 'left' (вышел из группы) или 'removed' (исключён).
        Возвращает True, если статус действительно изменился."""
        with self.tx() as c:
            u = c.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
            if not u or u["status"] == status:
                return False
            ts = now()
            if status == "active":
                c.execute("UPDATE users SET status='active', left_at=NULL WHERE id=%s", (user_id,))
                c.execute("UPDATE inactive_users SET restored_at=%s WHERE user_id=%s AND restored_at IS NULL",
                          (ts, user_id))
                self._log(c, actor_tg, actor_name, "member_restore", reason, user_id)
            else:
                c.execute("UPDATE users SET status=%s, left_at=%s WHERE id=%s", (status, ts, user_id))
                c.execute("INSERT INTO inactive_users(user_id, left_at, reason) VALUES (%s,%s,%s)",
                          (user_id, ts, reason))
                c.execute("UPDATE rank_requests SET status='cancelled', finished_at=%s "
                          "WHERE user_id=%s AND status IN ('pending','accepted')", (ts, user_id))
                action = "member_left" if status == "left" else "member_remove"
                self._log(c, actor_tg, actor_name, action, reason, user_id)
        return True

    def search_users(self, query, limit=10):
        q = (query or "").strip().lstrip("@").lower()
        if not q:
            return []
        sql = ("SELECT * FROM users WHERE position(%s::text in lower(roblox_name)) > 0 "
               "OR position(%s::text in lower(COALESCE(username, ''))) > 0")
        params = [q, q]
        if q.isascii() and q.isdigit() and len(q) <= 18:
            sql += " OR tg_id=%s OR roblox_id=%s"
            params += [int(q), int(q)]
        sql += " ORDER BY lower(roblox_name) LIMIT %s"
        params.append(limit)
        return self.rows(sql, params)

    def active_members(self, rank=None):
        sql = "SELECT u.* FROM users u JOIN ranks r ON r.code=u.rank_code WHERE u.status='active'"
        params = []
        if rank:
            sql += " AND u.rank_code=%s"
            params.append(rank)
        sql += " ORDER BY r.level DESC, lower(u.roblox_name)"
        return self.rows(sql, params)

    def count_active(self):
        return self.row("SELECT COUNT(*) FROM users WHERE status='active'")[0]

    def top_page(self, limit, offset):
        return self.rows(
            "SELECT u.*, r.level FROM users u JOIN ranks r ON r.code=u.rank_code "
            "WHERE u.status='active' "
            "ORDER BY r.level DESC, COALESCE(u.last_wins, -1) DESC, u.pvp_count DESC, u.joined_at ASC "
            "LIMIT %s OFFSET %s", (limit, offset))

    def user_matches(self, user_id, limit=5):
        return self.rows("SELECT * FROM pvp_matches WHERE user_id=%s ORDER BY id DESC LIMIT %s",
                         (user_id, limit))

    def add_violation(self, user_id, text, actor_tg, actor_name):
        self.log(actor_tg, actor_name, "violation", text, user_id)

    def violations(self, user_id, limit=5):
        return self.rows("SELECT * FROM history WHERE target_user_id=%s AND action='violation' "
                         "ORDER BY id DESC LIMIT %s", (user_id, limit))

    # ------------------------------------------------------------ работники и права
    def get_staff(self, staff_id):
        return self.row("SELECT * FROM staff WHERE id=%s", (staff_id,))

    def get_staff_by_tg(self, tg_id):
        return self.row("SELECT * FROM staff WHERE tg_id=%s", (tg_id,))

    def find_staff_by_username(self, username):
        return self.row("SELECT * FROM staff WHERE lower(username)=lower(%s)", (username,))

    def list_staff(self):
        return self.rows("SELECT s.*, u.roblox_name FROM staff s "
                         "LEFT JOIN users u ON u.tg_id = s.tg_id ORDER BY s.id")

    def get_perms(self, staff_id):
        return {r["permission"] for r in
                self.rows("SELECT permission FROM permissions WHERE staff_id=%s", (staff_id,))}

    def perms_map(self):
        result = {}
        for r in self.rows("SELECT staff_id, permission FROM permissions"):
            result.setdefault(r["staff_id"], set()).add(r["permission"])
        return result

    def has_permission(self, tg_id, permission) -> bool:
        return self.row(
            "SELECT 1 FROM staff s JOIN permissions p ON p.staff_id=s.id "
            "WHERE s.tg_id=%s AND p.permission=%s", (tg_id, permission)) is not None

    def staff_tg_with_perms(self, permissions):
        return [r["tg_id"] for r in self.rows(
            "SELECT DISTINCT s.tg_id FROM staff s JOIN permissions p ON p.staff_id=s.id "
            "WHERE s.tg_id IS NOT NULL AND p.permission = ANY(%s)", (list(permissions),))]

    def add_staff(self, tg_id, username, position, perms, actor_tg, actor_name, details):
        with self.tx() as c:
            cur = c.execute(
                'INSERT INTO staff(tg_id, username, "position", added_by, added_at) '
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (tg_id, username, position, actor_tg, now()))
            sid = cur.fetchone()["id"]
            for p in perms:
                c.execute("INSERT INTO permissions(staff_id, permission) VALUES (%s,%s)", (sid, p))
            self._log(c, actor_tg, actor_name, "staff_add", details)
        self._identity_cache.clear()  # чтобы работник, добавленный по @username, привязался при первом сообщении
        return sid

    def set_staff_position(self, staff_id, position, perms, actor_tg, actor_name, details):
        with self.tx() as c:
            c.execute('UPDATE staff SET "position"=%s WHERE id=%s', (position, staff_id))
            c.execute("DELETE FROM permissions WHERE staff_id=%s", (staff_id,))
            for p in perms:
                c.execute("INSERT INTO permissions(staff_id, permission) VALUES (%s,%s)", (staff_id, p))
            self._log(c, actor_tg, actor_name, "staff_position", details)

    def set_perms(self, staff_id, perms, actor_tg, actor_name, details):
        with self.tx() as c:
            c.execute("DELETE FROM permissions WHERE staff_id=%s", (staff_id,))
            for p in perms:
                c.execute("INSERT INTO permissions(staff_id, permission) VALUES (%s,%s)", (staff_id, p))
            self._log(c, actor_tg, actor_name, "perms_change", details)

    def remove_staff(self, staff_id, actor_tg, actor_name, details):
        with self.tx() as c:
            c.execute("DELETE FROM staff WHERE id=%s", (staff_id,))
            self._log(c, actor_tg, actor_name, "staff_remove", details)

    # ------------------------------------------------------------ заявки и PvP
    def create_request(self, user_id):
        with self.tx() as c:
            u = c.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
            if not u or u["status"] != "active":
                raise ValueError("Ты не состоишь в активном составе клана.")
            ts = now()
            if u["next_check_at"] and u["next_check_at"] > ts:
                raise CooldownError(u["next_check_at"] - ts)
            try:
                cur = c.execute("INSERT INTO rank_requests(user_id, status, created_at) "
                                "VALUES (%s, 'pending', %s) RETURNING id", (user_id, ts))
            except pgerrors.UniqueViolation:
                raise ValueError("У тебя уже есть активная заявка на проверку.")
            rid = cur.fetchone()["id"]
            self._log(c, u["tg_id"], u["username"] or u["roblox_name"], "request_create",
                      f"Заявка #{rid}", user_id)
        return rid

    _REQUEST_SQL = ("SELECT r.*, u.tg_id AS user_tg, u.username AS user_username, "
                    "u.roblox_name, u.rank_code, u.last_pvp_at, u.status AS user_status "
                    "FROM rank_requests r JOIN users u ON u.id = r.user_id ")

    def get_request(self, rid):
        return self.row(self._REQUEST_SQL + "WHERE r.id=%s", (rid,))

    def user_open_request(self, user_id):
        return self.row(self._REQUEST_SQL +
                        "WHERE r.user_id=%s AND r.status IN ('pending','accepted')", (user_id,))

    def pending_requests(self):
        return self.rows(self._REQUEST_SQL + "WHERE r.status='pending' ORDER BY r.id")

    def accepted_requests(self, by_tg=None):
        if by_tg is None:
            return self.rows(self._REQUEST_SQL + "WHERE r.status='accepted' ORDER BY r.id")
        return self.rows(self._REQUEST_SQL + "WHERE r.status='accepted' AND r.accepted_by=%s ORDER BY r.id",
                         (by_tg,))

    def accept_request(self, rid, checker_tg, checker_name):
        with self.tx() as c:
            r = c.execute(self._REQUEST_SQL + "WHERE r.id=%s", (rid,)).fetchone()
            if not r:
                raise ValueError("Заявка не найдена.")
            if r["user_tg"] == checker_tg:
                raise ValueError("Нельзя проверять самого себя.")
            cur = c.execute("UPDATE rank_requests SET status='accepted', accepted_by=%s, accepted_at=%s "
                            "WHERE id=%s AND status='pending'", (checker_tg, now(), rid))
            if cur.rowcount != 1:
                raise ValueError("Заявка уже принята другим проверяющим или отменена.")
            self._log(c, checker_tg, checker_name, "request_accept", f"Заявка #{rid}", r["user_id"])
        return dict(r)

    def release_request(self, rid, tg_id, actor_name, force=False):
        with self.tx() as c:
            r = c.execute("SELECT * FROM rank_requests WHERE id=%s", (rid,)).fetchone()
            if not r:
                return False
            sql = ("UPDATE rank_requests SET status='pending', accepted_by=NULL, accepted_at=NULL "
                   "WHERE id=%s AND status='accepted'")
            params = [rid]
            if not force:
                sql += " AND accepted_by=%s"
                params.append(tg_id)
            cur = c.execute(sql, params)
            if cur.rowcount != 1:
                return False
            self._log(c, tg_id, actor_name, "request_release", f"Заявка #{rid} возвращена в очередь",
                      r["user_id"])
        return True

    def cancel_request(self, rid, actor_tg, actor_name, only_user_id=None):
        with self.tx() as c:
            r = c.execute(self._REQUEST_SQL + "WHERE r.id=%s FOR UPDATE OF r", (rid,)).fetchone()
            if not r or r["status"] not in ("pending", "accepted"):
                return None
            if only_user_id is not None and r["user_id"] != only_user_id:
                return None
            c.execute("UPDATE rank_requests SET status='cancelled', finished_at=%s WHERE id=%s",
                      (now(), rid))
            self._log(c, actor_tg, actor_name, "request_cancel", f"Заявка #{rid} отменена", r["user_id"])
        return dict(r)

    def complete_pvp(self, request_id, checker_tg, checker_name, checker_wins, user_wins):
        """Вносит результат матча и автоматически пересчитывает ранг — всё в одной транзакции."""
        validate_scores(checker_wins, user_wins)
        with self.tx() as c:
            r = c.execute(self._REQUEST_SQL + "WHERE r.id=%s FOR UPDATE OF r, u", (request_id,)).fetchone()
            if not r or r["status"] != "accepted":
                raise ValueError("Заявка не принята или уже обработана.")
            if r["accepted_by"] != checker_tg and checker_tg != config.OWNER_ID:
                raise PermissionError("Эту заявку принял другой проверяющий.")
            if r["user_status"] != "active":
                raise ValueError("Участник больше не состоит в клане.")
            old_rank = r["rank_code"]
            new_rank, change = evaluate(old_rank, user_wins)
            ts = now()
            next_check = ts + config.CHECK_COOLDOWN_SECONDS
            cur = c.execute("UPDATE rank_requests SET status='done', finished_at=%s "
                            "WHERE id=%s AND status='accepted'", (ts, request_id))
            if cur.rowcount != 1:
                raise ValueError("Заявка уже обработана.")
            try:
                c.execute("INSERT INTO pvp_matches(request_id, user_id, checker_tg_id, checker_wins, "
                          "user_wins, old_rank, new_rank, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                          (request_id, r["user_id"], checker_tg, checker_wins, user_wins,
                           old_rank, new_rank, ts))
            except pgerrors.UniqueViolation:
                raise ValueError("Этот матч уже зарегистрирован.")
            c.execute("UPDATE users SET rank_code=%s, last_pvp_at=%s, next_check_at=%s, "
                      "pvp_count=pvp_count+1, last_wins=%s WHERE id=%s",
                      (new_rank, ts, next_check, user_wins, r["user_id"]))
            self._log(c, checker_tg, checker_name, "pvp",
                      f"Заявка #{request_id}: проверяющий {checker_wins} — участник {user_wins}",
                      r["user_id"])
            if new_rank != old_rank:
                self._log(c, checker_tg, checker_name, "rank_change", f"{old_rank} → {new_rank}",
                          r["user_id"])
        return {"user_id": r["user_id"], "user_tg": r["user_tg"], "old_rank": old_rank,
                "new_rank": new_rank, "change": change, "next_check_at": next_check}

    # ------------------------------------------------------------ статистика
    def stats(self):
        def one(sql, params=()):
            return self.row(sql, params)[0]
        return {
            "total": one("SELECT COUNT(*) FROM users"),
            "active": one("SELECT COUNT(*) FROM users WHERE status='active'"),
            "inactive": one("SELECT COUNT(*) FROM users WHERE status!='active'"),
            "ranks": {r["code"]: r["n"] for r in self.rows(
                "SELECT r.code, (SELECT COUNT(*) FROM users u WHERE u.rank_code=r.code "
                "AND u.status='active') AS n FROM ranks r")},
            "staff": one("SELECT COUNT(*) FROM staff"),
            "checkers": one("SELECT COUNT(DISTINCT staff_id) FROM permissions WHERE permission='pvp_result'"),
            "pvp": one("SELECT COUNT(*) FROM pvp_matches"),
            "requests": one("SELECT COUNT(*) FROM rank_requests"),
            "pending": one("SELECT COUNT(*) FROM rank_requests WHERE status IN ('pending','accepted')"),
        }

    # ------------------------------------------------------------ опубликованные сообщения
    def get_message(self, key):
        return self.row("SELECT * FROM messages WHERE key=%s", (key,))

    def set_message(self, key, chat_id, message_id, text_hash):
        self.run("INSERT INTO messages(key, chat_id, message_id, text_hash) VALUES (%s,%s,%s,%s) "
                 "ON CONFLICT(key) DO UPDATE SET chat_id=excluded.chat_id, "
                 "message_id=excluded.message_id, text_hash=excluded.text_hash",
                 (key, chat_id, message_id, text_hash))

    def delete_message(self, key):
        self.run("DELETE FROM messages WHERE key=%s", (key,))

    def messages_with_prefix(self, prefix):
        return self.rows("SELECT * FROM messages WHERE key LIKE %s", (prefix + "%",))

    # ------------------------------------------------------------ резервная копия (JSON-выгрузка)
    def backup(self):
        """Выгружает все таблицы в JSON-файл и возвращает путь к нему.

        Основное хранилище — Neon; этот файл нужен как дополнительная копия «на руки»
        (кнопка «Резервная копия базы» в панели владельца и ежедневная отправка владельцу).
        """
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.BACKUP_DIR / f"ds_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        dump = {"created_at": now(), "tables": {}}
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                for table in TABLES:
                    dump["tables"][table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        dest.write_text(json.dumps(dump, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        files = sorted(config.BACKUP_DIR.glob("ds_backup_*.json"))
        for old in files[:-config.BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest


db = Database()
