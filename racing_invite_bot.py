"""
Racing Master Discord Bot — Community Growth Invite Event
===========================================================
A production-ready Discord bot built with discord.py (v2.4+) and aiosqlite.
Tracks invites, daily speech, awards points, and manages a tier-based
reward draw at the end of the event.

Customisation Quick-Reference (bottom of file)
"""

import csv
import io
import os
import logging
import random
import time
from datetime import date, datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("racing_invite_bot")

# ── Load environment ─────────────────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional; set to a server ID for instant sync
ANNOUNCEMENT_CHANNEL_ID = os.getenv("ANNOUNCEMENT_CHANNEL_ID")
ACTIVE_CHANNEL_IDS = os.getenv("ACTIVE_CHANNEL_IDS")  # comma-separated
ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID")  # optional; "" falls back to administrator perm

# Pre-process ACTIVE_CHANNEL_IDS into a set of ints for fast lookup
if ACTIVE_CHANNEL_IDS:
    try:
        ACTIVE_CHANNEL_SET = {int(cid.strip()) for cid in ACTIVE_CHANNEL_IDS.split(",") if cid.strip()}
    except ValueError:
        log.error("ACTIVE_CHANNEL_IDS contains non-numeric values — speech tracking disabled.")
        ACTIVE_CHANNEL_SET = set()
else:
    ACTIVE_CHANNEL_SET = set()

if ANNOUNCEMENT_CHANNEL_ID:
    try:
        ANNOUNCEMENT_CHANNEL_ID = int(ANNOUNCEMENT_CHANNEL_ID)
    except ValueError:
        log.error("ANNOUNCEMENT_CHANNEL_ID must be an integer.")
        ANNOUNCEMENT_CHANNEL_ID = None

