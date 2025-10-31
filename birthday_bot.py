# birthday_bot.py
import os
import sqlite3
import asyncio
import random
from datetime import datetime, date
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands
discord.voice_client = None

# ---------------- ENV / CONFIG ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

# server default timezone (you can change per guild via /birthday default_tz)
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/London")

# persistent DB location (on Railway set DB_PATH=/data/birthdays.db)
DB_PATH = os.getenv("DB_PATH", "birthdays.db")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------------- DB HELPERS ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;

    -- user birthdays
    CREATE TABLE IF NOT EXISTS birthdays (
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        bday_day    INTEGER NOT NULL,
        bday_month  INTEGER NOT NULL,
        bday_year   INTEGER,
        timezone    TEXT,
        show_year   INTEGER DEFAULT 0,
        UNIQUE(guild_id, user_id)
    );

    -- guild settings
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id          INTEGER PRIMARY KEY,
        announce_channel  INTEGER,
        birthday_role     INTEGER,
        announce_text     TEXT,
        default_timezone  TEXT
    );

    -- daily announce log
    CREATE TABLE IF NOT EXISTS bday_announced (
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        announce_date TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id, announce_date)
    );

    -- 7-day reminder log
    CREATE TABLE IF NOT EXISTS bday_reminded (
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        remind_date  TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id, remind_date)
    );
    """)
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

def format_birthday(row, include_age=True):
    day = row["bday_day"]
    month = row["bday_month"]
    year = row["bday_year"]
    show_year = row["show_year"] == 1
    if not show_year or not year:
        return f"{day:02d}-{month:02d}"
    if include_age:
        today = date.today()
        age = today.year - year
        if (today.month, today.day) < (month, day):
            age -= 1
        return f"{day:02d}-{month:02d}-{year} ({age})"
    return f"{day:02d}-{month:02d}-{year}"

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

            # show age if user allowed it
            if bday_row["show_year"] == 1 and bday_row["bday_year"]:
                today = date.today()
                age = today.year - bday_row["bday_year"]
                if (today.month, today.day) < (bday_row["bday_month"], bday_row["bday_day"]):
                    age -= 1
                embed.add_field(name="Age", value=str(age), inline=True)

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

    # group by guild
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

            # birthday in user's year
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
class BirthdayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(name="birthday", description="Birthday commands")

    @group.command(name="set", description="Set your birthday")
    @app_commands.describe(
        date="Your birthday in YYYY-MM-DD",
        timezone="Your timezone, e.g. Europe/London",
        show_year="Show your birth year to others?"
    )
    async def set_birthday(self, interaction: discord.Interaction, date: str, timezone: str | None = None, show_year: bool = False):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)

        # parse date
        try:
            parts = date.split("-")
            if len(parts) != 3:
                raise ValueError
            year_i = int(parts[0])
            month_i = int(parts[1])
            day_i = int(parts[2])
            _ = datetime(year_i, month_i, day_i)
        except ValueError:
            return await interaction.response.send_message("❌ Use format `YYYY-MM-DD`", ephemeral=True)

        # validate timezone
        if timezone:
            try:
                _ = ZoneInfo(timezone)
            except Exception:
                return await interaction.response.send_message("❌ Invalid timezone. Example: `Europe/London` or `Asia/Qatar`", ephemeral=True)

        con = db()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO birthdays (guild_id, user_id, bday_day, bday_month, bday_year, timezone, show_year)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                bday_day=excluded.bday_day,
                bday_month=excluded.bday_month,
                bday_year=excluded.bday_year,
                timezone=excluded.timezone,
                show_year=excluded.show_year
        """, (guild.id, interaction.user.id, day_i, month_i, year_i, timezone, 1 if show_year else 0))
        con.commit()
        con.close()

        await interaction.response.send_message(
            f"✅ Birthday saved as **{day_i:02d}-{month_i:02d}-{year_i}**. Timezone: `{timezone or DEFAULT_TZ}`. Year visible: `{show_year}`",
            ephemeral=True
        )

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

        bday_str = format_birthday(row, include_age=True)
        tz = row["timezone"] or DEFAULT_TZ
        embed = discord.Embed(
            title=f"🎂 {user.display_name}'s birthday",
            description=f"**{bday_str}**",
            colour=discord.Colour.blurple()
        )
        embed.add_field(name="Timezone", value=tz)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

    @group.command(name="channel", description="Set the birthday announce channel (admin)")
    @app_commands.describe(channel="Channel to post birthday messages")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server.", ephemeral=True)
        set_guild_setting(interaction.guild_id, announce_channel=channel.id)
        await interaction.response.send_message(f"✅ Announce channel set to {channel.mention}", ephemeral=True)

    @group.command(name="role", description="Set the birthday role (admin)")
    @app_commands.describe(role="Role to give on birthday for 24h")
    async def set_role(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server.", ephemeral=True)
        set_guild_setting(interaction.guild_id, birthday_role=role.id)
        await interaction.response.send_message(f"✅ Birthday role set to {role.mention}", ephemeral=True)

    @group.command(name="message", description="Set the birthday announce message (admin)")
    @app_commands.describe(text="Use {mention}, {user}, {date}")
    async def set_message(self, interaction: discord.Interaction, text: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server.", ephemeral=True)
        set_guild_setting(interaction.guild_id, announce_text=text)
        await interaction.response.send_message("✅ Birthday message updated.", ephemeral=True)

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

# ---------------- TREE SYNC ----------------
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
