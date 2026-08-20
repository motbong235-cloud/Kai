# -*- coding: utf-8 -*-
"""
Kai Gift Bot — លក់ Gift ដោយ Admin ដាក់ដោយដៃ
====================================================
លំហូរ:
  1) User /start -> ជ្រើសរើស Gift ពី catalog
  2) User វាយ username Telegram (អ្នកទទួល gift)
  3) User បញ្ជាក់ order -> ប្រព័ន្ធបង្កើត KHQR (Bakong)
  4) Bot poll ការទូទាត់ -> ពេលបានលុយ -> status = paid_pending_delivery
  5) Admin ទទួលការជូនដំណឹងពេញលេញ (gift + username + buyer) ព្រមទាំងប៊ូតុង
     "✅ ដាក់ Gift រួច" — admin ត្រូវផ្ញើ gift ដោយខ្លួនឯង (ក្រៅ bot) រួចចុចប៊ូតុង
     ដើម្បីបិទ order។ គ្មានការ deliver ស្វ័យប្រវត្តិឡើយ។

ENV VARS ត្រូវការ:
  BOT_TOKEN        - Telegram bot token
  ADMIN_ID         - Telegram user id របស់ admin (default 8266854899)
  BAKONG_TOKEN     - Bearer token សម្រាប់ bakong_khqr API (ដូចគម្រោងចាស់)
  BAKONG_ACCOUNT_ID- គណនី Bakong (KHQR receiver, ex: your_account@wing)
  MERCHANT_NAME    - ឈ្មោះហាង បង្ហាញលើ QR
  DATA_DIR         - path ទុក JSON (default ./data) — ដាក់ /var/data លើ Render disk

Deploy: Render Background Worker (គ្មាន public port ក៏បាន ព្រោះ polling)
Persistence: JSON files ក្នុង DATA_DIR (gifts.json, orders.json)
"""

import os
import json
import time
import uuid
import logging
import threading
from datetime import datetime
from decimal import Decimal

import telebot
from telebot import types

# -------------------- CONFIG --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8266854899"))
BAKONG_TOKEN = os.environ.get("BAKONG_TOKEN", "")
BAKONG_ACCOUNT_ID = os.environ.get("BAKONG_ACCOUNT_ID", "")
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Kai Gift Shop")
DATA_DIR = os.environ.get("DATA_DIR", "./data")

os.makedirs(DATA_DIR, exist_ok=True)
GIFTS_FILE = os.path.join(DATA_DIR, "gifts.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kai_gift_bot")

if not BOT_TOKEN:
    raise RuntimeError("សូមកំណត់ BOT_TOKEN environment variable")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# -------------------- STORAGE HELPERS --------------------
_lock = threading.Lock()


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Load error {path}: {e}")
        return default


def _save(path, data):
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def load_gifts():
    return _load(GIFTS_FILE, {})


def save_gifts(d):
    _save(GIFTS_FILE, d)


def load_orders():
    return _load(ORDERS_FILE, {})


def save_orders(d):
    _save(ORDERS_FILE, d)


# default gifts if empty (admin can edit later)
if not load_gifts():
    save_gifts({
        "1": {"name": "🌹 Rose", "price": 1.0},
        "2": {"name": "🧸 Teddy Bear", "price": 3.0},
        "3": {"name": "💎 Diamond Gem", "price": 5.0},
    })

# -------------------- STATE (in-memory step tracking) --------------------
# user_id -> {"step": "...", "gift_id": "...", "recipient_username": "..."}
user_state = {}


def is_admin(uid):
    return uid == ADMIN_ID


# -------------------- KHQR (Bakong) --------------------
def create_khqr(amount_usd, order_id):
    """
    ប្រើ bakong_khqr library ដូចគម្រោងចាស់។
    ត្រឡប់ dict: {"qr_string":..., "md5":...} ឬ None បើបរាជ័យ។
    """
    try:
        from bakong_khqr import KHQR
        khqr_client = KHQR(BAKONG_TOKEN)
        qr_string = khqr_client.create_qr(
            bank_account=BAKONG_ACCOUNT_ID,
            merchant_name=MERCHANT_NAME,
            merchant_city="Phnom Penh",
            amount=float(amount_usd),
            currency="USD",
            store_label="KaiGift",
            phone_number="",
            bill_number=order_id,
            terminal_label="KaiGiftBot",
        )
        md5_hash = khqr_client.generate_md5(qr_string)
        return {"qr_string": qr_string, "md5": md5_hash, "client": khqr_client}
    except Exception as e:
        log.error(f"KHQR create error: {e}")
        return None