EVENT_START_DATE_STR = os.getenv("EVENT_START_DATE", "2026-05-21")
try:
    EVENT_START_DATE = datetime.strptime(EVENT_START_DATE_STR, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
except ValueError:
    log.error(f"EVENT_START_DATE '{EVENT_START_DATE_STR}' is invalid — anti-farm disabled.")
    EVENT_START_DATE = datetime.min.replace(tzinfo=timezone.utc)

# ── Reward constants ─────────────────────────────────────────────────────────
# Tier 3: Top 5 by final points
TIER_3_WINNER_COUNT = 5
TIER_3_REWARD = "2 Gem Car Steels + 588 Gold"

# Tier 2: Random draw from remaining users with ≥20 points (excl Top 5)
TIER_2_MIN_POINTS = 20
TIER_2_WINNER_COUNT = 10
TIER_2_REWARD = "1 Gem Car Steel + 388 Gold"

# Tier 1: Random draw from remaining users with ≥2 points (excl higher tiers)
TIER_1_MIN_POINTS = 2
TIER_1_WINNER_COUNT = 20
TIER_1_REWARD = "88 Diamonds + 288 Gold"

# Milestone thresholds for community-growth announcements
MILESTONES = [25000, 28000, 30000]

# Point values
POINTS_PER_INVITE = 10
POINTS_PER_DAILY_SPEECH = 2

# In-memory cooldown for /invite command (seconds between uses per user)
INVITE_COOLDOWN_SECONDS = 3600
_invite_cooldowns: dict[str, float] = {}

# ── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "invites.db")


async def init_db() -> aiosqlite.Connection:
    """Create tables if they don't exist and ensure default event_state row."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS invite_links (
            code TEXT PRIMARY KEY,
            inviter_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS invited_users (
            user_id TEXT PRIMARY KEY,
            inviter_id TEXT NOT NULL,
            invite_code TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_valid INTEGER DEFAULT 1
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS daily_speech (
            user_id TEXT,
            date TEXT,
            PRIMARY KEY (user_id, date)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS points (
            user_id TEXT PRIMARY KEY,
            points INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS event_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Ensure the 'active' key exists (default = '1')
    await db.execute(
        "INSERT OR IGNORE INTO event_state(key, value) VALUES ('active', '1')"
    )
    await db.commit()
    log.info("Database initialised successfully.")
    return db


async def is_event_active(db: aiosqlite.Connection) -> bool:
    """Return True if the event_state 'active' column equals '1'."""
    cursor = await db.execute("SELECT value FROM event_state WHERE key='active'")
    row = await cursor.fetchone()
    return row is not None and row["value"] == "1"


def is_admin(interaction: discord.Interaction) -> bool:
    """Check whether the interaction user has the admin role or administrator permission."""
    if ADMIN_ROLE_ID and interaction.guild:
        admin_role = interaction.guild.get_role(int(ADMIN_ROLE_ID))
        if admin_role and admin_role in interaction.user.roles:
            return True
    return interaction.user.guild_permissions.administrator


async def admin_only(interaction: discord.Interaction) -> bool:
    """Predicate for app_commands.check."""
    if not is_admin(interaction):
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True
        )
        return False
    return True


# ── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True        # member join / leave / update
intents.invites = True         # invite tracking
intents.message_content = True  # reading message content for speech tracking
intents.guilds = True

bot = commands.Bot(command_prefix="rc!", intents=intents)
bot.invite_cache: dict[str, int] = {}  # code -> uses


# ── Helper: send DM safely ───────────────────────────────────────────────────
async def safe_dm(user: discord.User, content: str | None = None, embed: discord.Embed | None = None):
    """Try to send a DM; silently ignore on failure."""
    try:
        if embed:
            await user.send(content=content, embed=embed)
        else:
            await user.send(content)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ══════════════════════════════════════════════════════════════════════════════
# EVENT HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    """Cache existing invites, sync command tree, start milestone watcher."""
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Pre-load all existing invites into the cache
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            for inv in invites:
                bot.invite_cache[inv.code] = inv.uses
            log.info(f"Cached {len(invites)} invites for guild '{guild.name}'.")
        except discord.Forbidden:
            log.warning(f"Missing 'Manage Server' permission in guild '{guild.name}'. Invite tracking disabled.")

    # Sync the slash command tree
    if GUILD_ID:
        try:
            target_guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=target_guild)
            synced = await bot.tree.sync(guild=target_guild)
            log.info(f"Synced {len(synced)} slash commands to guild {GUILD_ID}.")
        except (ValueError, discord.HTTPException) as e:
            log.error(f"Failed to sync commands to guild {GUILD_ID}: {e}")
            synced = await bot.tree.sync()
            log.info(f"Falling back to global sync: {len(synced)} commands synced.")
    else:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands globally.")

    # Start the milestone watcher background task
    if not milestone_watcher.is_running():
        milestone_watcher.start()

    log.info("Bot is ready. All systems initialised.")


@bot.event
async def on_invite_create(invite: discord.Invite):
    """A new invite was created — add it to the cache."""
    bot.invite_cache[invite.code] = invite.uses
    log.debug(f"New invite cached: {invite.code} (uses={invite.uses})")


@bot.event
async def on_member_join(member: discord.Member):
    """Handle new member join: detect who invited them, award points."""
    db = await init_db()
    try:
        if not await is_event_active(db):
            return

        guild = member.guild
        try:
            current_invites = await guild.invites()
        except discord.Forbidden:
            log.warning(f"Cannot fetch invites for guild '{guild.name}'.")
            return

        # Find which invite's usage increased by comparing with cache
        invited_by: str | None = None
        matched_code: str | None = None
        for inv in current_invites:
            old_uses = bot.invite_cache.get(inv.code, 0)
            if inv.uses > old_uses:
                matched_code = inv.code
                # Look up the inviter in our DB
                cursor = await db.execute(
                    "SELECT inviter_id FROM invite_links WHERE code = ?", (inv.code,)
                )
                row = await cursor.fetchone()
                if row:
                    invited_by = row["inviter_id"]
                break

        # Update the cache with fresh data
        for inv in current_invites:
            bot.invite_cache[inv.code] = inv.uses

        if invited_by is None or str(member.id) == invited_by:
            return  # self-invite or unknown inviter — no points awarded

        # Anti-farm: reject accounts created after the event start date
        if member.created_at >= EVENT_START_DATE:
            log.warning(
                f"Anti-farm: {member.id} joined via {invited_by}'s invite, "
                f"but account was created {member.created_at} (after {EVENT_START_DATE}). No points awarded."
            )
            return

        # Record the invited user
        await db.execute(
            "INSERT OR IGNORE INTO invited_users(user_id, inviter_id, invite_code, is_valid) VALUES (?, ?, ?, 1)",
            (str(member.id), invited_by, matched_code),
        )

        # Award points to the inviter
        await db.execute(
            "INSERT INTO points(user_id, points) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET points = points + ?",
            (invited_by, POINTS_PER_INVITE, POINTS_PER_INVITE),
        )
        await db.commit()

        log.info(f"User {member.id} joined via {invited_by}'s invite. +{POINTS_PER_INVITE}pts awarded.")

        # Send welcome DM
        welcome_embed = discord.Embed(
            title="🎉 Welcome to the Racing Master Community!",
            description=(
                "We're running a community growth event! Use the `/invite` command "
                "in the server to get your personal invite link and start earning rewards.\n\n"
                "Invite friends → earn points → win prizes 🏆"
            ),
            color=discord.Color.green(),
        )
        welcome_embed.add_field(
            name="How it works",
            value=(
                f"• Each successful invite: **+{POINTS_PER_INVITE}** points\n"
                f"• Daily message in event channels: **+{POINTS_PER_DAILY_SPEECH}** points\n"
                "• Top 5 get Tier 3 rewards at the end of the event!"
            ),
        )
        await safe_dm(member, embed=welcome_embed)

    except Exception as e:
        log.error(f"Error in on_member_join: {e}", exc_info=True)
    finally:
        await db.close()


@bot.event
async def on_member_remove(member: discord.Member):
    """Handle member leave: invalidate invite record and deduct points."""
    db = await init_db()
    try:
        if not await is_event_active(db):
            return

        cursor = await db.execute(
            "SELECT inviter_id FROM invited_users WHERE user_id = ? AND is_valid = 1",
            (str(member.id),),
        )
        row = await cursor.fetchone()
        if row is None:
            return

        inviter_id = row["inviter_id"]

        # Mark the invitation as no longer valid
        await db.execute(
            "UPDATE invited_users SET is_valid = 0 WHERE user_id = ?",
            (str(member.id),),
        )

        # Deduct points from the inviter, floor at 0
        await db.execute(
            "UPDATE points SET points = MAX(0, points - ?) WHERE user_id = ?",
            (POINTS_PER_INVITE, inviter_id),
        )
        await db.commit()

        log.info(f"User {member.id} left. {POINTS_PER_INVITE}pts deducted from inviter {inviter_id}.")

        # Notify the inviter via DM
        inviter = bot.get_user(int(inviter_id))
        if inviter:
            notification = (
                f"😢 A friend you invited (**{member.name}**) has left the server. "
                f"{POINTS_PER_INVITE} points have been deducted from your total. "
                f"Keep inviting to maintain your rank!"
            )
            await safe_dm(inviter, content=notification)

    except Exception as e:
        log.error(f"Error in on_member_remove: {e}", exc_info=True)
    finally:
        await db.close()


@bot.event
async def on_message(message: discord.Message):
    """Track daily speech in configured active channels and award points."""
    db = await init_db()
    try:
        if not await is_event_active(db):
            return

        if message.author.bot:
            return

        if message.channel.id not in ACTIVE_CHANNEL_SET:
            return

        today_str = date.today().isoformat()
        user_id = str(message.author.id)

        # Try to insert; if the row already exists (PK conflict), IGNORE returns rowcount=0
        cursor = await db.execute(
            "INSERT OR IGNORE INTO daily_speech(user_id, date) VALUES (?, ?)",
            (user_id, today_str),
        )
        if cursor.rowcount == 0:
            return  # already spoke today

        # Award daily speech points
        await db.execute(
            "INSERT INTO points(user_id, points) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET points = points + ?",
            (user_id, POINTS_PER_DAILY_SPEECH, POINTS_PER_DAILY_SPEECH),
        )
        await db.commit()

        # Acknowledge silently with a reaction (avoids channel spam)
        try:
            await message.add_reaction("✅")
        except discord.Forbidden:
            pass  # bot may not have 'Add Reactions' permission

    except Exception as e:
        log.error(f"Error in on_message: {e}", exc_info=True)
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASK — Milestone Watcher
# ══════════════════════════════════════════════════════════════════════════════

# Track which milestones have already been announced to avoid duplicates
announced_milestones: set[int] = set()


@tasks.loop(minutes=10)
async def milestone_watcher():
    """Every 10 minutes, check member count for milestones & refresh the public leaderboard."""
    if ANNOUNCEMENT_CHANNEL_ID is None:
        return

    for guild in bot.guilds:
        channel = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if channel is None:
            continue

        count = guild.member_count
        for ms in MILESTONES:
            if count >= ms and ms not in announced_milestones:
                announced_milestones.add(ms)
                embed = discord.Embed(
                    title="🎊 Server Milestone Reached!",
                    description=(
                        f"We just hit **{ms:,} members**! Thank you to everyone "
                        f"who invited friends and helped grow our community. "
                        f"Keep the momentum going — the next milestone is within reach! 🚀"
                    ),
                    color=discord.Color.gold(),
                )
                embed.set_footer(text=f"Current members: {count:,}")
                try:
                    await channel.send(
                        content="@everyone",
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(everyone=True),
                    )
                    log.info(f"Milestone {ms} announced in guild '{guild.name}'.")
                except discord.Forbidden:
                    log.warning("Missing 'Send Messages' permission for announcement channel.")
                break  # only announce one milestone per loop iteration

    # Refresh the public leaderboard message if it exists
    await _update_leaderboard_message()


@milestone_watcher.before_loop
async def before_milestone_watcher():
    """Wait until the bot is fully ready before starting the loop."""
    await bot.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════════════
# SHARED HANDLER FUNCTIONS  (used by both slash commands and button callbacks)
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_invite(interaction: discord.Interaction):
    """Generate a personal invite link for the interaction user (ephemeral)."""
    db = await init_db()
    try:
        if not await is_event_active(db):
            await interaction.response.send_message(
                "The event is currently inactive. Check back later!",
                ephemeral=True,
            )
            return

        # Cooldown check: one invite per hour per user
        user_id = str(interaction.user.id)
        now = time.time()
        last_used = _invite_cooldowns.get(user_id)
        if last_used is not None:
            remaining = int(INVITE_COOLDOWN_SECONDS - (now - last_used))
            if remaining > 0:
                minutes = remaining // 60
                seconds = remaining % 60
                await interaction.response.send_message(
                    f"⏳ You can generate another invite in **{minutes}m {seconds}s**.",
                    ephemeral=True,
                )
                return

        guild = interaction.guild
        target_channel = guild.system_channel or interaction.channel
        if target_channel is None:
            await interaction.response.send_message(
                "Could not find a suitable channel to create the invite.",
                ephemeral=True,
            )
            return

        try:
            invite = await target_channel.create_invite(
                max_age=0,
                max_uses=0,
                unique=True,
                reason=f"Event invite for {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I lack the 'Create Instant Invite' permission. Please ask an admin to fix this.",
                ephemeral=True,
            )
            return

        await db.execute(
            "INSERT OR REPLACE INTO invite_links(code, inviter_id) VALUES (?, ?)",
            (invite.code, str(interaction.user.id)),
        )
        await db.commit()
        bot.invite_cache[invite.code] = invite.uses
        _invite_cooldowns[user_id] = now

        embed = discord.Embed(
            title="📨 Your Personal Invite Link",
            description=(
                f"Share this link with friends to earn **{POINTS_PER_INVITE}** points per successful invite!\n\n"
                f"🔗 {invite.url}"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Rules",
            value=(
                "• Friends must stay in the server (if they leave, points are deducted)\n"
                "• No self-invites or alt accounts\n"
                "• Have fun and good luck! 🍀"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        log.info(f"User {interaction.user.id} created invite '{invite.code}'.")

    except Exception as e:
        log.error(f"Error in _handle_invite: {e}", exc_info=True)
        await interaction.response.send_message(
            "An unexpected error occurred. Please try again later.",
            ephemeral=True,
        )
    finally:
        await db.close()


async def _handle_rank(interaction: discord.Interaction):
    """Show the top 10 leaderboard and the interaction user's rank (ephemeral)."""
    db = await init_db()
    try:
        cursor = await db.execute(
            "SELECT user_id, points FROM points ORDER BY points DESC LIMIT 50"
        )
        rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No one has earned points yet. Use `/invite` to get started!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🏆 Invite Event Leaderboard — Top 10",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        top_display = rows[:10]
        leaderboard_lines: list[str] = []
        for idx, row in enumerate(top_display, 1):
            user = bot.get_user(int(row["user_id"]))
            name = user.display_name if user else f"User {row['user_id']}"
            leaderboard_lines.append(f"`#{idx:>2}` **{name}** — {row['points']} pts")
        embed.description = "\n".join(leaderboard_lines)

        user_id = str(interaction.user.id)
        user_points = 0
        user_rank: int | None = None
        for idx, row in enumerate(rows, 1):
            if row["user_id"] == user_id:
                user_points = row["points"]
                user_rank = idx
                break

        if user_rank is None:
            cursor = await db.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
            user_row = await cursor.fetchone()
            if user_row:
                user_points = user_row["points"]
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM points WHERE points > ?", (user_points,)
            )
            cnt_row = await cursor.fetchone()
            user_rank = cnt_row["cnt"] + 1 if cnt_row else 1

        if user_rank is not None and user_rank <= TIER_3_WINNER_COUNT:
            tier = "🥇 Tier 3  (top 5)"
        elif user_points >= TIER_2_MIN_POINTS:
            tier = "🥈 Tier 2  (≥20 pts)"
        elif user_points >= TIER_1_MIN_POINTS:
            tier = "🥉 Tier 1  (≥2 pts)"
        else:
            tier = "⬜ Not yet eligible"

        embed.add_field(
            name="Your Stats",
            value=(
                f"**Rank:** #{user_rank}\n"
                f"**Points:** {user_points}\n"
                f"**Eligibility:** {tier}"
            ),
            inline=False,
        )
        embed.set_footer(text="Keep inviting and chatting daily to climb the ranks!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        log.error(f"Error in _handle_rank: {e}", exc_info=True)
        await interaction.response.send_message(
            "An error occurred while fetching rankings.", ephemeral=True
        )
    finally:
        await db.close()


async def _handle_myinvites(interaction: discord.Interaction):
    """Show the interaction user's invited members list (ephemeral)."""
    db = await init_db()
    try:
        cursor = await db.execute(
            "SELECT user_id, invite_code, joined_at, is_valid "
            "FROM invited_users WHERE inviter_id = ? "
            "ORDER BY joined_at DESC",
            (str(interaction.user.id),),
        )
        rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "You haven't invited anyone yet. Use `/invite` to get your link!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📋 Your Invited Members",
            color=discord.Color.blurple(),
        )
        lines: list[str] = []
        for row in rows[:25]:
            uid_val = int(row["user_id"])
            is_valid = row["is_valid"]
            joined_at = row["joined_at"] or "unknown"

            if is_valid:
                member = interaction.guild.get_member(uid_val) if interaction.guild else None
                status_icon = "✅" if member else "⚠️"
                status_text = "Still in server" if member else "May have left"
            else:
                status_icon = "❌"
                status_text = "Left — points deducted"

            user = bot.get_user(uid_val)
            name = user.display_name if user else f"User {uid_val}"
            lines.append(
                f"{status_icon} **{name}**\n"
                f"   └ Joined: {joined_at} | {status_text}"
            )

        embed.description = "\n".join(lines) if lines else "No invites found."
        embed.set_footer(text=f"Total invites: {len(rows)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        log.error(f"Error in _handle_myinvites: {e}", exc_info=True)
        await interaction.response.send_message(
            "An error occurred while fetching your invites.", ephemeral=True
        )
    finally:
        await db.close()


async def _handle_progress(interaction: discord.Interaction):
    """Show server member count and milestone progress bars (ephemeral)."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command only works in servers.", ephemeral=True)
        return

    current = guild.member_count
    embed = discord.Embed(
        title="📊 Server Growth Progress",
        color=discord.Color.teal(),
    )

    BAR_LENGTH = 20
    lines: list[str] = []
    for ms in MILESTONES:
        progress = min(current / ms, 1.0)
        filled = int(BAR_LENGTH * progress)
        bar = "█" * filled + "░" * (BAR_LENGTH - filled)
        status = "✅ Reached!" if current >= ms else f"({current - ms:,} to go)"
        lines.append(f"**{ms:,}** members\n`{bar}` {current:,}/{ms:,}  {status}")

    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Current member count: {current:,}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _build_leaderboard_embed() -> discord.Embed:
    """Query the Top 10 by points (tie-broken by earliest invite time) and build a public embed."""
    db = await init_db()
    try:
        cursor = await db.execute(
            "SELECT user_id, points FROM points ORDER BY points DESC"
        )
        all_rows = await cursor.fetchall()

        if not all_rows:
            embed = discord.Embed(
                title="🏆 Invite Event Leaderboard — Top 10",
                description="No participants yet. Use `/invite` to get started!",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            return embed

        participants: list[dict] = []
        for row in all_rows:
            uid = row["user_id"]
            pts = row["points"]
            c = await db.execute(
                "SELECT MIN(created_at) FROM invite_links WHERE inviter_id = ?", (uid,)
            )
            earliest = await c.fetchone()
            earliest_time = earliest[0] if earliest and earliest[0] else "9999-99-99"
            participants.append({"user_id": uid, "points": pts, "earliest": earliest_time})

        participants.sort(key=lambda x: (-x["points"], x["earliest"]))
        top10 = participants[:10]

        embed = discord.Embed(
            title="🏆 Invite Event Leaderboard — Top 10",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        lines: list[str] = []
        for idx, p in enumerate(top10, 1):
            user = bot.get_user(int(p["user_id"]))
            name = user.display_name if user else f"User {p['user_id']}"
            lines.append(f"`#{idx:>2}` **{name}** — {p['points']} pts")
        embed.description = "\n".join(lines)
        embed.set_footer(text="Auto-refreshes every 10 minutes")
        return embed
    finally:
        await db.close()


async def _update_leaderboard_message():
    """If a leaderboard message ID is stored, fetch it and edit its embed."""
    db = await init_db()
    try:
        if not await is_event_active(db):
            return

        cursor = await db.execute("SELECT value FROM event_state WHERE key='leaderboard_msg_id'")
        row = await cursor.fetchone()
        if row is None or not row["value"]:
            return
        msg_id = int(row["value"])
        channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(msg_id)
        except discord.NotFound:
            await db.execute("DELETE FROM event_state WHERE key='leaderboard_msg_id'")
            await db.commit()
            return

        embed = await _build_leaderboard_embed()
        await msg.edit(embed=embed)
    except Exception as e:
        log.error(f"Failed to update leaderboard message: {e}", exc_info=True)
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="invite", description="Get your personal invite link and start earning points!")
async def cmd_invite(interaction: discord.Interaction):
    """Create a unique permanent invite (ephemeral reply)."""
    await _handle_invite(interaction)


@bot.tree.command(name="rank", description="View the top 10 leaderboard and your current rank.")
async def cmd_rank(interaction: discord.Interaction):
    """Show the leaderboard and user's rank (ephemeral reply)."""
    await _handle_rank(interaction)


@bot.tree.command(name="myinvites", description="See who you've invited and their current status.")
async def cmd_myinvites(interaction: discord.Interaction):
    """Display invited members list (ephemeral reply)."""
    await _handle_myinvites(interaction)


@bot.tree.command(name="progress", description="Check server growth progress toward milestones.")
async def cmd_progress(interaction: discord.Interaction):
    """Show milestone progress (ephemeral reply)."""
    await _handle_progress(interaction)


# ── Button-driven invite panel ───────────────────────────────────────────────

class InvitePanelView(discord.ui.View):
    """A public message with 4 interactive buttons that each reply ephemerally."""

    def __init__(self):
        super().__init__(timeout=None)  # persistent — never expires

    @discord.ui.button(label="Get Invite Link", style=discord.ButtonStyle.blurple, emoji="🔗", custom_id="panel:invite")
    async def btn_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_invite(interaction)

    @discord.ui.button(label="My Rank", style=discord.ButtonStyle.green, emoji="📊", custom_id="panel:rank")
    async def btn_rank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_rank(interaction)

    @discord.ui.button(label="My Invites", style=discord.ButtonStyle.blurple, emoji="👥", custom_id="panel:myinvites")
    async def btn_myinvites(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_myinvites(interaction)

    @discord.ui.button(label="Server Progress", style=discord.ButtonStyle.success, emoji="📈", custom_id="panel:progress")
    async def btn_progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_progress(interaction)


@bot.tree.command(name="post_invite_button", description="[Admin] Post the interactive invite panel with buttons.")
@app_commands.check(admin_only)
async def cmd_post_invite_button(interaction: discord.Interaction):
    """Post a public message containing the InvitePanelView button row."""
    embed = discord.Embed(
        title="🏁 Racing to 30K – Milestone Giveaway",
        description=(
            "We're sprinting toward **30,000 members** – and you're in the driver's seat.\n"
            "Hit each milestone to unlock bigger prize pools. The more you invite and engage, the more you earn!\n\n"
            "📅 **Event:** May 21 – June 4 (or extended depending on server progress)\n"
            "🎯 **Milestones:** 25,000 → 28,000 → 30,000\n\n"
            "🎁 **Reward Pools**\n\n"
            "🏆 **Top 5 Leaderboard:** 2 Ruby keys + 588 Gold\n"
            "🎲 **20+ Points Draw (10 winners):** 1 Ruby key + 388 Gold\n"
            "🎲 **2+ Points Draw (20 winners):** 88 Diamonds + 288 Gold\n\n"
            "📌 **How to Participate**\n\n"
            "**Invite:** Click 🔗 Get Invite Link below → share with friends → +10 pts each\n"
            "**Chat:** Talk daily in <#1506202426707939428> or <#819493701633310771> → +2 pts/day\n"
            "**Track:** Use the buttons below to see your rank, invites, and server progress in private\n\n"
            "Every invite and every message counts. Let's cross the finish line together! 🏎️"
        ),
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=InvitePanelView())
    await interaction.response.send_message("Panel posted!", ephemeral=True)


@bot.tree.command(name="setup_leaderboard", description="[Admin] Post a persistent Top 10 leaderboard in the announcement channel.")
@app_commands.check(admin_only)
async def cmd_setup_leaderboard(interaction: discord.Interaction):
    """Send a public leaderboard embed and store its message ID for auto-refresh."""
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID) if ANNOUNCEMENT_CHANNEL_ID else interaction.channel
    if channel is None:
        await interaction.response.send_message("No announcement channel configured.", ephemeral=True)
        return

    embed = await _build_leaderboard_embed()
    msg = await channel.send(embed=embed)

    db = await init_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO event_state(key, value) VALUES ('leaderboard_msg_id', ?)",
            (str(msg.id),),
        )
        await db.commit()
    finally:
        await db.close()

    await interaction.response.send_message(
        f"Leaderboard posted in {channel.mention} and will auto-refresh every 10 minutes.",
        ephemeral=True,
    )


