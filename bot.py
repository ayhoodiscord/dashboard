"""Orage - levels, cookies, streaks, anonymous messages & server tools."""
import os
import random
import re
import sqlite3
import time

import asyncio
import hashlib

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "orage.db")
TZ_OFFSET = float(os.getenv("TZ_OFFSET_HOURS", "7"))  # a new "day" starts at midnight in this UTC offset (7 = WIB)

# ---------- Settings (tweak freely) ----------
ORANGE = 0xFF9500
RED = 0xE5484D
FOOTER = "Orage"
XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN = 30
NGL_COOLDOWN = 30
NGL_MAX_LEN = 300
DAILY_BASE = 50
DAILY_STREAK_BONUS = 10   # extra cookies per streak day
DAILY_STREAK_CAP = 7      # bonus stops growing after this many days
OWNER_ID = 922394768397455391  # only this user can run /doublexp
DEFAULT_AUTOROLE_ID = 1557736869838462976  # used until you run /autorole set or /autorole off
WHERE_TO_START = (  # to link a channel write <#CHANNEL_ID>
    "📸 Try the **Daily Challenge** in the challenges channel\n"
    "🏅 Chat and level up to unlock exclusive roles\n"
    "🎁 Keep an eye out for giveaways"
)


def xp_needed(level: int) -> int:
    return 100 + 50 * level


def level_reward(level: int) -> int:
    return 25 + 5 * level


TITLES = [(0, "Fresh Dough"), (5, "Warm Cookie"), (10, "Choco Chipper"),
          (20, "Golden Baker"), (30, "Cookie Legend"), (50, "Cookie Monarch")]


def title_for(level: int) -> str:
    return [t for lv, t in TITLES if level >= lv][-1]


