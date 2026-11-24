"""Orage music: /play /skip /stop /queue. Loaded by bot.py as an extension."""
import asyncio
import collections
import shutil
import time

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks

ORANGE = 0xFF9500
FOOTER = "Orage"
IDLE_LEAVE = 180      # seconds without listeners / music before leaving the channel
MAX_QUEUE = 25
YDL_OPTS = {
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 15,
    "extract_flat": False,
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # bundled ffmpeg, so the host does not need to install it
    return imageio_ffmpeg.get_ffmpeg_exe()


def fmt_time(secs) -> str:
    if not secs:
        return "live"
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def search(query: str):
    """Blocking. Tries YouTube first, then SoundCloud."""
    if query.startswith(("http://", "https://")):
        targets = [query]
    else:
        targets = [f"ytsearch1:{query}", f"scsearch1:{query}"]

    for target in targets:
        try:
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(target, download=False)
            if "entries" in info:
                entry = (info.get("entries") or [None])[0]
            else:
                entry = info

            if entry:
                return {
                    "title": entry.get("title") or query,
                    "url": entry.get("webpage_url") or entry.get("url"),
                    "duration": entry.get("duration"),
                    "thumb": entry.get("thumbnail"),
                }
        except Exception as err:
            print(f"[music] {target} failed: {err}")
    return None


def resolve(url: str):
    """Blocking. Fresh direct stream URL (+ headers) right before playing."""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    return info["url"], info.get("http_headers") or {}


def embed(title: str, desc: str = None) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=ORANGE)
    e.set_footer(text=FOOTER)
    return e


def err(text: str) -> discord.Embed:
    return discord.Embed(description=f"❌  {text}", color=0xE5484D)


def track_embed(title: str, t: dict) -> discord.Embed:
    e = embed(title, f"**[{t['title']}]({t['url']})**")
    e.add_field(name="⏱️ Length", value=fmt_time(t["duration"]), inline=True)
    e.add_field(name="🙋 Requested by", value=f"<@{t['by']}>", inline=True)
    if t.get("thumb"):
        e.set_thumbnail(url=t["thumb"])
    return e