def check_khqr_paid(khqr_client, md5_hash):
    try:
        status = khqr_client.check_payment(md5_hash)
        return status == "PAID"
    except Exception as e:
        log.error(f"KHQR check error: {e}")
        return False


def poll_payment(order_id, khqr_client, md5_hash, timeout_sec=600, interval=5):
    """Background thread: poll រហូតបានលុយ ឬ timeout"""
    start = time.time()
    while time.time() - start < timeout_sec:
        orders = load_orders()
        order = orders.get(order_id)
        if not order or order.get("status") != "awaiting_payment":
            return  # cancelled/changed elsewhere
        if check_khqr_paid(khqr_client, md5_hash):
            order["status"] = "paid_pending_delivery"
            order["paid_at"] = datetime.now().isoformat()
            orders[order_id] = order
            save_orders(orders)
            notify_buyer_paid(order)
            notify_admin_new_order(order_id, order)
            return
        time.sleep(interval)
    # timeout -> mark expired
    orders = load_orders()
    order = orders.get(order_id)
    if order and order.get("status") == "awaiting_payment":
        order["status"] = "expired"
        orders[order_id] = order
        save_orders(orders)
        try:
            bot.send_message(order["buyer_id"], "⏰ QR ការទូទាត់របស់អ្នកបានផុតកំណត់។ សូម /start ម្តងទៀត។")
        except Exception:
            pass


def notify_buyer_paid(order):
    try:
        bot.send_message(
            order["buyer_id"],
            f"✅ ការទូទាត់បានជោគជ័យ!\n\n"
            f"🎁 Gift: {order['gift_name']}\n"
            f"👤 សម្រាប់: @{order['recipient_username']}\n\n"
            f"Admin នឹងដាក់ Gift ជូនអ្នកទទួលដោយដៃ។ សូមរង់ចាំបន្តិច 🙏"
        )
    except Exception as e:
        log.error(f"notify_buyer_paid error: {e}")


def notify_admin_new_order(order_id, order):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ ដាក់ Gift រួច", callback_data=f"deliver:{order_id}"))
    try:
        buyer = order.get("buyer_username") or order.get("buyer_id")
        bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Order ថ្មី (បានបង់ប្រាក់)</b>\n\n"
            f"🆔 Order: <code>{order_id}</code>\n"
            f"🎁 Gift: {order['gift_name']} (${order['price']})\n"
            f"👤 អ្នកទទួល (username): @{order['recipient_username']}\n"
            f"🙋 អ្នកទិញ: {buyer}\n"
            f"🕒 {order.get('paid_at','')}\n\n"
            f"👉 សូមផ្ញើ Gift ទៅ @{order['recipient_username']} ដោយផ្ទាល់ រួចចុចប៊ូតុងខាងក្រោម",
            reply_markup=markup,
        )
    except Exception as e:
        log.error(f"notify_admin_new_order error: {e}")


# -------------------- USER FLOW --------------------
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    user_state.pop(msg.from_user.id, None)
    gifts = load_gifts()
    if not gifts:
        bot.send_message(msg.chat.id, "❌ សូមទោស Shop មិនទាន់មាន Gift ទេឥឡូវនេះ។")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for gid, g in gifts.items():
        markup.add(types.InlineKeyboardButton(
            f"{g['name']} — ${g['price']}", callback_data=f"pick:{gid}"))
    bot.send_message(
        msg.chat.id,
        "🎁 <b>សូមស្វាគមន៍មកកាន់ Kai Gift Shop!</b>\n\n"
        "ជ្រើសរើស Gift ដែលអ្នកចង់ផ្តល់ជូន៖",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("pick:"))
def cb_pick_gift(call):
    gift_id = call.data.split(":", 1)[1]
    gifts = load_gifts()
    gift = gifts.get(gift_id)
    if not gift:
        bot.answer_callback_query(call.id, "Gift នេះលែងមានទៀតហើយ")
        return
    user_state[call.from_user.id] = {"step": "await_username", "gift_id": gift_id}
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"អ្នកបានជ្រើសរើស: <b>{gift['name']}</b> — ${gift['price']}\n\n"
        f"✍️ សូមផ្ញើ <b>username Telegram</b> របស់អ្នកដែលនឹងទទួល Gift នេះ\n"
        f"(ឧទាហរណ៍: @example_user)"
    )


