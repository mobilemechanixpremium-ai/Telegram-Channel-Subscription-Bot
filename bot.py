"""
MobileMechanix Subscription Bot
Python / pyTelegramBotAPI / MongoDB Atlas / Render

Important:
- One running instance only: Telegram long polling cannot run in two processes with the same token.
- UPI payment is manually verified by the administrator. The bot does not verify UPI payments automatically.
- Add the bot as an administrator to each private Telegram channel with Invite Users and Ban Users permissions.
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ---------- Configuration ----------
def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

BOT_TOKEN = required_env("BOT_TOKEN")
MONGO_URI = required_env("MONGO_URI")
ADMIN_ID = int(required_env("ADMIN_ID"))
UPI_ID = required_env("UPI_ID")
CONTACT_USERNAME = required_env("CONTACT_USERNAME").lstrip("@")
DB_NAME = os.getenv("DB_NAME", "sub_management")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mobilemechanix")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=15000,
    connectTimeoutMS=15000,
    socketTimeoutMS=20000,
    appname="MobileMechanixSubscriptionBot",
)
db = mongo[DB_NAME]
channels_col = db["channels"]
users_col = db["users"]
payments_col = db["payments"]

# Helpful indexes; safe to run at startup.
channels_col.create_index([("channel_id", ASCENDING)], unique=True)
users_col.create_index([("channel_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
users_col.create_index([("expiry", ASCENDING)])
payments_col.create_index([("status", ASCENDING), ("created_at", ASCENDING)])

# ---------- Health endpoint for Render / UptimeRobot ----------
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({"service": "MobileMechanix", "status": "ok"}), 200

@app.get("/health")
def health_detail():
    try:
        mongo.admin.command("ping")
        return jsonify({"service": "MobileMechanix", "status": "ok", "database": "ok"}), 200
    except PyMongoError:
        log.exception("Health check: MongoDB ping failed")
        return jsonify({"service": "MobileMechanix", "status": "degraded", "database": "error"}), 503

def run_web():
    port = int(os.getenv("PORT", "10000"))
    # Flask's development server is sufficient for this small Render health endpoint.
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ---------- Helpers ----------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def safe_answer(call, text=None, alert=False):
    try:
        bot.answer_callback_query(call.id, text=text or "", show_alert=alert)
    except Exception:
        log.debug("Could not answer callback", exc_info=True)

def get_channel(channel_id: int):
    return channels_col.find_one({"channel_id": int(channel_id), "admin_id": ADMIN_ID})

def plan_label(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        if days % 30 == 0:
            months = days // 30
            return f"{months} month" + ("" if months == 1 else "s")
        return f"{days} day" + ("" if days == 1 else "s")
    if minutes % 60 == 0:
        return f"{minutes // 60} hour" + ("" if minutes == 60 else "s")
    return f"{minutes} minutes"

def make_start_link(channel_id: int) -> str:
    return f"https://t.me/{bot.get_me().username}?start={channel_id}"

def parse_plans(raw: str) -> dict:
    plans = {}
    for item in raw.split(","):
        parts = item.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid plan entry: {item!r}. Use minutes:price.")
        mins_text, price_text = parts[0].strip(), parts[1].strip()
        if not mins_text.isdigit() or int(mins_text) <= 0:
            raise ValueError(f"Invalid duration: {mins_text!r}")
        try:
            price = float(price_text)
        except ValueError:
            raise ValueError(f"Invalid price: {price_text!r}")
        if price <= 0 or price > 10000000:
            raise ValueError("Prices must be greater than 0 and within a reasonable limit.")
        mins = int(mins_text)
        if str(mins) in plans:
            raise ValueError(f"Duplicate duration: {mins}")
        plans[str(mins)] = int(price) if price.is_integer() else price
    if not plans:
        raise ValueError("Enter at least one plan.")
    if len(plans) > 8:
        raise ValueError("Maximum 8 plans per channel.")
    return plans

def create_plan_keyboard(channel_doc):
    markup = InlineKeyboardMarkup(row_width=1)
    for mins_text, price in sorted(channel_doc["plans"].items(), key=lambda x: int(x[0])):
        markup.add(InlineKeyboardButton(
            f"💳 {plan_label(int(mins_text))} — ₹{price}",
            callback_data=f"select:{channel_doc['channel_id']}:{mins_text}",
        ))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    return markup

# ---------- Start and admin commands ----------
@bot.message_handler(commands=["start"])
def start_handler(message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 2:
        try:
            channel_id = int(args[1])
            channel_doc = get_channel(channel_id)
            if channel_doc:
                bot.send_message(
                    message.chat.id,
                    f"Welcome!\n\nChannel: {channel_doc['name']}\n\nChoose a subscription plan:",
                    reply_markup=create_plan_keyboard(channel_doc),
                )
                return
        except (ValueError, PyMongoError):
            log.exception("Failed to process start deep link")
            bot.send_message(message.chat.id, "Sorry, the subscription service is temporarily unavailable.")
            return

    if is_admin(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "✅ MobileMechanix Admin Panel\n\n"
            "/add — add or update a channel\n"
            "/channels — list managed channels\n"
            "/pending — show pending payments\n"
            "/health — test bot/database health",
        )
    else:
        bot.send_message(message.chat.id, "Welcome! Open the subscription link shared by the channel administrator.")

@bot.message_handler(commands=["health"])
def health_command(message):
    if not is_admin(message.from_user.id):
        return
    try:
        mongo.admin.command("ping")
        bot.reply_to(message, "✅ Bot handler is responding and MongoDB is reachable.")
    except PyMongoError:
        log.exception("Admin health command failed")
        bot.reply_to(message, "❌ MongoDB connection failed. Check Render logs and MONGO_URI.")

@bot.message_handler(commands=["channels"], func=lambda m: is_admin(m.from_user.id))
def list_channels(message):
    markup = InlineKeyboardMarkup(row_width=1)
    docs = list(channels_col.find({"admin_id": ADMIN_ID}).sort("name", ASCENDING))
    for doc in docs:
        markup.add(InlineKeyboardButton(
            f"📢 {doc['name']}", callback_data=f"manage:{doc['channel_id']}"
        ))
    markup.add(InlineKeyboardButton("➕ Add New Channel", callback_data="add_new"))
    bot.send_message(message.chat.id, "Your managed channels:" if docs else "No channels yet. Add one below.", reply_markup=markup)

@bot.message_handler(commands=["add"], func=lambda m: is_admin(m.from_user.id))
def add_channel_start(message):
    msg = bot.send_message(
        message.chat.id,
        "Forward a message from the target channel here.\n"
        "Make sure this bot is an administrator in that channel.",
    )
    bot.register_next_step_handler(msg, get_channel_from_forward)

@bot.callback_query_handler(func=lambda c: c.data == "add_new")
def add_new_callback(call):
    safe_answer(call)
    if not is_admin(call.from_user.id):
        return
    msg = bot.send_message(call.message.chat.id, "Forward a message from the target channel here.")
    bot.register_next_step_handler(msg, get_channel_from_forward)

def get_channel_from_forward(message):
    if not is_admin(message.from_user.id):
        return
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if not forwarded_chat:
        bot.send_message(message.chat.id, "❌ I couldn't detect a channel. Forward a channel message and try /add again.")
        return
    if forwarded_chat.type != "channel":
        bot.send_message(message.chat.id, "Please forward a message from a Telegram channel.")
        return
    channel_id = int(forwarded_chat.id)
    channel_name = forwarded_chat.title or str(channel_id)
    msg = bot.send_message(
        message.chat.id,
        f"Detected: {channel_name}\n\n"
        "Enter plan durations in minutes and prices, separated by commas.\n"
        "Example: 43200:599, 86400:1199, 129600:1500",
    )
    bot.register_next_step_handler(msg, save_channel_plans, channel_id, channel_name)

def save_channel_plans(message, channel_id, channel_name):
    if not is_admin(message.from_user.id):
        return
    try:
        plans = parse_plans(message.text or "")
        channels_col.update_one(
            {"channel_id": channel_id},
            {"$set": {"name": channel_name, "plans": plans, "admin_id": ADMIN_ID, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        bot.send_message(
            message.chat.id,
            f"✅ Channel saved: {channel_name}\nPlans saved: {len(plans)}\n\n"
            f"Subscription link:\n{make_start_link(channel_id)}",
        )
    except (ValueError, PyMongoError) as exc:
        log.exception("Saving channel failed")
        bot.send_message(message.chat.id, f"❌ Could not save plans: {str(exc)[:250]}\n\nExample: 43200:599, 86400:1199, 129600:1500")

@bot.callback_query_handler(func=lambda c: c.data.startswith("manage:"))
def manage_channel(call):
    safe_answer(call)
    if not is_admin(call.from_user.id):
        return
    try:
        channel_id = int(call.data.split(":", 1)[1])
        channel_doc = get_channel(channel_id)
        if not channel_doc:
            bot.send_message(call.message.chat.id, "Channel not found.")
            return
        bot.send_message(
            call.message.chat.id,
            f"📢 {channel_doc['name']}\n\nSubscription link:\n{make_start_link(channel_id)}\n\n"
            "To update prices, use /add and forward a message from the channel again.",
        )
    except Exception:
        log.exception("Manage channel failed")
        safe_answer(call, "Unable to open channel settings.", alert=True)

# ---------- Subscription selection and manual payment verification ----------
@bot.callback_query_handler(func=lambda c: c.data.startswith("select:"))
def select_plan(call):
    safe_answer(call)
    try:
        _, channel_text, mins_text = call.data.split(":")
        channel_id, minutes = int(channel_text), int(mins_text)
        channel_doc = get_channel(channel_id)
        if not channel_doc or mins_text not in channel_doc.get("plans", {}):
            bot.send_message(call.message.chat.id, "This plan is no longer available. Reopen the subscription link.")
            return
        price = channel_doc["plans"][mins_text]
        # UPI URI is encoded before embedding into the QR service URL.
        upi_uri = f"upi://pay?pa={UPI_ID}&am={price}&cu=INR&tn=MobileMechanix"
        qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=320x320&data=" + quote(upi_uri, safe="")
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid:{channel_id}:{minutes}"))
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
        bot.send_photo(
            call.message.chat.id, qr_url,
            caption=f"Plan: {plan_label(minutes)}\nAmount: ₹{price}\nUPI ID: {UPI_ID}\n\n"
                    "Pay using your UPI app, then tap “I Have Paid”. Payment must be verified by the administrator.",
            reply_markup=markup,
        )
    except Exception:
        log.exception("Plan selection failed")
        safe_answer(call, "Could not open this plan. Please try again.", alert=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("paid:"))
def payment_claim(call):
    safe_answer(call, "Your request was sent to the administrator.")
    try:
        _, channel_text, mins_text = call.data.split(":")
        channel_id, minutes = int(channel_text), int(mins_text)
        channel_doc = get_channel(channel_id)
        if not channel_doc or mins_text not in channel_doc.get("plans", {}):
            bot.send_message(call.message.chat.id, "This plan is unavailable.")
            return
        # Prevent duplicate pending requests for same user/channel/plan.
        existing = payments_col.find_one({
            "user_id": call.from_user.id, "channel_id": channel_id,
            "minutes": minutes, "status": "pending"
        })
        if existing:
            bot.send_message(call.message.chat.id, "You already have a pending payment request for this plan.")
            return
        price = channel_doc["plans"][mins_text]
        payment_id = payments_col.insert_one({
            "user_id": call.from_user.id,
            "username": call.from_user.username,
            "first_name": call.from_user.first_name,
            "channel_id": channel_id,
            "channel_name": channel_doc["name"],
            "minutes": minutes,
            "price": price,
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }).inserted_id
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{payment_id}"),
        )
        bot.send_message(
            ADMIN_ID,
            f"🔔 Payment verification required\n\n"
            f"User: {call.from_user.first_name} (@{call.from_user.username or 'no_username'})\n"
            f"User ID: {call.from_user.id}\nChannel: {channel_doc['name']}\n"
            f"Plan: {plan_label(minutes)}\nAmount: ₹{price}\nPayment request: {payment_id}\n\n"
            "Verify the payment in your UPI app before approving.",
            reply_markup=markup,
        )
        bot.send_message(call.message.chat.id, "Your payment claim was sent. Please wait for the administrator to verify it.")
    except Exception:
        log.exception("Payment claim failed")
        bot.send_message(call.message.chat.id, "Could not submit your payment request. Please try again later.")

@bot.message_handler(commands=["pending"], func=lambda m: is_admin(m.from_user.id))
def pending_payments(message):
    rows = list(payments_col.find({"status": "pending"}).sort("created_at", ASCENDING).limit(20))
    if not rows:
        bot.send_message(message.chat.id, "No pending payment requests.")
        return
    for row in rows:
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{row['_id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{row['_id']}"),
        )
        bot.send_message(
            message.chat.id,
            f"Pending payment\nUser ID: {row['user_id']}\nChannel: {row['channel_name']}\n"
            f"Plan: {plan_label(row['minutes'])}\nAmount: ₹{row['price']}\nID: {row['_id']}",
            reply_markup=markup,
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith("approve:"))
def approve_payment(call):
    safe_answer(call)
    if not is_admin(call.from_user.id):
        return
    try:
        from bson import ObjectId
        payment_id = ObjectId(call.data.split(":", 1)[1])
        payment = payments_col.find_one({"_id": payment_id, "status": "pending"})
        if not payment:
            bot.send_message(call.message.chat.id, "This payment request was already processed or could not be found.")
            return
        channel_doc = get_channel(payment["channel_id"])
        if not channel_doc:
            bot.send_message(call.message.chat.id, "Channel is no longer configured. Payment was not approved.")
            return
        minutes = int(payment["minutes"])
        expiry = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        # The bot must be an administrator with Invite Users permission.
        invite = bot.create_chat_invite_link(
            chat_id=payment["channel_id"],
            expire_date=int(expiry.timestamp()),
            member_limit=1,
            creates_join_request=False,
        )
        users_col.update_one(
            {"user_id": payment["user_id"], "channel_id": payment["channel_id"]},
            {"$set": {
                "user_id": payment["user_id"], "channel_id": payment["channel_id"],
                "expiry": expiry.timestamp(), "invite_link": invite.invite_link,
                "status": "active", "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        payments_col.update_one(
            {"_id": payment_id, "status": "pending"},
            {"$set": {"status": "approved", "approved_at": datetime.now(timezone.utc), "approved_by": ADMIN_ID}},
        )
        try:
            bot.send_message(
                payment["user_id"],
                f"🥳 Payment approved!\nChannel: {channel_doc['name']}\n"
                f"Subscription: {plan_label(minutes)}\nJoin link (one use, expires with the subscription):\n{invite.invite_link}\n\n"
                "Your channel access will be removed when the subscription expires.",
            )
        except Exception:
            log.exception("Approved, but could not DM user")
        bot.send_message(call.message.chat.id, f"✅ Approved user {payment['user_id']} for {plan_label(minutes)}.")
    except Exception:
        log.exception("Approving payment failed")
        bot.send_message(call.message.chat.id, "❌ Approval failed. Check Render logs and ensure the bot has channel admin permissions.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("reject:"))
def reject_payment(call):
    safe_answer(call)
    if not is_admin(call.from_user.id):
        return
    try:
        from bson import ObjectId
        payment_id = ObjectId(call.data.split(":", 1)[1])
        payment = payments_col.find_one_and_update(
            {"_id": payment_id, "status": "pending"},
            {"$set": {"status": "rejected", "rejected_at": datetime.now(timezone.utc), "rejected_by": ADMIN_ID}},
        )
        if not payment:
            bot.send_message(call.message.chat.id, "This request was already processed or not found.")
            return
        try:
            bot.send_message(payment["user_id"], "Your payment request was rejected. Please contact the administrator if you believe this is an error.")
        except Exception:
            log.exception("Could not notify rejected user")
        bot.send_message(call.message.chat.id, "❌ Payment request rejected.")
    except Exception:
        log.exception("Rejecting payment failed")
        bot.send_message(call.message.chat.id, "Could not reject this request. Check Render logs.")

# ---------- Expiry cleanup ----------
def kick_expired_users():
    now_ts = datetime.now(timezone.utc).timestamp()
    cursor = users_col.find({"expiry": {"$lte": now_ts}, "status": "active"}).limit(100)
    for user in cursor:
        try:
            # Ban then unban removes membership but allows future rejoining.
            bot.ban_chat_member(user["channel_id"], user["user_id"])
            bot.unban_chat_member(user["channel_id"], user["user_id"])
            try:
                bot.send_message(
                    user["user_id"],
                    "⚠️ Your subscription has expired.\n\nUse the original subscription link to renew.",
                )
            except Exception:
                log.info("Could not notify expired user %s", user["user_id"])
            users_col.update_one(
                {"_id": user["_id"], "status": "active"},
                {"$set": {"status": "expired", "expired_at": datetime.now(timezone.utc)}},
            )
        except Exception:
            # Keep the record active so it can be retried next run.
            log.exception("Failed to remove expired user %s from channel %s", user.get("user_id"), user.get("channel_id"))

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(kick_expired_users, "interval", minutes=1, id="expiry_cleanup", max_instances=1, coalesce=True)

# ---------- Startup ----------
def main():
    # Fail early on invalid database credentials/network configuration.
    mongo.admin.command("ping")
    me = bot.get_me()
    log.info("Starting bot @%s", me.username)
    # Do not start multiple polling processes with this same token.
    bot.remove_webhook()
    web_thread = threading.Thread(target=run_web, name="health-server", daemon=True)
    web_thread.start()
    scheduler.start()
    try:
        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=25,
            skip_pending=True,
            allowed_updates=["message", "callback_query", "channel_post"],
        )
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        mongo.close()

if __name__ == "__main__":
    main()
