"""
TikTok Follower Tracker — Telegram Bot
----------------------------------------
Tracks public TikTok follower counts over time and sends Telegram alerts
on milestones (round numbers) and drops.

Commands:
  /track <username>    - start tracking a TikTok account
  /untrack <username>  - stop tracking
  /list                - show all tracked accounts + current counts
  /stats <username>    - show recent history for one account
  /check               - force an immediate check of all accounts

Setup:
  1. pip install python-telegram-bot requests --upgrade
  2. Create a bot via @BotFather on Telegram, get the token
  3. Set the TELEGRAM_BOT_TOKEN environment variable (or edit CONFIG below)
  4. python bot.py

Notes:
  - Uses TikTok's public profile HTML page (no login/API key required).
  - TikTok can change its page structure at any time, which will break
    the scraper. If counts stop updating, check the SCRAPING section.
  - Only tracks PUBLIC accounts (private accounts won't show follower
    counts in the page source).
"""

import os
import re
import json
import time
import sqlite3
import logging
from datetime import datetime, timezone

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
CHECK_INTERVAL_SECONDS = 20 * 60          # 20 minutes
DB_PATH = os.path.join(os.path.dirname(__file__), "tracker.db")

MILESTONE_STEPS = [
    1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000,
    500_000, 1_000_000, 2_500_000, 5_000_000, 10_000_000,
]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tiktok_tracker")

# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            last_milestone_notified INTEGER DEFAULT 0,
            UNIQUE(chat_id, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS follower_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            follower_count INTEGER NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def add_tracked_account(chat_id: int, username: str) -> bool:
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO tracked_accounts (chat_id, username) VALUES (?, ?)",
            (chat_id, username.lower()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_tracked_account(chat_id: int, username: str) -> bool:
    conn = db_connect()
    cur = conn.execute(
        "DELETE FROM tracked_accounts WHERE chat_id = ? AND username = ?",
        (chat_id, username.lower()),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_tracked_accounts_for_chat(chat_id: int):
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM tracked_accounts WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    conn.close()
    return rows


def get_all_tracked_accounts():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM tracked_accounts").fetchall()
    conn.close()
    return rows


def record_follower_count(username: str, count: int):
    conn = db_connect()
    conn.execute(
        "INSERT INTO follower_history (username, follower_count, checked_at) VALUES (?, ?, ?)",
        (username.lower(), count, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_last_two_counts(username: str):
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM follower_history WHERE username = ? ORDER BY checked_at DESC LIMIT 2",
        (username.lower(),),
    ).fetchall()
    conn.close()
    return rows


def get_history(username: str, limit: int = 10):
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM follower_history WHERE username = ? ORDER BY checked_at DESC LIMIT ?",
        (username.lower(), limit),
    ).fetchall()
    conn.close()
    return rows


def update_last_milestone(chat_id: int, username: str, milestone: int):
    conn = db_connect()
    conn.execute(
        "UPDATE tracked_accounts SET last_milestone_notified = ? WHERE chat_id = ? AND username = ?",
        (milestone, chat_id, username.lower()),
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# SCRAPING — public TikTok profile page
# ----------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class ProfileNotFound(Exception):
    pass


class ScrapeError(Exception):
    pass


def fetch_follower_count(username: str) -> int:
    """
    Fetches the public TikTok profile page and extracts the follower count
    from the embedded JSON (__UNIVERSAL_DATA_FOR_REHYDRATION__ script tag).

    Raises ProfileNotFound if the account doesn't exist / page 404s,
    or ScrapeError if the page structure has changed and data can't be parsed.
    """
    username = username.lstrip("@").strip()
    url = f"https://www.tiktok.com/@{username}"

    resp = requests.get(url, headers=HEADERS, timeout=15)

    if resp.status_code == 404:
        raise ProfileNotFound(username)
    resp.raise_for_status()

    html = resp.text

    # TikTok embeds page data as JSON inside a script tag.
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise ScrapeError(
            "Could not find data script tag — TikTok may have changed its page structure."
        )

    try:
        data = json.loads(match.group(1))
        user_detail = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]
        user_info = user_detail["userInfo"]
        follower_count = user_info["stats"]["followerCount"]
        return int(follower_count)
    except (KeyError, ValueError, TypeError) as e:
        raise ScrapeError(f"Could not parse follower count from page data: {e}")


# ----------------------------------------------------------------------
# MILESTONE LOGIC
# ----------------------------------------------------------------------

def crossed_milestone(previous: int, current: int, last_notified: int):
    """
    Returns the milestone value if `current` has crossed a new milestone
    above both `previous` and `last_notified`, else None.
    """
    for step in MILESTONE_STEPS:
        if current >= step and previous < step and step > last_notified:
            return step
    return None


def format_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ----------------------------------------------------------------------
# TELEGRAM COMMAND HANDLERS
# ----------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 TikTok Follower Tracker\n\n"
        "Commands:\n"
        "/track <username> — start tracking an account\n"
        "/untrack <username> — stop tracking\n"
        "/list — show tracked accounts\n"
        "/stats <username> — recent history\n"
        "/check — force an immediate check\n"
    )


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /track <username>")
        return

    username = context.args[0].lstrip("@")
    chat_id = update.effective_chat.id

    await update.message.reply_text(f"🔍 Looking up @{username}...")

    try:
        count = fetch_follower_count(username)
    except ProfileNotFound:
        await update.message.reply_text(f"❌ Couldn't find a TikTok account @{username}.")
        return
    except ScrapeError as e:
        await update.message.reply_text(f"⚠️ Found the page but couldn't read follower count: {e}")
        return
    except requests.RequestException as e:
        await update.message.reply_text(f"⚠️ Network error reaching TikTok: {e}")
        return

    added = add_tracked_account(chat_id, username)
    if not added:
        await update.message.reply_text(f"You're already tracking @{username}.")
        return

    record_follower_count(username, count)
    await update.message.reply_text(
        f"✅ Now tracking @{username}\n"
        f"Current followers: {count:,} ({format_count(count)})\n"
        f"I'll check every {CHECK_INTERVAL_SECONDS // 60} min and alert you on "
        f"milestones or drops."
    )


async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /untrack <username>")
        return

    username = context.args[0].lstrip("@")
    chat_id = update.effective_chat.id

    removed = remove_tracked_account(chat_id, username)
    if removed:
        await update.message.reply_text(f"🗑️ Stopped tracking @{username}.")
    else:
        await update.message.reply_text(f"You weren't tracking @{username}.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    accounts = get_tracked_accounts_for_chat(chat_id)

    if not accounts:
        await update.message.reply_text("You're not tracking any accounts yet. Use /track <username>.")
        return

    lines = ["📋 Tracked accounts:\n"]
    for acc in accounts:
        history = get_last_two_counts(acc["username"])
        if history:
            current = history[0]["follower_count"]
            line = f"• @{acc['username']} — {current:,} followers"
            if len(history) > 1:
                prev = history[1]["follower_count"]
                delta = current - prev
                if delta > 0:
                    line += f" (+{delta:,})"
                elif delta < 0:
                    line += f" ({delta:,})"
        else:
            line = f"• @{acc['username']} — no data yet"
        lines.append(line)

    await update.message.reply_text("\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /stats <username>")
        return

    username = context.args[0].lstrip("@")
    history = get_history(username, limit=10)

    if not history:
        await update.message.reply_text(f"No history yet for @{username}.")
        return

    lines = [f"📈 Recent history for @{username}:\n"]
    for row in history:
        ts = datetime.fromisoformat(row["checked_at"]).strftime("%b %d %H:%M")
        lines.append(f"{ts} — {row['follower_count']:,}")

    await update.message.reply_text("\n".join(lines))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Checking all tracked accounts now...")
    await run_check_cycle(context.application)
    await update.message.reply_text("✅ Check complete. Use /list to see results.")


# ----------------------------------------------------------------------
# BACKGROUND CHECK LOOP
# ----------------------------------------------------------------------

async def run_check_cycle(application: Application):
    accounts = get_all_tracked_accounts()
    # Dedup usernames to avoid redundant HTTP requests when multiple chats
    # track the same account.
    seen_usernames = {}

    for acc in accounts:
        username = acc["username"]

        if username not in seen_usernames:
            try:
                count = fetch_follower_count(username)
                record_follower_count(username, count)
                seen_usernames[username] = count
            except ProfileNotFound:
                logger.warning("Profile not found during check: %s", username)
                continue
            except ScrapeError as e:
                logger.error("Scrape error for %s: %s", username, e)
                continue
            except requests.RequestException as e:
                logger.error("Network error for %s: %s", username, e)
                continue
        else:
            count = seen_usernames[username]

        history = get_last_two_counts(username)
        if len(history) < 2:
            continue

        current = history[0]["follower_count"]
        previous = history[1]["follower_count"]
        chat_id = acc["chat_id"]

        # Drop alert
        if current < previous:
            drop = previous - current
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📉 @{username} lost followers\n"
                        f"{previous:,} → {current:,} ({-drop:,})"
                    ),
                )
            except Exception as e:
                logger.error("Failed to send drop alert: %s", e)

        # Milestone alert
        milestone = crossed_milestone(previous, current, acc["last_milestone_notified"])
        if milestone:
            update_last_milestone(chat_id, username, milestone)
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎉 @{username} just crossed {format_count(milestone)} followers!\n"
                        f"Current: {current:,}"
                    ),
                )
            except Exception as e:
                logger.error("Failed to send milestone alert: %s", e)


async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running scheduled check cycle...")
    await run_check_cycle(context.application)


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    if TELEGRAM_BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN as an environment variable, or edit it "
            "directly in bot.py before running."
        )

    init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("track", cmd_track))
    application.add_handler(CommandHandler("untrack", cmd_untrack))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("check", cmd_check))

    # Schedule periodic background checks
    application.job_queue.run_repeating(
        periodic_check, interval=CHECK_INTERVAL_SECONDS, first=CHECK_INTERVAL_SECONDS
    )

    logger.info("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
