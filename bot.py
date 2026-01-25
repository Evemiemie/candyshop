import os
import re
import time
from typing import Optional, List, Tuple, Set

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()
DB_PATH = "cs_helper.db"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


# =========================
# DB
# =========================
async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            thread_id INTEGER,
            title TEXT NOT NULL,
            roles_text TEXT NOT NULL,
            after_text TEXT,
            ends_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            payout_role_id INTEGER,
            payout_role_name TEXT
        );
        """)

        # Слоты: один пользователь -> один слот, один слот -> один пользователь
        await db.execute("""
        CREATE TABLE IF NOT EXISTS content_assignments (
            content_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_index INTEGER NOT NULL,
            assigned_at INTEGER NOT NULL,
            PRIMARY KEY (content_id, user_id),
            UNIQUE (content_id, role_index)
        );
        """)

        # Attendance: дополнительные участники "выплаты" (опоздавшие, замены и т.п.)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS content_attendance (
            content_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            added_by INTEGER NOT NULL,
            added_at INTEGER NOT NULL,
            PRIMARY KEY (content_id, user_id)
        );
        """)

                # =========================
        # Attendance Leaderboard (UnbelievaBoat-like)
        # =========================

        # Статистика аттенденсов по серверу (all_time + week)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS attendance_stats (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            all_time INTEGER NOT NULL DEFAULT 0,
            week INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        # Чтобы один и тот же контент не засчитывался одному и тому же человеку несколько раз
        await db.execute("""
        CREATE TABLE IF NOT EXISTS attendance_awards (
            guild_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            awarded_by INTEGER NOT NULL,
            awarded_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, content_id, user_id)
        );
        """)

        # Лог изменений аттенденсов (для аудита, отчётов и weekly reset)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS attendance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content_id INTEGER,
            delta INTEGER NOT NULL,
            kind TEXT NOT NULL,
            actor_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)

        # Отметки weekly reset
        await db.execute("""
        CREATE TABLE IF NOT EXISTS attendance_weekly_resets (
            guild_id INTEGER NOT NULL,
            reset_at INTEGER NOT NULL,
            reset_by INTEGER NOT NULL
        );
        """)



        await db.commit()


async def db_create_content(
    guild_id: int,
    channel_id: int,
    title: str,
    roles_text: str,
    after_text: Optional[str],
    ends_at: int,
    created_by: int,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO contents (guild_id, channel_id, title, roles_text, after_text, ends_at, status, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, title, roles_text, after_text, ends_at, STATUS_OPEN, created_by, int(time.time()))
        )
        await db.commit()
        return cur.lastrowid


async def db_set_message_thread(content_id: int, message_id: int, thread_id: Optional[int]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE contents SET message_id = ?, thread_id = ? WHERE id = ?",
            (message_id, thread_id, content_id)
        )
        await db.commit()


