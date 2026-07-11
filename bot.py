from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
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
TRACK_NUMBER = os.getenv("TRACK_NUMBER", "1").strip()
TRACK_TITLES = {
    "1": "General-Purpose AI Agent",
    "2": "Video Captioning",
    "3": "Unicorn (Open Innovation)",
}
TRACK_TITLE = os.getenv("TRACK_TITLE", TRACK_TITLES.get(TRACK_NUMBER, "Unknown Track"))
LEADERBOARD_URL = os.getenv(
    "LEADERBOARD_URL",
    f"https://lablab.ai/ai-hackathons/amd-developer-hackathon-act-ii/live?track={TRACK_NUMBER}",
)
TARGET_NAMES = [
    name.strip()
    for name in os.getenv("TARGET_NAMES", "Brocacho AI Agent,Brocacho caption,Brochacos,Brocacho").split(",")
    if name.strip()
]
CHECK_INTERVAL_SECONDS = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
FIXED_CHAT_IDS = {int(x.strip()) for x in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()}
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
    last_scored: str | None
    last_submitted: str | None
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
    # Railway redeploys can erase local files. TELEGRAM_CHAT_IDS provides a persistent fallback.
    saved = {int(chat_id) for chat_id in load_json(SUBSCRIBERS_FILE, [])}
    return saved | FIXED_CHAT_IDS


def save_subscribers(chat_ids: Iterable[int]) -> None:
    save_json(SUBSCRIBERS_FILE, sorted(set(chat_ids)))