@bot.tree.command(name="stats", description="[Admin] View detailed event statistics.")
@app_commands.check(admin_only)
async def cmd_stats(interaction: discord.Interaction):
    """Display admin-only event statistics."""
    db = await init_db()
    try:
        # Number of unique inviters
        cursor = await db.execute("SELECT COUNT(DISTINCT inviter_id) FROM invite_links")
        inviter_count = (await cursor.fetchone())[0]

        # Number of successful (still valid) invitations
        cursor = await db.execute("SELECT COUNT(*) FROM invited_users WHERE is_valid = 1")
        valid_invites = (await cursor.fetchone())[0]

        # Number of unique daily speakers
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM daily_speech")
        daily_speakers = (await cursor.fetchone())[0]

        # Top 5 inviters by points
        cursor = await db.execute(
            "SELECT user_id, points FROM points ORDER BY points DESC LIMIT 5"
        )
        top5 = await cursor.fetchall()

        embed = discord.Embed(
            title="📈 Event Statistics (Admin)",
            color=discord.Color.dark_purple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Unique Inviters", value=str(inviter_count), inline=True)
        embed.add_field(name="Valid Invitations", value=str(valid_invites), inline=True)
        embed.add_field(name="Daily Active Speakers", value=str(daily_speakers), inline=True)

        if top5:
            top5_lines: list[str] = []
            for idx, row in enumerate(top5, 1):
                user = bot.get_user(int(row["user_id"]))
                name = user.display_name if user else f"User {row['user_id']}"
                top5_lines.append(f"`#{idx}` **{name}** — {row['points']} pts")
            embed.add_field(name="Top 5 Inviters", value="\n".join(top5_lines), inline=False)

        # Milestone progress
        if interaction.guild:
            current = interaction.guild.member_count
            prog_lines: list[str] = []
            for ms in MILESTONES:
                status = "✅" if current >= ms else "⬜"
                prog_lines.append(f"{status} {ms:,}")
            embed.add_field(
                name=f"Milestone Progress ({current:,} members)",
                value="\n".join(prog_lines),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        log.error(f"Error in /stats: {e}", exc_info=True)
        await interaction.response.send_message("Error fetching stats.", ephemeral=True)
    finally:
        await db.close()


@bot.tree.command(name="end", description="[Admin] End the event and announce winners.")
@app_commands.check(admin_only)
async def cmd_end(interaction: discord.Interaction):
    """Freeze the event, calculate winners, and post results."""
    await interaction.response.defer(thinking=True)  # this may take a moment

    db = await init_db()
    try:
        # 1. Freeze the event
        await db.execute("UPDATE event_state SET value = '0' WHERE key = 'active'")
        await db.commit()
        log.info("Event has been set to inactive.")

        # 2. Fetch all users with points, sorted descending
        cursor = await db.execute(
            "SELECT user_id, points FROM points ORDER BY points DESC"
        )
        all_users = await cursor.fetchall()

        if not all_users:
            await interaction.followup.send("No participants. Event ended.", ephemeral=True)
            return

        # ------------------------------------------------------------------
        # 3. Determine Tier 3 winners (Top 5 by points)
        #    Tie-breaking: earliest first invite creation time
        # ------------------------------------------------------------------
        sorted_all: list[dict] = []
        for row in all_users:
            uid = row["user_id"]
            pts = row["points"]
            # Find the earliest timestamp for tie-breaking
            c = await db.execute(
                "SELECT MIN(created_at) FROM invite_links WHERE inviter_id = ?", (uid,)
            )
            earliest = await c.fetchone()
            earliest_time = earliest[0] if earliest and earliest[0] else "9999-99-99"
            sorted_all.append({
                "user_id": uid,
                "points": pts,
                "earliest": earliest_time,
            })

        # Sort by points DESC, then earliest ASC (earlier = better)
        sorted_all.sort(key=lambda x: (-x["points"], x["earliest"]))

        tier3 = sorted_all[:TIER_3_WINNER_COUNT]
        tier3_ids = {u["user_id"] for u in tier3}

        # ------------------------------------------------------------------
        # 4. Determine Tier 2 winners
        #    Remaining users with ≥20 pts, random draw of 10
        # ------------------------------------------------------------------
        tier2_pool = [
            u for u in sorted_all
            if u["points"] >= TIER_2_MIN_POINTS and u["user_id"] not in tier3_ids
        ]
        if len(tier2_pool) <= TIER_2_WINNER_COUNT:
            tier2 = tier2_pool
        else:
            tier2 = random.sample(tier2_pool, TIER_2_WINNER_COUNT)
        tier2_ids = {u["user_id"] for u in tier2}

        # ------------------------------------------------------------------
        # 5. Determine Tier 1 winners
        #    Remaining users with ≥2 pts, random draw of 20
        # ------------------------------------------------------------------
        tier1_pool = [
            u for u in sorted_all
            if u["points"] >= TIER_1_MIN_POINTS
            and u["user_id"] not in tier3_ids
            and u["user_id"] not in tier2_ids
        ]
        if len(tier1_pool) <= TIER_1_WINNER_COUNT:
            tier1 = tier1_pool
        else:
            tier1 = random.sample(tier1_pool, TIER_1_WINNER_COUNT)

        # ------------------------------------------------------------------
        # 6. Build and send announcement embeds
        # ------------------------------------------------------------------
        channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID) if ANNOUNCEMENT_CHANNEL_ID else None
        if channel is None:
            # Fallback to the interaction channel
            channel = interaction.channel

        def fmt_user(entry: dict) -> str:
            uid = int(entry["user_id"])
            user = bot.get_user(uid)
            if user:
                return f"<@{uid}> (`{user.name}`)"
            return f"<@{uid}> (User ID: {uid})"

        # Event ended header
        header_embed = discord.Embed(
            title="🏁 Event Ended — Final Rankings",
            description=(
                "The community growth event has concluded. Thank you to everyone "
                "who participated! Here are the winners:"
            ),
            color=discord.Color.orange(),
        )
        header_embed.set_footer(text=f"Total participants: {len(all_users)}")
        await channel.send(embed=header_embed)

        # Tier 3
        t3_embed = discord.Embed(
            title=f"🥇 Tier 3 Winners — Top {TIER_3_WINNER_COUNT}",
            description=f"Each winner receives: **{TIER_3_REWARD}**",
            color=discord.Color.gold(),
        )
        t3_embed.add_field(
            name="Winners",
            value="\n".join(f"`#{i+1}` {fmt_user(u)} — **{u['points']}** pts" for i, u in enumerate(tier3)),
            inline=False,
        )
        await channel.send(embed=t3_embed)

        # Tier 2
        t2_embed = discord.Embed(
            title=f"🥈 Tier 2 Winners — Random Draw (≥{TIER_2_MIN_POINTS} pts)",
            description=f"Each winner receives: **{TIER_2_REWARD}**",
            color=discord.Color.from_rgb(192, 192, 192),
        )
        if tier2:
            t2_embed.add_field(
                name="Winners",
                value="\n".join(f"{fmt_user(u)} — **{u['points']}** pts" for u in tier2),
                inline=False,
            )
        else:
            t2_embed.add_field(name="Winners", value="No eligible participants.", inline=False)
        await channel.send(embed=t2_embed)

        # Tier 1
        t1_embed = discord.Embed(
            title=f"🥉 Tier 1 Winners — Random Draw (≥{TIER_1_MIN_POINTS} pts)",
            description=f"Each winner receives: **{TIER_1_REWARD}**",
            color=discord.Color.from_rgb(205, 127, 50),
        )
        if tier1:
            t1_embed.add_field(
                name="Winners",
                value="\n".join(f"{fmt_user(u)} — **{u['points']}** pts" for u in tier1),
                inline=False,
            )
        else:
            t1_embed.add_field(name="Winners", value="No eligible participants.", inline=False)
        await channel.send(embed=t1_embed)

        # Optional: DM Tier 3 winners (only a few, unlikely to cause rate limits)
        for entry in tier3:
            uid = int(entry["user_id"])
            user = bot.get_user(uid)
            if user:
                dm_embed = discord.Embed(
                    title="🎉 Congratulations! You won a Tier 3 prize!",
                    description=(
                        f"You placed in the **Top {TIER_3_WINNER_COUNT}** of the Racing Master "
                        f"Invite Event with **{entry['points']}** points!\n\n"
                        f"Your prize: **{TIER_3_REWARD}**\n\n"
                        "Please contact an admin to claim your reward."
                    ),
                    color=discord.Color.gold(),
                )
                await safe_dm(user, embed=dm_embed)

        log.info(
            f"Event ended. Tier3={len(tier3)}, Tier2={len(tier2)}, Tier1={len(tier1)}. "
            f"Seed not fixed (used random.sample)."
        )

        await interaction.followup.send(
            "Event has been ended and winners have been announced!", ephemeral=True
        )

    except Exception as e:
        log.error(f"Error in /end: {e}", exc_info=True)
        await interaction.followup.send(
            "An error occurred while ending the event. Check logs.", ephemeral=True
        )
    finally:
        await db.close()


# ── Admin: reset event ───────────────────────────────────────────────────────

@bot.tree.command(name="reset_event", description="[Admin] Fully reset the event: reactivate + wipe all data.")
@app_commands.check(admin_only)
async def cmd_reset_event(interaction: discord.Interaction):
    """Set event to active, clear all points/invites/speech data, remove leaderboard ID."""
    db = await init_db()
    try:
        await db.execute("UPDATE event_state SET value = '1' WHERE key = 'active'")
        await db.execute("DELETE FROM event_state WHERE key = 'leaderboard_msg_id'")
        await db.execute("DELETE FROM points")
        await db.execute("DELETE FROM invited_users")
        await db.execute("DELETE FROM invite_links")
        await db.execute("DELETE FROM daily_speech")
        await db.commit()

        bot.invite_cache.clear()
        _invite_cooldowns.clear()

        embed = discord.Embed(
            title="🔄 Event Reset",
            description=(
                "All event data has been wiped:\n"
                "• **points** — cleared\n"
                "• **invited_users** — cleared\n"
                "• **invite_links** — cleared\n"
                "• **daily_speech** — cleared\n"
                "• **event_state** — set to active\n"
                "• **invite_cache & cooldowns** — cleared"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="You can now run /setup_leaderboard to repost the leaderboard.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        log.info("Event fully reset via /reset_event.")

    except Exception as e:
        log.error(f"Error in /reset_event: {e}", exc_info=True)
        await interaction.response.send_message("Error resetting event. Check logs.", ephemeral=True)
    finally:
        await db.close()


# ── Admin: export data as CSV ────────────────────────────────────────────────

@bot.tree.command(name="export", description="[Admin] Export participant data as a CSV file.")
@app_commands.check(admin_only)
async def cmd_export(interaction: discord.Interaction):
    """Generate a CSV of all users who have points, with invite/speech breakdown."""
    await interaction.response.defer(thinking=True)

    db = await init_db()
    try:
        cursor = await db.execute(
            "SELECT user_id, points FROM points WHERE points > 0 ORDER BY points DESC"
        )
        point_rows = await cursor.fetchall()

        if not point_rows:
            await interaction.followup.send("No participants have earned points yet.", ephemeral=True)
            return

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["user_id", "username", "invite_points", "speech_points", "total_points"])

        total_participants = 0
        total_invites = 0
        total_speech = 0

        for row in point_rows:
            uid = row["user_id"]
            total_pts = row["points"]

            c = await db.execute(
                "SELECT COUNT(*) FROM invited_users WHERE inviter_id = ? AND is_valid = 1", (uid,)
            )
            invite_pts = (await c.fetchone())[0] * POINTS_PER_INVITE

            c = await db.execute(
                "SELECT COUNT(*) FROM daily_speech WHERE user_id = ?", (uid,)
            )
            speech_pts = (await c.fetchone())[0] * POINTS_PER_DAILY_SPEECH

            user = bot.get_user(int(uid))
            username = user.name if user else uid

            writer.writerow([uid, username, invite_pts, speech_pts, total_pts])

            total_participants += 1
            total_invites += invite_pts // POINTS_PER_INVITE
            total_speech += speech_pts // POINTS_PER_DAILY_SPEECH

        csv_bytes = io.BytesIO(output.getvalue().encode("utf-8"))
        file = discord.File(fp=csv_bytes, filename="racing_event_export.csv")

        summary = (
            f"**Total Participants:** {total_participants}\n"
            f"**Total Invite Successes:** {total_invites}\n"
            f"**Total Daily Speech Entries:** {total_speech}"
        )

        embed = discord.Embed(
            title="📤 Event Data Export",
            description=summary,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Exported at")
        embed.timestamp = datetime.now(timezone.utc)

        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        log.info(f"Export generated with {total_participants} participants by {interaction.user.id}.")

    except Exception as e:
        log.error(f"Error in /export: {e}", exc_info=True)
        await interaction.followup.send("Error generating export. Check logs.", ephemeral=True)
    finally:
        await db.close()


# ── Error handler for app command checks ─────────────────────────────────────
@cmd_reset_event.error
@cmd_export.error
@cmd_setup_leaderboard.error
@cmd_post_invite_button.error
@cmd_stats.error
@cmd_end.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle check failures gracefully."""
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True
        )
    else:
        log.error(f"Unhandled error in admin command: {error}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN is missing from .env file. Exiting.")
        exit(1)

    log.info("Starting Racing Master Invite Bot...")
    bot.run(DISCORD_TOKEN)


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMISATION GUIDE
# ══════════════════════════════════════════════════════════════════════════════
#
# To adjust the bot for your own server, edit the constants near the top of
# this file:
#
#   MILESTONES         = [25000, 28000, 30000]   ← change milestone thresholds
#   POINTS_PER_INVITE  = 10                       ← points for a valid invite
#   POINTS_PER_DAILY_SPEECH = 2                   ← points for daily message
#
#   TIER_3_WINNER_COUNT = 5                       ← how many top winners
#   TIER_3_REWARD       = "2 Ruby keys + 588 Gold"
#   TIER_2_MIN_POINTS   = 20
#   TIER_2_WINNER_COUNT = 10
#   TIER_2_REWARD       = "1 Ruby key + 388 Gold"
#   TIER_1_MIN_POINTS   = 2
#   TIER_1_WINNER_COUNT = 20
#   TIER_1_REWARD       = "88 Diamonds + 288 Gold"
#
# The .env file controls runtime configuration:
#   DISCORD_TOKEN        — your bot token (required)
#   GUILD_ID             — server ID for instant slash-command sync (optional)
#   ANNOUNCEMENT_CHANNEL_ID — where milestone + event-end results are posted
#   ACTIVE_CHANNEL_IDS   — comma-separated list of channels for speech tracking
#   ADMIN_ROLE_ID        — role that bypasses administrator-permission check
#
# Ensure the bot has these permissions in your server:
#   • Create Instant Invite
#   • Manage Server (to read invite uses)
#   • Read Messages / View Channels
#   • Send Messages
#   • Add Reactions
#   • Use Slash Commands
# ══════════════════════════════════════════════════════════════════════════════
