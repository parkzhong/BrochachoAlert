from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LEADERBOARD_URL = os.getenv(
    "LEADERBOARD_URL",
    "https://lablab.ai/ai-hackathons/amd-developer-hackathon-act-ii/live?track=2",
)
TARGET_NAMES = [
    name.strip()
    for name in os.getenv("TARGET_NAMES", "Brocacho caption,Brochacos,Brocacho").split(",")
    if name.strip()
]
CHECK_INTERVAL_SECONDS = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "300")))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "state.json"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("brochacos-watcher")


@dataclass(frozen=True)
class LeaderboardSnapshot:
    target: str
    rank: int | None
    submission: str | None
    team: str | None
    tokens: int | None
    accuracy: float | None
    status: str | None
    context: str

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def subscribers() -> set[int]:
    return {int(chat_id) for chat_id in load_json(SUBSCRIBERS_FILE, [])}


def save_subscribers(chat_ids: Iterable[int]) -> None:
    save_json(SUBSCRIBERS_FILE, sorted(set(chat_ids)))


def fetch_page() -> str:
    response = requests.get(
        LEADERBOARD_URL,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; BrochacosLeaderboardWatcher/1.0; "
                "+https://telegram.org/)"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    response.raise_for_status()
    return response.text


def clean_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def parse_snapshot(html: str) -> LeaderboardSnapshot:
    lines = clean_lines(html)
    lowered_aliases = [alias.casefold() for alias in TARGET_NAMES]

    match_index = None
    matched_target = None
    for i, line in enumerate(lines):
        folded = line.casefold()
        if any(alias in folded for alias in lowered_aliases):
            match_index = i
            matched_target = line
            break

    if match_index is None:
        raise LookupError(
            f"None of the target names were found: {', '.join(TARGET_NAMES)}"
        )

    window = lines[max(0, match_index - 8) : min(len(lines), match_index + 12)]
    context = " | ".join(window)

    rank = None
    for line in reversed(lines[max(0, match_index - 8) : match_index]):
        m = re.fullmatch(r"0?(\d{1,4})", line)
        if m:
            rank = int(m.group(1))
            break

    tokens = None
    accuracy = None
    status = None
    status_values = (
        "ACCURACY_GATE_FAILED",
        "INFRA_ERROR",
        "PULL_ERROR",
        "OUTPUT_MALFORMED",
        "OUTPUT_MISSING",
        "TIMEOUT",
        "RUNTIME_ERROR",
    )

    for line in window:
        if tokens is None:
            m = re.search(r"([\d,]+)\s+tokens?", line, re.I)
            if m:
                tokens = int(m.group(1).replace(",", ""))
        if accuracy is None:
            m = re.search(r"(\d+(?:\.\d+)?)%\s+accuracy", line, re.I)
            if not m:
                m = re.fullmatch(r"(\d+(?:\.\d+)?)%", line)
            if m:
                accuracy = float(m.group(1))
        if status is None:
            status = next((value for value in status_values if value in line), None)

    # On ranked entries, the line after the submission is commonly the team name.
    submission = matched_target
    team = None
    if match_index + 1 < len(lines):
        possible_team = lines[match_index + 1]
        if not re.search(r"tokens?|accuracy|%|ERROR|FAILED|TIMEOUT", possible_team, re.I):
            team = possible_team

    return LeaderboardSnapshot(
        target=matched_target or TARGET_NAMES[0],
        rank=rank,
        submission=submission,
        team=team,
        tokens=tokens,
        accuracy=accuracy,
        status=status,
        context=context,
    )


def get_snapshot() -> LeaderboardSnapshot:
    return parse_snapshot(fetch_page())


def describe(snapshot: LeaderboardSnapshot) -> str:
    fields = [f"<b>{snapshot.target}</b>"]
    fields.append(f"Rank: <b>#{snapshot.rank}</b>" if snapshot.rank else "Rank: not currently ranked")
    if snapshot.tokens is not None:
        fields.append(f"Tokens: <b>{snapshot.tokens:,}</b>")
    if snapshot.accuracy is not None:
        fields.append(f"Accuracy: <b>{snapshot.accuracy:.1f}%</b>")
    if snapshot.status:
        fields.append(f"Status: <b>{snapshot.status}</b>")
    fields.append(f'<a href="{LEADERBOARD_URL}">Open leaderboard</a>')
    return "\n".join(fields)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    chat_ids = subscribers()
    chat_ids.add(update.effective_chat.id)
    save_subscribers(chat_ids)
    await update.message.reply_text(
        "✅ Subscribed. I’ll notify this chat whenever the Brochacos leaderboard entry changes.\n\n"
        "Commands:\n/status — check now\n/stop — unsubscribe"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    chat_ids = subscribers()
    chat_ids.discard(update.effective_chat.id)
    save_subscribers(chat_ids)
    await update.message.reply_text("Notifications stopped for this chat.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        snapshot = await asyncio.to_thread(get_snapshot)
        await update.message.reply_text(
            describe(snapshot), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Manual status check failed")
        await update.message.reply_text(f"Could not read the leaderboard: {exc}")


async def check_leaderboard(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        current = await asyncio.to_thread(get_snapshot)
        previous_data = load_json(STATE_FILE, None)
        previous_fingerprint = previous_data.get("fingerprint") if previous_data else None

        save_json(
            STATE_FILE,
            {"fingerprint": current.fingerprint, "snapshot": asdict(current)},
        )

        # First successful check establishes a baseline without sending an alert.
        if previous_fingerprint is None or previous_fingerprint == current.fingerprint:
            return

        message = "🚨 <b>Brochacos leaderboard updated</b>\n\n" + describe(current)
        for chat_id in subscribers():
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed sending alert to chat %s", chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled leaderboard check failed")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("stop", stop))
    application.job_queue.run_repeating(
        check_leaderboard,
        interval=CHECK_INTERVAL_SECONDS,
        first=5,
        name="leaderboard-check",
    )
    logger.info("Watching %s every %s seconds", LEADERBOARD_URL, CHECK_INTERVAL_SECONDS)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
