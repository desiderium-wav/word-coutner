import discord
from discord.ext import commands, tasks
import sqlite3
import os
import datetime
import string
from collections import Counter
from io import BytesIO
import matplotlib.pyplot as plt

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="s ", intents=intents)

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

# Shortcut registration
SHORTCUTS = {}
def register_shortcuts():
    for command in bot.commands:
        if hasattr(command, "shortcut"):
            SHORTCUTS[command.shortcut] = command.name

# Background cache loop
@tasks.loop(minutes=5)
async def background_cache():
    for guild in bot.guilds:
        await cache_channel_history(guild)

async def cache_channel_history(guild):
    for channel in guild.text_channels:
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if message.author.bot:
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

@bot.hybrid_command(name="wordcount", description="Count how often a word was said in the server.")
async def wordcount(ctx, *, word: str):
    word = word.lower()
    cursor.execute("SELECT author_id, content FROM messages")
    rows = cursor.fetchall()
    total = 0
    user_counts = Counter()
    for author_id, content in rows:
        count = content.lower().split().count(word)
        if count > 0:
            user_counts[author_id] += count
            total += count
    if total == 0:
        await ctx.send(f"No cached messages contain `{word}`.")
        return
    top_users = user_counts.most_common(10)
    result_lines = []
    for uid, count in top_users:
        user = ctx.guild.get_member(uid)
        name = user.display_name if user else f"User {uid}"
        result_lines.append(f"**{name}** — {count} time(s)")
    await ctx.send(f"**📊 Word Count for `{word}`:**\n🔢 Total Mentions: `{total}`\n\n🏆 **Top 10 Users:**\n" + "\n".join(result_lines))
wordcount.shortcut = "wc"

@bot.hybrid_command(name="userword", description="See how often a user said a word.")
async def userword(ctx, word: str, member: discord.Member):
    word = word.lower()
    cursor.execute("SELECT content FROM messages WHERE author_id = ?", (member.id,))
    messages = cursor.fetchall()
    count = sum(msg[0].lower().split().count(word) for msg in messages)
    await ctx.send(f"**{member.display_name}** has said `{word}` **{count}** time(s).")
userword.shortcut = "uw"

@bot.hybrid_command(name="topwords", description="Show top 10 most used words in the server.")
async def topwords(ctx):
    cursor.execute("SELECT content FROM messages")
    rows = cursor.fetchall()
    word_counter = Counter()
    for (content,) in rows:
        for word in content.lower().translate(str.maketrans('', '', string.punctuation)).split():
            if word:
                word_counter[word] += 1
    top = word_counter.most_common(10)
    msg = "**📊 Server-Wide Top 10 Words:**\n" + "\n".join([f"`{w}` — {c} time(s)" for w, c in top])
    await ctx.send(msg)
topwords.shortcut = "top"

@bot.hybrid_command(name="mytopwords", description="Show your personal top 10 most used words.")
async def mytopwords(ctx):
    user_id = ctx.author.id
    cursor.execute("SELECT content FROM messages WHERE author_id = ?", (user_id,))
    rows = cursor.fetchall()
    word_counter = Counter()
    translator = str.maketrans('', '', string.punctuation)
    for (content,) in rows:
        cleaned = content.translate(translator).lower().split()
        for word in cleaned:
            if word:
                word_counter[word] += 1
    if not word_counter:
        await ctx.send("You haven't said anything worth counting yet.")
        return
    top_words = word_counter.most_common(10)
    result_lines = [f"`{word}` — {count} time(s)" for word, count in top_words]
    await ctx.send("**🧠 Your Top 10 Most Used Words:**\n" + "\n".join(result_lines))
mytopwords.shortcut = "mt"

@bot.hybrid_command(name="wordusage_day", description="Hourly usage graph of a word (today).")
async def wordusage_day(ctx, *, word: str):
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
    buf = generate_usage_graph(usage_by_hour, f"Hourly usage of '{word}' today")
    if buf:
        await ctx.send(file=discord.File(buf, filename="wordusage_day.png"))
    else:
        await ctx.send(f"No usage of `{word}` found today.")
wordusage_day.shortcut = "wd"

@bot.hybrid_command(name="wordusage_week", description="Daily usage graph (last 7 days).")
async def wordusage_week(ctx, *, word: str):
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
    buf = generate_usage_graph(usage_by_day, f"Daily usage of '{word}' (last 7 days)")
    if buf:
        await ctx.send(file=discord.File(buf, filename="wordusage_week.png"))
    else:
        await ctx.send(f"No usage of `{word}` found this week.")
wordusage_week.shortcut = "ww"

@bot.hybrid_command(name="wordusage_all", description="All-time usage graph of a word.")
async def wordusage_all(ctx, *, word: str):
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
        await ctx.send(file=discord.File(buf, filename="wordusage_all.png"))
    else:
        await ctx.send(f"No usage of `{word}` found in all-time history.")
wordusage_all.shortcut = "wa"

@bot.hybrid_command(name="whoinvented", description="Find the first user to say a word.")
async def whoinvented(ctx, *, word: str):
    word = word.lower()
    cursor.execute("SELECT author_id, timestamp, content FROM messages ORDER BY timestamp ASC")
    rows = cursor.fetchall()
    for author_id, timestamp, content in rows:
        if word in content.lower().split():
            user = ctx.guild.get_member(author_id)
            name = user.display_name if user else f"User {author_id}"
            await ctx.send(f"`{word}` was first said by **{name}** on `{timestamp}`.")
            return
    await ctx.send(f"No one has said `{word}` yet.")
whoinvented.shortcut = "inv"

@bot.hybrid_command(name="toxicrank", description="Rank users by their use of toxic language.")
async def toxicrank(ctx):
    cursor.execute("SELECT author_id, content FROM messages")
    rows = cursor.fetchall()
    toxicity = Counter()
    for author_id, content in rows:
        words = content.lower().translate(str.maketrans('', '', string.punctuation)).split()
        count = sum(1 for w in words if w in TOXIC_WORDS)
        if count > 0:
            toxicity[author_id] += count
    if not toxicity:
        await ctx.send("This server is suspiciously wholesome.")
        return
    top = toxicity.most_common(10)
    msg = "**☣️ Top 10 Most Toxic Users:**\n"
    for uid, count in top:
        user = ctx.guild.get_member(uid)
        name = user.display_name if user else f"User {uid}"
        msg += f"**{name}** — {count} toxic word(s)\n"
    await ctx.send(msg)
toxicrank.shortcut = "tox"

bot.run(os.getenv("DISCORD_TOKEN"))
