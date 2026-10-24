import os
import re
import glob
import asyncio
from collections import deque

import aiohttp
import discord
from discord.ext import commands
import yt_dlp

# Токен берётся из переменной окружения DISCORD_TOKEN (НЕ пиши его в код!)
TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.message_content = True  # нужно ещё включить в Developer Portal -> Bot -> Message Content Intent
bot = commands.Bot(command_prefix="!", intents=intents)

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YDL_BASE = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "playlistend": 25,
    "source_address": "0.0.0.0",  # принудительно IPv4 (у IPv6 дата-центров репутация хуже)
}
# Если положишь рядом cookies.txt (экспорт из браузера) — YouTube будет блокировать реже
if os.path.exists("cookies.txt"):
    YDL_BASE["cookiefile"] = "cookies.txt"


# ---------- работа с yt-dlp (блокирующая, поэтому вызываем через to_thread) ----------

def _search_sync(query: str):
    """Возвращает список треков [{'title', 'page'}]. Ссылка на сам поток берётся позже, при воспроизведении."""
    is_url = query.startswith(("http://", "https://"))
    targets = [query] if is_url else [f"ytsearch1:{query}", f"scsearch1:{query}"]  # YouTube, потом SoundCloud

    for target in targets:
        try:
            with yt_dlp.YoutubeDL({**YDL_BASE, "extract_flat": "in_playlist"}) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as e:
            print(f"[search] {target}: {e}")
            continue
        if not info:
            continue
        if info.get("entries"):
            tracks = []
            for e in info["entries"]:
                if not e:
                    continue
                page = e.get("webpage_url") or e.get("url")
                if page:
                    tracks.append({"title": e.get("title") or page, "page": page})
            if tracks:
                return tracks
        else:
            page = info.get("webpage_url") or info.get("url") or target
            return [{"title": info.get("title") or page, "page": page}]
    return []


# ---------- Яндекс Музыка ----------
# Бот берёт из ссылки список треков («Исполнитель - Название»), а звук ищет на YouTube/SoundCloud.
# Для этого нужен токен Яндекс Музыки в переменной YANDEX_TOKEN.

YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN")
SEARCH_PREFIX = os.environ.get("SEARCH_PREFIX", "ytsearch1")  # или "scsearch1" для SoundCloud
YANDEX_MAX_TRACKS = 100
YANDEX_URL_RE = re.compile(r"https?://music\.yandex\.\w+/\S+", re.I)
YA_API = "https://api.music.yandex.net"


def _ya_name(t: dict) -> str:
    artists = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
    title = t.get("title", "")
    if t.get("version"):
        title += f" ({t['version']})"
    return f"{artists} - {title}".strip(" -")


async def fetch_yandex_tracks(url: str) -> list[dict]:
    path = re.sub(r"^https?://[^/]+", "", url.split("?")[0].split("#")[0]).rstrip("/")
    headers = {
        "User-Agent": "Yandex-Music-API",
        "X-Yandex-Music-Client": "YandexMusicAndroid/24023231",
    }
    if YANDEX_TOKEN:  # токен необязателен: для публичных плейлистов можно попробовать без него
        headers["Authorization"] = f"OAuth {YANDEX_TOKEN}"
    async with aiohttp.ClientSession(headers=headers) as session:

        async def get(api_path: str):
            async with session.get(YA_API + api_path) as r:
                if r.status in (401, 403) and not YANDEX_TOKEN:
                    raise RuntimeError(
                        f"Яндекс требует авторизацию ({r.status}). Нужен токен YANDEX_TOKEN "
                        "(лучше от отдельного, не основного аккаунта)"
                    )
                if r.status != 200:
                    raise RuntimeError(f"Яндекс ответил {r.status} на запрос {api_path}")
                return (await r.json())["result"]

        raw = []  # список словарей-треков
        missing = []  # id треков, для которых нужно догрузить данные

        if m := re.fullmatch(r"/users/([^/]+)/playlists/([\w.-]+)", path):
            res = await get(f"/users/{m[1]}/playlists/{m[2]}")
        elif m := re.fullmatch(r"/playlists/([\w.-]+)", path):
            res = await get(f"/playlist/{m[1]}")
        elif m := re.fullmatch(r"(?:/album/\d+)?/track/(\d+)", path):
            raw = await get(f"/tracks/{m[1]}")
            res = None
        elif m := re.fullmatch(r"/album/(\d+)", path):
            res = await get(f"/albums/{m[1]}/with-tracks")
            raw = [t for vol in res.get("volumes", []) for t in vol]
            res = None
        else:
            raise RuntimeError("Не понимаю эту ссылку. Поддерживаются плейлисты, альбомы и треки")

        if res is not None:
            for item in res.get("tracks", [])[:YANDEX_MAX_TRACKS]:
                if item.get("track"):
                    raw.append(item["track"])
                elif item.get("id"):
                    album = item.get("albumId")
                    missing.append(f"{item['id']}:{album}" if album else str(item["id"]))

        if missing:
            async with session.post(YA_API + "/tracks", data={"track-ids": ",".join(missing)}) as r:
                if r.status == 200:
                    raw += (await r.json())["result"]

    names = [_ya_name(t) for t in raw[:YANDEX_MAX_TRACKS] if t.get("title")]
    return [{"title": n, "page": f"{SEARCH_PREFIX}:{n}"} for n in names]


CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
for _f in glob.glob(os.path.join(CACHE_DIR, "*")):  # чистим остатки с прошлого запуска
    try:
        os.remove(_f)
    except OSError:
        pass


def _download_sync(page_url: str) -> str:
    """Скачивает аудио во временный файл и возвращает путь к нему."""
    opts = {
        **YDL_BASE,
        "noplaylist": True,
        "format": "bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": os.path.join(CACHE_DIR, "%(id)s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=True)
        if info.get("entries"):
            info = info["entries"][0]
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        found = glob.glob(os.path.join(CACHE_DIR, f"{info['id']}.*"))
        if not found:
            raise RuntimeError("файл не скачался")
        path = found[0]
    return path


def start_prefetch(track: dict):
    """Запускает скачивание трека в фоне (если ещё не запущено)."""
    if "task" not in track:
        task = asyncio.create_task(asyncio.to_thread(_download_sync, track["page"]))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # без «Task exception never retrieved»
        track["task"] = task


class FileSource(discord.FFmpegPCMAudio):
    """Играет локальный файл и удаляет его, когда трек закончился."""

    def __init__(self, path: str):
        self._path = path
        super().__init__(path, options="-vn")

    def cleanup(self):
        super().cleanup()
        try:
            os.remove(self._path)
        except OSError:
            pass


LOW_CPU = os.environ.get("LOW_CPU") == "1"  # для слабых хостингов (Render free): звук без перекодирования


class OpusFileSource(discord.FFmpegOpusAudio):
    """Для webm/opus: ffmpeg просто перепаковывает звук, почти не нагружая процессор."""

    def __init__(self, path: str):
        self._path = path
        super().__init__(path, codec="copy", options="-vn")

    def cleanup(self):
        super().cleanup()
        try:
            os.remove(self._path)
        except OSError:
            pass


# ---------- состояние по каждому серверу ----------

class GuildState:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.volume = 0.5
        self.lock = asyncio.Lock()


states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in states:
        states[guild_id] = GuildState()
    return states[guild_id]


def prefetch_next(state: GuildState):
    """Пока играет текущий трек, заранее качаем следующий."""
    if state.queue:
        start_prefetch(state.queue[0])


async def play_next(guild: discord.Guild, channel: discord.abc.Messageable):
    state = get_state(guild.id)
    async with state.lock:
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            state.current = None
            return
        if vc.is_playing() or vc.is_paused():
            return

        while state.queue:
            track = state.queue.popleft()
            start_prefetch(track)
            if not track["task"].done():
                await channel.send(f"⏳ Загружаю: **{track['title']}**")
            try:
                path = await track["task"]
                if LOW_CPU and path.endswith(".webm"):
                    source = OpusFileSource(path)
                else:
                    source = discord.PCMVolumeTransformer(FileSource(path), volume=state.volume)
            except Exception as e:
                await channel.send(f"⚠️ Не удалось загрузить **{track['title']}**: {str(e)[:150]}")
                continue
            if not vc.is_connected():  # пока качали, бота выгнали из канала
                source.cleanup()
                state.current = None
                return
            state.current = track

            def after(error, guild=guild, channel=channel):
                if error:
                    print(f"[player] {error}")
                asyncio.run_coroutine_threadsafe(play_next(guild, channel), bot.loop)

            vc.play(source, after=after)
            prefetch_next(state)
            await channel.send(f"🎵 Сейчас играет: **{track['title']}**")
            return

        state.current = None


# ---------- команды ----------

@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user} (серверов: {len(bot.guilds)})")


@bot.command(aliases=["p"])
async def play(ctx, *, query: str):
    """!play название или ссылка (YouTube, SoundCloud и др., плейлисты тоже)"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("❌ Зайди в голосовой канал!")

    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()
    elif ctx.voice_client.channel != ctx.author.voice.channel:
        await ctx.voice_client.move_to(ctx.author.voice.channel)

    query = query.strip()
    if YANDEX_URL_RE.match(query):
        try:
            async with ctx.typing():
                tracks = await fetch_yandex_tracks(query)
        except Exception as e:
            print(f"[yandex] {e!r}")
            return await ctx.send(f"⚠️ Не получилось прочитать ссылку Яндекс Музыки: {str(e)[:200]}")
        if not tracks:
            return await ctx.send("❌ В этой ссылке не нашлось треков (плейлист пуст или закрыт)")
    else:
        async with ctx.typing():
            tracks = await asyncio.to_thread(_search_sync, query)
        if not tracks:
            return await ctx.send("❌ Ничего не найдено. Если ошибка повторяется на любых запросах, значит YouTube блокирует доступ")

    state = get_state(ctx.guild.id)
    state.queue.extend(tracks)
    prefetch_next(state)
    if len(tracks) == 1:
        await ctx.send(f"✅ Добавлено в очередь: **{tracks[0]['title']}**")
    else:
        await ctx.send(f"✅ Добавлено треков: **{len(tracks)}**")

    await play_next(ctx.guild, ctx.channel)


@bot.command()
async def skip(ctx):
    """Пропустить текущий трек"""
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # after() сам запустит следующий трек
        await ctx.send("⏭️ Пропущено")
    else:
        await ctx.send("❌ Сейчас ничего не играет")


@bot.command()
async def pause(ctx):
    """Поставить на паузу"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Пауза")
    else:
        await ctx.send("❌ Ничего не играет")


