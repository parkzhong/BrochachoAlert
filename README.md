# Brochacos Telegram Leaderboard Watcher

A small Telegram bot that checks the AMD Developer Hackathon ACT II live leaderboard and alerts subscribed chats whenever the Brochacos/Brocacho entry changes.

## 1. Create the Telegram bot

1. Open Telegram and message **@BotFather**.
2. Run `/newbot` and follow the instructions.
3. Copy the token BotFather gives you.

## 2. Run locally

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python bot.py
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

Put your BotFather token in `.env` before running.

## 3. Subscribe

Open your new bot in Telegram and send:

```text
/start
```

Useful commands:

- `/status` — check the current entry immediately
- `/stop` — unsubscribe that chat

The first scheduled check establishes the baseline. Alerts begin when a later check differs in rank, token count, accuracy, status, team/submission text, or nearby page context.

## Hosting

The program must remain running to send alerts. You can run it on your PC, a VPS, Railway, Render background worker, Fly.io, or another always-on Python host.

Docker:

```bash
docker build -t brochacos-watcher .
docker run -d --restart unless-stopped --env-file .env -v brochacos-data:/app/data brochacos-watcher
```

## Notes

- The supplied URL uses `track=2`, but currently renders the Track 1 General-Purpose AI Agent leaderboard.
- The current listed submission name is `Brocacho caption`, so the default aliases include that spelling plus `Brochacos` and `Brocacho`.
- The checker defaults to once every five minutes. Do not set it below 60 seconds.
