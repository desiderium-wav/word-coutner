import discord
from discord.ext import commands, tasks
import sqlite3
import os
import datetime
import string
from collections import Counter
from io import BytesIO
import matplotlib.pyplot as plt
import re
from discord import app_commands
import uwuipy
import asyncio

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

# Safely parse env variables (avoid ValueError on empty string)
log_channel_id = None
_raw_log_id = os.getenv("LOG_CHANNEL_ID")
if _raw_log_id and _raw_log_id.isdigit():
    log_channel_id = int(_raw_log_id)

raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip().isdigit()}
raw_role_ids = os.getenv("ALLOWED_ROLE_IDS", "")
ALLOWED_ROLE_IDS = {int(rid.strip()) for rid in raw_role_ids.split(",") if rid.strip().isdigit()}
raw_channel_ids = os.getenv("PURIFY_CHANNEL_IDS", "")
PURIFY_CHANNEL_IDS = {int(cid.strip()) for cid in raw_channel_ids.split(",") if cid.strip().isdigit()}

TOXIC_WORDS = set()
if os.path.exists("badwords_en.txt"):
    with open("badwords_en.txt", "r", encoding="utf-8") as f:
        TOXIC_WORDS = set(line.strip().lower() for line in f if line.strip())

def is_allowed(ctx):
    return ctx.author.id in ALLOWED_USER_IDS

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
@tasks.loop(minutes=120)
async def auto_purify():
    # Iterate configured channel ids rather than scanning all guild channels to avoid permission problems
    for cid in PURIFY_CHANNEL_IDS:
        channel = bot.get_channel(cid)
        if not channel or not isinstance(channel, discord.TextChannel):
            continue
        try:
            # We walk the history and delete non-image messages that don't have >=3 reactions
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
                    # If rate-limited or forbidden, log and break out to avoid hammering
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
                # fetch recent N messages (batched) to keep DB fairly fresh
                # use relatively small chunk to avoid long blocking calls
                async for message in channel.history(limit=500, oldest_first=False):
                    # skip bots and webhooks and DMs
                    if message.author.bot or message.webhook_id is not None or message.guild is None:
                        continue
                    # insert with guild_id
                    try:
                        cursor.execute(
                            "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (message.id, message.channel.id, message.author.id, message.content or "", message.created_at.isoformat(), message.guild.id)
                        )
                    except Exception:
                        # some messages might have unsupported characters; fallback to repr of content
                        cursor.execute(
                            "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (message.id, message.channel.id, message.author.id, (message.content or "").encode("utf-8", errors="replace").decode("utf-8"), message.created_at.isoformat(), message.guild.id)
                        )
                db.commit()
                await asyncio.sleep(0)  # yield
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
                # Skip bot commands/special messages if you want (retained previous behavior)
                if message.content.startswith(('s ', '/')):
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

@bot.event
async def on_message(message):
    if message.author.bot or message.webhook_id is not None:
        return

    if message.guild is None:
        return

    # Insert the message into DB (light-weight, ignores duplicates)
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp, guild_id) VALUES (?, ?, ?, ?, ?, ?)",
            (message.id, message.channel.id, message.author.id, message.content or "", message.created_at.isoformat(), message.guild.id)
        )
        db.commit()
    except Exception:
        pass

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
  
    if message.author.id in stalked_user_ids:
        try:
            await message.delete()
            await log_action(f"Deleted message from stalked user: {message.author.display_name}")
        except Exception as e:
            await log_action(f"Failed to delete stalked user message: {e}")
        return 

    if message.content.lower().startswith("s "):
        parts = message.content[2:].split()
        if not parts:
            return
        shortcut = parts[0].lower()
        args = parts[1:]
        if shortcut in SHORTCUTS:
            command = bot.get_command(SHORTCUTS[shortcut])
            if command:
                ctx = await bot.get_context(message)
                await ctx.invoke(command, *args)
                return

    await bot.process_commands(message)

# --- Counting & analysis commands (now guild-scoped) ---
@bot.hybrid_command(name="count", description="Count how often a word was said in the server.")
async def count(ctx, *, word: str):
    word = word.lower()
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
    cursor.execute("SELECT content FROM messages WHERE author_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
    messages = cursor.fetchall()
    count_ = sum(tokenize_text(msg[0] or "", stopwords).count(word) for msg in messages)
    await ctx.send(f"**{member.display_name}** has said `{word}` **{count_}** time(s). What a bitch.")
usercount.shortcut = "uc"

@bot.hybrid_command(name="top10", description="Show top 10 most used words in the server.")
async def top10(ctx):
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

# --- Admin commands, purify, and cache commands ---
@bot.hybrid_command(name="kill", description="Kill switch")
async def kill(ctx):
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
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
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
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
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    
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
        await log_action(f"Error in !purify: {e}")

    try:
        await ctx.message.delete()
    except Exception:
        pass

purify.shortcut = "pure"

@bot.hybrid_command(name="startpurify", description="Begin the auto-purify cycle")
async def startpurify(ctx):
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
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
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
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
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
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
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
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
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)

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
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)

    if member.id in uwulocked_user_ids:
        await ctx.send(f"🔒 **{member.display_name}** is already uwulocked.")
    else:
        uwulocked_user_ids.add(member.id)
        await ctx.send(f"💖 **{member.display_name}** is now uwulocked. Prepare for suffering.")
uwulock.shortcut = "uwu"

@bot.hybrid_command(name="unlock", description="Lift the curse.")
async def unlock(ctx, member: discord.Member):
    if not (
        any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles) or 
        ctx.author.id in ALLOWED_USER_IDS
    ):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)

    if member.id in uwulocked_user_ids:
        uwulocked_user_ids.remove(member.id)
        await ctx.send(f"🔓 **{member.display_name}** has been released from their torment.")
    else:
        await ctx.send(f"😇 **{member.display_name}** was not uwulocked.")
unlock.shortcut = "unuwu"

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token.strip() == "" or token.strip().lower() == "none":
        print("❌ DISCORD_TOKEN environment variable is not set.")
    else:
        bot.run(token)