@bot.command()
async def resume(ctx):
    """Снять с паузы"""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Продолжаем")
    else:
        await ctx.send("❌ Трек не на паузе")


@bot.command(aliases=["vol"])
async def volume(ctx, vol: int):
    """Громкость от 0 до 200"""
    if not 0 <= vol <= 200:
        return await ctx.send("❌ Укажи число от 0 до 200")
    if LOW_CPU:
        return await ctx.send("ℹ️ В режиме LOW_CPU громкость бота не регулируется. Меняй её в Discord (ПКМ по боту в канале)")
    state = get_state(ctx.guild.id)
    state.volume = vol / 100
    if ctx.voice_client and isinstance(ctx.voice_client.source, discord.PCMVolumeTransformer):
        ctx.voice_client.source.volume = state.volume
    await ctx.send(f"🔊 Громкость: {vol}%")


@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx):
    """Показать очередь"""
    state = get_state(ctx.guild.id)
    lines = []
    if state.current:
        lines.append(f"▶️ **{state.current['title']}**")
    lines += [f"{i + 1}. {t['title']}" for i, t in enumerate(list(state.queue)[:10])]
    if lines:
        extra = f"\n…и ещё {len(state.queue) - 10}" if len(state.queue) > 10 else ""
        await ctx.send(("📋 **Очередь:**\n" + "\n".join(lines) + extra)[:2000])
    else:
        await ctx.send("📭 Очередь пуста")


@bot.command()
async def clear(ctx):
    """Очистить очередь (текущий трек доиграет)"""
    get_state(ctx.guild.id).queue.clear()
    await ctx.send("🗑️ Очередь очищена")


@bot.command(aliases=["leave"])
async def stop(ctx):
    """Остановить музыку и выйти из канала"""
    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.current = None
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Отключился")


@bot.event
async def on_voice_state_update(member, before, after):
    """Если бот остался в канале один — выходим."""
    vc = member.guild.voice_client
    if vc and vc.channel and all(m.bot for m in vc.channel.members):
        state = get_state(member.guild.id)
        state.queue.clear()
        state.current = None
        await vc.disconnect()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Не хватает аргумента. Пример: `!help {ctx.command}`")
    elif isinstance(error, (commands.CommandNotFound, commands.BadArgument)):
        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ Неверный аргумент")
    else:
        print(f"[error] {error!r}")
        await ctx.send(f"⚠️ Ошибка: {str(error)[:200]}")


# ---------- мини-веб-сервер (нужен для Render; локально не включается) ----------
from aiohttp import web

_web_started = False


async def _health(request):
    return web.Response(text="OK")


async def _start_web():
    global _web_started
    port = os.environ.get("PORT")
    if not port or _web_started:
        return
    app = web.Application()
    app.router.add_get("/", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    _web_started = True
    print(f"[web] слушаю порт {port}")


# ---------- запуск ----------
# Если Discord/Cloudflare блокирует IP хостинга (ошибка 429 / 1015), бот НЕ завершается:
# иначе Render перезапускал бы его каждую секунду и бан только продлевался бы.
# Вместо этого бот ждёт и пробует снова всё реже.

async def main():
    discord.utils.setup_logging()
    await _start_web()  # веб-сервер поднимаем сразу, чтобы Render считал сервис живым

    delay = 300  # 5 минут, дальше 10, 20, 40, до часа
    while True:
        try:
            await bot.login(TOKEN)
            break
        except discord.LoginFailure:
            print("❌ Discord не принял токен. Проверь DISCORD_TOKEN, затем перезапусти сервис.")
            await asyncio.Event().wait()  # просто ждём, не долбим Discord повторными входами
        except (discord.HTTPException, aiohttp.ClientError, OSError) as e:
            print(f"⚠️ Не удалось войти в Discord ({getattr(e, 'status', type(e).__name__)}): "
                  f"вероятно, блокировка IP хостинга. Повтор через {delay // 60} мин.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 3600)

    try:
        await bot.connect(reconnect=True)
    except discord.PrivilegedIntentsRequired:
        print("❌ Включи Message Content Intent в Developer Portal (Bot -> Privileged Gateway Intents), "
              "затем перезапусти сервис.")
        await asyncio.Event().wait()
    finally:
        await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
