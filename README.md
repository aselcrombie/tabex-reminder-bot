# TabexReminder

Telegram bot that sends reminders for the Tabex (cytisine) 25-day course. Uses long polling and a background scheduler. Data is stored in a local `data.json` file (no database).

**Disclaimer:** This bot is a reminder only and does not replace medical advice or consultation with a doctor.

---

## Architecture

- **No webhooks** — long polling only
- **Single process** — bot and scheduler run in one process
- **Scheduler** — loop every 60 seconds: morning message (08:00), dose reminders, 21:00 missed check, pending (15 min) reminders
- **Storage** — `data.json` with user data (start date, timezone, current day, doses taken, etc.)

---

## Local development

1. Create a virtual environment and install dependencies:

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Set the Telegram bot token:

   ```bash
   export TELEGRAM_TOKEN=your_bot_token_here
   ```

3. Run the bot:

   ```bash
   python main.py
   ```

---

## Deployment on Render (Background Worker)

1. **Create a new Background Worker** on [Render](https://render.com):
   - New → Background Worker
   - Connect your repository (or push this project to GitHub/GitLab and connect it).

2. **Build & run:**
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`  
     (Render will use the `Procfile`: `worker: python main.py` if you use the Procfile-based setup.)

3. **Environment:**
   - Add environment variable:
     - **Key:** `TELEGRAM_TOKEN`
     - **Value:** your bot token from [@BotFather](https://t.me/BotFather)

4. **Persistent data (important):**  
   By default, Render's filesystem is ephemeral. Restarts will wipe `data.json`.

   - **TODO:** Enable **Persistent Disk** for this service in Render dashboard (if available for your plan), and store `data.json` on the mounted disk path (e.g. `/data/data.json`), or switch to an external store (e.g. S3, database) for production.

5. Deploy. The worker will start and the bot will run with long polling and the 60-second scheduler.

---

## Privacy notice (template)

- The bot stores only the data necessary for reminders: Telegram user ID, start date of the course, timezone offset, current day, doses taken, and reminder state.
- Data is stored in a single file (`data.json`) on the server (or on persistent storage if configured).
- We do not share your data with third parties. We do not use your data for analytics or advertising.
- You can stop using the bot at any time. To request deletion of your data, contact the operator (insert your contact here).
- This bot does not provide medical advice; it only sends reminders. Always follow your doctor’s instructions.

---

## Data structure (`data.json`)

```json
{
  "users": {
    "<telegram_id>": {
      "startDate": "YYYY-MM-DD",
      "timezone": "+5",
      "currentDay": 1,
      "takenToday": 0,
      "lastDoseTimestamp": null,
      "courseCompleted": false,
      "lastMorningMessageDate": null,
      "nextReminderTimestamp": null,
      "postponedReminderTimestamp": null
    }
  }
}
```

---

## Tabex schedule (intervals from last dose)

Reminders are **not** at fixed clock times. The next reminder is sent **N hours after** the user confirms the previous dose (or after the morning «Готово»).

| Days   | Doses per day | Interval between doses |
|--------|----------------|-------------------------|
| 1–3    | 6              | каждые 2 часа          |
| 4–12   | 5              | каждые 2,5 часа        |
| 13–16  | 4              | каждые 3 часа          |
| 17–20  | 3              | каждые 5 часов         |
| 21–25  | 2              | каждые 12 часов        |

---

## License

Use at your own responsibility. Not medical advice.
