"""Orage - leveling, cookies & anonymous messages. Orange, clean, playful."""
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

# ---------- Settings (tweak freely) ----------
ORANGE = 0xFF9500
XP_MIN, XP_MAX = 15, 25          # XP per message
XP_COOLDOWN = 30                 # seconds between XP gains per user
NGL_COOLDOWN = 30                # seconds between /ngl per user
NGL_MAX_LEN = 300
TAGLINE = "Orage • Chat, level up, collect cookies"


def xp_needed(level: int) -> int:
    return 100 + 50 * level


def level_reward(level: int) -> int:
    return 25 + 5 * level        # cookies for reaching `level`


TITLES = [  # (min level, title)
    (0, "Fresh Dough"), (5, "Warm Cookie"), (10, "Choco Chipper"),
    (20, "Golden Baker"), (30, "Cookie Legend"), (50, "Cookie Monarch"),
]


def title_for(level: int) -> str:
    return [t for lv, t in TITLES if level >= lv][-1]


# ---------- Database ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
with db:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        guild_id INTEGER, user_id INTEGER,
        level INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, cookies INTEGER DEFAULT 0,
        PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS ngl_optout(
        guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id, user_id));
    CREATE TABLE IF NOT EXISTS ngl_log(
        message_id INTEGER PRIMARY KEY, guild_id INTEGER,
        sender_id INTEGER, target_id INTEGER, created INTEGER);
    """)


def get_user(gid: int, uid: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO users(guild_id, user_id) VALUES(?, ?)", (gid, uid))
    return db.execute(
        "SELECT level, xp, cookies FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
    ).fetchone()


def get_rank(gid: int, level: int, xp: int) -> int:
    return db.execute(
        "SELECT COUNT(*) + 1 FROM users WHERE guild_id=? AND (level > ? OR (level = ? AND xp > ?))",
        (gid, level, level, xp),
    ).fetchone()[0]


# ---------- Bot ----------
intents = discord.Intents.default()  # no privileged intents needed
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
xp_last: dict[tuple[int, int], float] = {}
ngl_last: dict[int, float] = {}


def make_embed(title=None, desc=None) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=ORANGE)
    e.set_footer(text=TAGLINE, icon_url=bot.user.display_avatar.url if bot.user else None)
    return e


def bar(xp: int, need: int, size: int = 10) -> str:
    filled = min(size, int(size * xp / need)) if need else 0
    return "🟧" * filled + "⬜" * (size - filled)


# ---------- Anonymous messages (NGL) ----------
class ReplyModal(discord.ui.Modal, title="Reply to this message"):
    answer = discord.ui.TextInput(
        label="Your reply", style=discord.TextStyle.paragraph, max_length=NGL_MAX_LEN
    )

    def __init__(self, message: discord.Message):
        super().__init__()
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        text = discord.utils.escape_mentions(str(self.answer))
        e = make_embed(desc=f"> {text}")
        e.set_author(name=f"{interaction.user.display_name} replied",
                     icon_url=interaction.user.display_avatar.url)
        await self.message.reply(embed=e, mention_author=False)
        try:
            await self.message.edit(view=None)  # one reply per message
        except discord.HTTPException:
            pass
        await interaction.response.send_message("Reply sent 🧡", ephemeral=True)


class NGLReply(discord.ui.DynamicItem[discord.ui.Button],
               template=r"ngl:reply:(?P<target>[0-9]+)"):
    def __init__(self, target_id: int):
        super().__init__(discord.ui.Button(
            label="Reply", emoji="💬", style=discord.ButtonStyle.primary,
            custom_id=f"ngl:reply:{target_id}"))
        self.target_id = target_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["target"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("Only the recipient can reply 🤫", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReplyModal(interaction.message))


@bot.event
async def setup_hook():
    bot.add_dynamic_items(NGLReply)
    await bot.tree.sync()


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game("🍪 /help"))
    print(f"Orage online as {bot.user} (ID: {bot.user.id})")


# ---------- Leveling ----------
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return
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
        e = make_embed("🎉 Level up!",
                       f"{msg.author.mention} reached **Level {level}** · *{title_for(level)}*\n"
                       f"Fresh from the oven: **+{reward}** 🍪")
        e.set_thumbnail(url=msg.author.display_avatar.url)
        try:
            await msg.channel.send(embed=e, allowed_mentions=discord.AllowedMentions(users=[msg.author]))
        except discord.HTTPException:
            pass


# ---------- Commands ----------
@bot.tree.command(name="help", description="See what Orage can do")
async def help_cmd(i: discord.Interaction):
    e = make_embed("🐱 Orage")
    e.description = "Chat to earn XP. Level up to bake cookies. 🍪"
    e.add_field(name="🍪 Cookies & Levels",
                value="`/profile` your card\n`/leaderboard` top bakers\n`/givecookies` share the love", inline=False)
    e.add_field(name="📨 Anonymous",
                value="`/ngl` send an anonymous message\n`/ngl_toggle` turn receiving on/off", inline=False)
    await i.response.send_message(embed=e)


@bot.tree.command(name="profile", description="Show your profile card")
@app_commands.guild_only()
async def profile(i: discord.Interaction, member: discord.Member | None = None):
    member = member or i.user
    if member.bot:
        return await i.response.send_message("Bots don't eat cookies 🤖", ephemeral=True)
    with db:
        row = get_user(i.guild.id, member.id)
    level, xp, cookies = row["level"], row["xp"], row["cookies"]
    need = xp_needed(level)
    rank = get_rank(i.guild.id, level, xp)

    e = make_embed(f"{member.display_name}", f"**{title_for(level)}**")
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="⭐ Level", value=f"**{level}**", inline=True)
    e.add_field(name="🍪 Cookies", value=f"**{cookies:,}**", inline=True)
    e.add_field(name="🏆 Rank", value=f"**#{rank}**", inline=True)
    e.add_field(name="✨ Progress", value=f"{bar(xp, need)}\n`{xp:,} / {need:,} XP`", inline=False)
    await i.response.send_message(embed=e)


@bot.tree.command(name="leaderboard", description="Top 10 bakers in this server")
@app_commands.guild_only()
async def leaderboard(i: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, level, cookies FROM users WHERE guild_id=? "
        "ORDER BY level DESC, xp DESC LIMIT 10", (i.guild.id,)).fetchall()
    if not rows:
        return await i.response.send_message("Nobody has XP yet. Start chatting! 💬", ephemeral=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[n] if n < 3 else f'`#{n + 1}`'} <@{r['user_id']}> · Lv **{r['level']}** · {r['cookies']:,} 🍪"
             for n, r in enumerate(rows)]
    await i.response.send_message(embed=make_embed("🏆 Leaderboard", "\n".join(lines)))


@bot.tree.command(name="givecookies", description="Give some of your cookies to someone")
@app_commands.guild_only()
@app_commands.describe(member="Who gets the cookies", amount="How many cookies")
async def givecookies(i: discord.Interaction, member: discord.Member,
                      amount: app_commands.Range[int, 1, 1_000_000]):
    if member.bot or member.id == i.user.id:
        return await i.response.send_message("Pick someone else 🍪", ephemeral=True)
    ok = False
    with db:
        giver = get_user(i.guild.id, i.user.id)
        get_user(i.guild.id, member.id)
        if giver["cookies"] >= amount:
            db.execute("UPDATE users SET cookies = cookies - ? WHERE guild_id=? AND user_id=?",
                       (amount, i.guild.id, i.user.id))
            db.execute("UPDATE users SET cookies = cookies + ? WHERE guild_id=? AND user_id=?",
                       (amount, i.guild.id, member.id))
            ok = True
    if not ok:
        return await i.response.send_message(
            f"You only have **{giver['cookies']:,}** 🍪. Keep chatting to bake more!", ephemeral=True)
    await i.response.send_message(embed=make_embed(
        "🎁 Cookies delivered!", f"{i.user.mention} gave **{amount:,}** 🍪 to {member.mention}"))


@bot.tree.command(name="ngl", description="Send an anonymous message to someone")
@app_commands.guild_only()
@app_commands.describe(member="Who it's for", message="Your anonymous message")
async def ngl(i: discord.Interaction, member: discord.Member,
              message: app_commands.Range[str, 1, NGL_MAX_LEN]):
    if member.bot or member.id == i.user.id:
        return await i.response.send_message("Pick another member 🙈", ephemeral=True)
    if db.execute("SELECT 1 FROM ngl_optout WHERE guild_id=? AND user_id=?",
                  (i.guild.id, member.id)).fetchone():
        return await i.response.send_message("That person turned off anonymous messages.", ephemeral=True)
    wait = NGL_COOLDOWN - (time.time() - ngl_last.get(i.user.id, 0))
    if wait > 0:
        return await i.response.send_message(f"Slow down! Try again in {int(wait) + 1}s ⏳", ephemeral=True)
    ngl_last[i.user.id] = time.time()

    text = discord.utils.escape_mentions(message)
    e = make_embed("📨 Anonymous message", f"> {text}")
    view = discord.ui.View(timeout=None)
    view.add_item(NGLReply(member.id))
    try:
        sent = await i.channel.send(
            content=member.mention, embed=e, view=view,
            allowed_mentions=discord.AllowedMentions(users=[member]))
    except discord.HTTPException:
        return await i.response.send_message("I can't post in this channel 😿", ephemeral=True)
    with db:
        db.execute("INSERT INTO ngl_log VALUES(?, ?, ?, ?, ?)",
                   (sent.id, i.guild.id, i.user.id, member.id, int(time.time())))
    await i.response.send_message(
        "Sent! 🤫 Other members can't see who you are (server admins can trace abuse).", ephemeral=True)


@bot.tree.command(name="ngl_toggle", description="Turn receiving anonymous messages on/off")
@app_commands.guild_only()
async def ngl_toggle(i: discord.Interaction):
    with db:
        cur = db.execute("DELETE FROM ngl_optout WHERE guild_id=? AND user_id=?", (i.guild.id, i.user.id))
        if cur.rowcount:
            return await i.response.send_message("Anonymous messages are now **ON** 📨", ephemeral=True)
        db.execute("INSERT INTO ngl_optout VALUES(?, ?)", (i.guild.id, i.user.id))
    await i.response.send_message("Anonymous messages are now **OFF** 🔕", ephemeral=True)


@bot.tree.command(name="ngl_trace", description="(Admin) Find who sent an anonymous message")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(message_id="Right-click the message > Copy Message ID")
async def ngl_trace(i: discord.Interaction, message_id: str):
    if not re.fullmatch(r"\d+", message_id):
        return await i.response.send_message("Invalid message ID.", ephemeral=True)
    row = db.execute("SELECT sender_id, target_id FROM ngl_log WHERE message_id=? AND guild_id=?",
                     (int(message_id), i.guild.id)).fetchone()
    if not row:
        return await i.response.send_message("No record found.", ephemeral=True)
    await i.response.send_message(
        f"Sender: <@{row['sender_id']}> → Target: <@{row['target_id']}>", ephemeral=True)


@bot.tree.error
async def on_error(i: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission for that 😾"
    else:
        msg = "Oops, something went wrong. Try again!"
        print(error)
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is missing. Add it to .env or your host's variables.")
bot.run(TOKEN)
