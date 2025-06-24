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

def load_stopwords(path="stopwords.txt"):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return {line.strip().lower() for line in file if line.strip()}
    except FileNotFoundError:
        print("⚠️ stopwords.txt not found. No stopwords loaded.")
        return set()

def tokenize_text(text):
    text = re.sub(r"(https?://\S+|www\.\S+)", "", text)
    text = re.sub(r"@[\w_]+", "", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"[’']", "", text)
    return re.findall(r"\b[a-zA-Z]{2,}\b", text.lower())

STOPWORDS = load_stopwords()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="s ", intents=intents)

kill_switch_engaged = False
auto_purify_enabled = False
stalked_user_ids = set()

log_channel_id = int(os.getenv("LOG_CHANNEL_ID", "0"))
raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip().isdigit()}

conn = sqlite3.connect("message_cache.db")
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    author_id INTEGER,
    content TEXT,
    timestamp TEXT
)
''')
conn.commit()

# Toxic word list (auto-load if available)
TOXIC_WORDS = set()
if os.path.exists("badwords_en.txt"):
    with open("badwords_en.txt", "r", encoding="utf-8") as f:
        TOXIC_WORDS = set(line.strip().lower() for line in f if line.strip())

def is_allowed(ctx):
    return ctx.author.id in ALLOWED_USER_IDS

# Shortcut registration
SHORTCUTS = {}
def register_shortcuts():
    for command in bot.commands:
        if hasattr(command, "shortcut"):
            SHORTCUTS[command.shortcut] = command.name

# Log setup
async def log_action(message):
    log_channel = bot.get_channel(log_channel_id)
    if log_channel:
        await log_channel.send(message)
    print(f"[LOG] {message}")

@tasks.loop(minutes=120)
async def auto_purify():
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if "naked" in channel.name.lower():
                try:
                    messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
                    for msg in messages:
                        if not msg.attachments and msg.author != bot.user:
                            if msg.reactions and sum([r.count for r in msg.reactions]) >= 3:
                                continue
                            await msg.delete()
                            await log_action(f"Auto-deleted message from {msg.author.display_name} in #{channel.name}")
                except Exception as e:
                    await log_action(f"Error in auto-purify for #{channel.name}: {e}")

# Background cache loop
@tasks.loop(minutes=5)
async def background_cache():
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                async for message in channel.history(limit=100, oldest_first=False):
                    if message.author.bot:
                        continue
                    cursor.execute(
                        "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                        (message.id, message.channel.id, message.author.id, message.content, message.created_at.isoformat())
                    )
            except Exception as e:
                print(f"[ERROR] background_cache failed in {channel.name}: {e}")
    db.commit()

async def cache_channel_history(guild):
    for channel in guild.text_channels:
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.bot:
                    continue
                if message.content.startswith(('s ', '/')):
                    continue
                cursor.execute("SELECT 1 FROM messages WHERE message_id = ?", (message.id,))
                if cursor.fetchone():
                    continue
                cursor.execute(
                    "INSERT INTO messages (message_id, channel_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (message.id, channel.id, message.author.id, message.content, str(message.created_at))
                )
                conn.commit()
        except Exception:
            pass  # Silent fail

# Word usage graph helper
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

# On ready: sync slash + register shortcuts
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

# Shortcut command handling
@bot.event
async def on_message(message):
    if message.author.bot:
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

# --- HYBRID COMMANDS BELOW ---

@bot.hybrid_command(name="count", description="Count how often a word was said in the server.")
async def count(ctx, *, word: str):
    word = word.lower()
    cursor.execute("SELECT author_id, content FROM messages")
    rows = cursor.fetchall()
    total = 0
    user_counts = Counter()
    for author_id, content in rows:
        count_ = content.lower().split().count(word)
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
    cursor.execute("SELECT content FROM messages WHERE author_id = ?", (member.id,))
    messages = cursor.fetchall()
    count_ = sum(msg[0].lower().split().count(word) for msg in messages)
    await ctx.send(f"**{member.display_name}** has said `{word}` **{count_}** time(s). What a bitch.")
usercount.shortcut = "uc"

@bot.hybrid_command(name="top10", description="Show top 10 most used words in the server.")
async def top10(ctx):
    cursor.execute("SELECT content FROM messages")
    rows = cursor.fetchall()
    word_counter = Counter()
    for (content,) in rows:
        words = content.lower().translate(str.maketrans('', '', string.punctuation)).split()
        for word in words:
            if word and word not in STOPWORDS:
                word_counter[word] += 1
    top = word_counter.most_common(10)
    msg = "**📊 Top 10 Most Used Words in this Godforsaken Place (Filtered):**\n" + "\n".join([f"`{w}` — {c} time(s)" for w, c in top])
    await ctx.send(msg)
top10.shortcut = "top"

@bot.hybrid_command(name="mylist", description="Show your personal top 10 most used words.")
async def mylist(ctx):
    user_id = ctx.author.id
    cursor.execute("SELECT content FROM messages WHERE author_id = ?", (user_id,))
    rows = cursor.fetchall()
    word_counter = Counter()
    translator = str.maketrans('', '', string.punctuation)
    for (content,) in rows:
        cleaned = content.translate(translator).lower().split()
        for word in cleaned:
            if word and word not in STOPWORDS:
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
    cursor.execute("SELECT timestamp, content FROM messages")
    rows = cursor.fetchall()
    today = datetime.datetime.utcnow().date()
    usage_by_hour = {}
    for timestamp, content in rows:
        ts = datetime.datetime.fromisoformat(timestamp)
        if ts.date() != today:
            continue
        if word in content.lower().split():
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
    cursor.execute("SELECT timestamp, content FROM messages")
    rows = cursor.fetchall()
    today = datetime.datetime.utcnow().date()
    usage_by_day = {}
    for timestamp, content in rows:
        ts = datetime.datetime.fromisoformat(timestamp)
        if (today - ts.date()).days > 6:
            continue
        if word in content.lower().split():
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
    cursor.execute("SELECT timestamp, content FROM messages")
    rows = cursor.fetchall()
    usage_by_day = {}
    for timestamp, content in rows:
        if word in content.lower().split():
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
    cursor.execute("SELECT author_id, timestamp, content FROM messages ORDER BY timestamp ASC")
    rows = cursor.fetchall()
    for author_id, timestamp, content in rows:
        if word in content.lower().split():
            user = ctx.guild.get_member(author_id)
            name = user.display_name if user else f"User {author_id}"
            await ctx.send(f"`{word}` was first said by **{name}** on `{timestamp}`. What a legend.")
            return
    await ctx.send(f"No one has said `{word}` yet. Do it yourself, coward.")
whoinvented.shortcut = "inv"

@bot.hybrid_command(name="toxicityrank", description="Rank users by their use of toxic language.")
async def toxicityrank(ctx):
    cursor.execute("SELECT author_id, content FROM messages")
    rows = cursor.fetchall()
    toxicity = Counter()
    for author_id, content in rows:
        words = content.lower().translate(str.maketrans('', '', string.punctuation)).split()
        count_ = sum(1 for w in words if w in TOXIC_WORDS)
        if count_ > 0:
            toxicity[author_id] += count_
    if not toxicity:
        await ctx.send("This server is suspiciously wholesome.")
        return
    top = toxicity.most_common(10)
    msg = "**☣️ Top 10 Most Based Users:**\n"
    for uid, count_ in top:
        user = ctx.guild.get_member(uid)
        name = user.display_name if user else f"User {uid}"
        msg += f"**{name}** — {count_} toxic word(s)\n"
    await ctx.send(msg)
toxicityrank.shortcut = "based"

@bot.hybrid_command(name="kill", description="Kill switch")
async def kill(ctx):
    if not is_allowed(ctx):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    global kill_switch_engaged
    kill_switch_engaged = True
    await ctx.send("☠️ Kill switch engaged. All bot activity halted.")
    await log_action("Kill switch was engaged.")
    await ctx.message.delete()
kill.shortcut = "k"

@bot.hybrid_command(name="revive", description="Disengage the kill switch")
async def revive(ctx):
    if not is_allowed(ctx):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    global kill_switch_engaged
    kill_switch_engaged = False
    await ctx.send("🩺 Kill switch disengaged. Bot is operational.")
    await log_action("Kill switch disengaged.")
    await ctx.message.delete()
revive.shortcut = "rv"

@bot.hybrid_command(name="purify", description="Manual start for the purify cycle")
async def purify(ctx):
    if not is_allowed(ctx):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    try:
        deleted = 0
        async for msg in ctx.channel.history(limit=None, oldest_first=True):
            if not msg.attachments and msg.author != bot.user:
                if msg.reactions and sum([r.count for r in msg.reactions]) >= 3:
                    continue
                await msg.delete()
                deleted += 1
        await ctx.send(f"🧼 Purified {deleted} messages.", delete_after=5)
    except Exception as e:
        await log_action(f"Error in !purify: {e}")
    await ctx.message.delete()
purify.shortcut = "pure"

@bot.hybrid_command(name="startpurify", description="Begin the auto-purify cycle")
async def startpurify(ctx):
    if not is_allowed(ctx):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    global auto_purify_enabled
    if not auto_purify.is_running():
        auto_purify.start()
        auto_purify_enabled = True
        await log_action("Auto purify started.")
        await ctx.send("🔁 Auto purify is now running.", delete_after=5)
    await ctx.message.delete()
startpurify.shortcut = "startp"

@bot.hybrid_command(name="stoppurify", description="Stop the auto-purify cycle")
async def stoppurify(ctx):
    if not is_allowed(ctx):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    global auto_purify_enabled
    if auto_purify.is_running():
        auto_purify.cancel()
        auto_purify_enabled = False
        await log_action("Auto purify stopped.")
        await ctx.send("⛔ Auto purify has been stopped.", delete_after=5)
    await ctx.message.delete()
stoppurify.shortcut = "stopp"

@bot.hybrid_command(name="startstalk", description="Stalk a user through time and space")
@commands.has_permissions(administrator=True)
async def startstalk(ctx, target: discord.Member):
    if not (is_allowed(ctx) or ctx.author.guild_permissions.administrator):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    stalked_user_ids.add(target.id)
    await log_action(f"Started stalking {target.display_name}.")
    await ctx.send(f"👀 Now stalking {target.display_name}", delete_after=5)
    await ctx.message.delete()
startstalk.shortcut = "stalk"

@bot.hybrid_command(name="stopstalk", description="Release your target, they've suffered enough")
@commands.has_permissions(administrator=True)
async def stopstalk(ctx, target: discord.Member):
    if not (is_allowed(ctx) or ctx.author.guild_permissions.administrator):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    stalked_user_ids.discard(target.id)
    await log_action(f"Stopped stalking {target.display_name}.")
    await ctx.send(f"🚫 No longer stalking {target.display_name}", delete_after=5)
    await ctx.message.delete()
stopstalk.shortcut = "unstalk"
        
@bot.hybrid_command(name="initcache", description="One-time deep crawl to cache all messages in server history.")
@commands.has_permissions(administrator=True)
async def initcache(ctx):
    if not (is_allowed(ctx) or ctx.author.guild_permissions.administrator):
        return await ctx.send("❌ You don't have permission to use this command.", delete_after=5)
    await ctx.send("🧠 Starting deep cache of all server messages. This may take a while...")
    for channel in ctx.guild.text_channels:
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.bot:
                    continue
                cursor.execute(
                    "INSERT OR IGNORE INTO messages (message_id, channel_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (message.id, message.channel.id, message.author.id, message.content, message.created_at.isoformat())
                )
        except Exception as e:
            print(f"[ERROR] Failed to cache channel {channel.name}: {e}")
    db.commit()
    await ctx.send("✅ Deep cache complete.")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token.strip() == "" or token.strip().lower() == "none":
        print("❌ DISCORD_TOKEN environment variable is not set.")
    else:
        bot.run(token)