def today() -> int:
    return int((time.time() + TZ_OFFSET * 3600) // 86400)


def next_day_ts() -> int:
    return int((today() + 1) * 86400 - TZ_OFFSET * 3600)


# ---------- Database ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
with db:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        guild_id INTEGER, user_id INTEGER,
        level INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, cookies INTEGER DEFAULT 0,
        PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS guild_config(
        guild_id INTEGER PRIMARY KEY, autorole_id INTEGER, welcome_channel_id INTEGER);
    CREATE TABLE IF NOT EXISTS channels(
        guild_id INTEGER, kind TEXT, channel_id INTEGER, PRIMARY KEY(guild_id, kind));
    CREATE TABLE IF NOT EXISTS level_roles(
        guild_id INTEGER, level INTEGER, role_id INTEGER, PRIMARY KEY(guild_id, level));
    CREATE TABLE IF NOT EXISTS streaks(
        guild_id INTEGER, user_id INTEGER, last_day INTEGER, streak INTEGER, best INTEGER,
        PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS daily_claims(
        guild_id INTEGER, user_id INTEGER, last_day INTEGER, PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS shop_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, kind TEXT, name TEXT,
        description TEXT, price INTEGER, role_id INTEGER DEFAULT 0, value INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS shop_seeded(guild_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS user_items(
        guild_id INTEGER, user_id INTEGER, freezes INTEGER DEFAULT 0, PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS boosts(
        guild_id INTEGER, user_id INTEGER, until INTEGER, mult INTEGER, PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS ticket_config(
        guild_id INTEGER PRIMARY KEY, staff_role_id INTEGER, category_id INTEGER);
    CREATE TABLE IF NOT EXISTS tickets(
        channel_id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER, created INTEGER);
    CREATE TABLE IF NOT EXISTS xp_events(guild_id INTEGER PRIMARY KEY, until INTEGER);
    CREATE TABLE IF NOT EXISTS challenge_posts(guild_id INTEGER PRIMARY KEY, last_day INTEGER);
    CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel_id INTEGER,
        due INTEGER, text TEXT);
    CREATE TABLE IF NOT EXISTS afk(
        guild_id INTEGER, user_id INTEGER, reason TEXT, since INTEGER,
        PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS ngl_optout(
        guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS ngl_log(
        message_id INTEGER PRIMARY KEY, guild_id INTEGER,
        sender_id INTEGER, target_id INTEGER, created INTEGER);
    """)


for _col in ("claimed_by INTEGER", "kind TEXT"):
    try:
        db.execute(f"ALTER TABLE tickets ADD COLUMN {_col}")
    except sqlite3.OperationalError:
        pass

afk_users = {(r["guild_id"], r["user_id"]) for r in db.execute("SELECT guild_id, user_id FROM afk")}


def get_user(gid: int, uid: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO users(guild_id, user_id) VALUES(?, ?)", (gid, uid))
    return db.execute("SELECT level, xp, cookies FROM users WHERE guild_id=? AND user_id=?",
                      (gid, uid)).fetchone()


def get_rank(gid: int, level: int, xp: int) -> int:
    return db.execute(
        "SELECT COUNT(*) + 1 FROM users WHERE guild_id=? AND (level > ? OR (level = ? AND xp > ?))",
        (gid, level, level, xp)).fetchone()[0]


def get_channel_id(gid: int, kind: str) -> int:
    r = db.execute("SELECT channel_id FROM channels WHERE guild_id=? AND kind=?", (gid, kind)).fetchone()
    return r["channel_id"] if r else 0


def set_channel_id(gid: int, kind: str, cid: int) -> None:
    if cid:
        db.execute("INSERT INTO channels VALUES(?, ?, ?) ON CONFLICT(guild_id, kind) "
                   "DO UPDATE SET channel_id=excluded.channel_id", (gid, kind, cid))
    else:
        db.execute("DELETE FROM channels WHERE guild_id=? AND kind=?", (gid, kind))


def get_autorole(gid: int) -> int:
    r = db.execute("SELECT autorole_id FROM guild_config WHERE guild_id=?", (gid,)).fetchone()
    return (r["autorole_id"] or 0) if r else DEFAULT_AUTOROLE_ID


def set_autorole(gid: int, rid: int) -> None:
    db.execute("INSERT INTO guild_config(guild_id, autorole_id) VALUES(?, ?) ON CONFLICT(guild_id) "
               "DO UPDATE SET autorole_id=excluded.autorole_id", (gid, rid))


def get_streak(gid: int, uid: int) -> tuple[int, int]:
    """(current streak, best streak). A streak that missed a full day counts as 0."""
    r = db.execute("SELECT last_day, streak, best FROM streaks WHERE guild_id=? AND user_id=?",
                   (gid, uid)).fetchone()
    if not r:
        return 0, 0
    return (r["streak"] if r["last_day"] >= today() - 1 else 0), r["best"]


# ---------- Bot & UI helpers ----------
intents = discord.Intents.default()
intents.members = True  # privileged: enable "Server Members Intent" in the Developer Portal
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
xp_last: dict[tuple[int, int], float] = {}
streak_seen: dict[tuple[int, int], int] = {}
ngl_last: dict[int, float] = {}


def base(title=None, desc=None, color=ORANGE, footer=True) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color)
    if footer:
        e.set_footer(text=FOOTER, icon_url=bot.user.display_avatar.url if bot.user else None)
    return e


def ok_embed(text: str) -> discord.Embed:
    return base(desc=f"✅  {text}", footer=False)


def err_embed(text: str) -> discord.Embed:
    return base(desc=f"❌  {text}", color=RED, footer=False)


async def fail(i: discord.Interaction, text: str):
    await i.response.send_message(embed=err_embed(text), ephemeral=True)


async def done(i: discord.Interaction, text: str):
    await i.response.send_message(embed=ok_embed(text), ephemeral=True)


def bar(xp: int, need: int, size: int = 10) -> str:
    filled = min(size, int(size * xp / need)) if need else 0
    return "🟧" * filled + "⬜" * (size - filled)


async def post_to(guild: discord.Guild, kind: str, fallback, **kwargs):
    """Send to the channel an admin picked for `kind`, else to `fallback`."""
    cid = get_channel_id(guild.id, kind)
    ch = (guild.get_channel(cid) if cid else None) or fallback
    if ch is None:
        return
    try:
        await ch.send(**kwargs)
    except discord.HTTPException:
        pass


# ---------- Events ----------
@bot.event
async def setup_hook():
    bot.add_dynamic_items(NGLReply, SelfRoleButton, SelfRoleMine)
    reminder_loop.start()
    challenge_loop.start()
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlView())
    await bot.tree.sync()


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game("🍪 /help"))
    print(f"Orage online as {bot.user} (ID: {bot.user.id})")


async def give_level_roles(member: discord.Member, level: int) -> list[discord.Role]:
    rows = db.execute("SELECT role_id FROM level_roles WHERE guild_id=? AND level<=?",
                      (member.guild.id, level)).fetchall()
    given = []
    for r in rows:
        role = member.guild.get_role(r["role_id"])
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Orage level reward")
                given.append(role)
            except discord.HTTPException:
                pass
    return given


def streak_announce_embed(member: discord.Member, n: int, best: int) -> discord.Embed:
    e = base(desc=f"{member.mention} streaks **{n}** 🔥\n-# Best: {best}")
    e.set_thumbnail(url=member.display_avatar.url)
    return e


async def handle_streak(msg: discord.Message):
    """One message per day = one streak day. Miss a day and it resets."""
    gid, uid, d = msg.guild.id, msg.author.id, today()
    if streak_seen.get((gid, uid)) == d:
        return
    streak_seen[(gid, uid)] = d
    with db:
        r = db.execute("SELECT last_day, streak, best FROM streaks WHERE guild_id=? AND user_id=?",
                       (gid, uid)).fetchone()
        if r and r["last_day"] == d:
            return
        n = r["streak"] + 1 if r and r["last_day"] == d - 1 else 1
        best = max(n, r["best"] if r else 0)
        db.execute("INSERT INTO streaks VALUES(?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                   "last_day=excluded.last_day, streak=excluded.streak, best=excluded.best",
                   (gid, uid, d, n, best))
    await post_to(msg.guild, "streak", msg.channel, embed=streak_announce_embed(msg.author, n, best),
                  allowed_mentions=discord.AllowedMentions.none())


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return
    await handle_afk(msg)
    await handle_streak(msg)

    key = (msg.guild.id, msg.author.id)
    now = time.time()
    if now - xp_last.get(key, 0) < XP_COOLDOWN:
        return
    xp_last[key] = now

    with db:
        row = get_user(*key)
        level, xp, cookies = row["level"], row["xp"] + random.randint(XP_MIN, XP_MAX) * xp_mult(msg.guild.id), row["cookies"]
        start_level, reward = level, 0
        while xp >= xp_needed(level):
            xp -= xp_needed(level)
            level += 1
            reward += level_reward(level)
        cookies += reward
        db.execute("UPDATE users SET level=?, xp=?, cookies=? WHERE guild_id=? AND user_id=?",
                   (level, xp, cookies, *key))

    if level > start_level:
        unlocked = await give_level_roles(msg.author, level)
        e = base("⬆️  Level up!", f"{msg.author.mention} is now **Level {level}** · *{title_for(level)}*")
        e.set_thumbnail(url=msg.author.display_avatar.url)
        e.add_field(name="🍪 Reward", value=f"+{reward:,} cookies", inline=True)
        if unlocked:
            e.add_field(name="🎖️ Unlocked", value=" ".join(r.mention for r in unlocked), inline=True)
        await post_to(msg.guild, "levelup", msg.channel, embed=e,
                      allowed_mentions=discord.AllowedMentions(users=[msg.author]))


def welcome_embed(member: discord.Member) -> discord.Embed:
    e = discord.Embed(title="👋 Welcome to Ayhoo!!", color=ORANGE, description=(
        f"Hey {member.mention}, you made it! 🧡\n\n"
        "We're really happy to have you here. Grab a seat, say hi, and make yourself at home."))
    e.add_field(name="🌟 Where to start", value=WHERE_TO_START, inline=False)
    e.add_field(name="\u200b", value="*Talk. Play. Create. Connect.*", inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    e.set_footer(text="Orage",
                 icon_url=bot.user.display_avatar.url if bot.user else None)
    return e


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    await send_log(member.guild, "📥  Member joined", f"{member.mention} · `{member}`\nAccount created <t:{int(member.created_at.timestamp())}:R>", member)
    rid = get_autorole(member.guild.id)
    if rid and (role := member.guild.get_role(rid)):
        try:
            await member.add_roles(role, reason="Orage autorole")
        except discord.HTTPException as err:
            print("Autorole failed:", err)
    cid = get_channel_id(member.guild.id, "welcome")
    if cid and (ch := member.guild.get_channel(cid)):
        try:
            await ch.send(embed=welcome_embed(member))
        except discord.HTTPException as err:
            print("Welcome message failed:", err)


# ---------- /help ----------
HELP_PAGES = {
    "home": ("🐱  Orage Help",
             "Chat, level up and collect cookies. 🧡\nPick a category from the menu below.",
             [("✨ How it works",
               "Send messages → earn XP\nLevel up → earn 🍪 cookies & roles\nChat once a day → keep your 🔥 streak"),
              ("🚀 Quick start",
               "`/profile` see your card\n`/daily` claim free cookies\n`/shop` spend your cookies\n`/challenge` today's challenge\n`/leaderboard` see the top members")]),
    "cookies": ("🍪  Cookies & Levels", "Everything about your progress.",
                [("Commands",
                  "`/profile` `[member]`\nView a profile card.\n\n"
                  "`/leaderboard`\nTop members by level, cookies or streak.\n\n"
                  "`/daily`\nClaim your daily cookies. A longer streak means a bigger reward.\n\n"
                  "`/givecookies` `member` `amount`\nShare your cookies with someone.")]),
    "streaks": ("🔥  Streaks", "Send **at least one message each day** to grow your streak.",
                [("Commands", "`/streak` `[member]`\nSee your streak and best streak."),
                 ("How it works",
                  "• First message of the day → Orage posts `@you streaks 5 🔥`\n"
                  "• Miss a full day → the streak resets\n"
                  f"• A new day starts at midnight (UTC{TZ_OFFSET:+g})\n"
                  "• A longer streak boosts `/daily`")]),
    "ngl": ("📨  Anonymous Messages", "Say it without saying who you are.",
            [("Commands",
              "`/ngl` `member` `message`\nSend an anonymous message. They can reply with a button.\n\n"
              "`/ngl_toggle`\nTurn receiving anonymous messages on or off."),
             ("Good to know", "Be kind. Admins can trace abuse.")]),
    "shop": ("🛍️  Role Shop", "Spend your cookies on exclusive roles.",
             [("Commands", "`/shop`\nBrowse the roles and buy one with 🍪 cookies."),
              ("Earn cookies", "Level up by chatting · `/daily` · `/givecookies` from friends")]),
    "fun": ("🎲  Fun", "Light and silly.",
            [("Commands", "`/8ball` `question`\n`/choose` `options`\n`/hug` `member`\n"
                          "`/ship` `member` `[member]`\n`/meow`")]),
    "utils": ("🧰  Utilities", "Small tools that make life easier.",
              [("Commands", "`/remind` `when` `message`\nLike `10m`, `2h` or `1h30m`.\n\n"
                            "`/poll` `question` `options`\nOptions separated by commas.\n\n"
                            "`/afk` `[reason]`\nOrage tells people you're away.\n\n"
                            "`/userinfo` · `/avatar` · `/serverinfo`"),
              ("Tickets", "`/ticket close`\nClose the ticket you're in.\n`/ticket add` `member`\nAdd someone to a ticket (staff).")]),
    "admin": ("🛠️  Server Setup", "For members with **Manage Server**.",
              [("Choose channels", "`/channel set` `feature` `#channel`\nWelcome, level-up and streak messages.\n"
                                   "`/channel clear` `feature`\nWelcome turns off; the others post where the member chatted."),
               ("Roles", "`/autorole set` `role` · `/autorole off`\n"
                         "`/levelrole add` `level` `role` · `remove` · `list`"),
               ("Shop", "`/shopsetup` `name` `price` `[color]`\nCreate a role and sell it.\n`/shopadmin price` · `/shopadmin remove`"),
               ("Tickets", "`/ticket setup` `channel` `staff_role` `[category]`\nPost the 🎫 panel (Partnership · Help · Suggest · Report)."),
               ("Self-roles", "`/selfrole` `[channel]`\nPost the 🧡 *Pick Your Vibe* role panel."),
               ("Logs & challenge", "`/setlog` `[channel]`\nSend all server logs to one channel (leave empty to turn off).\n`/channel set` → *Daily challenge* posts a new one every day."),
               ("Tools", "`/settings` see every setting\n`/welcome_preview` preview the welcome card\n"
                         "`/ngl_trace` `message_id` find an anonymous sender")]),
}


def help_embed(key: str) -> discord.Embed:
    title, desc, fields = HELP_PAGES[key]
    e = base(title, desc)
    for name, value in fields:
        e.add_field(name=name, value=value, inline=False)
    return e


class HelpView(discord.ui.View):
    def __init__(self, is_admin: bool):
        super().__init__(timeout=300)
        opts = [discord.SelectOption(label="Home", value="home", emoji="🏠", description="Overview & quick start"),
                discord.SelectOption(label="Cookies & Levels", value="cookies", emoji="🍪", description="Profile, daily, leaderboard"),
                discord.SelectOption(label="Streaks", value="streaks", emoji="🔥", description="Chat daily, keep the flame"),
                discord.SelectOption(label="Shop", value="shop", emoji="🛍️", description="Boosts, freezes & roles"),
                discord.SelectOption(label="Anonymous", value="ngl", emoji="📨", description="Send anonymous messages"),
                discord.SelectOption(label="Fun", value="fun", emoji="🎲", description="8ball, hug, ship & more"),
                discord.SelectOption(label="Utilities", value="utils", emoji="🧰", description="Remind, poll, AFK, info")]
        if is_admin:
            opts.append(discord.SelectOption(label="Server Setup", value="admin", emoji="🛠️", description="Channels, roles, rewards"))
        select = discord.ui.Select(placeholder="Choose a category…", options=opts)
        select.callback = self.pick
        self.add_item(select)

    async def pick(self, i: discord.Interaction):
        await i.response.edit_message(embed=help_embed(i.data["values"][0]), view=self)


@bot.tree.command(name="help", description="Browse everything Orage can do")
async def help_cmd(i: discord.Interaction):
    admin = bool(i.guild and i.user.guild_permissions.manage_guild)
    await i.response.send_message(embed=help_embed("home"), view=HelpView(admin), ephemeral=True)


# ---------- Profile, leaderboard, cookies ----------
@bot.tree.command(name="profile", description="Show a profile card")
@app_commands.guild_only()
async def profile(i: discord.Interaction, member: discord.Member | None = None):
    member = member or i.user
    if member.bot:
        return await fail(i, "Bots don't eat cookies 🤖")
    with db:
        row = get_user(i.guild.id, member.id)
    level, xp, cookies = row["level"], row["xp"], row["cookies"]
    need = xp_needed(level)
    rank = get_rank(i.guild.id, level, xp)
    streak, best = get_streak(i.guild.id, member.id)

    e = base(desc=f"**{title_for(level)}**\nLevel **{level}**")
    e.set_author(name=f"{member.display_name}'s profile", icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="🍪 Cookies", value=f"**{cookies:,}**", inline=True)
    e.add_field(name="🔥 Streak", value=f"**{streak}** day{'s' if streak != 1 else ''}\n-# Best: {best}", inline=True)
    e.add_field(name="🏆 Rank", value=f"**#{rank}**", inline=True)
    e.add_field(name=f"⭐ Level {level} → {level + 1}",
                value=f"{bar(xp, need)}  **{int(100 * xp / need)}%**\n`{xp:,} / {need:,} XP`", inline=False)
    await i.response.send_message(embed=e)


@bot.tree.command(name="streak", description="See your streak and best streak")
@app_commands.guild_only()
async def streak_cmd(i: discord.Interaction, member: discord.Member | None = None):
    member = member or i.user
    if member.bot:
        return await fail(i, "Bots don't keep streaks 🤖")
    n, best = get_streak(i.guild.id, member.id)
    e = base()
    e.set_author(name=f"{member.display_name}'s streak", icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="🔥 Streak", value=f"**{n}** day{'s' if n != 1 else ''}", inline=True)
    e.add_field(name="🏆 Best", value=f"**{best}** day{'s' if best != 1 else ''}", inline=True)
    await i.response.send_message(embed=e)


def leaderboard_embed(guild: discord.Guild, mode: str) -> discord.Embed:
    if mode == "cookies":
        rows = db.execute("SELECT user_id, cookies AS v FROM users WHERE guild_id=? AND cookies>0 "
                          "ORDER BY cookies DESC LIMIT 10", (guild.id,)).fetchall()
        fmt, title = (lambda v: f"{v:,} 🍪"), "🍪  Top Cookies"
    elif mode == "streaks":
        rows = db.execute("SELECT user_id, streak AS v FROM streaks WHERE guild_id=? AND last_day>=? "
                          "ORDER BY streak DESC LIMIT 10", (guild.id, today() - 1)).fetchall()
        fmt, title = (lambda v: f"{v} 🔥"), "🔥  Top Streaks"
    else:
        rows = db.execute("SELECT user_id, level AS v FROM users WHERE guild_id=? "
                          "ORDER BY level DESC, xp DESC LIMIT 10", (guild.id,)).fetchall()
        fmt, title = (lambda v: f"Lv {v}"), "⭐  Top Levels"
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[n] if n < 3 else f'`{n + 1}.`'}  <@{r['user_id']}> · **{fmt(r['v'])}**"
             for n, r in enumerate(rows)]
    e = base(title, "\n".join(lines) or "Nobody here yet. Start chatting! 💬")
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    return e


class LeaderboardView(discord.ui.View):
    def __init__(self, owner_id: int, mode: str = "levels"):
        super().__init__(timeout=120)
        self.owner_id, self.mode, self.message = owner_id, mode, None
        self._sync()

    def _sync(self):
        for b in self.children:
            b.style = discord.ButtonStyle.primary if b.custom_id == self.mode else discord.ButtonStyle.secondary

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.owner_id:
            await i.response.send_message("Run `/leaderboard` yourself to browse 😸", ephemeral=True)
            return False
        return True

    async def switch(self, i: discord.Interaction, mode: str):
        self.mode = mode
        self._sync()
        await i.response.edit_message(embed=leaderboard_embed(i.guild, mode), view=self)

    @discord.ui.button(label="Levels", emoji="⭐", custom_id="levels")
    async def b_levels(self, i: discord.Interaction, _):
        await self.switch(i, "levels")

    @discord.ui.button(label="Cookies", emoji="🍪", custom_id="cookies")
    async def b_cookies(self, i: discord.Interaction, _):
        await self.switch(i, "cookies")

    @discord.ui.button(label="Streaks", emoji="🔥", custom_id="streaks")
    async def b_streaks(self, i: discord.Interaction, _):
        await self.switch(i, "streaks")

    async def on_timeout(self):
        for b in self.children:
            b.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="leaderboard", description="See the top members")
@app_commands.guild_only()
async def leaderboard(i: discord.Interaction):
    view = LeaderboardView(i.user.id)
    await i.response.send_message(embed=leaderboard_embed(i.guild, "levels"), view=view)
    view.message = await i.original_response()


@bot.tree.command(name="daily", description="Claim your daily cookies")
@app_commands.guild_only()
async def daily(i: discord.Interaction):
    d = today()
    with db:
        get_user(i.guild.id, i.user.id)
        r = db.execute("SELECT last_day FROM daily_claims WHERE guild_id=? AND user_id=?",
                       (i.guild.id, i.user.id)).fetchone()
        if r and r["last_day"] == d:
            return await fail(i, f"You already claimed today. Come back <t:{next_day_ts()}:R> 🍪")
        streak, _ = get_streak(i.guild.id, i.user.id)
        bonus = min(streak, DAILY_STREAK_CAP) * DAILY_STREAK_BONUS
        reward = DAILY_BASE + bonus
        db.execute("UPDATE users SET cookies = cookies + ? WHERE guild_id=? AND user_id=?",
                   (reward, i.guild.id, i.user.id))
        db.execute("INSERT INTO daily_claims VALUES(?, ?, ?) ON CONFLICT(guild_id, user_id) "
                   "DO UPDATE SET last_day=excluded.last_day", (i.guild.id, i.user.id, d))
    e = base("🎁  Daily cookies", f"**+{reward:,}** 🍪 for {i.user.mention}")
    e.add_field(name="🔥 Streak bonus", value=f"{streak}-day streak → **+{bonus}**", inline=True)
    e.add_field(name="⏰ Next claim", value=f"<t:{next_day_ts()}:R>", inline=True)
    await i.response.send_message(embed=e)


@bot.tree.command(name="givecookies", description="Give some of your cookies to someone")
@app_commands.guild_only()
@app_commands.describe(member="Who gets the cookies", amount="How many cookies")
async def givecookies(i: discord.Interaction, member: discord.Member,
                      amount: app_commands.Range[int, 1, 1_000_000]):
    if member.bot or member.id == i.user.id:
        return await fail(i, "Pick someone else 🍪")
    ok, have = False, 0
    with db:
        giver = get_user(i.guild.id, i.user.id)
        get_user(i.guild.id, member.id)
        have = giver["cookies"]
        if have >= amount:
            db.execute("UPDATE users SET cookies = cookies - ? WHERE guild_id=? AND user_id=?",
                       (amount, i.guild.id, i.user.id))
            db.execute("UPDATE users SET cookies = cookies + ? WHERE guild_id=? AND user_id=?",
                       (amount, i.guild.id, member.id))
            ok = True
    if not ok:
        return await fail(i, f"You only have **{have:,}** 🍪. Keep chatting to bake more!")
    e = base("🎁  Cookies delivered", f"{i.user.mention} gave **{amount:,}** 🍪 to {member.mention}")
    e.set_thumbnail(url=member.display_avatar.url)
    await i.response.send_message(embed=e)


# ---------- Anonymous messages (NGL) ----------
class ReplyModal(discord.ui.Modal, title="Reply to this message"):
    answer = discord.ui.TextInput(label="Your reply", style=discord.TextStyle.paragraph,
                                  max_length=NGL_MAX_LEN)

    def __init__(self, message: discord.Message):
        super().__init__()
        self.message = message

    async def on_submit(self, i: discord.Interaction):
        e = base(desc=f"> {discord.utils.escape_mentions(str(self.answer))}", footer=False)
        e.set_author(name=f"{i.user.display_name} replied", icon_url=i.user.display_avatar.url)
        await self.message.reply(embed=e, mention_author=False)
        try:
            await self.message.edit(view=None)  # one reply per message
        except discord.HTTPException:
            pass
        await done(i, "Reply sent 🧡")


class NGLReply(discord.ui.DynamicItem[discord.ui.Button], template=r"ngl:reply:(?P<target>[0-9]+)"):
    def __init__(self, target_id: int):
        super().__init__(discord.ui.Button(label="Reply", emoji="💬", style=discord.ButtonStyle.primary,
                                           custom_id=f"ngl:reply:{target_id}"))
        self.target_id = target_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["target"]))

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.target_id:
            await fail(i, "Only the recipient can reply 🤫")
            return False
        return True

    async def callback(self, i: discord.Interaction):
        await i.response.send_modal(ReplyModal(i.message))


@bot.tree.command(name="ngl", description="Send an anonymous message to someone")
@app_commands.guild_only()
@app_commands.describe(member="Who it's for", message="Your anonymous message")
async def ngl(i: discord.Interaction, member: discord.Member,
              message: app_commands.Range[str, 1, NGL_MAX_LEN]):
    if member.bot or member.id == i.user.id:
        return await fail(i, "Pick another member 🙈")
    if db.execute("SELECT 1 FROM ngl_optout WHERE guild_id=? AND user_id=?",
                  (i.guild.id, member.id)).fetchone():
        return await fail(i, "That person turned off anonymous messages.")
    wait = NGL_COOLDOWN - (time.time() - ngl_last.get(i.user.id, 0))
    if wait > 0:
        return await fail(i, f"Slow down! Try again in {int(wait) + 1}s ⏳")
    ngl_last[i.user.id] = time.time()

    e = base("📨  Anonymous message",
             f"> {discord.utils.escape_mentions(message)}\n-# Sent anonymously · only {member.display_name} can reply")
    view = discord.ui.View(timeout=None)
    view.add_item(NGLReply(member.id))
    try:
        sent = await i.channel.send(content=member.mention, embed=e, view=view,
                                    allowed_mentions=discord.AllowedMentions(users=[member]))
    except discord.HTTPException:
        return await fail(i, "I can't post in this channel 😿")
    with db:
        db.execute("INSERT INTO ngl_log VALUES(?, ?, ?, ?, ?)",
                   (sent.id, i.guild.id, i.user.id, member.id, int(time.time())))
    await done(i, "Sent! 🤫 Other members can't see who you are (admins can trace abuse).")


@bot.tree.command(name="ngl_toggle", description="Turn receiving anonymous messages on or off")
@app_commands.guild_only()
async def ngl_toggle(i: discord.Interaction):
    with db:
        cur = db.execute("DELETE FROM ngl_optout WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        if cur.rowcount:
            return await done(i, "Anonymous messages are now **ON** 📨")
        db.execute("INSERT INTO ngl_optout VALUES(?, ?)", (i.guild.id, i.user.id))
    await done(i, "Anonymous messages are now **OFF** 🔕")


# ---------- Utilities ----------
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DURATION_RE = re.compile(r"\s*(\d+)\s*([smhd])", re.I)
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def clean(text: str) -> str:
    return discord.utils.escape_mentions(text)


def parse_duration(text: str) -> int | None:
    t, pos, total = text.strip(), 0, 0
    while pos < len(t):
        m = DURATION_RE.match(t, pos)
        if not m:
            return None
        total += int(m[1]) * UNITS[m[2].lower()]
        pos = m.end()
    return total or None


@bot.tree.command(name="remind", description="Set a reminder")
@app_commands.describe(when="Like 10m, 2h, 1d or 1h30m", message="What should I remind you about?")
async def remind(i: discord.Interaction, when: str, message: app_commands.Range[str, 1, 300]):
    secs = parse_duration(when)
    if not secs or secs < 30 or secs > 30 * 86400:
        return await fail(i, "Pick a time between 30 seconds and 30 days, like `10m`, `2h` or `1h30m`.")
    if db.execute("SELECT COUNT(*) FROM reminders WHERE user_id=?", (i.user.id,)).fetchone()[0] >= 10:
        return await fail(i, "You already have 10 reminders waiting.")
    due = int(time.time()) + secs
    with db:
        db.execute("INSERT INTO reminders(user_id, channel_id, due, text) VALUES(?, ?, ?, ?)",
                   (i.user.id, i.channel_id, due, message))
    await i.response.send_message(
        embed=base("⏰  Reminder set", f"I'll remind you <t:{due}:R>\n> {clean(message)}"), ephemeral=True)


@tasks.loop(seconds=20)
async def reminder_loop():
    rows = db.execute("SELECT id, user_id, channel_id, text FROM reminders WHERE due<=?",
                      (int(time.time()),)).fetchall()
    for r in rows:
        with db:
            db.execute("DELETE FROM reminders WHERE id=?", (r["id"],))
        e = base("⏰  Reminder", f"> {clean(r['text'])}")
        ch = bot.get_channel(r["channel_id"]) if r["channel_id"] else None
        try:
            if ch:
                await ch.send(content=f"<@{r['user_id']}>", embed=e,
                              allowed_mentions=discord.AllowedMentions(users=True))
            else:
                raise discord.HTTPException
        except Exception:
            try:
                user = await bot.fetch_user(r["user_id"])
                await user.send(embed=e)
            except discord.HTTPException:
                pass


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


@bot.tree.command(name="poll", description="Create a quick poll")
@app_commands.guild_only()
@app_commands.describe(question="What are you asking?", options="2-5 options separated by commas")
async def poll(i: discord.Interaction, question: app_commands.Range[str, 1, 200], options: str):
    opts = [o.strip() for o in options.split(",") if o.strip()]
    if not 2 <= len(opts) <= 5:
        return await fail(i, "Give me 2 to 5 options, separated by commas.")
    e = base(f"📊  {clean(question)}", "\n".join(f"{NUMS[n]}  {clean(o)}" for n, o in enumerate(opts)))
    e.set_author(name=f"Poll by {i.user.display_name}", icon_url=i.user.display_avatar.url)
    await i.response.send_message(embed=e)
    msg = await i.original_response()
    for n in range(len(opts)):
        try:
            await msg.add_reaction(NUMS[n])
        except discord.HTTPException:
            break


async def handle_afk(msg: discord.Message):
    gid, uid = msg.guild.id, msg.author.id
    if (gid, uid) in afk_users:
        afk_users.discard((gid, uid))
        with db:
            db.execute("DELETE FROM afk WHERE guild_id=? AND user_id=?", (gid, uid))
        await msg.channel.send(embed=ok_embed(f"Welcome back, {msg.author.mention}! AFK removed."),
                               delete_after=10, allowed_mentions=discord.AllowedMentions.none())
    seen = set()
    for m in msg.mentions:
        if m.id == uid or m.bot or m.id in seen or (gid, m.id) not in afk_users:
            continue
        seen.add(m.id)
        r = db.execute("SELECT reason, since FROM afk WHERE guild_id=? AND user_id=?", (gid, m.id)).fetchone()
        if r:
            await msg.reply(embed=base(desc=f"💤  **{m.display_name}** is AFK: {clean(r['reason'])}\n"
                                            f"-# Since <t:{r['since']}:R>", footer=False),
                            mention_author=False, delete_after=15)


@bot.tree.command(name="afk", description="Let people know you're away")
@app_commands.guild_only()
async def afk(i: discord.Interaction, reason: app_commands.Range[str, 1, 100] = "AFK"):
    with db:
        db.execute("INSERT OR REPLACE INTO afk VALUES(?, ?, ?, ?)",
                   (i.guild.id, i.user.id, reason, int(time.time())))
    afk_users.add((i.guild.id, i.user.id))
    await i.response.send_message(embed=base(desc=f"💤  {i.user.mention} is now AFK: {clean(reason)}", footer=False))


@bot.tree.command(name="avatar", description="Show someone's avatar")
async def avatar(i: discord.Interaction, member: discord.User | None = None):
    member = member or i.user
    e = base(f"{member.display_name}'s avatar")
    e.set_image(url=member.display_avatar.with_size(1024).url)
    await i.response.send_message(embed=e)


@bot.tree.command(name="userinfo", description="Show info about a member")
@app_commands.guild_only()
async def userinfo(i: discord.Interaction, member: discord.Member | None = None):
    member = member or i.user
    with db:
        row = get_user(i.guild.id, member.id)
    streak, _ = get_streak(i.guild.id, member.id)
    roles = [r.mention for r in reversed(member.roles[1:])]
    e = base(desc=f"{member.mention}\n-# ID: {member.id}")
    e.set_author(name=str(member), icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="📅 Joined", value=discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "?", inline=True)
    e.add_field(name="🎂 Created", value=discord.utils.format_dt(member.created_at, "D"), inline=True)
    e.add_field(name="⭐ Level", value=f"{row['level']} · {row['cookies']:,} 🍪 · {streak} 🔥", inline=False)
    e.add_field(name=f"🎭 Roles ({len(roles)})", value=" ".join(roles[:8]) + (" …" if len(roles) > 8 else "") or "None", inline=False)
    await i.response.send_message(embed=e)


@bot.tree.command(name="serverinfo", description="Show info about this server")
@app_commands.guild_only()
async def serverinfo(i: discord.Interaction):
    g = i.guild
    e = base(g.name, f"-# ID: {g.id}")
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    e.add_field(name="👑 Owner", value=f"<@{g.owner_id}>", inline=True)
    e.add_field(name="👥 Members", value=f"{g.member_count:,}", inline=True)
    e.add_field(name="🚀 Boosts", value=str(g.premium_subscription_count), inline=True)
    e.add_field(name="💬 Channels", value=f"{len(g.text_channels)} text · {len(g.voice_channels)} voice", inline=True)
    e.add_field(name="🎭 Roles", value=str(len(g.roles) - 1), inline=True)
    e.add_field(name="🎂 Created", value=discord.utils.format_dt(g.created_at, "D"), inline=True)
    await i.response.send_message(embed=e)


# ---------- Fun ----------
EIGHT_BALL = ["Yes, definitely!", "It is certain.", "Looks good to me.", "Ask again later.",
              "I can't tell right now.", "Hmm, doubtful.", "My sources say no.", "Absolutely not."]
HUGS = ["wraps them in the warmest hug 🤗", "sends a big cozy hug 🧡", "gives a gentle head-pat and a hug 🐾",
        "squeezes them tight 🫂", "shares a hug and a fresh cookie 🍪"]
MEOWS = ["Cats sleep 12-16 hours a day.", "A cat's purr can help calm stress.",
         "Cats have a special ear muscle set that lets them rotate their ears.",
         "Orange cats are mostly boys, about 80% of them!",
         "A cat's nose print is as unique as a fingerprint.", "Cats can jump up to six times their body length."]
@bot.tree.command(name="8ball", description="Ask the magic 8-ball")
async def eight_ball(i: discord.Interaction, question: app_commands.Range[str, 1, 200]):
    e = base("🔮  Magic 8-ball")
    e.add_field(name="You asked", value=clean(question), inline=False)
    e.add_field(name="Orage says", value=f"**{random.choice(EIGHT_BALL)}**", inline=False)
    await i.response.send_message(embed=e)


@bot.tree.command(name="choose", description="Let Orage decide for you")
@app_commands.describe(options="Separate with commas, e.g. pizza, noodles, rice")
async def choose(i: discord.Interaction, options: str):
    opts = [o.strip() for o in options.split(",") if o.strip()]
    if len(opts) < 2:
        return await fail(i, "Give me at least 2 options, separated by commas.")
    await i.response.send_message(embed=base("🤔  Orage picks…", f"**{clean(random.choice(opts))}**"))


@bot.tree.command(name="hug", description="Give someone a hug")
@app_commands.guild_only()
async def hug(i: discord.Interaction, member: discord.Member):
    if member.id == i.user.id:
        return await fail(i, "Hug someone else, I'll hug you after 🐱")
    await i.response.send_message(embed=base(desc=f"{i.user.mention} {random.choice(HUGS)} → {member.mention}", footer=False))


@bot.tree.command(name="ship", description="Check how well two people match")
@app_commands.guild_only()
async def ship(i: discord.Interaction, first: discord.Member, second: discord.Member | None = None):
    second = second or i.user
    a, b = sorted([first.id, second.id])
    pct = int(hashlib.md5(f"{a}-{b}".encode()).hexdigest(), 16) % 101
    hearts = round(pct / 10)
    note = ("Just friends 🐾" if pct < 30 else "There's a spark ✨" if pct < 60
            else "Cute together 🧡" if pct < 85 else "Meant to be 💞")
    h1, h2 = first.display_name, second.display_name
    name = h1[:max(1, len(h1) // 2)] + h2[len(h2) // 2:]
    e = base("💘  Ship", f"{first.mention} × {second.mention}\n\n**{clean(name)}**\n"
                         f"{'🧡' * hearts}{'🤍' * (10 - hearts)}  **{pct}%**\n{note}")
    await i.response.send_message(embed=e)


@bot.tree.command(name="meow", description="Orage shares a cat moment")
async def meow(i: discord.Interaction):
    await i.response.send_message(embed=base("🐱  Meow!", random.choice(MEOWS)))


# ---------- Shop (roles only) ----------
def shop_items(gid: int) -> list:
    return db.execute("SELECT * FROM shop_items WHERE guild_id=? AND kind='role' ORDER BY price, id", (gid,)).fetchall()


def find_item(gid: int, arg: str):
    if arg.isdigit():
        r = db.execute("SELECT * FROM shop_items WHERE guild_id=? AND id=? AND kind='role'", (gid, int(arg))).fetchone()
        if r:
            return r
    return db.execute("SELECT * FROM shop_items WHERE guild_id=? AND kind='role' AND LOWER(name)=LOWER(?)",
                      (gid, arg)).fetchone()


def shop_embed(member: discord.Member, cookies: int, items: list) -> discord.Embed:
    lines = []
    for r in items:
        owned = any(role.id == r["role_id"] for role in member.roles)
        lines.append(f"<@&{r['role_id']}> · 🍪 **{r['price']:,}**{'  ✅ *Owned*' if owned else ''}\n"
                     f"-# {clean(r['description'])}")
    body = "\n\n".join(lines) if lines else "No roles yet. Admins can create some with `/shopsetup`."
    e = base("🛍️  Role Shop", f"You have **{cookies:,}** 🍪\n\n{body}")
    if member.guild.icon:
        e.set_thumbnail(url=member.guild.icon.url)
    return e


async def do_purchase(i: discord.Interaction, item) -> tuple[bool, str]:
    gid, uid, price = i.guild.id, i.user.id, item["price"]
    role = i.guild.get_role(item["role_id"])
    if not role or not role.is_assignable():
        return False, "That role isn't available right now."
    if role in i.user.roles:
        return False, "You already have this role."
    with db:
        have = get_user(gid, uid)["cookies"]
        if have < price:
            return False, f"You need **{price:,}** 🍪 but only have **{have:,}**."
        db.execute("UPDATE users SET cookies = cookies - ? WHERE guild_id=? AND user_id=?", (price, gid, uid))
    try:
        await i.user.add_roles(role, reason="Orage shop")
    except discord.HTTPException:
        with db:  # refund
            db.execute("UPDATE users SET cookies = cookies + ? WHERE guild_id=? AND user_id=?", (price, gid, uid))
        return False, "I couldn't give you that role. You were not charged."
    await send_log(i.guild, "🛍️  Role purchased", f"{i.user.mention} bought {role.mention} for 🍪 **{price:,}**", i.user)
    return True, f"You got {role.mention}! 🎭"


class ConfirmBuy(discord.ui.View):
    def __init__(self, item_id: int):
        super().__init__(timeout=60)
        self.item_id = item_id

    @discord.ui.button(label="Buy", emoji="🛒", style=discord.ButtonStyle.success)
    async def buy(self, i: discord.Interaction, _):
        item = db.execute("SELECT * FROM shop_items WHERE id=? AND guild_id=?", (self.item_id, i.guild.id)).fetchone()
        if not item:
            return await i.response.edit_message(embed=err_embed("That item is gone."), view=None)
        ok, text = await do_purchase(i, item)
        await i.response.edit_message(embed=ok_embed(text) if ok else err_embed(text), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _):
        await i.response.edit_message(embed=base(desc="Purchase cancelled.", footer=False), view=None)


class ShopView(discord.ui.View):
    def __init__(self, items: list, member: discord.Member):
        super().__init__(timeout=300)
        if items:
            owned = {r.id for r in member.roles}
            opts = [discord.SelectOption(label=r["name"][:100], value=str(r["id"]), emoji="🎭",
                                         description=(f"✅ Owned" if r["role_id"] in owned else f"🍪 {r['price']:,}")[:100])
                    for r in items[:25]]
            sel = discord.ui.Select(placeholder="Pick a role to buy…", options=opts)
            sel.callback = self.pick
            self.add_item(sel)

    async def pick(self, i: discord.Interaction):
        item = db.execute("SELECT * FROM shop_items WHERE id=? AND guild_id=?",
                          (int(i.data["values"][0]), i.guild.id)).fetchone()
        if not item:
            return await fail(i, "That role is gone.")
        e = base("🛒  Confirm purchase", f"<@&{item['role_id']}>\nPrice: 🍪 **{item['price']:,}**\n"
                                         f"-# {clean(item['description'])}")
        await i.response.send_message(embed=e, view=ConfirmBuy(item["id"]), ephemeral=True)


@bot.tree.command(name="shop", description="Buy exclusive roles with cookies")
@app_commands.guild_only()
async def shop(i: discord.Interaction):
    items = shop_items(i.guild.id)
    with db:
        cookies = get_user(i.guild.id, i.user.id)["cookies"]
    await i.response.send_message(embed=shop_embed(i.user, cookies, items), view=ShopView(items, i.user), ephemeral=True)


# ---------- Tickets ----------
TICKET_TYPES = {  # kind: (emoji, label, [(question, style)])
    "partnership": ("🤝", "Partnership", [("Server name & invite link", discord.TextStyle.short),
                                          ("What can you offer?", discord.TextStyle.paragraph)]),
    "help": ("🆘", "Help", [("What do you need help with?", discord.TextStyle.paragraph)]),
    "suggest": ("💡", "Suggestion", [("Your suggestion", discord.TextStyle.paragraph)]),
    "report": ("🚨", "Report", [("Who or what are you reporting?", discord.TextStyle.short),
                                ("What happened?", discord.TextStyle.paragraph)]),
}


def ticket_cfg(gid: int):
    return db.execute("SELECT staff_role_id, category_id FROM ticket_config WHERE guild_id=?", (gid,)).fetchone()


def is_staff(member: discord.Member) -> bool:
    cfg = ticket_cfg(member.guild.id)
    return member.guild_permissions.manage_guild or bool(
        cfg and any(r.id == cfg["staff_role_id"] for r in member.roles))


async def ticket_precheck(i: discord.Interaction):
    """Returns the staff role if the member may open a ticket, else answers with an error and returns None."""
    g, cfg = i.guild, ticket_cfg(i.guild.id)
    staff = g.get_role(cfg["staff_role_id"]) if cfg else None
    if not staff:
        await fail(i, "Tickets aren't set up yet. Ask an admin to run `/ticket setup`.")
        return None
    old = db.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=?", (g.id, i.user.id)).fetchone()
    if old:
        if g.get_channel(old["channel_id"]):
            await fail(i, f"You already have an open ticket: <#{old['channel_id']}>")
            return None
        with db:
            db.execute("DELETE FROM tickets WHERE channel_id=?", (old["channel_id"],))
    return staff


class TicketModal(discord.ui.Modal):
    def __init__(self, kind: str):
        emoji, label, fields = TICKET_TYPES[kind]
        super().__init__(title=f"{emoji} {label}")
        self.kind, self.inputs = kind, []
        for question, style in fields:
            t = discord.ui.TextInput(label=question, style=style, required=True,
                                     max_length=1000 if style == discord.TextStyle.paragraph else 200)
            self.add_item(t)
            self.inputs.append(t)

    async def on_submit(self, i: discord.Interaction):
        await create_ticket(i, self.kind, [(t.label, str(t)) for t in self.inputs])


async def create_ticket(i: discord.Interaction, kind: str, answers: list):
    staff = await ticket_precheck(i)
    if not staff:
        return
    g, cfg = i.guild, ticket_cfg(i.guild.id)
    emoji, label, _ = TICKET_TYPES[kind]
    await i.response.defer(ephemeral=True)
    category = g.get_channel(cfg["category_id"]) if cfg["category_id"] else None
    slug = re.sub(r"[^a-z0-9]+", "-", i.user.name.lower()).strip("-") or "user"
    overwrites = {
        g.default_role: discord.PermissionOverwrite(view_channel=False),
        i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                            read_message_history=True),
        staff: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                           read_message_history=True, manage_messages=True),
        g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                          read_message_history=True, manage_channels=True),
    }
    try:
        ch = await g.create_text_channel(f"{kind}-{slug}", category=category, overwrites=overwrites,
                                         topic=f"{label} ticket · {i.user} ({i.user.id})", reason="Orage ticket")
    except discord.HTTPException:
        return await i.followup.send(embed=err_embed("I couldn't create the channel. I need **Manage Channels**."),
                                     ephemeral=True)
    with db:
        db.execute("INSERT INTO tickets(channel_id, guild_id, user_id, created, kind) VALUES(?, ?, ?, ?, ?)",
                   (ch.id, g.id, i.user.id, int(time.time()), kind))
    await send_log(g, "🎫  Ticket opened", f"{i.user.mention} opened a **{label}** ticket: {ch.mention}", i.user)
    e = base(f"{emoji}  {label} ticket",
             f"Hi {i.user.mention}, thanks for reaching out! {staff.mention} will be with you soon.\n"
             "-# Staff can press **Claim** to take this ticket.")
    for question, answer in answers:
        e.add_field(name=question, value=clean(answer)[:1000], inline=False)
    await ch.send(content=f"{i.user.mention} {staff.mention}", embed=e, view=TicketControlView(),
                  allowed_mentions=discord.AllowedMentions(users=[i.user], roles=[staff]))
    await i.followup.send(embed=ok_embed(f"Your ticket is ready: {ch.mention}"), ephemeral=True)


async def close_ticket(i: discord.Interaction):
    row = db.execute("SELECT user_id FROM tickets WHERE channel_id=?", (i.channel_id,)).fetchone()
    if not row:
        return await fail(i, "This isn't a ticket channel.")
    if i.user.id != row["user_id"] and not is_staff(i.user):
        return await fail(i, "Only the ticket owner or staff can close this.")
    await send_log(i.guild, "🔒  Ticket closed", f"`#{i.channel.name}` closed by {i.user.mention}\nOpened by <@{row['user_id']}>", i.user)
    await i.response.send_message(embed=base(desc="🔒  Closing this ticket in 5 seconds…", footer=False))
    await asyncio.sleep(5)
    ticket_closing.add(i.channel_id)
    with db:
        db.execute("DELETE FROM tickets WHERE channel_id=?", (i.channel_id,))
    try:
        await i.channel.delete(reason=f"Ticket closed by {i.user}")
    except discord.HTTPException:
        pass


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def go(self, i: discord.Interaction, kind: str):
        if await ticket_precheck(i):
            await i.response.send_modal(TicketModal(kind))

    @discord.ui.button(label="Partnership", emoji="🤝", style=discord.ButtonStyle.primary,
                       custom_id="orage:ticket:partnership")
    async def b_partnership(self, i: discord.Interaction, _):
        await self.go(i, "partnership")

    @discord.ui.button(label="Help", emoji="🆘", style=discord.ButtonStyle.success, custom_id="orage:ticket:help")
    async def b_help(self, i: discord.Interaction, _):
        await self.go(i, "help")

    @discord.ui.button(label="Suggest", emoji="💡", style=discord.ButtonStyle.secondary,
                       custom_id="orage:ticket:suggest")
    async def b_suggest(self, i: discord.Interaction, _):
        await self.go(i, "suggest")

    @discord.ui.button(label="Report", emoji="🚨", style=discord.ButtonStyle.danger, custom_id="orage:ticket:report")
    async def b_report(self, i: discord.Interaction, _):
        await self.go(i, "report")


class TicketControlView(discord.ui.View):
    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            for c in self.children:
                if getattr(c, "custom_id", "") == "orage:ticket:claim":
                    c.disabled, c.label = True, "Claimed"

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id="orage:ticket:claim")
    async def claim(self, i: discord.Interaction, _):
        row = db.execute("SELECT claimed_by FROM tickets WHERE channel_id=?", (i.channel_id,)).fetchone()
        if not row:
            return await fail(i, "This isn't a ticket channel.")
        if not is_staff(i.user):
            return await fail(i, "Only staff can claim tickets.")
        if row["claimed_by"]:
            return await fail(i, f"Already claimed by <@{row['claimed_by']}>.")
        with db:
            db.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=? AND claimed_by IS NULL",
                       (i.user.id, i.channel_id))
        e = discord.Embed.from_dict(i.message.embeds[0].to_dict())
        e.add_field(name="🙋 Claimed by", value=i.user.mention, inline=False)
        await i.response.edit_message(embed=e, view=TicketControlView(claimed=True))
        await send_log(i.guild, "🙋  Ticket claimed", f"{i.user.mention} claimed {i.channel.mention}", i.user)
        await i.channel.send(embed=base(desc=f"🙋  {i.user.mention} claimed this ticket and will help you now.",
                                        footer=False),
                             allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="orage:ticket:close")
    async def close_btn(self, i: discord.Interaction, _):
        await close_ticket(i)


@bot.event
async def on_guild_channel_delete(ch):
    with db:
        db.execute("DELETE FROM tickets WHERE channel_id=?", (ch.id,))
    if ch.id in ticket_closing:
        ticket_closing.discard(ch.id)
        return
    await send_log(ch.guild, "🗑️  Channel deleted", f"`#{ch.name}`")


ticket_group = app_commands.Group(name="ticket", description="Support tickets", guild_only=True)


@ticket_group.command(name="setup", description="Post the ticket panel in a channel")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Where the 🎫 panel goes", staff_role="Role that can see and answer tickets",
                       category="Category for new tickets (optional)")
async def ticket_setup(i: discord.Interaction, channel: discord.TextChannel, staff_role: discord.Role,
                       category: discord.CategoryChannel | None = None):
    me = i.guild.me
    p = channel.permissions_for(me)
    if not (p.view_channel and p.send_messages and p.embed_links) or not me.guild_permissions.manage_channels:
        return await fail(i, f"I need **Manage Channels** and *Send Messages* + *Embed Links* in {channel.mention}.")
    with db:
        db.execute("INSERT INTO ticket_config VALUES(?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                   "staff_role_id=excluded.staff_role_id, category_id=excluded.category_id",
                   (i.guild.id, staff_role.id, category.id if category else 0))
    e = base("🎫  Contact the Team",
             "Pick what fits and we'll open a **private ticket** just for you:\n\n"
             "🤝 **Partnership** · collab with us\n"
             "🆘 **Help** · need a hand?\n"
             "💡 **Suggest** · got an idea?\n"
             "🚨 **Report** · something's wrong")
    if i.guild.icon:
        e.set_thumbnail(url=i.guild.icon.url)
    await channel.send(embed=e, view=TicketPanelView())
    await done(i, f"Ticket panel posted in {channel.mention}. Staff: {staff_role.mention}")


@ticket_group.command(name="close", description="Close the ticket you're in")
async def ticket_close(i: discord.Interaction):
    await close_ticket(i)


@ticket_group.command(name="add", description="Add someone to this ticket (staff)")
async def ticket_add(i: discord.Interaction, member: discord.Member):
    if not db.execute("SELECT 1 FROM tickets WHERE channel_id=?", (i.channel_id,)).fetchone():
        return await fail(i, "This isn't a ticket channel.")
    if not is_staff(i.user):
        return await fail(i, "Only staff can add people to a ticket.")
    await i.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True,
                                    attach_files=True)
    await i.response.send_message(embed=ok_embed(f"{member.mention} was added to this ticket."))


# ---------- Self roles ("Pick Your Vibe") ----------
# Edit role IDs / labels here. exclusive=True means picking one removes the others in that group.
SELFROLES = [
    {"title": "👤 Identity", "note": "Pick one. Switching replaces the other.", "exclusive": True,
     "roles": [(1566264857055994006, "Male", "♂️", discord.ButtonStyle.secondary),
               (1566265056079913060, "Female", "♀️", discord.ButtonStyle.secondary)]},
    {"title": "🔔 Stay in the Loop", "note": "Chat reviews, updates and surprises.", "exclusive": False,
     "roles": [(1570801878965559377, "Ayhoo Ping", "🔔", discord.ButtonStyle.success)]},
]


def selfrole_lookup(rid: int):
    for group in SELFROLES:
        for role in group["roles"]:
            if role[0] == rid:
                return group, role
    return None


class SelfRoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"orage:sr:(?P<id>[0-9]+)"):
    def __init__(self, rid: int, label: str = "Role", emoji: str | None = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"orage:sr:{rid}"))
        self.rid = rid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        rid = int(match["id"])
        found = selfrole_lookup(rid)
        return cls(rid, *found[1][1:]) if found else cls(rid)

    async def callback(self, i: discord.Interaction):
        found = selfrole_lookup(self.rid)
        group, member = (found[0] if found else None), i.user
        role = i.guild.get_role(self.rid)
        if not role or not role.is_assignable():
            return await fail(i, "I can't manage that role. Move the **Orage** role above it.")
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Orage self-role")
                return await done(i, f"Removed {role.mention} 👋")
            swapped = []
            if group and group["exclusive"]:
                swapped = [o for o in (i.guild.get_role(r[0]) for r in group["roles"] if r[0] != self.rid)
                           if o and o in member.roles]
                if swapped:
                    await member.remove_roles(*swapped, reason="Orage self-role")
            await member.add_roles(role, reason="Orage self-role")
        except discord.HTTPException:
            return await fail(i, "Something went wrong. Try again!")
        extra = f"\n-# Replaced {', '.join(o.mention for o in swapped)}" if swapped else ""
        await done(i, f"You're now {role.mention} ✨{extra}")


class SelfRoleMine(discord.ui.DynamicItem[discord.ui.Button], template=r"orage:sr:mine"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="My roles", emoji="✨", style=discord.ButtonStyle.secondary,
                                           custom_id="orage:sr:mine"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, i: discord.Interaction):
        have = {r.id for r in i.user.roles}
        lines = [f"{'✅' if rid in have else '▫️'} <@&{rid}>" for g in SELFROLES for rid, *_ in g["roles"]]
        await i.response.send_message(embed=base("✨  Your vibe", "\n".join(lines)), ephemeral=True)


def selfrole_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(title="✨ Pick Your Vibe", color=ORANGE, description=(
        "Make yourself at home! Tap a button to grab a role, and tap it again anytime to take it off. 🧡"))
    for g in SELFROLES:
        lines = "\n".join(f"{emoji} <@&{rid}>" for rid, _, emoji, _ in g["roles"])
        e.add_field(name=g["title"], value=f"{lines}\n-# {g['note']}", inline=False)
    e.set_footer(text="Orage",
                 icon_url=bot.user.display_avatar.url if bot.user else None)
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    return e


@bot.tree.command(name="selfrole", description="Post the self-role panel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Where to post it (default: this channel)")
async def selfrole(i: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or i.channel
    for g in SELFROLES:
        for rid, label, _, _ in g["roles"]:
            role = i.guild.get_role(rid)
            if not role:
                return await fail(i, f"I can't find the **{label}** role (ID `{rid}`). Fix the IDs in `SELFROLES`.")
            if not role.is_assignable():
                return await fail(i, f"I can't assign {role.mention}. Move the **Orage** role above it and give me *Manage Roles*.")
    view = discord.ui.View(timeout=None)
    for g in SELFROLES:
        for rid, label, emoji, style in g["roles"]:
            view.add_item(SelfRoleButton(rid, label, emoji, style))
    view.add_item(SelfRoleMine())
    try:
        await channel.send(embed=selfrole_embed(i.guild), view=view)
    except discord.HTTPException:
        return await fail(i, f"I can't post in {channel.mention}.")
    await done(i, f"Self-role panel posted in {channel.mention}")


# ---------- Logs, Double XP & daily challenges ----------
ticket_closing: set = set()


async def send_log(guild: discord.Guild, title: str, desc: str, user=None):
    cid = get_channel_id(guild.id, "log")
    ch = guild.get_channel(cid) if cid else None
    if not ch:
        return
    e = base(title, desc)
    e.timestamp = discord.utils.utcnow()
    if user:
        e.set_thumbnail(url=user.display_avatar.url)
    try:
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


@bot.event
async def on_member_remove(member: discord.Member):
    roles = ", ".join(r.mention for r in member.roles[1:]) or "None"
    await send_log(member.guild, "📤  Member left", f"{member.mention} · `{member}`\n**Roles:** {roles}", member)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    await send_log(guild, "🔨  Member banned", f"{user.mention} · `{user}`", user)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    await send_log(guild, "🕊️  Member unbanned", f"{user.mention} · `{user}`", user)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if after.bot:
        return
    added = [r.mention for r in after.roles if r not in before.roles]
    removed = [r.mention for r in before.roles if r not in after.roles]
    if added or removed:
        lines = [after.mention]
        if added:
            lines.append("➕ " + " ".join(added))
        if removed:
            lines.append("➖ " + " ".join(removed))
        await send_log(after.guild, "🎭  Roles updated", "\n".join(lines), after)
    if before.nick != after.nick:
        await send_log(after.guild, "✏️  Nickname changed",
                       f"{after.mention}\n{before.nick or before.name} → {after.nick or after.name}", after)


@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    desc = f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}"
    if message.content:
        desc += f"\n**Message:** {clean(message.content)[:900]}"
    if message.attachments:
        desc += f"\n**Attachments:** {len(message.attachments)}"
    await send_log(message.guild, "🗑️  Message deleted", desc, message.author)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or before.channel == after.channel:
        return
    if before.channel is None:
        desc = f"{member.mention} joined {after.channel.mention}"
    elif after.channel is None:
        desc = f"{member.mention} left {before.channel.mention}"
    else:
        desc = f"{member.mention} moved {before.channel.mention} → {after.channel.mention}"
    await send_log(member.guild, "🔊  Voice", desc, member)


@bot.event
async def on_guild_channel_create(ch):
    await send_log(ch.guild, "📁  Channel created", f"{ch.mention}")


@bot.event
async def on_guild_role_create(role: discord.Role):
    await send_log(role.guild, "🎭  Role created", f"{role.mention} (`{role.name}`)")


@bot.event
async def on_guild_role_delete(role: discord.Role):
    await send_log(role.guild, "🎭  Role deleted", f"`{role.name}`")


@bot.tree.command(name="setlog", description="Pick the channel for all server logs")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Where logs go (leave empty to turn logs off)")
async def setlog(i: discord.Interaction, channel: discord.TextChannel | None = None):
    if channel is None:
        with db:
            set_channel_id(i.guild.id, "log", 0)
        return await done(i, "Logs are now **off**.")
    p = channel.permissions_for(i.guild.me)
    if not (p.view_channel and p.send_messages and p.embed_links):
        return await fail(i, f"I need *View Channel*, *Send Messages* and *Embed Links* in {channel.mention}.")
    with db:
        set_channel_id(i.guild.id, "log", channel.id)
    await done(i, f"All logs will be posted in {channel.mention}")
    await send_log(i.guild, "📜  Logging enabled", f"Set by {i.user.mention}", i.user)


# ---------- Double XP (owner only) ----------
def xp_mult(gid: int) -> int:
    r = db.execute("SELECT until FROM xp_events WHERE guild_id=?", (gid,)).fetchone()
    return 2 if r and r["until"] > time.time() else 1


@bot.tree.command(name="doublexp", description="(Owner) Start or stop a Double XP event")
@app_commands.guild_only()
@app_commands.describe(duration="How long, like 1h, 2h30m or 1d. Type off to stop.")
async def doublexp(i: discord.Interaction, duration: str):
    if i.user.id != OWNER_ID:
        return await fail(i, "Only the bot owner can use this command.")
    if duration.strip().lower() in ("off", "stop", "end"):
        with db:
            db.execute("DELETE FROM xp_events WHERE guild_id=?", (i.guild.id,))
        await i.response.send_message(embed=base("⚡  Double XP ended", "XP is back to normal."))
        return await send_log(i.guild, "⚡  Double XP ended", f"Stopped by {i.user.mention}", i.user)
    secs = parse_duration(duration)
    if not secs or secs < 60 or secs > 7 * 86400:
        return await fail(i, "Pick a time between 1 minute and 7 days, like `1h`, `2h30m` or `1d`. Or type `off`.")
    until = int(time.time()) + secs
    with db:
        db.execute("INSERT INTO xp_events VALUES(?, ?) ON CONFLICT(guild_id) DO UPDATE SET until=excluded.until",
                   (i.guild.id, until))
    await i.response.send_message(embed=base("⚡  Double XP is on!",
                                             f"All XP is **doubled** until <t:{until}:R>.\nKeep chatting! 💬"))
    await send_log(i.guild, "⚡  Double XP started", f"By {i.user.mention} · ends <t:{until}:R>", i.user)


# ---------- Daily challenges ----------
CHALLENGES = [
    'Share a photo of your favorite item you own.',
    'Show us something orange near you.',
    'Post a photo of the view from your window.',
    "What's your favorite snack or drink? Show it.",
    'Draw anything in two minutes and share it.',
    'Share a photo of your pet or your favorite animal.',
    "Name the song you can't stop replaying this week.",
    'Show your desk or study corner.',
    'Take a photo of the sky right now.',
    'Share a screenshot of your best game moment.',
    'Post something handmade, by you or anyone.',
    'Show the coolest thing in your room.',
    'Share the cover of a book, comic, or manga you love.',
    "Post the best meal you've ever eaten.",
    'Show us something green.',
    'Describe your day in exactly five words.',
    'Share a photo of your favorite pair of shoes.',
    'Pick your favorite emoji and explain why.',
    'Post a photo of a plant, flower, or tree.',
    'Show something that makes you smile.',
    'Share your go-to comfort food.',
    'Post a photo of your favorite mug, cup, or bottle.',
    "What's a small thing that made your day better lately?",
    'Show your wallpaper and tell us why you picked it.',
    'Share a doodle of your favorite animal.',
    'Post a photo of something blue.',
    "What's the best movie or series you watched recently?",
    'Show us your favorite place to relax at home.',
    'Share a quote you live by.',
    'Post a photo of your favorite hoodie, jacket, or cap.',
    'Tell us your hidden talent.',
    'Show a photo of something old that you still love.',
    'Share your favorite game character and why.',
    "Show what's in your backpack or bag.",
    'Draw your mood today using only three colors.',
    'Share your favorite place to eat.',
    'Post a photo of your morning.',
    'What song makes you feel unstoppable?',
    'Show us a collection you have, big or small.',
    'Share a photo of your favorite keyboard, controller, or gadget.',
    'Post something yellow.',
    'Tell us two truths and one lie about yourself.',
    'Show your favorite sticker, poster, or keychain.',
    "What's the most useful app on your phone? Why?",
    'Share a photo of a cloud that looks like something.',
    'Post your favorite dessert.',
    'Show us the best thing you found at home today.',
    "What's your dream trip? Share a picture of it.",
    'Share a photo of your favorite pen, notebook, or tool.',
    'Describe your perfect weekend in one sentence.',
    'Post a photo of your lunch or dinner today.',
    'Show us something red.',
    "What's the best advice anyone has given you?",
    'Share your favorite memory from this server.',
    'Post a photo of something tiny and cute.',
    'Show us how you take your coffee, tea, or favorite drink.',
    'Which game have you played the most? Show a screenshot.',
    'Share a photo of a candle, lamp, or cozy light.',
    'Draw a cat in 60 seconds.',
    'Post a photo of your favorite corner of the world.',
    'Show something you made in the kitchen.',
    'Share a song that reminds you of summer.',
    'Show us the shoes by your door.',
    'Tell us your favorite word and what it means to you.',
    'Post a photo of something shiny.',
    "What's your favorite thing to do on a rainy day?",
    "Share the best gift you've ever received.",
    'Show us your favorite fruit.',
    'Post a photo of a street, path, or road you like.',
    'Share a movie quote you know by heart.',
    'Show us something purple.',
    "What's one skill you want to learn this year?",
    'Share a photo of your favorite toy, plushie, or figure.',
    'Describe your dream room or setup.',
    'Post a picture of the night sky or city lights.',
    "What's the funniest thing that happened to you this month?",
    'Show your favorite food from your hometown.',
    'Share your top three songs right now.',
    'Post a photo of a sunrise or sunset.',
    'Show us something that is older than you.',
    "What's your favorite childhood game?",
    'Draw yourself as an animal.',
    'Share a photo of something that looks like a face.',
    'Show us your favorite tree or park.',
    'Tell us about a time you were really proud of yourself.',
    'Describe a song that stuck with you, in your own words.',
    'Show us something black and white.',
    "What's the first thing you do after waking up?",
    'Share your favorite drink for a hot day.',
    "Post a photo of something you're grateful for today.",
    'Which fictional place would you live in? Why?',
    'Show us an interesting shadow you found.',
    'Share your all-time favorite snack combo.',
    'Post a photo of your headphones, speaker, or instrument.',
    "Tell us what you'd do with a totally free day.",
    'Show the best thing you ate this week.',
    "Draw the logo of a server you'd start.",
    'Share a kind message that made you feel appreciated.',
    'Post something that looks like a smile.',
    "What's one thing you want the whole server to try today?",
]


def challenge_embed(day: int, channel_id: int = 0) -> discord.Embed:
    n = day % len(CHALLENGES)
    where = f"Share it in <#{channel_id}>" if channel_id else "Share it in the chat"
    e = base("📸  Daily Challenge", f"-# Challenge {n + 1} of {len(CHALLENGES)}\n\n**{CHALLENGES[n]}**\n\n{where}")
    e.add_field(name="⏰ Next challenge", value=f"<t:{next_day_ts()}:R>", inline=False)
    return e


@bot.tree.command(name="challenge", description="See today's daily challenge")
async def challenge(i: discord.Interaction):
    cid = get_channel_id(i.guild.id, "challenge") if i.guild else 0
    await i.response.send_message(embed=challenge_embed(today(), cid))


@tasks.loop(minutes=1)
async def challenge_loop():
    d = today()
    for r in db.execute("SELECT guild_id, channel_id FROM channels WHERE kind='challenge'").fetchall():
        last = db.execute("SELECT last_day FROM challenge_posts WHERE guild_id=?", (r["guild_id"],)).fetchone()
        if last and last["last_day"] == d:
            continue
        guild = bot.get_guild(r["guild_id"])
        ch = guild.get_channel(r["channel_id"]) if guild else None
        if not ch:
            continue
        try:
            await ch.send(embed=challenge_embed(d, ch.id))
        except discord.HTTPException:
            continue
        with db:
            db.execute("INSERT INTO challenge_posts VALUES(?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                       "last_day=excluded.last_day", (r["guild_id"], d))


@challenge_loop.before_loop
async def before_challenge_loop():
    await bot.wait_until_ready()


# ---------- Admin: setup ----------
ADMIN = dict(guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
perm = app_commands.checks.has_permissions(manage_guild=True)
channel_group = app_commands.Group(name="channel", description="Choose where Orage posts", **ADMIN)
autorole_group = app_commands.Group(name="autorole", description="Role given to new members", **ADMIN)
levelrole_group = app_commands.Group(name="levelrole", description="Roles unlocked by level", **ADMIN)

FEATURES = [app_commands.Choice(name="Welcome messages", value="welcome"),
            app_commands.Choice(name="Level-up announcements", value="levelup"),
            app_commands.Choice(name="Streak announcements", value="streak"),
            app_commands.Choice(name="Daily challenge", value="challenge")]
FEATURE_NAMES = {c.value: c.name for c in FEATURES}


@channel_group.command(name="set", description="Pick the channel for a feature")
@perm
@app_commands.choices(feature=FEATURES)
async def channel_set(i: discord.Interaction, feature: app_commands.Choice[str], channel: discord.TextChannel):
    p = channel.permissions_for(i.guild.me)
    if not (p.view_channel and p.send_messages and p.embed_links):
        return await fail(i, f"I need *View Channel*, *Send Messages* and *Embed Links* in {channel.mention}.")
    with db:
        set_channel_id(i.guild.id, feature.value, channel.id)
    await done(i, f"**{feature.name}** will be posted in {channel.mention}")


@channel_group.command(name="clear", description="Reset a feature's channel")
@perm
@app_commands.choices(feature=FEATURES)
async def channel_clear(i: discord.Interaction, feature: app_commands.Choice[str]):
    with db:
        set_channel_id(i.guild.id, feature.value, 0)
    extra = {"welcome": "Welcome messages are now **off**.",
             "challenge": "Daily challenges will no longer be posted automatically."}.get(
        feature.value, f"**{feature.name}** will post in the channel where the member chatted.")
    await done(i, extra)


@autorole_group.command(name="set", description="Set the role new members get")
@perm
async def autorole_set(i: discord.Interaction, role: discord.Role):
    if not role.is_assignable():
        return await fail(i, "I can't give that role. Give Orage *Manage Roles* and move the **Orage** role above it.")
    with db:
        set_autorole(i.guild.id, role.id)
    await done(i, f"New members will get {role.mention}")


@autorole_group.command(name="off", description="Turn autorole off")
@perm
async def autorole_off(i: discord.Interaction):
    with db:
        set_autorole(i.guild.id, 0)
    await done(i, "Autorole is now **off**.")


@levelrole_group.command(name="add", description="Give a role when members reach a level")
@perm
async def levelrole_add(i: discord.Interaction, level: app_commands.Range[int, 1, 500], role: discord.Role):
    if not role.is_assignable():
        return await fail(i, "I can't give that role. Move the **Orage** role above it.")
    with db:
        db.execute("INSERT OR REPLACE INTO level_roles VALUES(?, ?, ?)", (i.guild.id, level, role.id))
    await done(i, f"Level **{level}** → {role.mention}")


@levelrole_group.command(name="remove", description="Remove a level reward")
@perm
async def levelrole_remove(i: discord.Interaction, level: app_commands.Range[int, 1, 500]):
    with db:
        cur = db.execute("DELETE FROM level_roles WHERE guild_id=? AND level=?", (i.guild.id, level))
    await (done(i, "Removed.") if cur.rowcount else fail(i, "No reward at that level."))


@levelrole_group.command(name="list", description="Show all level rewards")
@perm
async def levelrole_list(i: discord.Interaction):
    rows = db.execute("SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level",
                      (i.guild.id,)).fetchall()
    text = "\n".join(f"Level **{r['level']}** → <@&{r['role_id']}>" for r in rows) or "No level rewards yet."
    await i.response.send_message(embed=base("🎖️  Level rewards", text), ephemeral=True)


shopadmin_group = app_commands.Group(name="shopadmin", description="Manage the role shop", **ADMIN)


async def item_autocomplete(i: discord.Interaction, current: str):
    return [app_commands.Choice(name=r["name"][:100], value=str(r["id"]))
            for r in shop_items(i.guild.id) if current.lower() in r["name"].lower()][:25]


@bot.tree.command(name="shopsetup", description="Create a role and sell it in the shop")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@perm
@app_commands.describe(name="Role name", price="Cookies it costs", color="Hex color like #FF9500 (optional)",
                       description="Short description (optional)")
async def shopsetup(i: discord.Interaction, name: app_commands.Range[str, 1, 60],
                    price: app_commands.Range[int, 1, 10_000_000], color: str | None = None,
                    description: str | None = None):
    if not i.guild.me.guild_permissions.manage_roles:
        return await fail(i, "I need the **Manage Roles** permission to create roles.")
    if color and not re.fullmatch(r"#?[0-9a-fA-F]{6}", color.strip()):
        return await fail(i, "Color must be a hex code like `#FF9500`.")
    items = shop_items(i.guild.id)
    if len(items) >= 20:
        return await fail(i, "The shop is full (20 roles). Remove one first.")
    if any(r["name"].lower() == name.lower() for r in items):
        return await fail(i, "There's already a shop role with that name.")
    colour = discord.Colour(int(color.strip().lstrip("#"), 16)) if color else discord.Colour(ORANGE)
    try:
        role = await i.guild.create_role(name=name, colour=colour, permissions=discord.Permissions.none(),
                                         reason=f"Orage shop role by {i.user}")
    except discord.HTTPException:
        return await fail(i, "I couldn't create the role. Check my permissions.")
    with db:
        db.execute("INSERT INTO shop_items(guild_id, kind, name, description, price, role_id) VALUES(?, ?, ?, ?, ?, ?)",
                   (i.guild.id, "role", name, (description or f"Show off the {name} role.")[:80], price, role.id))
    await done(i, f"Created {role.mention} and added it to the shop for 🍪 **{price:,}**.\n"
                  "-# Drag it in Server Settings → Roles if you want it higher in the list.")


@shopadmin_group.command(name="price", description="Change a role's price")
@perm
async def shop_price(i: discord.Interaction, item: str, price: app_commands.Range[int, 1, 10_000_000]):
    row = find_item(i.guild.id, item)
    if not row:
        return await fail(i, "I can't find that role in the shop.")
    with db:
        db.execute("UPDATE shop_items SET price=? WHERE id=?", (price, row["id"]))
    await done(i, f"<@&{row['role_id']}> now costs 🍪 **{price:,}**")


@shopadmin_group.command(name="remove", description="Remove a role from the shop")
@perm
@app_commands.describe(delete_role="Also delete the Discord role itself")
async def shop_remove(i: discord.Interaction, item: str, delete_role: bool = False):
    row = find_item(i.guild.id, item)
    if not row:
        return await fail(i, "I can't find that role in the shop.")
    with db:
        db.execute("DELETE FROM shop_items WHERE id=?", (row["id"],))
    if delete_role and (role := i.guild.get_role(row["role_id"])):
        try:
            await role.delete(reason=f"Removed from Orage shop by {i.user}")
        except discord.HTTPException:
            pass
    await done(i, f"Removed **{clean(row['name'])}** from the shop.")


shop_price.autocomplete("item")(item_autocomplete)
shop_remove.autocomplete("item")(item_autocomplete)


@bot.tree.command(name="settings", description="See Orage's settings for this server")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@perm
async def settings(i: discord.Interaction):
    g = i.guild

    def chan(kind, fallback):
        cid = get_channel_id(g.id, kind)
        return f"<#{cid}>" if cid and g.get_channel(cid) else fallback

    rid = get_autorole(g.id)
    role = g.get_role(rid) if rid else None
    n_rewards = db.execute("SELECT COUNT(*) FROM level_roles WHERE guild_id=?", (g.id,)).fetchone()[0]
    e = base("⚙️  Orage settings", "Change these with `/channel`, `/autorole` and `/levelrole`.")
    e.add_field(name="👋 Welcome", value=chan("welcome", "Off · use `/channel set`"), inline=True)
    e.add_field(name="⬆️ Level-ups", value=chan("levelup", "Where members chat"), inline=True)
    e.add_field(name="🔥 Streaks", value=chan("streak", "Where members chat"), inline=True)
    e.add_field(name="📜 Logs", value=chan("log", "Off · use `/setlog`"), inline=True)
    e.add_field(name="📸 Challenge", value=chan("challenge", "Off · use `/channel set`"), inline=True)
    e.add_field(name="🎭 Autorole", value=role.mention if role else "Off", inline=True)
    e.add_field(name="🎖️ Level rewards", value=f"{n_rewards} set", inline=True)
    e.add_field(name="🕛 Day resets", value=f"Midnight UTC{TZ_OFFSET:+g}", inline=True)
    await i.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="welcome_preview", description="Preview the welcome card")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@perm
async def welcome_preview(i: discord.Interaction):
    cid = get_channel_id(i.guild.id, "welcome")
    note = f"Preview · posts in <#{cid}>" if cid else "Preview · welcome is **off**, use `/channel set`"
    await i.response.send_message(note, embed=welcome_embed(i.user), ephemeral=True)


@bot.tree.command(name="ngl_trace", description="(Admin) Find who sent an anonymous message")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(message_id="Long-press the message > Copy Message ID")
async def ngl_trace(i: discord.Interaction, message_id: str):
    if not re.fullmatch(r"\d+", message_id):
        return await fail(i, "Invalid message ID.")
    row = db.execute("SELECT sender_id, target_id FROM ngl_log WHERE message_id=? AND guild_id=?",
                     (int(message_id), i.guild.id)).fetchone()
    if not row:
        return await fail(i, "No record found.")
    await i.response.send_message(
        embed=base("🔎  Trace", f"Sender: <@{row['sender_id']}>\nTarget: <@{row['target_id']}>"), ephemeral=True)


for grp in (channel_group, autorole_group, levelrole_group, shopadmin_group, ticket_group):
    bot.tree.add_command(grp)


@bot.tree.error
async def on_error(i: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission for that 😾"
    else:
        msg = "Oops, something went wrong. Try again!"
        print(error)
    if i.response.is_done():
        await i.followup.send(embed=err_embed(msg), ephemeral=True)
    else:
        await fail(i, msg)


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is missing. Add it to .env or your host's variables.")
bot.run(TOKEN)