@bot.message_handler(func=lambda m: user_state.get(m.from_user.id, {}).get("step") == "await_username")
def handle_username_input(msg):
    uid = msg.from_user.id
    username = msg.text.strip().lstrip("@")
    if not username or " " in username or len(username) < 3:
        bot.send_message(msg.chat.id, "❌ Username មិនត្រឹមត្រូវ សូមផ្ញើម្តងទៀត (ឧ. @example_user)")
        return

    state = user_state[uid]
    gift_id = state["gift_id"]
    gifts = load_gifts()
    gift = gifts.get(gift_id)
    if not gift:
        bot.send_message(msg.chat.id, "❌ Gift នេះលែងមានទៀតហើយ សូម /start ម្តងទៀត")
        user_state.pop(uid, None)
        return

    state["recipient_username"] = username
    state["step"] = "confirm"

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ បញ្ជាក់ និងបង់ប្រាក់", callback_data=f"confirm:{gift_id}"),
        types.InlineKeyboardButton("❌ បោះបង់", callback_data="cancel"),
    )
    bot.send_message(
        msg.chat.id,
        f"📋 <b>សូមពិនិត្យ Order របស់អ្នក</b>\n\n"
        f"🎁 Gift: {gift['name']}\n"
        f"💵 តម្លៃ: ${gift['price']}\n"
        f"👤 អ្នកទទួល: @{username}\n\n"
        f"បើត្រឹមត្រូវ សូមចុច 'បញ្ជាក់' ដើម្បីទទួល QR ទូទាត់",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data == "cancel")
def cb_cancel(call):
    user_state.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "បានបោះបង់")
    bot.send_message(call.message.chat.id, "❌ Order ត្រូវបានបោះបង់។ សូម /start ម្តងទៀត។")


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm:"))
def cb_confirm_order(call):
    uid = call.from_user.id
    gift_id = call.data.split(":", 1)[1]
    state = user_state.get(uid)
    if not state or state.get("gift_id") != gift_id or "recipient_username" not in state:
        bot.answer_callback_query(call.id, "សូម /start ម្តងទៀត")
        return

    gifts = load_gifts()
    gift = gifts.get(gift_id)
    if not gift:
        bot.answer_callback_query(call.id, "Gift នេះលែងមានទៀតហើយ")
        return

    order_id = uuid.uuid4().hex[:10]
    price = float(gift["price"])

    qr_data = create_khqr(price, order_id)
    if not qr_data:
        bot.answer_callback_query(call.id, "បរាជ័យក្នុងការបង្កើត QR")
        bot.send_message(call.message.chat.id, "❌ មិនអាចបង្កើត QR ទូទាត់បានទេ សូមព្យាយាមម្តងទៀត ឬទាក់ទង Admin")
        return

    orders = load_orders()
    orders[order_id] = {
        "order_id": order_id,
        "buyer_id": uid,
        "buyer_username": call.from_user.username or call.from_user.first_name,
        "gift_id": gift_id,
        "gift_name": gift["name"],
        "price": price,
        "recipient_username": state["recipient_username"],
        "status": "awaiting_payment",
        "created_at": datetime.now().isoformat(),
    }
    save_orders(orders)
    user_state.pop(uid, None)

    bot.answer_callback_query(call.id)
    try:
        import qrcode
        from io import BytesIO
        img = qrcode.make(qr_data["qr_string"])
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        bot.send_photo(
            call.message.chat.id, buf,
            caption=f"💳 សូមស្កេន QR ដើម្បីទូទាត់ ${price}\n"
                    f"🆔 Order: <code>{order_id}</code>\n\n"
                    f"⏳ QR នេះមានសុពលភាព 10 នាទី",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            f"💳 KHQR String:\n<code>{qr_data['qr_string']}</code>\n\nOrder: {order_id}",
        )

    t = threading.Thread(
        target=poll_payment,
        args=(order_id, qr_data["client"], qr_data["md5"]),
        daemon=True,
    )
    t.start()


