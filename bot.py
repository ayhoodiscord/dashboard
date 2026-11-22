"""Orage - levels, cookies, streaks, anonymous messages & server tools."""
import os
import random
import re
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "orage.db")
TZ_OFFSET = float(os.getenv("TZ_OFFSET_HOURS", "7"))  # a new "day" starts at midnight in this UTC offset (7 = WIB)

# ---------- Settings (tweak freely) ----------
ORANGE = 0xFF9500
RED = 0xE5484D
FOOTER = "Orage • Chat, level up, collect cookies"
XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN = 30
NGL_COOLDOWN = 30
NGL_MAX_LEN = 300
DAILY_BASE = 50
DAILY_STREAK_BONUS = 10   # extra cookies per streak day
DAILY_STREAK_CAP = 7      # bonus stops growing after this many days
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
    CREATE TABLE IF NOT EXISTS ngl_optout(
        guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS ngl_log(
        message_id INTEGER PRIMARY KEY, guild_id INTEGER,
        sender_id INTEGER, target_id INTEGER, created INTEGER);
    """)


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
    bot.add_dynamic_items(NGLReply)
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


async def handle_streak(msg: discord.Message):
    """One message per day = one streak day."""
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
    await post_to(msg.guild, "streak", msg.channel, content=f"{msg.author.mention} streaks {n} 🔥",
                  allowed_mentions=discord.AllowedMentions.none())


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return
    await handle_streak(msg)

    key = (msg.guild.id, msg.author.id)
    now = time.time()
    if now - xp_last.get(key, 0) < XP_COOLDOWN:
        return
    xp_last[key] = now

    with db:
        row = get_user(*key)
        level, xp, cookies = row["level"], row["xp"] + random.randint(XP_MIN, XP_MAX), row["cookies"]
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
    e = discord.Embed(title="👋 Welcome to Orage!!", color=ORANGE, description=(
        f"Hey {member.mention}, you made it! 🧡\n\n"
        "We're really happy to have you here. Grab a seat, say hi, and make yourself at home."))
    e.add_field(name="🌟 Where to start", value=WHERE_TO_START, inline=False)
    e.add_field(name="\u200b", value="*Talk. Play. Create. Connect.*", inline=False)
    e.set_thumbnail(url=member.display_avatar.url)
    e.set_footer(text="— Orage • Glad you're here 🧡",
                 icon_url=bot.user.display_avatar.url if bot.user else None)
    return e


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
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
               "`/profile` see your card\n`/daily` claim free cookies\n`/leaderboard` see the top members")]),
    "cookies": ("🍪  Cookies & Levels", "Everything about your progress.",
                [("Commands",
                  "`/profile` `[member]`\nView a profile card.\n\n"
                  "`/leaderboard`\nTop members by level, cookies or streak.\n\n"
                  "`/daily`\nClaim your daily cookies. A longer streak means a bigger reward.\n\n"
                  "`/givecookies` `member` `amount`\nShare your cookies with someone.")]),
    "streaks": ("🔥  Streaks",
                "Send **at least one message each day** to grow your streak.",
                [("How it works",
                  "• First message of the day → Orage posts `@you streaks 5 🔥`\n"
                  "• Miss a full day → the streak resets\n"
                  f"• A new day starts at midnight (UTC{TZ_OFFSET:+g})\n"
                  "• Your best streak shows on `/profile`")]),
    "ngl": ("📨  Anonymous Messages", "Say it without saying who you are.",
            [("Commands",
              "`/ngl` `member` `message`\nSend an anonymous message. They can reply with a button.\n\n"
              "`/ngl_toggle`\nTurn receiving anonymous messages on or off."),
             ("Good to know", "Be kind. Admins can trace abuse.")]),
    "admin": ("🛠️  Server Setup", "For members with **Manage Server**.",
              [("Choose channels", "`/channel set` `feature` `#channel`\nWelcome, level-up and streak messages.\n"
                                   "`/channel clear` `feature`\nWelcome turns off; the others post where the member chatted."),
               ("Roles", "`/autorole set` `role` · `/autorole off`\n"
                         "`/levelrole add` `level` `role` · `remove` · `list`"),
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
                discord.SelectOption(label="Anonymous", value="ngl", emoji="📨", description="Send anonymous messages")]
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

    e = base("📨  Anonymous message", f"> {discord.utils.escape_mentions(message)}")
    e.set_footer(text=f"Sent anonymously • Only {member.display_name} can reply")
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


# ---------- Admin: setup ----------
ADMIN = dict(guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
perm = app_commands.checks.has_permissions(manage_guild=True)
channel_group = app_commands.Group(name="channel", description="Choose where Orage posts", **ADMIN)
autorole_group = app_commands.Group(name="autorole", description="Role given to new members", **ADMIN)
levelrole_group = app_commands.Group(name="levelrole", description="Roles unlocked by level", **ADMIN)

FEATURES = [app_commands.Choice(name="Welcome messages", value="welcome"),
            app_commands.Choice(name="Level-up announcements", value="levelup"),
            app_commands.Choice(name="Streak announcements", value="streak")]
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
    extra = "Welcome messages are now **off**." if feature.value == "welcome" \
        else f"**{feature.name}** will post in the channel where the member chatted."
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


for grp in (channel_group, autorole_group, levelrole_group):
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