async def db_get_content_by_id(content_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM contents WHERE id = ?", (content_id,))
        return await cur.fetchone()


async def db_get_content_by_thread(thread_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM contents WHERE thread_id = ?", (thread_id,))
        return await cur.fetchone()


async def db_close_content(content_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE contents SET status = ? WHERE id = ?", (STATUS_CLOSED, content_id))
        await db.commit()


async def db_set_payout_role(content_id: int, role_id: int, role_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE contents SET payout_role_id = ?, payout_role_name = ? WHERE id = ?",
            (role_id, role_name, content_id)
        )
        await db.commit()


# ---------- slots ----------
async def db_assign_user(content_id: int, user_id: int, role_index: int) -> Tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM content_assignments WHERE content_id = ? AND role_index = ?",
            (content_id, role_index)
        )
        row = await cur.fetchone()
        if row is not None and int(row[0]) != int(user_id):
            return False, "Роль занята."

        await db.execute(
            "DELETE FROM content_assignments WHERE content_id = ? AND user_id = ?",
            (content_id, user_id)
        )

        await db.execute(
            "INSERT INTO content_assignments (content_id, user_id, role_index, assigned_at) VALUES (?, ?, ?, ?)",
            (content_id, user_id, role_index, int(time.time()))
        )
        await db.commit()

    return True, f"Записан на роль {role_index}."


async def db_unassign_user(content_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM content_assignments WHERE content_id = ? AND user_id = ?",
            (content_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def db_unassign_by_role_index(content_id: int, role_index: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM content_assignments WHERE content_id = ? AND role_index = ?",
            (content_id, role_index)
        )
        row = await cur.fetchone()
        if row is None:
            return None

        user_id = int(row[0])
        await db.execute(
            "DELETE FROM content_assignments WHERE content_id = ? AND role_index = ?",
            (content_id, role_index)
        )
        await db.commit()
        return user_id


async def db_get_roster(content_id: int) -> List[Tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT role_index, user_id FROM content_assignments WHERE content_id = ? ORDER BY role_index ASC",
            (content_id,)
        )
        rows = await cur.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]


# ---------- attendance (late joiners) ----------
async def db_attend_add_many(content_id: int, user_ids: List[int], added_by: int) -> Tuple[int, int]:
    """returns (added_count, already_count)"""
    added = 0
    already = 0
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        for uid in user_ids:
            try:
                await db.execute(
                    "INSERT INTO content_attendance (content_id, user_id, added_by, added_at) VALUES (?, ?, ?, ?)",
                    (content_id, uid, added_by, now)
                )
                added += 1
            except aiosqlite.IntegrityError:
                already += 1
        await db.commit()
    return added, already


async def db_attend_remove(content_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM content_attendance WHERE content_id = ? AND user_id = ?",
            (content_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def db_attend_list(content_id: int) -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM content_attendance WHERE content_id = ? ORDER BY added_at ASC",
            (content_id,)
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


# ---------- union helpers ----------
async def db_get_all_participants(content_id: int) -> List[int]:
    """unique union: slots + attendance"""
    roster = await db_get_roster(content_id)
    attend = await db_attend_list(content_id)
    s: List[int] = []
    seen: Set[int] = set()
    for _, uid in roster:
        if uid not in seen:
            seen.add(uid)
            s.append(uid)
    for uid in attend:
        if uid not in seen:
            seen.add(uid)
            s.append(uid)
    return s

# =========================
# ATTENDANCE LEADERBOARD DB
# =========================
async def _att_stats_ensure(guild_id: int, user_id: int) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO attendance_stats (guild_id, user_id, all_time, week, updated_at)
            VALUES (?, ?, 0, 0, ?)
        """, (guild_id, user_id, now))
        await db.execute("""
            UPDATE attendance_stats SET updated_at = ? WHERE guild_id = ? AND user_id = ?
        """, (now, guild_id, user_id))
        await db.commit()


async def db_att_award_for_content(
    guild_id: int,
    content_id: int,
    user_ids: List[int],
    awarded_by: int,
) -> Tuple[int, int]:
    """
    Засчитывает аттенденсы за конкретный content_id.
    Гарантия: один и тот же user_id для данного content_id не будет начислен дважды.
    returns (awarded_count, already_count)
    """
    now = int(time.time())
    awarded = 0
    already = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for uid in user_ids:
            # ensure stats row
            await db.execute("""
                INSERT OR IGNORE INTO attendance_stats (guild_id, user_id, all_time, week, updated_at)
                VALUES (?, ?, 0, 0, ?)
            """, (guild_id, uid, now))

            try:
                await db.execute("""
                    INSERT INTO attendance_awards (guild_id, content_id, user_id, awarded_by, awarded_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (guild_id, content_id, uid, awarded_by, now))
            except aiosqlite.IntegrityError:
                already += 1
                continue

            # increment stats
            await db.execute("""
                UPDATE attendance_stats
                SET all_time = all_time + 1,
                    week = week + 1,
                    updated_at = ?
                WHERE guild_id = ? AND user_id = ?
            """, (now, guild_id, uid))

            # event log
            await db.execute("""
                INSERT INTO attendance_events (guild_id, user_id, content_id, delta, kind, actor_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, uid, content_id, 1, "content_award", awarded_by, now))

            awarded += 1

        await db.commit()

    return awarded, already


async def db_att_remove_for_content(
    guild_id: int,
    content_id: int,
    user_id: int,
    removed_by: int,
) -> Tuple[bool, str]:
    """
    Удаляет аттенденс у user_id за конкретный content_id (если он был начислен).
    """
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT 1 FROM attendance_awards
            WHERE guild_id = ? AND content_id = ? AND user_id = ?
        """, (guild_id, content_id, user_id))
        row = await cur.fetchone()
        if row is None:
            return False, "У пользователя нет аттенденса за этот контент."

        await db.execute("""
            DELETE FROM attendance_awards
            WHERE guild_id = ? AND content_id = ? AND user_id = ?
        """, (guild_id, content_id, user_id))

        # ensure stats row exists (на всякий)
        await db.execute("""
            INSERT OR IGNORE INTO attendance_stats (guild_id, user_id, all_time, week, updated_at)
            VALUES (?, ?, 0, 0, ?)
        """, (guild_id, user_id, now))

        # decrement stats (не уходим в минус)
        await db.execute("""
            UPDATE attendance_stats
            SET all_time = CASE WHEN all_time > 0 THEN all_time - 1 ELSE 0 END,
                week = CASE WHEN week > 0 THEN week - 1 ELSE 0 END,
                updated_at = ?
            WHERE guild_id = ? AND user_id = ?
        """, (now, guild_id, user_id))

        # event log
        await db.execute("""
            INSERT INTO attendance_events (guild_id, user_id, content_id, delta, kind, actor_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, content_id, -1, "content_remove", removed_by, now))

        await db.commit()

    return True, "Аттенденс удалён."


async def db_att_leaderboard(guild_id: int, scope: str = "week", limit: int = 10) -> List[Tuple[int, int]]:
    """
    scope: 'week' or 'all_time'
    returns list of (user_id, count)
    """
    col = "week" if scope == "week" else "all_time"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"""
            SELECT user_id, {col}
            FROM attendance_stats
            WHERE guild_id = ?
            ORDER BY {col} DESC, updated_at ASC
            LIMIT ?
        """, (guild_id, limit))
        rows = await cur.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]


async def db_att_get_user(guild_id: int, user_id: int) -> Tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT all_time, week FROM attendance_stats
            WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))
        row = await cur.fetchone()
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])


async def db_att_weekly_reset(guild_id: int, reset_by: int) -> Tuple[int, List[Tuple[int, int]]]:
    """
    Обнуляет week всем, кто имеет week > 0.
    Возвращает (affected_users, snapshot_top) где snapshot_top = топ (user_id, week) ДО сброса.
    """
    now = int(time.time())
    snapshot_top = await db_att_leaderboard(guild_id, scope="week", limit=25)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(*) FROM attendance_stats
            WHERE guild_id = ? AND week > 0
        """, (guild_id,))
        cnt_row = await cur.fetchone()
        affected = int(cnt_row[0]) if cnt_row else 0

        await db.execute("""
            UPDATE attendance_stats
            SET week = 0, updated_at = ?
            WHERE guild_id = ?
        """, (now, guild_id))

        await db.execute("""
            INSERT INTO attendance_weekly_resets (guild_id, reset_at, reset_by)
            VALUES (?, ?, ?)
        """, (guild_id, now, reset_by))

        await db.execute("""
            INSERT INTO attendance_events (guild_id, user_id, content_id, delta, kind, actor_id, created_at)
            VALUES (?, ?, NULL, 0, 'weekly_reset', ?, ?)
        """, (guild_id, reset_by, reset_by, now))

        await db.commit()

    return affected, snapshot_top


# =========================
# HELPERS
# =========================
def ts_discord(unix_ts: int) -> str:
    return f"<t:{unix_ts}:F>"


def normalize_roles_lines(raw: str) -> List[str]:
    # Каждая строка = роль
    lines = [ln.strip() for ln in raw.splitlines()]
    return [ln for ln in lines if ln]


def is_organizer_or_admin(member: discord.Member, created_by: int) -> bool:
    if member.id == created_by:
        return True
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.manage_roles


def _fmt_lb(title: str, rows: List[Tuple[int, int]]) -> str:
    lines: List[str] = []
    lines.append(f"**{title}**")
    if not rows:
        lines.append("_пока пусто_")
        return "\n".join(lines)

    for i, (uid, cnt) in enumerate(rows, start=1):
        lines.append(f"{i}. <@{uid}> — **{cnt}**")
    return "\n".join(lines)



# FIX: корректный парсинг <@123> и <@!123>
MENTION_ID_RE = re.compile(r"<@!?(\\d+)>".replace("\\\\d", r"\d"))  # безопасно для копипасты
# фактически это эквивалентно: re.compile(r"<@!?(\d+)>")

def parse_user_ids_from_text(text: str) -> List[int]:
    ids: List[int] = []
    for m in MENTION_ID_RE.finditer(text):
        ids.append(int(m.group(1)))
    # unique preserve order
    seen = set()
    out = []
    for uid in ids:
        if uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def _ends_at_line(ends_at: int) -> str:
    """
    ends_at:
      0  -> таймер отключён
      >0 -> таймер 
    """
    if ends_at and ends_at > 0:
        return f"Запись до: {ts_discord(ends_at)}"
    return "Запись до: _без таймера (закрытие вручную)_"


def _deadline_expired(ends_at: int) -> bool:
    return bool(ends_at and ends_at > 0 and int(time.time()) > int(ends_at))


def build_main_post_text(
    content_id: int,
    title: str,
    status: str,
    ends_at: int,
    message_id: int,
    thread_id: Optional[int],
    roles_lines: List[str],
    roster: List[Tuple[int, int]],
    attendance_only: List[int],
    after_text: Optional[str],
) -> str:
    by_index = {idx: uid for idx, uid in roster}
    participants_count = len({uid for _, uid in roster}) + len(attendance_only)

    lines: List[str] = []
    lines.append(f"**Контент #{content_id}: {title}**")
    lines.append(f"Статус: `{status}`")
    lines.append(_ends_at_line(int(ends_at)))
    if _deadline_expired(int(ends_at)):
        lines.append("⚠️ Таймер записи истёк (самозапись закрыта, организатор может добавлять вручную).")
    lines.append(f"Участники: **{participants_count}**")
    lines.append(f"ID: `{message_id}`")
    if thread_id:
        lines.append(f"Ветка: <#{thread_id}>")
    lines.append("")
    lines.append("**Роли**")
    for i, role_name in enumerate(roles_lines, start=1):
        uid = by_index.get(i)
        if uid:
            lines.append(f"{i}. {role_name} — <@{uid}>")
        else:
            lines.append(f"{i}. {role_name} — _свободно_")

    if attendance_only:
        lines.append("")
        lines.append("**Дополнительно**")
        start = len(roles_lines) + 1
        for j, uid in enumerate(attendance_only, start=start):
            lines.append(f"{j}. <@{uid}>")

    if after_text and after_text.strip():
        lines.append("")
        lines.append("**Примечание**")
        lines.append(after_text.strip())

    lines.append("")
    lines.append("**В ветке:**")
    lines.append("➣ `1` — занять роль (если свободна)")
    lines.append("➣ `-` — выписаться с роли")
    return "\n".join(lines)


async def refresh_main_post(guild: discord.Guild, content_row) -> None:
    try:
        channel = guild.get_channel(int(content_row["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return

        msg = await channel.fetch_message(int(content_row["message_id"]))
        roles_lines = normalize_roles_lines(str(content_row["roles_text"]))
        roster = await db_get_roster(int(content_row["id"]))
        attend = await db_attend_list(int(content_row["id"]))

        roster_uids = {uid for _, uid in roster}
        attendance_only = [uid for uid in attend if uid not in roster_uids]

        text = build_main_post_text(
            content_id=int(content_row["id"]),
            title=str(content_row["title"]),
            status=str(content_row["status"]),
            ends_at=int(content_row["ends_at"]),
            message_id=int(content_row["message_id"]),
            thread_id=int(content_row["thread_id"]) if content_row["thread_id"] else None,
            roles_lines=roles_lines,
            roster=roster,
            attendance_only=attendance_only,
            after_text=str(content_row["after_text"]) if content_row["after_text"] else None
        )

        await msg.edit(content=text, embed=None)
    except Exception:
        return


def parse_thread_command(text: str) -> Tuple[str, Optional[int]]:
    """
    kind:
      self_join(number)       : "2"
      self_leave              : "-"
      org_attend_add          : "+ @user @user"
      org_assign_slot(number) : "+2 @user"
      org_kick_role(number)   : "-5"
      org_kick_user           : "- @user"
      help                    : "help"
      unknown
    """
    t = text.strip()

    if t == "" or t.lower() in ("help",):
        return "help", None

    if t == "-":
        return "self_leave", None

    if t.isdigit():
        return "self_join", int(t)

    m = re.match(r"^\+(\d+)\s+.+$", t)
    if m:
        return "org_assign_slot", int(m.group(1))

    if t.startswith("+"):
        return "org_attend_add", None

    m = re.match(r"^-(\d+)$", t)
    if m:
        return "org_kick_role", int(m.group(1))

    if t.startswith("-"):
        return "org_kick_user", None

    return "unknown", None


# =========================
# BOT
# =========================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


@bot.event
async def on_ready():
    await db_init()
    try:
        synced = await bot.tree.sync()
        print(f"Ready. Synced commands: {len(synced)}")
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user} (id={bot.user.id})")


# =========================
# MODALS
# =========================
class ContentCreateModal(discord.ui.Modal, title="Создать контент"):
    title_text = discord.ui.TextInput(
        label="Заголовок",
        placeholder="Например: пути неисповедимы",
        max_length=150
    )
    roles_text = discord.ui.TextInput(
        label="Роли (каждая строка = слот)",
        placeholder="Танк\nХил\nДД\nДД\nДД\nСтоп",
        style=discord.TextStyle.paragraph,
        max_length=1500
    )
    after_text = discord.ui.TextInput(
        label="Примечание (опционально)",
        placeholder="Например: /join NickName",
        required=False,
        max_length=900
    )
    thread_name = discord.ui.TextInput(
        label="Имя ветки (опционально)",
        placeholder="Если пусто — будет как заголовок",
        required=False,
        max_length=100
    )

    def __init__(self, duration_minutes: int, create_thread: bool, auto_assign_organizer: bool = True):
        super().__init__()
        self.duration_minutes = duration_minutes
        self.create_thread = create_thread
        self.auto_assign_organizer = auto_assign_organizer

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        roles_lines = normalize_roles_lines(str(self.roles_text))
        if not roles_lines:
            await interaction.followup.send("Нужно указать хотя бы одну роль.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Команду нужно запускать в текстовом канале.", ephemeral=True)
            return

        # Таймер теперь необязательный:
        # duration_minutes == 0 -> ends_at = 0 (таймера нет)
        if self.duration_minutes and self.duration_minutes > 0:
            ends_at = int(time.time()) + self.duration_minutes * 60
        else:
            ends_at = 0

        title = str(self.title_text).strip()
        after = str(self.after_text).strip() if str(self.after_text).strip() else None

        content_id = await db_create_content(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            title=title,
            roles_text="\n".join(roles_lines),
            after_text=after,
            ends_at=ends_at,
            created_by=interaction.user.id
        )

        msg = await channel.send(content=f"**CS Контент #{content_id}: {title}**\nСоздание…")
        message_id = msg.id

        thread_id = None
        if self.create_thread:
            try:
                tname = str(self.thread_name).strip() if str(self.thread_name).strip() else f"{title} (CS#{content_id})"
                thread = await msg.create_thread(name=tname, auto_archive_duration=1440)
                thread_id = thread.id
                await thread.send(
                    "Пишите **ЦИФРУ** чтобы занять соответствующую роль.\n"
                    "Отправьте `-` чтобы выписаться.\n"
                )
            except discord.Forbidden:
                thread_id = None

        await db_set_message_thread(content_id, message_id, thread_id)

        if self.auto_assign_organizer and len(roles_lines) >= 1:
            await db_assign_user(content_id, interaction.user.id, 1)

        row = await db_get_content_by_id(content_id)
        if row and interaction.guild:
            await refresh_main_post(interaction.guild, row)

        await interaction.followup.send(f"Контент создан: CS#{content_id}", ephemeral=True)


class AttendAddModal(discord.ui.Modal, title="Добавить людей"):
    def __init__(self, default_content_id: Optional[int] = None):
        super().__init__()
        self.content_id_input = discord.ui.TextInput(
            label="Content ID",
            placeholder="Например: 12",
            required=True,
            max_length=10,
            default=str(default_content_id) if default_content_id is not None else None
        )
        self.users_input = discord.ui.TextInput(
            label="Пользователи",
            placeholder="@user1 @user2 @user3 (можно в строку или столбиком)",
            style=discord.TextStyle.paragraph,
            max_length=2000
        )
        self.add_item(self.content_id_input)
        self.add_item(self.users_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send("Guild недоступен.", ephemeral=True)
            return

        try:
            content_id = int(str(self.content_id_input).strip())
        except ValueError:
            await interaction.followup.send("Content ID должен быть числом.", ephemeral=True)
            return

        row = await db_get_content_by_id(content_id)
        if row is None:
            await interaction.followup.send("Контент не найден.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.followup.send("Не удалось получить данные участника.", ephemeral=True)
            return

        if not is_organizer_or_admin(member, int(row["created_by"])):
            await interaction.followup.send("Недостаточно прав.", ephemeral=True)
            return

        user_ids = parse_user_ids_from_text(str(self.users_input))
        if not user_ids:
            await interaction.followup.send("Нет упоминаний. Укажите пользователей через @.", ephemeral=True)
            return

        added, already = await db_attend_add_many(content_id, user_ids, interaction.user.id)

        # если payout роль уже существует — выдаём сразу
        role = None
        if row["payout_role_id"]:
            role = interaction.guild.get_role(int(row["payout_role_id"]))

        assigned = 0
        failed = 0
        if role is not None:
            for uid in user_ids:
                m = interaction.guild.get_member(uid)
                if m is None:
                    try:
                        m = await interaction.guild.fetch_member(uid)
                    except Exception:
                        failed += 1
                        continue
                try:
                    await m.add_roles(role, reason=f"CS add_ppl content {content_id}")
                    assigned += 1
                except Exception:
                    failed += 1

        if interaction.guild:
            row2 = await db_get_content_by_id(content_id)
            if row2:
                await refresh_main_post(interaction.guild, row2)

        msg = f"Добавлено: {added}. Уже было: {already}."
        if role is not None:
            msg += f" Роль выдана: {assigned}. Ошибок: {failed}."
        else:
            msg += " Роль ещё не создана."

        await interaction.followup.send(msg, ephemeral=True)


# =========================
# THREAD SIGNUP
# =========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.guild is None:
        return
    if not isinstance(message.channel, discord.Thread):
        return

    content = await db_get_content_by_thread(message.channel.id)
    if content is None:
        return

    cmd = message.content.strip()
    if not (cmd.isdigit() or cmd in ("-", "help") or cmd.startswith("+") or cmd.startswith("-")):
        return

    content_id = int(content["id"])
    ends_at = int(content["ends_at"])
    status = str(content["status"])

    # получаем member и права
    member = message.guild.get_member(message.author.id)
    if member is None:
        try:
            member = await message.guild.fetch_member(message.author.id)
        except Exception:
            member = None

    is_org = isinstance(member, discord.Member) and is_organizer_or_admin(member, int(content["created_by"]))

    kind, num = parse_thread_command(cmd)

    # self_leave разрешим всегда (даже если закрыто)
    if kind == "self_leave":
        removed = await db_unassign_user(content_id, message.author.id)
        await message.add_reaction("✅") if removed else await message.add_reaction("ℹ️")
        row2 = await db_get_content_by_id(content_id)
        if row2:
            await refresh_main_post(message.guild, row2)
        await _try_delete_command(message)
        return

    # help — всегда
    if kind == "help":
        await message.reply(
        "Команды:\n"
        "➢ `2` — занять роль №2 (если дедлайн не истёк)\n"
        "➢ `-` — выписаться с роли\n\n"
        "Если вы не можете записаться на контент, обратитесь к организатору контента, для того чтобы он записал вас.\n",
        
        mention_author=False
)

        return

    # Если контент вручную закрыт — самозапись запрещена, но орг-команды разрешены
    if status != STATUS_OPEN and not is_org:
        await message.reply("Запись закрыта.", mention_author=False)
        return

    # Если таймер есть и истёк — самозапись запрещена, но орг-команды разрешены
    if kind == "self_join" and _deadline_expired(ends_at) and not is_org:
        await message.reply("Время записи истекло. Обратитесь к организатору.", mention_author=False)
        await _try_delete_command(message)
        return

    roles_lines = normalize_roles_lines(str(content["roles_text"]))
    max_slot = len(roles_lines)

    # ====== самозапись цифрой ======
    if kind == "self_join":
        if num is None or num < 1 or num > max_slot:
            await message.reply(f"Неверный номер. Допустимо: 1..{max_slot}", mention_author=False)
            return
        ok, txt = await db_assign_user(content_id, message.author.id, int(num))
        await message.add_reaction("✅" if ok else "⛔")
        if not ok:
            await message.reply(txt, mention_author=False)
        row2 = await db_get_content_by_id(content_id)
        if row2:
            await refresh_main_post(message.guild, row2)
        await _try_delete_command(message)
        return

    # ====== орг: + @user @user (attendance) ======
    if kind == "org_attend_add":
        if not is_org:
            await message.reply("Недостаточно прав.", mention_author=False)
            return
        user_ids = [m.id for m in message.mentions]
        if not user_ids:
            await message.reply("Формат: `+ @user @user ...`", mention_author=False)
            return
        added, already = await db_attend_add_many(content_id, user_ids, message.author.id)
        await message.add_reaction("✅")
        await message.reply(f"Добавлено: {added}. Уже были: {already}.", mention_author=False)
        row2 = await db_get_content_by_id(content_id)
        if row2:
            await refresh_main_post(message.guild, row2)
        await _try_delete_command(message)
        return

    # ====== орг: +2 @user (назначить в слот) ======
    if kind == "org_assign_slot":
        if not is_org:
            await message.reply("Недостаточно прав.", mention_author=False)
            return
        if num is None or num < 1 or num > max_slot:
            await message.reply(f"Неверный номер. Допустимо: 1..{max_slot}", mention_author=False)
            return
        if not message.mentions:
            await message.reply("Формат: `+2 @user`", mention_author=False)
            return
        target = message.mentions[0]
        ok, txt = await db_assign_user(content_id, target.id, int(num))
        await message.reply(f"{target.mention}: {txt}", mention_author=False)
        row2 = await db_get_content_by_id(content_id)
        if row2:
            await refresh_main_post(message.guild, row2)
        await _try_delete_command(message)
        return

    # ====== орг: -5 (выкинуть из слота) ======
    if kind == "org_kick_role":
        if not is_org:
            await message.reply("Недостаточно прав.", mention_author=False)
            return
        if num is None or num < 1 or num > max_slot:
            await message.reply(f"Неверный номер. Допустимо: 1..{max_slot}", mention_author=False)
            return
        kicked = await db_unassign_by_role_index(content_id, int(num))
        if kicked is None:
            await message.reply("Роль свободна.", mention_author=False)
        else:
            await message.reply(f"Выписано с роли {num}: <@{kicked}>", mention_author=False)
        row2 = await db_get_content_by_id(content_id)
        if row2:
            await refresh_main_post(message.guild, row2)
        await _try_delete_command(message)
        return

    # ====== орг: - @user (удалить из слотов/attendance) ======
    if kind == "org_kick_user":
        if not is_org:
            await message.reply("Недостаточно прав.", mention_author=False)
            return
        if not message.mentions:
            await message.reply("Формат: `- @user`", mention_author=False)
            return
        target = message.mentions[0]
        removed_slot = await db_unassign_user(content_id, target.id)
        removed_att = await db_attend_remove(content_id, target.id)
        await message.reply("Выписан." if (removed_slot or removed_att) else "Пользователь не записан.", mention_author=False)
        row2 = await db_get_content_by_id(content_id)
        if row2:
            await refresh_main_post(message.guild, row2)
        await _try_delete_command(message)
        return


async def _try_delete_command(message: discord.Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="healthcheck", description="Проверка статуса бота и базы данных")
async def healthcheck(interaction: discord.Interaction):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("SELECT 1")
        await interaction.response.send_message("OK: бот онлайн, БД доступна.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"ERROR: {e}", ephemeral=True)


@bot.tree.command(name="content_create", description="Создать контент через форму")
@app_commands.describe(
    duration_minutes="Время записи на контент (0 = без таймера).",
    create_thread="Создать ветку автоматически"
)
async def content_create(interaction: discord.Interaction, duration_minutes: int = 180, create_thread: bool = True):
    # теперь разрешаем 0 (без таймера)
    if duration_minutes < 0 or duration_minutes > 24 * 60:
        await interaction.response.send_message("duration_minutes должно быть в диапазоне 0..1440.", ephemeral=True)
        return
    await interaction.response.send_modal(ContentCreateModal(duration_minutes=duration_minutes, create_thread=create_thread))


@bot.tree.command(name="add_ppl", description="Добавить людей")
async def attend_add(interaction: discord.Interaction):
    default_content_id = None
    if isinstance(interaction.channel, discord.Thread):
        row = await db_get_content_by_thread(interaction.channel.id)
        if row is not None:
            default_content_id = int(row["id"])
    await interaction.response.send_modal(AttendAddModal(default_content_id=default_content_id))


@bot.tree.command(name="content_close", description="Закрыть запись на контент")
@app_commands.describe(content_id="ID контента")
async def content_close(interaction: discord.Interaction, content_id: int):
    row = await db_get_content_by_id(content_id)
    if row is None:
        await interaction.response.send_message("Контент не найден.", ephemeral=True)
        return
    await db_close_content(content_id)
    row2 = await db_get_content_by_id(content_id)
    if row2 and interaction.guild:
        await refresh_main_post(interaction.guild, row2)
    await interaction.response.send_message(f"Контент #{content_id} закрыт.", ephemeral=True)


@bot.tree.command(name="role_from_content", description="Создать роль и выдать всем участникам")
@app_commands.describe(content_id="ID контента", role_name="Название роли (по умолчанию = заголовок)")
async def role_from_content(interaction: discord.Interaction, content_id: int, role_name: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)

    row = await db_get_content_by_id(content_id)
    if row is None:
        await interaction.followup.send("Контент не найден.")
        return

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Guild недоступен.")
        return

    user_ids = await db_get_all_participants(content_id)
    if not user_ids:
        await interaction.followup.send("Нет участников.")
        return

    base_name = role_name.strip() if role_name and role_name.strip() else str(row["title"])
    final_role_name = f"{base_name} [CS#{content_id}]"

    try:
        role = await guild.create_role(name=final_role_name, reason=f"CS payout role for content {content_id}")
    except discord.Forbidden:
        await interaction.followup.send("Нет прав.")
        return

    failed = []
    assigned = 0
    for uid in user_ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                failed.append(uid)
                continue
        try:
            await member.add_roles(role, reason=f"CS content {content_id} payout")
            assigned += 1
        except discord.Forbidden:
            failed.append(uid)

    await db_set_payout_role(content_id, role.id, role.name)

    msg = f"Роль `{role.name}` выдана {assigned}/{len(user_ids)} участникам."
    if failed:
        msg += "\nНе удалось: " + ", ".join([f"<@{u}>" for u in failed[:20]])
    await interaction.followup.send(msg)


@bot.tree.command(name="role_clear", description="Удалить роль")
@app_commands.describe(content_id="ID контента")
async def role_clear(interaction: discord.Interaction, content_id: int):
    await interaction.response.defer(ephemeral=True)

    row = await db_get_content_by_id(content_id)
    if row is None:
        await interaction.followup.send("Контент не найден.")
        return

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Guild недоступен.")
        return

    role_id = row["payout_role_id"]
    role_name = row["payout_role_name"]

    role = None
    if role_id:
        role = guild.get_role(int(role_id))
    if role is None and role_name:
        role = discord.utils.get(guild.roles, name=str(role_name))

    if role is None:
        await interaction.followup.send("Роль не найдена.")
        return

    try:
        await role.delete(reason=f"CS payout role cleanup for content {content_id}")
        await interaction.followup.send(f"Роль `{role.name}` удалена.")
    except discord.Forbidden:
        await interaction.followup.send("Нет прав на удаление роли.")

@bot.tree.command(name="att_add", description="Добавить аттенденсы за контент")
@app_commands.describe(content_id="ID контента")
async def att_add(interaction: discord.Interaction, content_id: int):
    await interaction.response.defer(ephemeral=True)

    row = await db_get_content_by_id(content_id)
    if row is None:
        await interaction.followup.send("Контент не найден.", ephemeral=True)
        return

    if interaction.guild is None:
        await interaction.followup.send("Guild недоступен.", ephemeral=True)
        return

    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        await interaction.followup.send("Не удалось получить данные участника.", ephemeral=True)
        return

    if not is_organizer_or_admin(member, int(row["created_by"])):
        await interaction.followup.send("Недостаточно прав.", ephemeral=True)
        return

    # Берём актуальный финальный список участников (слоты + attendance)
    user_ids = await db_get_all_participants(content_id)
    if not user_ids:
        await interaction.followup.send("Нет участников.", ephemeral=True)
        return

    awarded, already = await db_att_award_for_content(
        guild_id=int(interaction.guild_id),
        content_id=int(content_id),
        user_ids=user_ids,
        awarded_by=int(interaction.user.id),
    )

    await interaction.followup.send(
        f"Аттенденсы начислены.\n"
        f"Начислено: {awarded}.\n"
        f"Уже было за этот контент: {already}.",
        ephemeral=True
    )

@bot.tree.command(name="att_stats", description="Общая статистика аттенденсов")
async def att_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    if interaction.guild_id is None:
        await interaction.followup.send("Guild недоступен.")
        return

    top_week = await db_att_leaderboard(int(interaction.guild_id), scope="week", limit=10)
    top_all = await db_att_leaderboard(int(interaction.guild_id), scope="all_time", limit=10)

    my_all, my_week = await db_att_get_user(int(interaction.guild_id), int(interaction.user.id))

    text = []
    text.append(_fmt_lb("Лидерборд аттенденсов (неделя)", top_week))
    text.append("")
    text.append(_fmt_lb("Лидерборд аттенденсов (всё время)", top_all))
    text.append("")
    text.append(f"Твоя статистика: неделя — **{my_week}**, всё время — **{my_all}**")

    await interaction.followup.send("\n".join(text))

@bot.tree.command(name="att_remove", description="Удалить аттенденс за конкретный контент у пользователя")
@app_commands.describe(content_id="ID контента", user="Пользователь")
async def att_remove(interaction: discord.Interaction, content_id: int, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    row = await db_get_content_by_id(content_id)
    if row is None:
        await interaction.followup.send("Контент не найден.", ephemeral=True)
        return

    if interaction.guild is None:
        await interaction.followup.send("Guild недоступен.", ephemeral=True)
        return

    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        await interaction.followup.send("Не удалось получить данные участника.", ephemeral=True)
        return

    if not is_organizer_or_admin(member, int(row["created_by"])):
        await interaction.followup.send("Недостаточно прав.", ephemeral=True)
        return

    ok, msg = await db_att_remove_for_content(
        guild_id=int(interaction.guild_id),
        content_id=int(content_id),
        user_id=int(user.id),
        removed_by=int(interaction.user.id),
    )
    await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="att_week_reset", description="Прислать лог недели и обнулить weekly аттенденсы")
async def att_week_reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None or interaction.guild_id is None:
        await interaction.followup.send("Guild недоступен.", ephemeral=True)
        return

    # Только админ/организатор (можно упростить до admin perms)
    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        await interaction.followup.send("Не удалось получить данные участника.", ephemeral=True)
        return
    perms = member.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        await interaction.followup.send("Недостаточно прав.", ephemeral=True)
        return

    affected, snapshot_top = await db_att_weekly_reset(int(interaction.guild_id), int(interaction.user.id))

    # Лог недели = топ-список ДО сброса + сколько затронуто
    text = []
    text.append(_fmt_lb("Лог аттенденсов за неделю (топ до обнуления)", snapshot_top))
    text.append("")
    text.append(f"Обнулено weekly значений: **{affected}**")

    # Лог лучше отправлять не эпхемерал, чтобы был виден команде
    await interaction.followup.send("\n".join(text), ephemeral=False)



# =========================
# MAIN
# =========================
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN не найден. Проверьте .env")
    bot.run(token)


if __name__ == "__main__":
    main()
