import discord
from discord.ext import commands, tasks
import sqlite3
import os
import json
import random
import aiohttp
import datetime
import string
from collections import Counter
from io import BytesIO
import matplotlib.pyplot as plt
import re
from discord import app_commands
import uwuipy
import asyncio
from gif_engine.db import init_gif_db
from gif_engine.search import search_gifs
from gif_engine.ingest import ingest_result
from gif_engine.prune import prune_dead_urls

# --- Database setup (with migration for guild_id) ---
db = sqlite3.connect("wordcount.db", check_same_thread=False)
cursor = db.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    author_id INTEGER,
    content TEXT,
    timestamp TEXT
)
''')
db.commit()

cursor.execute("PRAGMA journal_mode=WAL;")

# Ensure schema has guild_id column (safe migration)
cursor.execute("PRAGMA table_info(messages)")
cols = [r[1] for r in cursor.fetchall()]
if "guild_id" not in cols:
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN guild_id INTEGER")
        db.commit()
        print("✅ Migrated messages table: added guild_id column.")
    except Exception as e:
        print(f"⚠️ Could not add guild_id column: {e}")

# --- Helpers and config loading ---
def load_stopwords(path="stopwords.txt"):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return {line.strip().lower() for line in file if line.strip()}
    except FileNotFoundError:
        print("⚠️ stopwords.txt not found. No stopwords loaded.")
        return set()

stopwords = load_stopwords()

def tokenize_text(text, stopwords=None):
    # remove links, mentions and hashtags first
    text = re.sub(r"(https?://\S+|www\.\S+)", "", text)
    text = re.sub(r"@[\w_]+", "", text)
    text = re.sub(r"#\w+", "", text)

    # normalize apostrophes
    text = text.replace("’", "'")

    # extract tokens (at least 2 chars) and allow some punctuation chars intentionally
    raw_tokens = re.findall(r"\b[\w\*#@!$%]{2,}\b", text.lower())

    # remove emoji shortcodes like :smile:
    raw_tokens = [token for token in raw_tokens if not (token.startswith(":") and token.endswith(":"))]

    # keep tokens that contain a letter (not just numbers or symbols)
    tokens = [token for token in raw_tokens if any(c.isalpha() for c in token)]

    if stopwords:
        tokens = [token for token in tokens if token not in stopwords]

    return tokens

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

kill_switch_engaged = False
auto_purify_enabled = False
stalked_user_ids = set()
uwu = uwuipy.Uwuipy()
uwulocked_user_ids = set()
webhook_cache = {}

bot = commands.Bot(command_prefix="s ", intents=intents)

kms_media_path = "kms_media.json"
try:
    with open(kms_media_path, "r", encoding="utf-8") as f:
        kms_data = json.load(f)
        kms_media_list = kms_data.get("media", [])
except Exception as e:
    print(f"⚠️ Failed to load KMS media: {e}")
    kms_media_list = []

# Safely parse env variables (avoid ValueError on empty string)
log_channel_id = None
_raw_log_id = os.getenv("LOG_CHANNEL_ID")
if _raw_log_id and _raw_log_id.isdigit():
    log_channel_id = int(_raw_log_id)

# Removed ALLOWED_USER_IDS / ALLOWED_ROLE_IDS. Use admin checks instead.

raw_channel_ids = os.getenv("PURIFY_CHANNEL_IDS", "")
PURIFY_CHANNEL_IDS = {int(cid.strip()) for cid in raw_channel_ids.split(",") if cid.strip().isdigit()}

TOXIC_WORDS = set()
if os.path.exists("badwords_en.txt"):
    with open("badwords_en.txt", "r", encoding="utf-8") as f:
        TOXIC_WORDS = set(line.strip().lower() for line in f if line.strip())

SHORTCUTS = {}
def register_shortcuts():
    for command in bot.commands:
        if hasattr(command, "shortcut"):
            SHORTCUTS[command.shortcut] = command.name

async def log_action(message):
    if log_channel_id:
        try:
            log_channel = bot.get_channel(log_channel_id)
            if log_channel:
                await log_channel.send(message)
        except Exception:
            pass
    print(f"[LOG] {message}")

async def handle_gif_query(message, query: str):
    try:
        # 1. Try local semantic search
        result = await search_gifs(
            query=query,
            allow_nsfw=message.channel.is_nsfw()
        )

        # 2. If nothing found, search online (inside search_gifs)
        if not result:
            await message.channel.send("❌ No results found.")
            return

        # 3. Send media as embed or file
        embed = discord.Embed()
        embed.set_image(url=result["url"])
        await message.channel.send(embed=embed)

        # 4. Persist result for future queries
        ingest_result(
            query=query,
            url=result["url"],
            source=result["source"],
            nsfw=result["nsfw"]
        )

    except Exception as e:
        await log_action(f"GIF query error: {e}")

# --- Purify helpers ---
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")

def message_has_image_attachment(msg: discord.Message) -> bool:
    if not msg.attachments:
        return False
    for att in msg.attachments:
        # content_type may be None in some cases; fallback to filename check
        ctype = getattr(att, "content_type", None)
        if ctype and ctype.startswith("image"):
            return True
        if att.filename and att.filename.lower().endswith(IMAGE_EXTENSIONS):
            return True
    return False

# --- Maintenance tasks and caching ---
@tasks.loop(hours=12)
async def gif_prune_task():
    prune_dead_urls()

@tasks.loop(minutes=120)
async def auto_purify():
    # Iterate configured channel ids rather than scanning all guild channels to avoid permission problems
    for cid in PURIFY_CHANNEL_IDS:
        channel = bot.get_channel(cid)
        if not channel or not isinstance(channel, discord.TextChannel):
            continue
        try:
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.author == bot.user:
                    continue
                if message_has_image_attachment(msg):
                    continue
                # keep if reactions >= 3
                if msg.reactions and sum(r.count for r in msg.reactions) >= 3:
                    continue
                try:
                    await msg.delete()
                    await log_action(f"Auto-deleted message from {msg.author.display_name} in #{channel.name}")
                except discord.HTTPException as e:
                    await log_action(f"Failed deleting message in #{channel.name}: {e}")
                    await asyncio.sleep(1)
        except Exception as e:
            await log_action(f"Error in auto-purify for #{channel.name if channel else cid}: {e}")

@tasks.loop(minutes=5)
async def background_cache():
    # Fetch recent history from every accessible channel to keep DB up to date
    for guild in bot.guilds:
        for channel in guild.text_channels:
            # only cache channels we can read
            if not channel.permissions_for(guild.me).read_message_history:
                continue
            try:
                async for message in channel.history(limit=500, oldest_first=False):
                    if message.author.bot or message.webhook_id is not None or message.guild is None:
                        continue
                    try:
                        cursor.execute(
                            "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (message.id, message.channel.id, message.author.id, message.content or "", message.created_at.isoformat(), message.guild.id)
                        )
                    except Exception:
                        cursor.execute(
                            "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (message.id, message.channel.id, message.author.id, (message.content or "").encode("utf-8", errors="replace").decode("utf-8"), message.created_at.isoformat(), message.guild.id)
                        )
                db.commit()
                await asyncio.sleep(0)
            except Exception as e:
                print(f"[ERROR] background_cache failed in {channel.name if channel else 'unknown'}: {e}")

async def cache_channel_history(guild: discord.Guild):
    # Deep history crawl for a single guild (used internally if needed)
    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).read_message_history:
            print(f"[SKIP] No permission to read {channel.name}")
            continue
        batch = []
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.bot or message.webhook_id is not None or message.guild is None:
                    continue
                if message.content and message.content.startswith(('s ', '/')):
                    # keep previous behavior to ignore bot commands if present
                    continue
                batch.append((
                    message.id,
                    message.channel.id,
                    message.author.id,
                    message.content or "",
                    message.created_at.isoformat(),
                    message.guild.id
                ))
                if len(batch) >= 500:
                    cursor.executemany(
                        "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                        batch
                    )
                    db.commit()
                    batch.clear()
            if batch:
                cursor.executemany(
                    "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                    batch
                )
                db.commit()
                batch.clear()
        except Exception as e:
            print(f"[ERROR] cache_channel_history failed for {channel.name}: {e}")

# --- Utility to generate graphs ---
def generate_usage_graph(data_dict, title):
    if not data_dict:
        return None
    x = sorted(data_dict.keys())
    y = [data_dict[k] for k in x]
    plt.figure(figsize=(10, 4))
    plt.plot(x, y, marker='o')
    plt.xticks(rotation=45)
    plt.title(title)
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# --- Bot events ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user.name}")
    
    try:
        synced = await bot.tree.sync()
        print(f"🔁 Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"⚠️ Failed to sync slash commands: {e}")
   
    register_shortcuts()
   
    if not background_cache.is_running():
        background_cache.start()
   
    if not gif_prune_task.is_running():
        gif_prune_task.start()


@bot.event
async def on_message(message):
    if message.author.bot or message.webhook_id is not None:
        return

    # Skip DMs if needed
    if message.guild is None:
        return

    # --- Database insert ---
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
            (message.id, message.channel.id, message.author.id, message.content or "", message.created_at.isoformat(), message.guild.id)
        )
        db.commit()
    except Exception:
        pass

    # --- Uwu lock handling ---
    if message.author.id in uwulocked_user_ids:
        try:
            await message.delete()
            channel = message.channel
            if channel.id not in webhook_cache:
                webhooks = await channel.webhooks()
                webhook = discord.utils.get(webhooks, name="UwuFiend")
                if webhook is None:
                    webhook = await channel.create_webhook(name="UwuFiend")
                webhook_cache[channel.id] = webhook
            else:
                webhook = webhook_cache[channel.id]

            uwu_text = uwu.uwuify(message.content).strip()
            if len(uwu_text) > 2000:
                uwu_text = uwu_text[:1997] + "..."
            await webhook.send(
                content=uwu_text,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
            )
        except Exception as e:
            print(f"[UWULOCK ERROR] Failed to uwuify message: {e}")

    # --- Stalked user handling ---
    if message.author.id in stalked_user_ids:
        try:
            await message.delete()
            await log_action(f"Deleted message from stalked user: {message.author.display_name}")
        except Exception as e:
            await log_action(f"Failed to delete stalked user message: {e}")
        return  # Stop further processing

    # --- Shortcut command handling ---
    if message.content and message.content.lower().startswith("s "):
        parts = message.content[2:].split()
        if parts:
            shortcut = parts[0].lower()
            args = parts[1:]
            if shortcut in SHORTCUTS:
                command = bot.get_command(SHORTCUTS[shortcut])
                if command:
                    ctx = await bot.get_context(message)
                    await ctx.invoke(command, *args)
                    return

    # --- GIF query handling ---
    if message.content.startswith("//"):
        query = message.content[2:].strip()
        if query:
            await handle_gif_query(message, query)

    # Finally, process commands
    await bot.process_commands(message)

# --- Counting & analysis commands (now guild-scoped) ---
@bot.hybrid_command(name="count", description="Count how often a word was said in the server.")
async def count(ctx, *, word: str):
    word = word.lower()
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT author_id, content FROM messages WHERE guild_id = ?", (ctx.guild.id,))
    rows = cursor.fetchall()
    total = 0
    user_counts = Counter()
    for author_id, content in rows:
        tokens = tokenize_text(content or "", stopwords)
        count_ = tokens.count(word)
        if count_ > 0:
            user_counts[author_id] += count_
            total += count_
    if total == 0:
        await ctx.send(f"Not one soul has deemed `{word}` worth using except you. Loser.")
        return
    top_users = user_counts.most_common(10)
    result_lines = []
    for uid, count_ in top_users:
        user = ctx.guild.get_member(uid)
        name = user.display_name if user else f"User {uid}"
        result_lines.append(f"**{name}** — {count_} time(s)")
    await ctx.send(f"**📊 Here you go your highness, your stupid chart for `{word}`:**\n🔢 Total Mentions: `{total}`\n\n🏆 **Top 10 Users:**\n" + "\n".join(result_lines))
count.shortcut = "c"

@bot.hybrid_command(name="usercount", description="See how often a user said a word.")
async def usercount(ctx, word: str, member: discord.Member):
    word = word.lower()
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT content FROM messages WHERE author_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    messages = cursor.fetchall()
    count_ = sum(tokenize_text(msg[0] or "", stopwords).count(word) for msg in messages)
    await ctx.send(f"**{member.display_name}** has said `{word}` **{count_}** time(s). What a bitch.")
usercount.shortcut = "uc"

@bot.hybrid_command(name="top10", description="Show top 10 most used words in the server.")
async def top10(ctx):
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT content FROM messages WHERE guild_id = ?", (ctx.guild.id,))
    rows = cursor.fetchall()
    word_counter = Counter()
    for (content,) in rows:
        words = tokenize_text(content or "", stopwords)
        for w in words:
            if w and w not in stopwords:
                word_counter[w] += 1
    top = word_counter.most_common(10)
    msg = "**📊 Top 10 Most Used Words in this Godforsaken Place (Filtered):**\n" + "\n".join([f"`{w}` — {c} time(s)" for w, c in top])
    await ctx.send(msg)
top10.shortcut = "top"

@bot.hybrid_command(name="mylist", description="Show your personal top 10 most used words.")
async def mylist(ctx):
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    user_id = ctx.author.id
    cursor.execute("SELECT content FROM messages WHERE author_id = ? AND guild_id = ?", (user_id, ctx.guild.id))
    rows = cursor.fetchall()
    word_counter = Counter()
    for (content,) in rows:
        cleaned = tokenize_text(content or "", stopwords)
        for word in cleaned:
            if word and word not in stopwords:
                word_counter[word] += 1
    if not word_counter:
        await ctx.send("You haven't said anything interesting yet. Have you tried sucking a little less?")
        return
    top_words = word_counter.most_common(10)
    result_lines = [f"`{word}` — {count_} time(s)" for word, count_ in top_words]
    await ctx.send("**🧠 Your Top 10 Words, you fuckin narcissist:**\n" + "\n".join(result_lines))
mylist.shortcut = "me"

@bot.hybrid_command(name="daily", description="Hourly usage graph of a word (today).")
async def daily(ctx, *, word: str):
    word = word.lower()
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT timestamp, content FROM messages WHERE guild_id = ?", (ctx.guild.id,))
    rows = cursor.fetchall()
    today = datetime.datetime.utcnow().date()
    usage_by_hour = {}
    for timestamp, content in rows:
        try:
            ts = datetime.datetime.fromisoformat(timestamp)
        except Exception:
            continue
        if ts.date() != today:
            continue
        if word in tokenize_text(content or "", stopwords):
            hour = ts.strftime("%H:00")
            usage_by_hour[hour] = usage_by_hour.get(hour, 0) + 1
    buf = generate_usage_graph(usage_by_hour, f"Here's your fuckin graph for '{word}' today. Asshole.")
    if buf:
        await ctx.send(file=discord.File(buf, filename="daily.png"))
    else:
        await ctx.send(f"No one said `{word}` today. Bet you feel stupid now, don't you.")
daily.shortcut = "day"

@bot.hybrid_command(name="thisweek", description="Daily usage graph (last 7 days).")
async def thisweek(ctx, *, word: str):
    word = word.lower()
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT timestamp, content FROM messages WHERE guild_id = ?", (ctx.guild.id,))
    rows = cursor.fetchall()
    today = datetime.datetime.utcnow().date()
    usage_by_day = {}
    for timestamp, content in rows:
        try:
            ts = datetime.datetime.fromisoformat(timestamp)
        except Exception:
            continue
        if (today - ts.date()).days > 6:
            continue
        if word in tokenize_text(content or "", stopwords):
            day = ts.strftime("%a %m/%d")
            usage_by_day[day] = usage_by_day.get(day, 0) + 1
    buf = generate_usage_graph(usage_by_day, f"Fuck you and your graph for '{word}' (last 7 days)")
    if buf:
        await ctx.send(file=discord.File(buf, filename="thisweek.png"))
    else:
        await ctx.send(f"Nobody said `{word}` this week. Dumbass.")
thisweek.shortcut = "week"

@bot.hybrid_command(name="alltime", description="All-time usage graph of a word.")
async def alltime(ctx, *, word: str):
    word = word.lower()
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT timestamp, content FROM messages WHERE guild_id = ?", (ctx.guild.id,))
    rows = cursor.fetchall()
    usage_by_day = {}
    for timestamp, content in rows:
        if word in tokenize_text(content or "", stopwords):
            # timestamp stored in ISO; take date
            day = timestamp.split("T")[0]
            usage_by_day[day] = usage_by_day.get(day, 0) + 1
    buf = generate_usage_graph(usage_by_day, f"All-time usage of '{word}'")
    if buf:
        await ctx.send(file=discord.File(buf, filename="alltime.png"))
    else:
        await ctx.send(f"No usage of `{word}` found in all-time history.")
alltime.shortcut = "all"

@bot.hybrid_command(name="whoinvented", description="Find the first user to say a word.")
async def whoinvented(ctx, *, word: str):
    word = word.lower()
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT author_id, timestamp, content FROM messages WHERE guild_id = ? ORDER BY timestamp ASC", (ctx.guild.id,))
    rows = cursor.fetchall()
    for author_id, timestamp, content in rows:
        if word in tokenize_text(content or "", stopwords):
            user = ctx.guild.get_member(author_id)
            name = user.display_name if user else f"User {author_id}"
            await ctx.send(f"`{word}` was first said by **{name}** on `{timestamp}`. What a legend.")
            return
    await ctx.send(f"No one has said `{word}` yet. Do it yourself, coward.")
whoinvented.shortcut = "inv"

@bot.hybrid_command(name="toxicityrank", description="Shows the top toxic users or a user's most toxic words.")
@app_commands.describe(user="(Optional) See toxicity ranking for a specific user")
async def toxicityrank(ctx, user: discord.Member = None):
    if ctx.guild is None:
        return await ctx.send("This command must be used in a server.")
    cursor.execute("SELECT author_id, content FROM messages WHERE guild_id = ?", (ctx.guild.id,))
    rows = cursor.fetchall()
    
    toxicity = Counter()
    for author_id, content in rows:
        words = tokenize_text(content or "", stopwords)
        count_ = sum(1 for w in words if w in TOXIC_WORDS)
        if count_ > 0:
            toxicity[author_id] += count_
    
    if not toxicity:
        await ctx.send("This server is suspiciously wholesome.")
        return

    if user:
        user_msgs = [content for uid, content in rows if uid == user.id]
        user_words = Counter()
        for msg in user_msgs:
            words = tokenize_text(msg or "", stopwords)
            for w in words:
                if w in TOXIC_WORDS:
                    user_words[w] += 1
        if not user_words:
            await ctx.send(f"**{user.display_name}** has not said anything toxic (yet).")
            return
        sorted_users = [uid for uid, _ in toxicity.most_common()]
        rank = sorted_users.index(user.id) + 1 if user.id in sorted_users else "Unranked"
        msg = f"**☣️ Toxicity Report for {user.display_name}**\n"
        msg += f"**Rank:** {rank}\n"
        msg += "**Top 10 Toxic Words:**\n"
        for word_, count in user_words.most_common(10):
            msg += f"`{word_}` — {count} time(s)\n"
        await ctx.send(msg)
    else:
        top = toxicity.most_common(10)
        msg = "**☣️ Top 10 Most Based Users:**\n"
        for uid, count_ in top:
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            msg += f"**{name}** — {count_} toxic word(s)\n"
        await ctx.send(msg)

toxicityrank.shortcut = "based"

# --- Admin check helper ---
def is_guild_admin(ctx):
    # ctx may be Interaction or Context; both have author attribute
    author = getattr(ctx, "author", None)
    guild = getattr(ctx, "guild", None)
    if not author or not guild:
        return False
    perms = author.guild_permissions
    return perms.administrator

# --- Admin commands, purify, and cache commands (now admin-only via permissions) ---
@bot.hybrid_command(name="kill", description="Kill switch")
async def kill(ctx):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)
    global kill_switch_engaged
    kill_switch_engaged = True
    await ctx.send("☠️ Kill switch engaged. All bot activity halted.")
    await log_action("Kill switch was engaged.")
    try:
        await ctx.message.delete()
    except Exception:
        pass
kill.shortcut = "k"

@bot.hybrid_command(name="revive", description="Disengage the kill switch")
async def revive(ctx):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)
    global kill_switch_engaged
    kill_switch_engaged = False
    await ctx.send("🩺 Kill switch disengaged. Bot is operational.")
    await log_action("Kill switch disengaged.")
    try:
        await ctx.message.delete()
    except Exception:
        pass
revive.shortcut = "rv"

@bot.hybrid_command(name="purify", description="Manual start for the purify cycle")
async def purify(ctx):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)
    
    try:
        deleted = 0
        if ctx.channel.id in PURIFY_CHANNEL_IDS:
            async for msg in ctx.channel.history(limit=None, oldest_first=True):
                if msg.author == bot.user:
                    continue
                if message_has_image_attachment(msg):
                    continue
                if msg.reactions and sum(r.count for r in msg.reactions) >= 3:
                    continue
                try:
                    await msg.delete()
                    deleted += 1
                except discord.HTTPException as e:
                    await log_action(f"Failed to delete message in purify: {e}")
                    await asyncio.sleep(1)
            await ctx.send(f"🧼 Purified {deleted} messages.", delete_after=5)
        else:
            await ctx.send("❌ This channel is not marked for purification.", delete_after=5)
    except Exception as e:
        await log_action(f"Error in purify: {e}")

    try:
        await ctx.message.delete()
    except Exception:
        pass

purify.shortcut = "pure"

@bot.hybrid_command(name="startpurify", description="Begin the auto-purify cycle")
async def startpurify(ctx):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)
    global auto_purify_enabled
    if not auto_purify.is_running():
        auto_purify.start()
        auto_purify_enabled = True
        await log_action("Auto purify started.")
        await ctx.send("🔁 Auto purify is now running.", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass
startpurify.shortcut = "startp"

@bot.hybrid_command(name="stoppurify", description="Stop the auto-purify cycle")
async def stoppurify(ctx):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)
    global auto_purify_enabled
    if auto_purify.is_running():
        auto_purify.cancel()
        auto_purify_enabled = False
        await log_action("Auto purify stopped.")
        await ctx.send("⛔ Auto purify has been stopped.", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass
stoppurify.shortcut = "stopp"

@bot.hybrid_command(name="startstalk", description="Stalk a user through time and space")
async def startstalk(ctx, target: discord.Member):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)
    stalked_user_ids.add(target.id)
    await log_action(f"Started stalking {target.display_name}.")
    await ctx.send(f"👀 Now stalking {target.display_name}", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass
startstalk.shortcut = "stalk"

@bot.hybrid_command(name="stopstalk", description="Release your target, they've suffered enough")
async def stopstalk(ctx, target: discord.Member):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)
    stalked_user_ids.discard(target.id)
    await log_action(f"Stopped stalking {target.display_name}.")
    await ctx.send(f"🚫 No longer stalking {target.display_name}", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass
stopstalk.shortcut = "unstalk"
        
@bot.hybrid_command(name="initcache", description="Deep crawl to cache ALL messages in server history (fresh).")
async def initcache(ctx):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)

    await ctx.defer(ephemeral=False)
    await ctx.channel.send("🧠 Starting full deep cache of ALL server messages. This may take a while...")

    total_cached = 0
    progress_update_interval = 1000
    batch = []

    for channel in ctx.guild.text_channels:
        try:
            if not channel.permissions_for(ctx.guild.me).read_message_history:
                print(f"[SKIP] No permission to read {channel.name}")
                continue

            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.bot or message.webhook_id is not None:
                    continue

                batch.append((
                    message.id,
                    message.channel.id,
                    message.author.id,
                    message.content or "",
                    message.created_at.isoformat(),
                    ctx.guild.id
                ))
                total_cached += 1

                if len(batch) >= 500:
                    cursor.executemany(
                        "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                        batch
                    )
                    db.commit()
                    batch.clear()

                if total_cached % progress_update_interval == 0:
                    await ctx.channel.send(f"📊 Cached {total_cached} messages so far...")

            # Flush leftover for this channel
            if batch:
                cursor.executemany(
                    "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                    batch
                )
                db.commit()
                batch.clear()

        except Exception as e:
            print(f"[ERROR] Failed to cache channel {channel.name}: {e}")

    await ctx.channel.send(f"✅ Deep cache complete. Cached {total_cached} messages total.")

@bot.hybrid_command(name="uwulock", description="heh.")
async def uwulock(ctx, member: discord.Member):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)

    if member.id in uwulocked_user_ids:
        await ctx.send(f"🔒 **{member.display_name}** is already uwulocked.")
    else:
        uwulocked_user_ids.add(member.id)
        await ctx.send(f"💖 **{member.display_name}** is now uwulocked. Prepare for suffering.")
uwulock.shortcut = "uwu"

@bot.hybrid_command(name="unlock", description="Lift the curse.")
async def unlock(ctx, member: discord.Member):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)

    if member.id in uwulocked_user_ids:
        uwulocked_user_ids.remove(member.id)
        await ctx.send(f"🔓 **{member.display_name}** has been released from their torment.")
    else:
        await ctx.send(f"😇 **{member.display_name}** was not uwulocked.")
unlock.shortcut = "unuwu"

@bot.hybrid_command(
    name="kms",
    description="Post a random KMS media for fun."
)
@app_commands.describe()
async def kms(ctx):
    if not kms_media_list:
        return await ctx.send("❌ No KMS media loaded.")

    url = random.choice(kms_media_list)

    # Attempt to fetch and post the media as a file (not just a link)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return await ctx.send("❌ Failed to fetch media.")
                data = await resp.read()
                filename = url.split("/")[-1]
                await ctx.send(file=discord.File(BytesIO(data), filename=filename))
    except Exception as e:
        await ctx.send(f"⚠️ Error posting media: {e}")

# Add all shortcuts
kms.shortcut = "killme"
bot.add_command(commands.HybridCommand(kms, name="suicide"))
bot.add_command(commands.HybridCommand(kms, name="hahahwhatif"))
bot.add_command(commands.HybridCommand(kms, name="bruhimmakms"))
bot.add_command(commands.HybridCommand(kms, name="welp"))

@bot.hybrid_command(
    name="verifycache",
    description="Verify cached message counts vs Discord history for this guild. (Admin only)"
)
@app_commands.describe(
    find_missing="Set to true to sample messages and reveal some missing cached messages (slow).",
    sample_per_channel="How many sample messages to check per channel when finding missing messages."
)
async def verifycache(ctx, find_missing: bool = False, sample_per_channel: int = 5):
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=5)

    await ctx.defer(ephemeral=False)
    guild = ctx.guild
    if guild is None:
        return await ctx.send("This command must be run in a guild (server).")

    results = []
    total_discord = 0
    total_db = 0
    channels_checked = 0
    progress_interval = 5
    long_report_lines = []

    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).read_message_history:
            results.append(f"#{channel.name}: SKIPPED (no read_message_history permission)")
            continue

        channels_checked += 1
        discord_count = 0
        sample_messages = []
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.bot or message.webhook_id is not None:
                    continue
                discord_count += 1
                if find_missing and len(sample_messages) < sample_per_channel:
                    sample_messages.append((message.id, message.author.display_name, message.created_at.isoformat(), (message.content or "")[:300]))
        except Exception as e:
            results.append(f"#{channel.name}: ERROR while reading history: {e}")
            continue

        cursor.execute("SELECT COUNT(*) FROM messages WHERE channel_id = ? AND guild_id = ?", (channel.id, guild.id))
        try:
            db_count = cursor.fetchone()[0]
        except Exception:
            db_count = 0

        total_discord += discord_count
        total_db += db_count

        missing = max(0, discord_count - db_count)
        pct_cached = (db_count / discord_count * 100) if discord_count > 0 else 100.0
        results.append(f"#{channel.name}: Discord={discord_count:,}  DB={db_count:,}  Missing={missing:,}  Cached={pct_cached:.1f}%")

        if find_missing and sample_messages:
            for mid, author_name, ts, content_snip in sample_messages:
                cursor.execute("SELECT 1 FROM messages WHERE message_id = ? AND guild_id = ? LIMIT 1", (mid, guild.id))
                exists = cursor.fetchone() is not None
                if not exists:
                    long_report_lines.append(f"Missing in DB — channel=#{channel.name} author={author_name} ts={ts} msg_id={mid} content_snip={repr(content_snip)[:200]}")

        if channels_checked % progress_interval == 0:
            try:
                await ctx.channel.send(f"🔁 Progress: checked {channels_checked} channels so far...")
            except Exception:
                pass

    summary = [
        f"✅ Verify cache complete for **{guild.name}**",
        f"Channels checked: {channels_checked}",
        f"Total Discord messages (non-bot): {total_discord:,}",
        f"Total cached in DB: {total_db:,}",
        f"Overall cached: {(total_db/total_discord*100) if total_discord>0 else 100.0:.1f}%"
    ]
    body_lines = summary + [""] + ["Per-channel summary:"] + results
    if len(body_lines) > 200 or len("\n".join(results)) > 1500 or (find_missing and long_report_lines):
        report_text = "\n".join(body_lines)
        if find_missing and long_report_lines:
            report_text += "\n\nSAMPLED MISSING MESSAGES:\n" + "\n".join(long_report_lines)
        buffer = BytesIO(report_text.encode("utf-8"))
        buffer.seek(0)
        await ctx.send(file=discord.File(fp=buffer, filename=f"verifycache_{guild.id}.txt"))
    else:
        await ctx.send("```\n" + "\n".join(body_lines + ([""] + long_report_lines if long_report_lines else [])) + "\n```")

    try:
        await ctx.message.delete()
    except Exception:
        pass

# --- Retroactive migration command: backfill guild_id for rows where NULL ---
@bot.hybrid_command(
    name="backfill_guildids",
    description="Retroactively assign guild_id for cached messages where missing. Admin only."
)
@app_commands.describe(
    confirm="Set to true to actually perform the update. If false, the command will show what would be changed.",
    fetch_unresolved="If true, attempt to fetch unresolved channel IDs from the Discord API (may be slow / rate-limited)."
)
async def backfill_guildids(ctx, confirm: bool = False, fetch_unresolved: bool = True):
    """
    Maps messages rows with guild_id IS NULL by using bot.get_channel(channel_id)
    or, optionally, bot.fetch_channel(channel_id) for distinct channel_id present in the DB with NULL guild_id.
    - If confirm is False: reports counts and which channel_ids are mappable.
    - If confirm is True: performs UPDATE ... WHERE guild_id IS NULL AND channel_id = ?
    Note: only channels the bot currently sees or can fetch will be backfilled.
    """
    if not is_guild_admin(ctx):
        return await ctx.send("❌ You must be a server administrator to use this command.", delete_after=10)

    await ctx.defer(ephemeral=False)

    cursor.execute("SELECT COUNT(*) FROM messages WHERE guild_id IS NULL")
    total_null = cursor.fetchone()[0]
    if total_null == 0:
        return await ctx.send("✅ No messages with NULL guild_id found. Nothing to backfill.")

    # Get distinct channel IDs with missing guild_id
    cursor.execute("SELECT DISTINCT channel_id FROM messages WHERE guild_id IS NULL")
    rows = cursor.fetchall()
    channel_ids = [r[0] for r in rows if r and r[0] is not None]

    mappable = []
    unmappable = []
    total_mappable_rows = 0

    # First pass: try to resolve from cache via bot.get_channel
    for cid in channel_ids:
        channel = bot.get_channel(cid)
        if channel and getattr(channel, "guild", None):
            guild_obj = channel.guild
            count = cursor.execute("SELECT COUNT(*) FROM messages WHERE guild_id IS NULL AND channel_id = ?", (cid,)).fetchone()[0]
            if count > 0:
                mappable.append((cid, guild_obj.id, guild_obj.name, channel.name, count))
                total_mappable_rows += count
        else:
            unmappable.append(cid)

    fetched_mappable = []
    fetch_errors = []
    # Optionally attempt to fetch unresolved channels via API (for channels not in cache)
    if unmappable and fetch_unresolved:
        fetch_limit = 50  # safety limit to avoid extremely long runs; adjust if needed
        fetched_attempts = 0
        for cid in list(unmappable):  # iterate on a copy since we may modify unmappable
            if fetched_attempts >= fetch_limit:
                break
            try:
                # Attempt to fetch the channel from the API
                ch = await bot.fetch_channel(cid)
                if ch and getattr(ch, "guild", None):
                    guild_obj = ch.guild
                    count = cursor.execute("SELECT COUNT(*) FROM messages WHERE guild_id IS NULL AND channel_id = ?", (cid,)).fetchone()[0]
                    if count > 0:
                        fetched_mappable.append((cid, guild_obj.id, guild_obj.name, getattr(ch, "name", "unknown"), count))
                else:
                    fetch_errors.append((cid, "no guild info"))
                # If fetch succeeded remove from unmappable
                if cid in unmappable:
                    unmappable.remove(cid)
                fetched_attempts += 1
            except discord.NotFound:
                fetch_errors.append((cid, "NotFound"))
            except discord.Forbidden:
                fetch_errors.append((cid, "Forbidden"))
            except discord.HTTPException as e:
                fetch_errors.append((cid, f"HTTPException: {e}"))
            except Exception as e:
                fetch_errors.append((cid, f"Other: {e}"))
            # be gentle with the API
            await asyncio.sleep(0.2)

    # accumulate totals including fetched
    for cid, gid, gname, cname, cnt in fetched_mappable:
        mappable.append((cid, gid, gname, cname, cnt))
        total_mappable_rows += cnt

    # Build report
    report_lines = [
        f"Total rows with NULL guild_id: {total_null}",
        f"Distinct channel_ids with NULL guild_id: {len(channel_ids)}",
        f"Channels that can be backfilled (bot can see or fetch): {len(mappable)} covering {total_mappable_rows} rows",
        f"Channels that cannot be resolved by the bot (not visible): {len(unmappable)}",
        ""
    ]

    for cid, gid, gname, cname, cnt in mappable:
        report_lines.append(f"- channel_id={cid}  guild_id={gid} ({gname})  channel_name=#{cname}  rows={cnt}")

    if unmappable:
        report_lines.append("")
        report_lines.append("Unmappable channel IDs (bot cannot resolve these):")
        report_lines.extend([f"- {cid}" for cid in unmappable[:25]])
        if len(unmappable) > 25:
            report_lines.append(f"... and {len(unmappable)-25} more")

    if fetch_errors:
        report_lines.append("")
        report_lines.append("Fetch attempts and errors (for unresolved channels):")
        for cid, err in fetch_errors[:50]:
            report_lines.append(f"- {cid}: {err}")
        if len(fetch_errors) > 50:
            report_lines.append(f"... and {len(fetch_errors)-50} more")

    if not confirm:
        report_lines.append("")
        report_lines.append("No changes were made. Re-run the command with confirm=True to apply the updates.")
        report_text = "\n".join(report_lines)
        if len(report_text) > 1500:
            buf = BytesIO(report_text.encode("utf-8"))
            buf.seek(0)
            await ctx.send(file=discord.File(fp=buf, filename=f"backfill_preview_{ctx.guild.id if ctx.guild else 'global'}.txt"))
        else:
            await ctx.send("```\n" + report_text + "\n```")
        try:
            await ctx.message.delete()
        except Exception:
            pass
        return

    # Confirm is True: perform updates for all mappable (including fetched)
    updated_total = 0
    updated_channels = 0
    for cid, gid, gname, cname, cnt in mappable:
        try:
            cursor.execute("UPDATE messages SET guild_id = ? WHERE guild_id IS NULL AND channel_id = ?", (gid, cid))
            updated = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else cnt  # best-effort
            if updated > 0:
                updated_total += updated
                updated_channels += 1
        except Exception as e:
            report_lines.append(f"Error updating channel_id {cid}: {e}")

    db.commit()
    remaining_null = cursor.execute("SELECT COUNT(*) FROM messages WHERE guild_id IS NULL").fetchone()[0]

    report_lines.append("")
    report_lines.append(f"Applied updates to {updated_channels} channels, {updated_total} rows updated.")
    report_lines.append(f"Remaining rows with NULL guild_id: {remaining_null}")
    report_text = "\n".join(report_lines)
    if len(report_text) > 1500:
        buf = BytesIO(report_text.encode("utf-8"))
        buf.seek(0)
        await ctx.send(file=discord.File(fp=buf, filename=f"backfill_result_{ctx.guild.id if ctx.guild else 'global'}.txt"))
    else:
        await ctx.send("```\n" + report_text + "\n```")

    try:
        await ctx.message.delete()
    except Exception:
        pass

def bootstrap():
    init_gif_db()

bootstrap()

# --- Remaining run ---
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token.strip() == "" or token.strip().lower() == "none":
        print("❌ DISCORD_TOKEN environment variable is not set.")
    else:
        bot.run(token)