def fetch_page() -> str:
    # Add a cache-busting query parameter so the CDN does not return an old leaderboard snapshot.
    separator = "&" if "?" in LEADERBOARD_URL else "?"
    fresh_url = f"{LEADERBOARD_URL}{separator}_={int(time.time())}"
    response = requests.get(
        fresh_url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; BrochacosLeaderboardWatcher/1.0; "
                "+https://telegram.org/)"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
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


def _all_element_text(element) -> str:
    """Return visible text plus useful tooltip/accessibility attributes."""
    parts = [element.get_text(" ", strip=True)]
    for node in [element, *element.find_all(True)]:
        for attr in ("title", "aria-label", "data-tooltip-content", "datetime"):
            value = node.get(attr)
            if value:
                parts.append(str(value))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _find_target_container(soup: BeautifulSoup):
    aliases = sorted(TARGET_NAMES, key=len, reverse=True)
    # Prefer exact/long aliases so a generic word such as "Brocacho" cannot
    # accidentally match unrelated page content before the actual submission.
    candidates = []
    for text_node in soup.find_all(string=True):
        text = re.sub(r"\s+", " ", str(text_node)).strip()
        if not text:
            continue
        folded = text.casefold()
        matched = next((a for a in aliases if a.casefold() in folded), None)
        if matched:
            candidates.append((len(matched), text_node))
    if not candidates:
        raise LookupError(f"None of the target names were found: {', '.join(TARGET_NAMES)}")

    _, node = max(candidates, key=lambda item: item[0])
    element = node.parent
    status_pattern = re.compile(
        r"ACCURACY_GATE_FAILED|INFRA_ERROR|PULL_ERROR|OUTPUT_MALFORMED|"
        r"OUTPUT_MISSING|INVALID_RESULTS_SCHEMA|TIMEOUT|RUNTIME_ERROR"
    )

    # Walk upward until the complete card/row is captured. Stop at a reasonably
    # small container to avoid borrowing values from neighbouring submissions.
    best = element
    for _ in range(8):
        if element is None:
            break
        blob = _all_element_text(element)
        if status_pattern.search(blob) or re.search(r"\btokens?\b.*\baccuracy\b", blob, re.I):
            best = element
            # Timestamp tooltips are often one or two wrappers higher.
            if re.search(r"submitted|resubmitted|scored|checked", blob, re.I):
                break
        if len(blob) > 2500:
            break
        element = element.parent
    return best


def _extract_timestamp(blob: str, labels: tuple[str, ...]) -> str | None:
    # Examples currently used by the site include:
    # "last resubmitted Jul 11, 22:52 GMT+8" and "checked Jul 11, 22:34 GMT+8".
    label_group = "|".join(re.escape(x) for x in labels)
    patterns = (
        rf"(?:{label_group})\s*[:\-]?\s*([A-Z][a-z]{{2}}\s+\d{{1,2}},?\s+\d{{1,2}}:\d{{2}}(?:\s*[AP]M)?(?:\s*GMT[+-]\d{{1,2}})?)",
        rf"(?:{label_group})\s*[:\-]?\s*(\d{{4}}-\d{{2}}-\d{{2}}[T ]\d{{2}}:\d{{2}}(?::\d{{2}})?(?:\.\d+)?(?:Z|[+-]\d{{2}}:\d{{2}})?)",
    )
    for pattern in patterns:
        match = re.search(pattern, blob, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def parse_snapshot(html: str) -> LeaderboardSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    container = _find_target_container(soup)
    blob = _all_element_text(container)

    status_values = (
        "ACCURACY_GATE_FAILED",
        "INFRA_ERROR",
        "PULL_ERROR",
        "OUTPUT_MALFORMED",
        "OUTPUT_MISSING",
        "INVALID_RESULTS_SCHEMA",
        "TIMEOUT",
        "RUNTIME_ERROR",
    )
    status = next((value for value in status_values if re.search(rf"\b{re.escape(value)}\b", blob)), None)

    # Use the longest configured alias present as the displayed target name.
    submission = next(
        (alias for alias in sorted(TARGET_NAMES, key=len, reverse=True) if alias.casefold() in blob.casefold()),
        TARGET_NAMES[0],
    )

    rank = None
    tokens = None
    accuracy = None
    team = None

    rank_match = re.search(r"(?:^|\s)(?:0)?(\d{1,4})(?:\s|\.)", blob)
    token_match = re.search(r"([\d,]+)\s+tokens?", blob, re.I)
    accuracy_match = re.search(r"(\d+(?:\.\d+)?)%\s*(?:accuracy)?", blob, re.I)

    if status is None:
        if rank_match:
            rank = int(rank_match.group(1))
        if token_match:
            tokens = int(token_match.group(1).replace(",", ""))
        if accuracy_match:
            accuracy = float(accuracy_match.group(1))
    elif status == "ACCURACY_GATE_FAILED" and accuracy_match:
        accuracy = float(accuracy_match.group(1))

    last_submitted = _extract_timestamp(
        blob,
        ("last resubmitted", "last submitted", "submitted"),
    )
    last_scored = _extract_timestamp(
        blob,
        ("last scored", "scored", "checked"),
    )

    # Fallback: the timestamp wrapper may sit just outside the card chosen above.
    if (last_submitted is None or last_scored is None) and container.parent is not None:
        parent_blob = _all_element_text(container.parent)
        last_submitted = last_submitted or _extract_timestamp(
            parent_blob, ("last resubmitted", "last submitted", "submitted")
        )
        last_scored = last_scored or _extract_timestamp(
            parent_blob, ("last scored", "scored", "checked")
        )

    return LeaderboardSnapshot(
        target=submission,
        rank=rank,
        submission=submission,
        team=team,
        tokens=tokens,
        accuracy=accuracy,
        status=status,
        last_scored=last_scored,
        last_submitted=last_submitted,
        context=blob[:1200],
    )


def get_snapshot() -> LeaderboardSnapshot:
    return parse_snapshot(fetch_page())


def describe(snapshot: LeaderboardSnapshot) -> str:
    fields = [
        f"<b>{snapshot.target}</b>",
        f"Track: <b>{TRACK_NUMBER} — {TRACK_TITLE}</b>",
    ]
    fields.append(f"Rank: <b>#{snapshot.rank}</b>" if snapshot.rank else "Rank: not currently ranked")
    if snapshot.tokens is not None:
        fields.append(f"Tokens: <b>{snapshot.tokens:,}</b>")
    if snapshot.accuracy is not None:
        fields.append(f"Accuracy: <b>{snapshot.accuracy:.1f}%</b>")
    fields.append(f"Status: <b>{snapshot.status or 'No status shown'}</b>")
    if snapshot.last_scored:
        fields.append(f"Last scored: <b>{snapshot.last_scored}</b>")
    if snapshot.last_submitted:
        fields.append(f"Last submitted: <b>{snapshot.last_submitted}</b>")
    fields.append(f'<a href="{LEADERBOARD_URL}">Open leaderboard</a>')
    return "\n".join(fields)


def _display_value(field: str, value) -> str:
    if value is None:
        return "—"
    if field == "rank":
        return f"#{value}"
    if field == "tokens":
        return f"{value:,}"
    if field == "accuracy":
        return f"{value:.1f}%"
    return str(value)


def describe_changes(previous: LeaderboardSnapshot, current: LeaderboardSnapshot) -> str:
    labels = {
        "rank": "Rank",
        "tokens": "Tokens",
        "accuracy": "Accuracy",
        "status": "Status",
        "submission": "Submission",
        "team": "Team",
        "last_scored": "Last scored",
        "last_submitted": "Last submitted",
    }
    changes = []
    for field, label in labels.items():
        old = getattr(previous, field)
        new = getattr(current, field)
        if old != new:
            changes.append(
                f"• <b>{label}:</b> {_display_value(field, old)} → {_display_value(field, new)}"
            )
    return "\n".join(changes) if changes else "• Leaderboard entry changed"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    chat_ids = subscribers()
    chat_ids.add(update.effective_chat.id)
    save_subscribers(chat_ids)
    await update.message.reply_text(
        f"✅ Subscribed to Brochacos updates for Track {TRACK_NUMBER} — {TRACK_TITLE}.\n"
        f"Chat ID: <code>{update.effective_chat.id}</code>\n\n"
        "For reliable alerts after Railway redeploys, save this ID in the Railway variable "
        "<code>TELEGRAM_CHAT_IDS</code>.\n\n"
        "Commands:\n/status — check now\n/stop — unsubscribe",
        parse_mode=ParseMode.HTML
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
        previous_snapshot = None
        if previous_data and previous_data.get("snapshot"):
            try:
                previous_snapshot = LeaderboardSnapshot(**previous_data["snapshot"])
            except TypeError:
                logger.warning("Ignoring incompatible saved snapshot")

        save_json(
            STATE_FILE,
            {"fingerprint": current.fingerprint, "snapshot": asdict(current)},
        )

        # First successful check establishes a baseline without sending an alert.
        if previous_fingerprint is None or previous_fingerprint == current.fingerprint:
            return

        changes = (
            describe_changes(previous_snapshot, current)
            if previous_snapshot is not None
            else "• Leaderboard entry changed"
        )
        message = (
            f"🚨 <b>Brochacos update — Track {TRACK_NUMBER}</b>\n"
            f"<b>{TRACK_TITLE}</b>\n\n"
            f"<b>Changes</b>\n{changes}\n\n"
            + describe(current)
        )
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