class Player:
    def __init__(self):
        self.queue = collections.deque()
        self.current = None
        self.channel = None
        self.idle_since = time.time()


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    def player(self, gid: int) -> Player:
        return self.players.setdefault(gid, Player())

    def _after_play(self, guild: discord.Guild, error: Exception | None):
        if error:
            print(f"[music] FFmpeg playback error in {guild.name}: {error}")
        asyncio.run_coroutine_threadsafe(self.start_next(guild), self.bot.loop)

    async def start_next(self, guild: discord.Guild, announce: bool = True):
        p, vc = self.player(guild.id), guild.voice_client
        if not vc or not vc.is_connected():
            p.queue.clear()
            p.current = None
            return
        if not p.queue:
            p.current, p.idle_since = None, time.time()
            return

        track = p.current = p.queue.popleft()
        try:
            loop = asyncio.get_running_loop()
            stream, headers = await loop.run_in_executor(None, resolve, track["url"])

            before = FFMPEG_BEFORE
            if headers:
                raw_headers = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                safe_headers = raw_headers.replace('"', '\\"')
                before += f' -headers "{safe_headers}"'

            source = discord.FFmpegPCMAudio(
                stream,
                executable=ffmpeg_exe(),
                before_options=before,
                options="-vn -loglevel warning",
            )
        except Exception as e:
            print(f"[music] could not play {track['title']}: {e}")
            if p.channel:
                await p.channel.send(embed=err(f"I couldn't play **{track['title']}**, skipping it."))
            return await self.start_next(guild)

        vc.play(source, after=lambda e: self._after_play(guild, e))
        p.idle_since = time.time()
        if announce and p.channel:
            await p.channel.send(embed=track_embed("🎵  Now playing", track))

    @app_commands.command(name="play", description="Play a song in your voice channel")
    @app_commands.guild_only()
    @app_commands.describe(title="Song title (and artist)")
    async def play(self, i: discord.Interaction, title: str):
        voice = i.user.voice
        if not voice or not voice.channel:
            return await i.response.send_message(embed=err("Join a voice channel first 🎧"), ephemeral=True)

        vc = i.guild.voice_client
        if vc and vc.channel != voice.channel and any(not m.bot for m in vc.channel.members):
            return await i.response.send_message(embed=err(f"I'm already playing in {vc.channel.mention}."), ephemeral=True)

        p = self.player(i.guild.id)
        if len(p.queue) >= MAX_QUEUE:
            return await i.response.send_message(embed=err("The queue is full."), ephemeral=True)

        await i.response.defer()
        track = await asyncio.get_running_loop().run_in_executor(None, search, title)
        if not track or not track["url"]:
            return await i.followup.send(embed=err("I couldn't find that song. Try another title."))

        track["by"] = i.user.id
        try:
            if not vc:
                vc = await voice.channel.connect(self_deaf=True)
            elif vc.channel != voice.channel:
                await vc.move_to(voice.channel)
        except Exception as e:
            print(f"[music] connect failed: {e}")
            return await i.followup.send(embed=err("I couldn't join your voice channel. Check my *Connect* and *Speak* permissions."))

        p.channel = i.channel
        starting = not (vc.is_playing() or vc.is_paused() or p.current)
        p.queue.append(track)
        await i.followup.send(embed=track_embed("🎵  Now playing" if starting else f"➕  Added to queue · #{len(p.queue)}", track))
        if starting:
            await self.start_next(i.guild, announce=False)

    def _check(self, i: discord.Interaction):
        vc = i.guild.voice_client
        if not vc or not vc.is_connected():
            return None, "I'm not playing anything."
        if not i.user.voice or i.user.voice.channel != vc.channel:
            return None, "Join my voice channel first."
        return vc, None

    @app_commands.command(name="skip", description="Skip the current song")
    @app_commands.guild_only()
    async def skip(self, i: discord.Interaction):
        vc, problem = self._check(i)
        if problem or not (vc.is_playing() or vc.is_paused()):
            return await i.response.send_message(embed=err(problem or "Nothing is playing."), ephemeral=True)
        vc.stop()
        await i.response.send_message(embed=embed(desc="⏭️  Skipped."))

    @app_commands.command(name="stop", description="Stop the music and leave")
    @app_commands.guild_only()
    async def stop(self, i: discord.Interaction):
        vc, problem = self._check(i)
        if problem:
            return await i.response.send_message(embed=err(problem), ephemeral=True)
        p = self.player(i.guild.id)
        p.queue.clear()
        p.current = None
        await vc.disconnect()
        await i.response.send_message(embed=embed(desc="⏹️  Stopped. See you! 🧡"))

    @app_commands.command(name="queue", description="See what's playing and what's next")
    @app_commands.guild_only()
    async def queue(self, i: discord.Interaction):
        p = self.player(i.guild.id)
        if not p.current and not p.queue:
            return await i.response.send_message(embed=err("The queue is empty. Use `/play`."), ephemeral=True)
        lines = []
        if p.current:
            lines.append(f"**Now playing**\n[{p.current['title']}]({p.current['url']}) · {fmt_time(p.current['duration'])}")
        if p.queue:
            nxt = [f"`{n}.` [{t['title']}]({t['url']}) · {fmt_time(t['duration'])}" for n, t in enumerate(list(p.queue)[:10], 1)]
            more = f"\n-# +{len(p.queue) - 10} more" if len(p.queue) > 10 else ""
            lines.append("**Up next**\n" + "\n".join(nxt) + more)
        await i.response.send_message(embed=embed("🎶  Queue", "\n\n".join(lines)))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id and after.channel is None:  # kicked or disconnected
            p = self.player(member.guild.id)
            p.queue.clear()
            p.current = None

    @tasks.loop(seconds=10)
    async def tick(self):
        """Credits listening time to the leaderboard and leaves empty or idle channels."""
        now = time.time()
        for g in self.bot.guilds:
            vc = g.voice_client
            if not vc or not vc.is_connected():
                continue
            p = self.player(g.id)
            listeners = [m for m in vc.channel.members
                         if not m.bot and m.voice and not (m.voice.self_deaf or m.voice.deaf)]
            if vc.is_playing() and listeners:
                p.idle_since = now
                with self.bot.db:
                    for m in listeners:
                        self.bot.db.execute(
                            "INSERT INTO music_stats VALUES(?, ?, 10) ON CONFLICT(guild_id, user_id) "
                            "DO UPDATE SET seconds = seconds + 10", (g.id, m.id))
            elif now - p.idle_since > IDLE_LEAVE:
                p.queue.clear()
                p.current = None
                await vc.disconnect()

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Music(bot))