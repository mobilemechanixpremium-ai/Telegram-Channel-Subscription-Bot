# MobileMechanix Subscription Bot

A Telegram subscription bot for private channels using GitHub, Render, MongoDB Atlas and manual UPI verification.

## Features

- Admin-only channel setup and plan editing
- Subscription plans stored in MongoDB Atlas
- UPI QR checkout and manual admin approval (not automatic payment verification)
- One-use invite links that expire with the subscription duration
- Scheduled expired-member removal
- Pending-payment list and duplicate-pending-request prevention
- `/health` web endpoint and `/health` Telegram admin command
- Startup checks and useful logs

## Important limitations

1. **Run only one instance** of this bot for a given Telegram bot token. Multiple polling processes cause Telegram `409 Conflict`.
2. The bot must be a channel administrator with permission to invite users and ban/remove members.
3. UPI payment is verified by the human admin in their UPI app. The “I Have Paid” button is only a claim.
4. Telegram bot API limitations apply. An invite link expiring does not itself remove an already joined user; the scheduled expiry job handles removal.
5. Render Free services may spin down or have limited availability. UptimeRobot cannot guarantee a free service remains continuously awake, and frequent pings do not replace an always-on worker. See Render plan limits.
6. This project is a practical starting point, not a guarantee of production-grade financial security. Test with a private test channel before selling access.

## Files

- `bot.py` — bot and health server
- `requirements.txt` — dependencies
- `render.yaml` — optional Render blueprint
- `.gitignore` — excludes local secrets

## 1. Create / check your Telegram bot

1. Use BotFather to create the bot and copy its token.
2. Open the bot once and send `/start`.
3. Record your numeric Telegram user ID as `ADMIN_ID` (use a trusted ID tool or a bot you already trust).
4. Add the bot to your private channel as an administrator. Grant invite-user and ban-user permissions.

## 2. MongoDB Atlas

1. Create or select a cluster.
2. Under **Database Access**, create a database user. This is not your Atlas website login.
3. Give it read/write access to the `sub_management` database.
4. Under **Network Access**, allow connections from Render. If Render does not provide stable outbound IPs for your plan, Atlas may require a broader access entry; understand the risk and restrict it where possible. Do not expose database credentials.
5. In Atlas, choose **Connect → Drivers** and copy the URI. Replace the password placeholder with the database user's password. URL-encode reserved characters in the password.
6. Database name used by default: `sub_management`.

## 3. Upload to GitHub

1. Create a new repository, e.g. `mobilemechanix-subscription-bot`.
2. Upload `bot.py`, `requirements.txt`, `render.yaml`, `README.md`, and `.gitignore`.
3. Never upload `.env`, bot tokens, MongoDB URIs, screenshots containing secrets, or database passwords.
4. If a secret was ever committed to GitHub, rotate it immediately; deleting it from the latest commit is not enough.

## 4. Deploy on Render

Create a **Web Service** connected to the GitHub repository.

- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Health check path: `/health` (optional)

Add these environment variables in Render → service → Environment:

| Key | Value |
|---|---|
| `BOT_TOKEN` | BotFather token |
| `MONGO_URI` | Full Atlas driver URI |
| `ADMIN_ID` | Your numeric Telegram user ID |
| `UPI_ID` | Your UPI ID, e.g. `name@bank` |
| `CONTACT_USERNAME` | Admin Telegram username without `@` |
| `DB_NAME` | `sub_management` (optional) |
| `LOG_LEVEL` | `INFO` (optional) |

Save and deploy. Check Logs. The startup log should say `Starting bot @...`. If MongoDB fails, verify URI/password, database user privileges and Atlas IP access list.

**Start only one service/instance** with this bot token. If you previously deployed the same token elsewhere, stop the old process first.

## 5. Test in this order

1. Open the Render service URL. `/` should return `{"service":"MobileMechanix","status":"ok"}`.
2. Open `/health`. It should return status `ok` and database `ok`.
3. Open the bot in Telegram and send `/start`.
4. As admin, send `/health`; expect a successful database ping.
5. Send `/add`, forward a message from the private channel, then enter:
   `43200:599, 86400:1199, 129600:1500`
6. Send `/channels` and test the subscription link from a non-admin test account.
7. Test a low-priced/short-duration plan in a private test channel. Confirm the admin receives a payment request, approve it only after checking the UPI transaction, and verify the invite works.
8. Verify the test user is removed after the expiry time. Do not test expiry with paying customers.

## 6. UptimeRobot

1. Create an HTTP(s) monitor.
2. Monitor the Render service URL, or preferably `/health`.
3. Use a reasonable monitoring interval supported by your UptimeRobot plan.
4. Configure email alerts.
5. UptimeRobot checks whether the HTTP endpoint responds; it does not confirm that Telegram polling is healthy. Check Render logs too.

## Troubleshooting

### Telegram 409 Conflict
- Stop old local Python runs and any duplicate Render service/worker.
- Ensure only one process calls `bot.infinity_polling()` for the token.
- Check the Render service instance/worker count and startup logs.

### MongoDB authentication failed
- Reset the **database user's** password in Atlas Database Access.
- Update `MONGO_URI` in Render Environment and redeploy.
- URL-encode reserved characters in the password.
- Do not paste the URI or password into public chats.

### Bot cannot create invite links or remove members
- Add the bot as a channel admin and grant invite users and ban users permissions.
- Confirm the stored channel ID belongs to the private channel.

### Render health endpoint works but bot doesn't respond
- Review logs for token, network, MongoDB, and Telegram errors.
- Confirm `BOT_TOKEN` is correct and the bot has not been blocked.
- Confirm only one polling process is running.

## Security checklist

- Keep all secrets in Render Environment, not GitHub.
- Use a unique strong MongoDB database-user password.
- Give the database user only the permissions it needs.
- Do not approve payment claims without verifying the transaction in your UPI app.
- Restrict who can access admin commands; this code checks the configured numeric `ADMIN_ID`.
- Test all channel invite/expiry flows in a private test channel first.