# -------------------- ADMIN: DELIVERY CONFIRMATION --------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("deliver:"))
def cb_mark_delivered(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    order_id = call.data.split(":", 1)[1]
    orders = load_orders()
    order = orders.get(order_id)
    if not order:
        bot.answer_callback_query(call.id, "រកមិនឃើញ Order")
        return
    if order["status"] == "delivered":
        bot.answer_callback_query(call.id, "Order នេះបានដាក់រួចហើយ")
        return

    order["status"] = "delivered"
    order["delivered_at"] = datetime.now().isoformat()
    orders[order_id] = order
    save_orders(orders)

    bot.answer_callback_query(call.id, "✅ បានកត់ត្រាថាដាក់ Gift រួច")
    try:
        bot.edit_message_text(
            call.message.text + "\n\n✅ <b>DELIVERED</b>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    except Exception:
        pass
    try:
        bot.send_message(
            order["buyer_id"],
            f"🎉 Gift '{order['gift_name']}' ត្រូវបានដាក់ជូន @{order['recipient_username']} រួចរាល់!\n"
            f"អរគុណសម្រាប់ការគាំទ្រ 🙏"
        )
    except Exception:
        pass


# -------------------- ADMIN: MANAGE GIFTS --------------------
@bot.message_handler(commands=["addgift"])
def cmd_addgift(msg):
    if not is_admin(msg.from_user.id):
        return
    bot.send_message(
        msg.chat.id,
        "✍️ ផ្ញើតាមទម្រង់: <code>ឈ្មោះ | តម្លៃ</code>\n"
        "ឧទាហរណ៍: <code>🎂 Cake | 2.5</code>"
    )
    bot.register_next_step_handler(msg, _process_addgift)


def _process_addgift(msg):
    if not is_admin(msg.from_user.id):
        return
    try:
        name, price = msg.text.split("|")
        price = float(price.strip())
        gifts = load_gifts()
        new_id = str(max([int(k) for k in gifts.keys()] + [0]) + 1)
        gifts[new_id] = {"name": name.strip(), "price": price}
        save_gifts(gifts)
        bot.send_message(msg.chat.id, f"✅ បានបន្ថែម Gift #{new_id}: {name.strip()} (${price})")
    except Exception:
        bot.send_message(msg.chat.id, "❌ ទម្រង់មិនត្រឹមត្រូវ សូមព្យាយាមម្តងទៀត /addgift")


@bot.message_handler(commands=["listgifts"])
def cmd_listgifts(msg):
    if not is_admin(msg.from_user.id):
        return
    gifts = load_gifts()
    if not gifts:
        bot.send_message(msg.chat.id, "គ្មាន Gift ទេ")
        return
    lines = [f"#{gid} — {g['name']} — ${g['price']}" for gid, g in gifts.items()]
    bot.send_message(msg.chat.id, "🎁 <b>បញ្ជី Gift:</b>\n\n" + "\n".join(lines))


@bot.message_handler(commands=["removegift"])
def cmd_removegift(msg):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2:
        bot.send_message(msg.chat.id, "ប្រើ: /removegift <id>")
        return
    gid = parts[1]
    gifts = load_gifts()
    if gid in gifts:
        removed = gifts.pop(gid)
        save_gifts(gifts)
        bot.send_message(msg.chat.id, f"🗑 បានលុប: {removed['name']}")
    else:
        bot.send_message(msg.chat.id, "រកមិនឃើញ Gift ID នេះទេ")


@bot.message_handler(commands=["orders"])
def cmd_orders(msg):
    if not is_admin(msg.from_user.id):
        return
    orders = load_orders()
    pending = {k: v for k, v in orders.items() if v["status"] == "paid_pending_delivery"}
    if not pending:
        bot.send_message(msg.chat.id, "✅ គ្មាន Order កំពុងរង់ចាំដាក់ Gift ទេ")
        return
    for oid, o in pending.items():
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ ដាក់ Gift រួច", callback_data=f"deliver:{oid}"))
        bot.send_message(
            msg.chat.id,
            f"🆔 {oid}\n🎁 {o['gift_name']} (${o['price']})\n👤 @{o['recipient_username']}",
            reply_markup=markup,
        )


@bot.message_handler(commands=["stats"])
def cmd_stats(msg):
    if not is_admin(msg.from_user.id):
        return
    orders = load_orders()
    total = len(orders)
    delivered = sum(1 for o in orders.values() if o["status"] == "delivered")
    pending = sum(1 for o in orders.values() if o["status"] == "paid_pending_delivery")
    revenue = sum(o["price"] for o in orders.values() if o["status"] in ("delivered", "paid_pending_delivery"))
    bot.send_message(
        msg.chat.id,
        f"📊 <b>ស្ថិតិ</b>\n\n"
        f"សរុប Order: {total}\n"
        f"បានដាក់រួច: {delivered}\n"
        f"កំពុងរង់ចាំ: {pending}\n"
        f"💵 ចំណូលសរុប: ${revenue:.2f}",
    )


# -------------------- GLOBAL ERROR HANDLING --------------------
@bot.middleware_handler(update_types=["message", "callback_query"])
def _guard(bot_instance, update):
    pass


def _global_exception_wrapper():
    while True:
        try:
            log.info("Bot polling started...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error(f"Polling crashed: {e}, restarting in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    # Flask keep-alive for Render Web Service (optional if using Background Worker)
    try:
        from flask import Flask
        app = Flask(__name__)

        @app.route("/")
        def home():
            return "Kai Gift Bot is running"

        threading.Thread(
            target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))),
            daemon=True,
        ).start()
    except ImportError:
        pass

    _global_exception_wrapper()
