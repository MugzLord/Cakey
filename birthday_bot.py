# birthday_bot.py
# Cakey – advanced birthday bot (modal + wishes + 7-day reminder)
# ---------------------------------------------------------------
# NOTE (for Railway):
#   - set DISCORD_NO_VOICE before importing discord
#   - set envs: DISCORD_TOKEN, DEFAULT_TZ, DB_PATH
import os
os.environ["DISCORD_NO_VOICE"] = "1"

import sqlite3
import asyncio
import random
from datetime import datetime, date
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ---------------- ENV / CONFIG ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/London")
DB_PATH = os.getenv("DB_PATH", "birthdays.db")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------------- DB HELPERS ----------------
def db():
    # make sure dir exists if path is like /data/birthdays.db
    folder = os.path.dirname(DB_PATH)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS birthdays (
        guild_id      INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        bday_day      INTEGER NOT NULL,
        bday_month    INTEGER NOT NULL,
        bday_year     INTEGER,
        timezone      TEXT,
        show_year     INTEGER DEFAULT 0,
        birthday_wish TEXT,
        UNIQUE(guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id          INTEGER PRIMARY KEY,
        announce_channel  INTEGER,
        birthday_role     INTEGER,
        announce_text     TEXT,
        default_timezone  TEXT
    );

    CREATE TABLE IF NOT EXISTS bday_announced (
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        announce_date TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id, announce_date)
    );

    CREATE TABLE IF NOT EXISTS bday_reminded (
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        remind_date  TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id, remind_date)
    );
    """)
    # ensure legacy DBs get birthday_wish
    cur.execute("PRAGMA table_info(birthdays)")
    cols = [r[1] for r in cur.fetchall()]
    if "birthday_wish" not in cols:
        cur.execute("ALTER TABLE birthdays ADD COLUMN birthday_wish TEXT")
    con.commit()
    con.close()

init_db()

# ---------------- CONSTANTS / BANTER ----------------
BANTER_7DAYS = [
    "Heads up, {user} turns legendary in 7 days. Start planning chaos. 🎉",
    "Alert: {user}'s birthday loading… 7 days to act like you didn’t forget. ⏰",
    "7 days till {user} expects gifts, noise and attention. Don’t flop. 💅",
    "{user} is about to level up in 7 days – line up the edits and credits.",
    "Countdown: 7 days till we bully-celebrate {user}'s existence. 🎂"
]

HBD_LYRICS = [
    "🎵 Happy birthday to you…",
    "🎵 Happy birthday to you…",
    "🎵 Happy birthday dear {name}…",
    "🎵 Happy birthday to youuu! 🎂✨"
]

# ---------------- UTILS ----------------
def get_guild_settings(guild_id: int):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    con.close()
    return row

def set_guild_setting(guild_id: int, **kwargs):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT guild_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute("INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
    for k, v in kwargs.items():
        cur.execute(f"UPDATE guild_settings SET {k}=? WHERE guild_id=?", (v, guild_id))
    con.commit()
    con.close()

def format_birthday(row):
    # day-month only
    return f"{row['bday_day']:02d}-{row['bday_month']:02d}"

def user_local_today(tz_str: str | None):
    try:
        tz = ZoneInfo(tz_str) if tz_str else ZoneInfo(DEFAULT_TZ)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
    now = datetime.now(tz)
    return now.date(), tz

def already_announced_today(guild_id: int, user_id: int):
    con = db()
    cur = con.cursor()
    cur.execute(
        "SELECT 1 FROM bday_announced WHERE guild_id=? AND user_id=? AND announce_date=?",
        (guild_id, user_id, date.today().isoformat())
    )
    r = cur.fetchone()
    con.close()
    return r is not None

def already_reminded(guild_id: int, user_id: int, remind_date: str):
    con = db()
    cur = con.cursor()
    cur.execute(
        "SELECT 1 FROM bday_reminded WHERE guild_id=? AND user_id=? AND remind_date=?",
        (guild_id, user_id, remind_date)
    )
    r = cur.fetchone()
    con.close()
    return r is not None

# ---------------- SINGING ----------------
async def sing_happy_birthday(channel: discord.TextChannel, member: discord.Member):
    display_name = member.display_name
    for line in HBD_LYRICS:
        line = line.replace("{name}", display_name)
        await channel.send(line)
        await asyncio.sleep(1.3)
    await channel.send(f"🎉 Drop some love for {member.mention} in here or you’re off the guestlist.")

# ---------------- ANNOUNCE (card + role + sing) ----------------
async def announce_birthday(guild: discord.Guild, member: discord.Member, settings_row, bday_row):
    channel_id = settings_row["announce_channel"] if settings_row else None
    role_id = settings_row["birthday_role"] if settings_row else None
    text = settings_row["announce_text"] if (settings_row and settings_row["announce_text"]) else "🎂 Happy Birthday, {mention}! Have an amazing day! 🥳"

    # give role for 24h
    if role_id:
        role = guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role, reason="Birthday role")
                async def remove_later():
                    await asyncio.sleep(24 * 60 * 60)
                    g = bot.get_guild(guild.id)
                    if not g:
                        return
                    m = g.get_member(member.id)
                    if not m:
                        return
                    r = g.get_role(role_id)
                    if r and r in m.roles:
                        await m.remove_roles(r, reason="Birthday over")
                bot.loop.create_task(remove_later())
            except discord.Forbidden:
                pass

    # send card
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            bday_str = format_birthday(bday_row)
            msg = (
                text.replace("{mention}", member.mention)
                    .replace("{user}", str(member))
                    .replace("{date}", bday_str)
            )

            embed = discord.Embed(
                title="🎂 Birthday Card",
                description=msg,
                colour=discord.Colour.magenta()
            )
            embed.add_field(name="Birthday", value=bday_str, inline=True)

            # if they had a wish, add it
            if bday_row["birthday_wish"]:
                embed.add_field(name="Wish", value=bday_row["birthday_wish"][:200], inline=False)

            if member.display_avatar:
                embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
                embed.set_thumbnail(url=member.display_avatar.url)
            else:
                embed.set_author(name=member.display_name)

            embed.set_footer(text="Have the best one. 💜")

            await channel.send(embed=embed)

            # sing in chat
            try:
                await sing_happy_birthday(channel, member)
            except Exception as e:
                print("Error singing birthday:", e)

    # log announce
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO bday_announced (guild_id, user_id, announce_date) VALUES (?,?,?)",
        (guild.id, member.id, date.today().isoformat())
    )
    con.commit()
    con.close()

# ---------------- TASK: CHECK TODAY BIRTHDAYS ----------------
@tasks.loop(minutes=5)
async def birthday_checker():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM birthdays")
    all_bdays = cur.fetchall()
    con.close()

    # group by guild
    guild_map = {}
    for row in all_bdays:
        guild_map.setdefault(row["guild_id"], []).append(row)

    for guild_id, rows in guild_map.items():
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        settings_row = get_guild_settings(guild_id)

        for row in rows:
            user_tz = row["timezone"] or (settings_row["default_timezone"] if settings_row and settings_row["default_timezone"] else DEFAULT_TZ)
            today_local, _ = user_local_today(user_tz)
            if today_local.day == row["bday_day"] and today_local.month == row["bday_month"]:
                if already_announced_today(guild_id, row["user_id"]):
                    continue
                member = guild.get_member(row["user_id"])
                if not member:
                    continue
                try:
                    await announce_birthday(guild, member, settings_row, row)
                except Exception as e:
                    print("announce error:", e)

@birthday_checker.before_loop
async def before_checker():
    await bot.wait_until_ready()
    print("Birthday checker started.")

# ---------------- TASK: 7-DAY REMINDER ----------------
@tasks.loop(hours=24)
async def birthday_prechecker():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM birthdays")
    all_bdays = cur.fetchall()
    con.close()

    today_utc = date.today().isoformat()

    guild_map = {}
    for row in all_bdays:
        guild_map.setdefault(row["guild_id"], []).append(row)

    for guild_id, rows in guild_map.items():
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        settings_row = get_guild_settings(guild_id)
        channel_id = settings_row["announce_channel"] if settings_row else None
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if not channel:
            continue

        for row in rows:
            user_tz = row["timezone"] or (settings_row["default_timezone"] if settings_row and settings_row["default_timezone"] else DEFAULT_TZ)
            user_today, _ = user_local_today(user_tz)

            bday = date(user_today.year, row["bday_month"], row["bday_day"])
            if bday < user_today:
                bday = date(user_today.year + 1, row["bday_month"], row["bday_day"])
            delta = (bday - user_today).days

            if delta == 7:
                if already_reminded(guild_id, row["user_id"], today_utc):
                    continue
                member = guild.get_member(row["user_id"])
                if not member:
                    continue

                banter = random.choice(BANTER_7DAYS).replace("{user}", member.mention)

                embed = discord.Embed(
                    title="📅 7-Day Birthday Alert",
                    description=banter,
                    colour=discord.Colour.gold()
                )
                embed.add_field(name="Birthday date", value=f"{row['bday_day']:02d}-{row['bday_month']:02d}", inline=True)
                embed.set_footer(text="Set your birthday with /birthday set")

                await channel.send(embed=embed)

                con2 = db()
                cur2 = con2.cursor()
                cur2.execute(
                    "INSERT OR IGNORE INTO bday_reminded (guild_id, user_id, remind_date) VALUES (?,?,?)",
                    (guild_id, row["user_id"], today_utc)
                )
                con2.commit()
                con2.close()

@birthday_prechecker.before_loop
async def before_prechecker():
    await bot.wait_until_ready()
    print("7-day birthday prechecker started.")

# ---------------- COG / SLASH COMMANDS ----------------
class BirthdayModal(discord.ui.Modal, title="Set your birthday"):
    day = discord.ui.TextInput(label="Day (1-31)", placeholder="31", max_length=2)
    month = discord.ui.TextInput(label="Month (1-12)", placeholder="10", max_length=2)
    wish = discord.ui.TextInput(label="Birthday wish (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

    def __init__(self, interaction: discord.Interaction):
        super().__init__()
        self.interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        # validate
        try:
            day_i = int(str(self.day.value).strip())
            month_i = int(str(self.month.value).strip())
            _ = datetime(2000, month_i, day_i)
        except Exception:
            return await interaction.response.send_message("❌ Day/Month invalid. Use e.g. day=31, month=10", ephemeral=True)

        wish_text = str(self.wish.value).strip() if self.wish.value else None

        settings_row = get_guild_settings(guild.id)
        auto_tz = settings_row["default_timezone"] if (settings_row and settings_row["default_timezone"]) else DEFAULT_TZ

        con = db()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO birthdays (guild_id, user_id, bday_day, bday_month, timezone, show_year, birthday_wish)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                bday_day=excluded.bday_day,
                bday_month=excluded.bday_month,
                timezone=excluded.timezone,
                show_year=0,
                birthday_wish=excluded.birthday_wish
        """, (guild.id, user.id, day_i, month_i, auto_tz, wish_text))
        con.commit()
        con.close()

        await interaction.response.send_message(
            f"✅ Saved **{day_i:02d}-{month_i:02d}**. Timezone: `{auto_tz}`" + (f"\n📝 Wish: {wish_text}" if wish_text else ""),
            ephemeral=True
        )

class BirthdayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(name="birthday", description="Birthday commands")

    # /birthday set -> modal
    @group.command(name="set", description="Set your birthday")
    async def set_birthday(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BirthdayModal(interaction))

    # /birthday view
    @group.command(name="view", description="View someone's birthday")
    async def view_birthday(self, interaction: discord.Interaction, user: discord.Member | None = None):
        user = user or interaction.user
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM birthdays WHERE guild_id=? AND user_id=?", (interaction.guild_id, user.id))
        row = cur.fetchone()
        con.close()
        if not row:
            return await interaction.response.send_message("No birthday set for that user.", ephemeral=True)

        bday_str = format_birthday(row)
        tz = row["timezone"] or DEFAULT_TZ
        embed = discord.Embed(
            title=f"🎂 {user.display_name}'s birthday",
            description=f"**{bday_str}**",
            colour=discord.Colour.blurple()
        )
        embed.add_field(name="Timezone", value=tz)
        if row["birthday_wish"]:
            embed.add_field(name="Wish", value=row["birthday_wish"], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # /birthday upcoming
    @group.command(name="upcoming", description="Show upcoming birthdays")
    @app_commands.describe(days="How many days ahead to look (default 30)")
    async def upcoming(self, interaction: discord.Interaction, days: int = 30):
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM birthdays WHERE guild_id=?", (interaction.guild_id,))
        rows = cur.fetchall()
        con.close()

        if not rows:
            return await interaction.response.send_message("No birthdays saved yet.", ephemeral=True)

        today = date.today()
        upcoming_list = []
        for r in rows:
            bd = date(today.year, r["bday_month"], r["bday_day"])
            if bd < today:
                bd = date(today.year + 1, r["bday_month"], r["bday_day"])
            delta = (bd - today).days
            if 0 <= delta <= days:
                upcoming_list.append((delta, r))

        upcoming_list.sort(key=lambda x: x[0])
        desc_lines = []
        for delta, r in upcoming_list[:20]:
            member = interaction.guild.get_member(r["user_id"])
            name = member.mention if member else f"<@{r['user_id']}>"
            desc_lines.append(f"**{delta}d** → {name} ({r['bday_day']:02d}-{r['bday_month']:02d})")

        if not desc_lines:
            return await interaction.response.send_message("No upcoming birthdays in that range.", ephemeral=True)

        embed = discord.Embed(
            title=f"🎉 Upcoming birthdays (next {days} days)",
            description="\n".join(desc_lines),
            colour=discord.Colour.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # /birthday list
    @group.command(name="list", description="List all birthdays for a month")
    @app_commands.describe(month="Month number 1-12")
    async def list_month(self, interaction: discord.Interaction, month: int):
        if not (1 <= month <= 12):
            return await interaction.response.send_message("Month must be 1-12.", ephemeral=True)
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM birthdays WHERE guild_id=? AND bday_month=? ORDER BY bday_day", (interaction.guild_id, month))
        rows = cur.fetchall()
        con.close()
        if not rows:
            return await interaction.response.send_message("No birthdays for that month.", ephemeral=True)
        lines = []
        for r in rows:
            member = interaction.guild.get_member(r["user_id"])
            name = member.display_name if member else f"User {r['user_id']}"
            lines.append(f"**{r['bday_day']:02d}** — {name}")
        embed = discord.Embed(
            title=f"📅 Birthdays in month {month}",
            description="\n".join(lines),
            colour=discord.Colour.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ADMIN: /birthday channel
    @group.command(name="channel", description="Set the birthday announce channel (admin)")
    @app_commands.describe(channel="Channel to post birthday messages")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server.", ephemeral=True)
        set_guild_setting(interaction.guild_id, announce_channel=channel.id)
        await interaction.response.send_message(f"✅ Announce channel set to {channel.mention}", ephemeral=True)

    # ADMIN: /birthday role
    @group.command(name="role", description="Set the birthday role (admin)")
    @app_commands.describe(role="Role to give on birthday for 24h")
    async def set_role(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server.", ephemeral=True)
        set_guild_setting(interaction.guild_id, birthday_role=role.id)
        await interaction.response.send_message(f"✅ Birthday role set to {role.mention}", ephemeral=True)

    # ADMIN: /birthday message
    @group.command(name="message", description="Set the birthday announce message (admin)")
    @app_commands.describe(text="Use {mention}, {user}, {date}")
    async def set_message(self, interaction: discord.Interaction, text: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server.", ephemeral=True)
        set_guild_setting(interaction.guild_id, announce_text=text)
        await interaction.response.send_message("✅ Birthday message updated.", ephemeral=True)

    # ADMIN: /birthday default_tz
    @group.command(name="default_tz", description="Set default timezone for this guild (admin)")
    async def set_default_tz(self, interaction: discord.Interaction, timezone: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server.", ephemeral=True)
        try:
            _ = ZoneInfo(timezone)
        except Exception:
            return await interaction.response.send_message("❌ Invalid timezone.", ephemeral=True)
        set_guild_setting(interaction.guild_id, default_timezone=timezone)
        await interaction.response.send_message(f"✅ Default timezone set to `{timezone}`", ephemeral=True)

    # ADMIN: /birthday wishes
    @group.command(name="wishes", description="(admin) View birthday wishes")
    async def view_wishes(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to view wishes.", ephemeral=True)
        con = db()
        cur = con.cursor()
        cur.execute("""
            SELECT * FROM birthdays
            WHERE guild_id=? AND birthday_wish IS NOT NULL AND birthday_wish <> ''
            ORDER BY bday_month, bday_day
        """, (interaction.guild_id,))
        rows = cur.fetchall()
        con.close()

        if not rows:
            return await interaction.response.send_message("No wishes submitted yet 💤", ephemeral=True)

        lines = []
        for r in rows[:25]:
            member = interaction.guild.get_member(r["user_id"])
            name = member.display_name if member else f"User {r['user_id']}"
            lines.append(f"**{r['bday_day']:02d}-{r['bday_month']:02d}** — {name}:\n> {r['birthday_wish'][:180]}")

        embed = discord.Embed(
            title="🎁 Birthday wishes",
            description="\n\n".join(lines),
            colour=discord.Colour.purple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup_tree():
    await bot.wait_until_ready()
    bot.tree.add_command(BirthdayCog(bot).group)
    await bot.tree.sync()
    print("Slash commands synced.")

# ---------------- EVENTS ----------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not birthday_checker.is_running():
        birthday_checker.start()
    if not birthday_prechecker.is_running():
        birthday_prechecker.start()
    bot.loop.create_task(setup_tree())

# ---------------- RUN ----------------
bot.run(TOKEN)
