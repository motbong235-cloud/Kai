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
  BAKONG_TOKEN     - Bakong Developer Token (ចុះឈ្មោះ https://api-bakong.nbc.gov.kh/register/)
                     ឬ RBK Relay Token (https://bakongrelay.com) បើ server នៅក្រៅកម្ពុជា
  BAKONG_ACCOUNT_ID- គណនី Bakong (KHQR receiver, ex: your_account@wing)
  MERCHANT_NAME    - ឈ្មោះហាង បង្ហាញលើ QR
  DATA_DIR         - path ទុក JSON (default ./data) — ដាក់ /var/data លើ Render disk

⚠️ សំខាន់អំពី Bakong Auto-Payment:
  - Bakong Open API check-by-md5 ដំណើរការតែលើ Dynamic KHQR ប៉ុណ្ណោះ (static=False)
  - Server ក្រៅកម្ពុជា (ដូចជា Render) ជាញឹកញាប់ជួប HTTP 403 ព្រោះ Bakong Open API
    កំណត់ geo/IP — ដំណោះស្រាយ: ប្រើ RBK Relay Token ជំនួស Bakong Token ធម្មតា
    (bakong-khqr SDK v0.5+ support ដោយផ្ទាល់, គ្មានកូដផ្លាស់ប្តូរ)
  - Bot ធ្វើ health-check ស្វ័យប្រវត្តិពេលចាប់ផ្តើម ជូនដំណឹង Admin ភ្លាមៗបើ Token/connectivity មានបញ្ហា
  - ប្រើ Smart Polling matrix (bakong-khqr v0.6.0+): 5s→10s→15s→300s តាមពេលកន្លងទៅ
    ដើម្បីសន្សំ API quota

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
# emoji = fallback unicode emoji (used if premium emoji fails / client unsupported)
# premium_emoji_id = Telegram custom_emoji_id (document_id) - ស្រេចចិត្ត
if not load_gifts():
    save_gifts({
        "1": {"name": "Rose", "price": 1.0, "emoji": "🌹", "premium_emoji_id": None},
        "2": {"name": "Teddy Bear", "price": 3.0, "emoji": "🧸", "premium_emoji_id": None},
        "3": {"name": "Diamond Gem", "price": 5.0, "emoji": "💎", "premium_emoji_id": None},
    })

# -------------------- PREMIUM EMOJI HELPERS --------------------
# គំនិត: text មាន placeholder emoji ធម្មតារួចហើយ (fallback), បើ gift/label មាន
# premium_emoji_id យើងគ្របលើវាដោយ MessageEntity(type="custom_emoji") ត្រង់ offset
# ដដែល។ Client ណាដែលមិនអាចបង្ហាញ custom emoji នឹងឃើញ fallback emoji ជំនួសដោយស្វ័យប្រវត្តិ។
# _wrap_safe_call ការពារករណី Bot API បដិសេធ custom_emoji_id ខូច/លែងមាន — retry
# ដោយផ្ញើសារដដែលគ្មាន entities ដើម្បីកុំឲ្យ order/flow ដាច់។

def _emoji_len(s: str) -> int:
    """Telegram counts offsets in UTF-16 code units, not python chars."""
    return len(s.encode("utf-16-le")) // 2


def extract_custom_emoji_from_message(msg):
    """
    ចាប់យក (fallback_char, custom_emoji_id) ដោយស្វ័យប្រវត្តិពីសារដែល admin ផ្ញើ —
    admin គ្រាន់តែផ្ញើ premium emoji ផ្ទាល់ (វាយ/ចម្លងបិទភ្ជាប់ក្នុងសារ ឬផ្ញើជា
    custom emoji sticker) មិនចាំបាច់ដឹង ID ដោយដៃទេ។ ត្រឡប់ (None, None) បើរកមិនឃើញ។
    """
    # ករណី 1: admin ផ្ញើជា custom emoji sticker
    if getattr(msg, "content_type", None) == "sticker" and msg.sticker is not None:
        cid = getattr(msg.sticker, "custom_emoji_id", None)
        if cid:
            return (getattr(msg.sticker, "emoji", None) or "🎁"), cid

    # ករណី 2: premium emoji ស្ថិតក្នុងអត្ថបទ (text ឬ caption) ជា custom_emoji entity
    text = msg.text or msg.caption or ""
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            utf16 = text.encode("utf-16-le")
            start, length = ent.offset * 2, (ent.offset + ent.length) * 2
            fallback_char = utf16[start:length].decode("utf-16-le")
            return fallback_char, ent.custom_emoji_id
    return None, None


def build_line_with_premium_emoji(fallback_emoji: str, label: str, premium_emoji_id=None):
    """
    សង់​បន្ទាត់មួយ: "<fallback_emoji> label" ព្រមទាំង entities list
    (custom_emoji entity គ្របលើ fallback_emoji ប្រសិនបើមាន premium_emoji_id)
    ត្រឡប់ (text, entities_list, text_length_in_utf16)
    """
    text = f"{fallback_emoji} {label}"
    entities = []
    if premium_emoji_id:
        entities.append(types.MessageEntity(
            type="custom_emoji",
            offset=0,
            length=_emoji_len(fallback_emoji),
            custom_emoji_id=str(premium_emoji_id),
        ))
    return text, entities, _emoji_len(text)


def build_catalog_message(gifts: dict):
    """
    សង់សារ catalog ពេញលេញ (header + list of gifts) ជាមួយ premium emoji
    entities សម្រាប់រាល់ gift ដែលមាន premium_emoji_id។
    ត្រឡប់ (full_text, entities_list)
    """
    header = "🎁 សូមស្វាគមន៍មកកាន់ Kai Gift Shop!\n\nជ្រើសរើស Gift ដែលអ្នកចង់ផ្តល់ជូន៖\n\n"
    text = header
    entities = []
    offset = _emoji_len(header)
    for gid, g in gifts.items():
        fallback = g.get("emoji", "🎁")
        premium_id = g.get("premium_emoji_id")
        line = f"{fallback} {g['name']} — ${g['price']}\n"
        if premium_id:
            entities.append(types.MessageEntity(
                type="custom_emoji",
                offset=offset,
                length=_emoji_len(fallback),
                custom_emoji_id=str(premium_id),
            ))
        text += line
        offset += _emoji_len(line)
    return text, entities


# -------------------- CUSTOM EMOJI VALIDATION (ការពារ emoji ចាស់បាត់) --------------------
# បញ្ហាចាស់: ពេលសារមួយមាន custom_emoji ច្រើន gift ក្នុងពេលតែមួយ, បើ id មួយខូច/
# ផុតសុពលភាព Telegram បដិសេធ *ទាំងសារ* ដែលធ្វើឲ្យ emoji ចាស់ៗ (ត្រឹមត្រូវ) ក៏
# រលាយបាត់ដែរ ព្រោះ fallback ចាស់ដកចេញ entities ទាំងអស់។ ដំណោះស្រាយ: validate
# custom_emoji_id នីមួយៗជាមុន ដក *តែ id ខូច* ចេញ ទុក id ត្រឹមត្រូវនៅដដែល
# (text underneath នៅតែមាន fallback unicode emoji ជាធម្មតា).
_emoji_validity_cache = {}  # custom_emoji_id(str) -> bool


def _validate_custom_emoji_ids(ids):
    ids = [str(i) for i in ids]
    uncached = [i for i in ids if i not in _emoji_validity_cache]
    if uncached:
        try:
            stickers = bot.get_custom_emoji_stickers(uncached)
            valid_now = {getattr(s, "custom_emoji_id", None) for s in stickers}
            for i in uncached:
                _emoji_validity_cache[i] = i in valid_now
        except Exception as e:
            log.warning(f"get_custom_emoji_stickers validation unavailable, trusting ids: {e}")
            for i in uncached:
                _emoji_validity_cache[i] = True  # មិនអាច validate បាន -> កុំបដិសេធ, ទុកឲ្យ send-time fallback ដោះស្រាយ
    return {i for i in ids if _emoji_validity_cache.get(i, True)}


def filter_valid_entities(entities):
    """ដក custom_emoji entity ដែល id ខូចចេញតែម្នាក់ឯង, ទុក id ត្រឹមត្រូវនៅដដែល"""
    if not entities:
        return entities
    ids = list({e.custom_emoji_id for e in entities})
    valid_ids = _validate_custom_emoji_ids(ids)
    kept = [e for e in entities if e.custom_emoji_id in valid_ids]
    if len(kept) != len(entities):
        log.info(f"filter_valid_entities: dropped {len(entities)-len(kept)} invalid custom_emoji entity(ies)")
    return kept


def invalidate_emoji_cache(custom_emoji_id):
    _emoji_validity_cache.pop(str(custom_emoji_id), None)


def safe_send_message(chat_id, text, entities=None, reply_markup=None, **kwargs):
    """ផ្ញើសារជាមួយ premium emoji entities, ដក id ខូចចេញតែម្នាក់ឯង, fallback ពេញលេញបើនៅតែបរាជ័យ"""
    entities = filter_valid_entities(entities)
    try:
        if entities:
            # entities និង parse_mode មិនអាចប្រើជាមួយគ្នាបានទេ (Bot API) -> បិទ parse_mode
            return bot.send_message(chat_id, text, entities=entities, parse_mode=None, reply_markup=reply_markup, **kwargs)
        return bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
    except Exception as e:
        log.warning(f"safe_send_message premium-emoji fallback triggered: {e}")
        try:
            return bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
        except Exception as e2:
            log.error(f"safe_send_message hard failure: {e2}")
            return None


def safe_edit_message_text(text, chat_id, message_id, entities=None, reply_markup=None, **kwargs):
    entities = filter_valid_entities(entities)
    try:
        if entities:
            return bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                          entities=entities, parse_mode=None, reply_markup=reply_markup, **kwargs)
        return bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                      reply_markup=reply_markup, **kwargs)
    except Exception as e:
        log.warning(f"safe_edit_message_text premium-emoji fallback triggered: {e}")
        try:
            return bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup, **kwargs)
        except Exception as e2:
            log.error(f"safe_edit_message_text hard failure: {e2}")
            return None


def build_button(text, callback_data, icon_custom_emoji_id=None, style=None):
    """
    សង់ InlineKeyboardButton ជាមួយ icon_custom_emoji_id/style (Bot API 9.4+)
    style ត្រូវតែជាមួយក្នុងចំណោម 3 តម្លៃប៉ុណ្ណោះ (verified ពី Telegram Bot API):
      - "primary" = ខៀវ  (សកម្មភាពសំខាន់/default)
      - "success" = បៃតង (បញ្ជាក់/OK/positive action)
      - "danger"  = ក្រហម (លុប/បោះបង់/destructive action)
    បើមិនកំណត់ style — Telegram client ប្រើពណ៌ default របស់វា (មិនមែន "secondary"
    ព្រោះតម្លៃនោះមិនមានទេ — ការសាកល្បងចាស់ខុស)។
    បើ telebot version មិន support field ទាំងនេះ វា fallback ទៅប៊ូតុងធម្មតា
    ដោយស្វ័យប្រវត្តិ (គ្មាន crash) ដោយសារ try/except ខាងក្រោម។
    """
    kwargs = {}
    if icon_custom_emoji_id:
        kwargs["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    if style:
        kwargs["style"] = style  # ឧ. "primary" / "secondary" / "positive" / "negative"
    try:
        return types.InlineKeyboardButton(text=text, callback_data=callback_data, **kwargs)
    except TypeError:
        # telebot version ចាស់មិនស្គាល់ field ថ្មី -> ធ្វើប៊ូតុងធម្មតា
        return types.InlineKeyboardButton(text=text, callback_data=callback_data)


# -------------------- STATE (in-memory step tracking) --------------------
# user_id -> {"step": "...", "gift_id": "...", "recipient_username": "..."}
user_state = {}


def is_admin(uid):
    return uid == ADMIN_ID


# -------------------- KHQR (Bakong) --------------------
# សំខាន់ៗដែលរកឃើញពី Bakong Open API ផ្លូវការ + bakong-khqr SDK (v0.6.0+):
#   1) ត្រូវការ Bakong Developer Token ចុះឈ្មោះនៅ https://api-bakong.nbc.gov.kh/register/
#      (production) ឬ SIT sandbox សម្រាប់សាកល្បង។ Token ត្រូវ renew តាមកាលកំណត់។
#   2) Server ដែល host bot (ឧ. Render) ជាញឹកញាប់ស្ថិតនៅក្រៅប្រទេសកម្ពុជា ដែល
#      Bakong Open API អាចនឹងបដិសេធ (HTTP 403 Forbidden) ដោយសារ geo/IP restriction។
#      ដំណោះស្រាយ: ប្រើ Bakong Relay (RBK Token ពី https://bakongrelay.com) ជំនួស
#      Bakong Token ធម្មតា ដែល bakong-khqr SDK support ដោយផ្ទាល់ (គ្មានកូដផ្លាស់ប្តូរ)។
#   3) API check-by-md5 អនុវត្តតែលើ Dynamic KHQR ប៉ុណ្ណោះ (static=False ដែលយើងប្រើ)។
#      QR មានសុពលភាពមិនលើសពី ~10 នាទីតាមអនុសាសន៍ផ្លូវការ។
#   4) v0.6.0+ គាំទ្រ "Smart Polling": ហៅ check_payment(md5, start_time=...) នឹងត្រឡប់
#      (status, next_delay) ជំនួសពី string ធម្មតា — កាត់បន្ថយ API call ច្រើន (5s ដំបូង
#      -> 10s -> 15s -> 300s តាមពេលកន្លងទៅ) ជៀសវាងអស់ quota Token។
BAKONG_USE_RELAY = os.environ.get("BAKONG_USE_RELAY", "false").lower() == "true"
_bakong_token_warned = False  # ជៀសវាងផ្ញើសារព្រមានដដែលៗច្រើនដង


def _notify_admin_bakong_issue(reason: str):
    """ជូនដំណឹង admin តែម្តងគត់ក្នុងមួយ run លើបញ្ហា Bakong (403 geo-block / token invalid)"""
    global _bakong_token_warned
    if _bakong_token_warned:
        return
    _bakong_token_warned = True
    try:
        bot.send_message(
            ADMIN_ID,
            f"⚠️ <b>Bakong KHQR មានបញ្ហា</b>\n\n{reason}\n\n"
            f"💡 បើ server នេះមិនមែននៅកម្ពុជា (ឧ. Render) Bakong Open API អាចនឹង "
            f"បដិសេធសំណើ (403) ។ ដំណោះស្រាយ: ចុះឈ្មោះ RBK Token នៅ bakongrelay.com "
            f"រួចដាក់ BAKONG_TOKEN ជា RBK token ហើយកំណត់ BAKONG_USE_RELAY=true។",
        )
    except Exception:
        pass


def create_khqr(amount_usd, order_id):
    """
    ប្រើ bakong_khqr library (official Bakong Open API wrapper)។
    ត្រឡប់ dict: {"qr_string":..., "md5":..., "client":..., "created_at": float} ឬ None បើបរាជ័យ។
    """
    try:
        from bakong_khqr import KHQR
        khqr_client = KHQR(BAKONG_TOKEN)
        qr_string = khqr_client.create_qr(
            account_id=BAKONG_ACCOUNT_ID,  # ឈ្មោះ parameter ថ្មី (bank_account ចាស់ deprecated)
            merchant_name=MERCHANT_NAME,
            merchant_city="Phnom Penh",
            amount=float(amount_usd),
            currency="USD",
            store_label="KaiGift",
            phone_number="",
            bill_number=order_id,
            terminal_label="KaiGiftBot",
            static=False,   # ត្រូវតែ Dynamic ដើម្បីអាច check-by-md5 បាន
            expiration=1,   # ថ្ងៃ (poll_payment ខាងក្រោមកំណត់ timeout ខ្លីជាងនេះ ~10 នាទី)
        )
        md5_hash = khqr_client.generate_md5(qr_string)
        return {"qr_string": qr_string, "md5": md5_hash, "client": khqr_client, "created_at": time.time()}
    except Exception as e:
        err = str(e)
        log.error(f"KHQR create error: {err}")
        if "403" in err or "Forbidden" in err:
            _notify_admin_bakong_issue(f"បរាជ័យបង្កើត KHQR (403 Forbidden): {err}")
        return None


def check_khqr_status(khqr_client, md5_hash, start_time=None):
    """
    ត្រឡប់ (is_paid: bool, next_delay: int)។ ប្រើ Smart Polling (v0.6.0+) បើ start_time
    ត្រូវបានផ្តល់ — fallback ទៅ legacy call + fixed delay បើ library ចាស់ជាងមិន support។
    """
    try:
        if start_time is not None:
            result = khqr_client.check_payment(md5_hash, start_time=start_time)
            if isinstance(result, tuple):
                status, next_delay = result
                return status == "PAID", int(next_delay)
            return result == "PAID", 5  # library ចាស់ត្រឡប់ string តែម្នាក់ despite start_time
        status = khqr_client.check_payment(md5_hash)
        return status == "PAID", 5
    except Exception as e:
        err = str(e)
        log.error(f"KHQR check error: {err}")
        if "403" in err or "Forbidden" in err:
            _notify_admin_bakong_issue(f"បរាជ័យ check payment (403 Forbidden): {err}")
        return False, 5


def poll_payment(order_id, khqr_client, md5_hash, timeout_sec=600, created_at=None):
    """Background thread: Smart Polling រហូតបានលុយ ឬ timeout (matrix: 5s→10s→15s→300s)"""
    start = created_at or time.time()
    while time.time() - start < timeout_sec:
        orders = load_orders()
        order = orders.get(order_id)
        if not order or order.get("status") != "awaiting_payment":
            return  # cancelled/changed elsewhere
        is_paid, next_delay = check_khqr_status(khqr_client, md5_hash, start_time=start)
        if is_paid:
            order["status"] = "paid_pending_delivery"
            order["paid_at"] = datetime.now().isoformat()
            orders[order_id] = order
            save_orders(orders)
            notify_buyer_paid(order)
            notify_admin_new_order(order_id, order)
            return
        time.sleep(min(next_delay, timeout_sec))
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
    markup.add(build_button("✅ ដាក់ Gift រួច", f"deliver:{order_id}", style="success"))
    try:
        buyer = order.get("buyer_username") or order.get("buyer_id")
        gifts = load_gifts()
        gift = gifts.get(order.get("gift_id"), {})
        fallback = gift.get("emoji", "🎁")
        premium_id = gift.get("premium_emoji_id")

        header = "🆕 Order ថ្មី (បានបង់ប្រាក់)\n\n" \
                 f"🆔 Order: {order_id}\n" \
                 f"🎁 Gift: "
        gift_line, gift_entities, _ = build_line_with_premium_emoji(fallback, order['gift_name'], premium_id)
        text = f"{header}{gift_line} (${order['price']})\n" \
               f"👤 អ្នកទទួល (username): @{order['recipient_username']}\n" \
               f"🙋 អ្នកទិញ: {buyer}\n" \
               f"🕒 {order.get('paid_at','')}\n\n" \
               f"👉 សូមផ្ញើ Gift ទៅ @{order['recipient_username']} ដោយផ្ទាល់ រួចចុចប៊ូតុងខាងក្រោម"
        entities = []
        if gift_entities:
            offset_shift = _emoji_len(header)
            for ent in gift_entities:
                entities.append(types.MessageEntity(
                    type="custom_emoji",
                    offset=ent.offset + offset_shift,
                    length=ent.length,
                    custom_emoji_id=ent.custom_emoji_id,
                ))
        safe_send_message(ADMIN_ID, text, entities=entities, reply_markup=markup)
    except Exception as e:
        log.error(f"notify_admin_new_order error: {e}")


# -------------------- USER FLOW --------------------
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    user_state.pop(msg.from_user.id, None)
    if is_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "🛠 អ្នកជា Admin — វាយ /admin ដើម្បីបើក Admin Panel (ប៊ូតុង)")
    gifts = load_gifts()
    if not gifts:
        safe_send_message(msg.chat.id, "❌ សូមទោស Shop មិនទាន់មាន Gift ទេឥឡូវនេះ។")
        return

    text, entities = build_catalog_message(gifts)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for gid, g in gifts.items():
        btn_text = f"{g.get('emoji', '🎁')} {g['name']} — ${g['price']}"
        markup.add(build_button(
            btn_text, f"pick:{gid}",
            icon_custom_emoji_id=g.get("premium_emoji_id"),
        ))
    safe_send_message(msg.chat.id, text, entities=entities, reply_markup=markup)


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

    fallback = gift.get("emoji", "🎁")
    intro = "អ្នកបានជ្រើសរើស: "
    line, gift_entities, _ = build_line_with_premium_emoji(fallback, gift["name"], gift.get("premium_emoji_id"))
    text = f"{intro}{line} — ${gift['price']}\n\n" \
           f"✍️ សូមផ្ញើ username Telegram របស់អ្នកដែលនឹងទទួល Gift នេះ\n" \
           f"(ឧទាហរណ៍: @example_user)"
    entities = []
    if gift_entities:
        offset_shift = _emoji_len(intro)
        for ent in gift_entities:
            entities.append(types.MessageEntity(
                type="custom_emoji",
                offset=ent.offset + offset_shift,
                length=ent.length,
                custom_emoji_id=ent.custom_emoji_id,
            ))
    safe_send_message(call.message.chat.id, text, entities=entities)


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
        build_button("✅ បញ្ជាក់ និងបង់ប្រាក់", f"confirm:{gift_id}", style="success"),
        build_button("❌ បោះបង់", "cancel", style="danger"),
    )

    header = "📋 សូមពិនិត្យ Order របស់អ្នក\n\n🎁 Gift: "
    fallback = gift.get("emoji", "🎁")
    gift_line, gift_entities, _ = build_line_with_premium_emoji(fallback, gift["name"], gift.get("premium_emoji_id"))
    text = f"{header}{gift_line}\n💵 តម្លៃ: ${gift['price']}\n👤 អ្នកទទួល: @{username}\n\n" \
           f"បើត្រឹមត្រូវ សូមចុច 'បញ្ជាក់' ដើម្បីទទួល QR ទូទាត់"
    entities = []
    if gift_entities:
        offset_shift = _emoji_len(header)
        for ent in gift_entities:
            entities.append(types.MessageEntity(
                type="custom_emoji",
                offset=ent.offset + offset_shift,
                length=ent.length,
                custom_emoji_id=ent.custom_emoji_id,
            ))
    safe_send_message(msg.chat.id, text, entities=entities, reply_markup=markup)


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
        kwargs={"created_at": qr_data.get("created_at")},
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
def admin_main_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        build_button("➕ បន្ថែម Gift", "adm:addgift", style="success"),
        build_button("📋 បញ្ជី Gift", "adm:list"),
    )
    markup.add(
        build_button("😀 ដាក់ Premium Emoji", "adm:setemoji"),
        build_button("🗑 ដក Premium Emoji", "adm:removeemoji"),
    )
    markup.add(
        build_button("❌ លុប Gift", "adm:removegift", style="danger"),
        build_button("📦 Order កំពុងរង់ចាំ", "adm:orders"),
    )
    markup.add(build_button("📊 ស្ថិតិ", "adm:stats"))
    return markup


@bot.message_handler(commands=["admin"])
def cmd_admin_panel(msg):
    if not is_admin(msg.from_user.id):
        return
    bot.send_message(msg.chat.id, "🛠 <b>Admin Panel</b>\n\nជ្រើសរើសសកម្មភាព៖", reply_markup=admin_main_menu_markup())


@bot.callback_query_handler(func=lambda c: c.data == "adm:menu")
def cb_admin_menu(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("🛠 <b>Admin Panel</b>\n\nជ្រើសរើសសកម្មភាព៖",
                               chat_id=call.message.chat.id, message_id=call.message.message_id,
                               reply_markup=admin_main_menu_markup())
    except Exception:
        bot.send_message(call.message.chat.id, "🛠 <b>Admin Panel</b>\n\nជ្រើសរើសសកម្មភាព៖",
                          reply_markup=admin_main_menu_markup())


def _back_button_markup():
    m = types.InlineKeyboardMarkup()
    m.add(build_button("◀️ ត្រឡប់ក្រោយ", "adm:menu"))
    return m


# ---------- ADD GIFT ----------
@bot.callback_query_handler(func=lambda c: c.data == "adm:addgift")
def cb_addgift(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "✍️ ផ្ញើតាមទម្រង់: <code>ឈ្មោះ | តម្លៃ | emoji</code>\n\n"
        "ឧទាហរណ៍ (emoji ធម្មតា):\n<code>Cake | 2.5 | 🎂</code>\n\n"
        "ឧទាហរណ៍ (Premium Emoji): គ្រាន់តែ <b>វាយ/ចម្លងបិទភ្ជាប់ Premium Emoji ផ្ទាល់</b> "
        "ជំនួសកន្លែង emoji នោះ — Bot នឹងចាប់យក ID ដោយស្វ័យប្រវត្តិ:\n"
        "<code>Cake | 2.5 | 🎂</code> ← (ដែល 🎂 ជា Premium Emoji ដែលអ្នកបិទភ្ជាប់)\n\n"
        "💡 ត្រូវការគណនី Telegram Premium ដើម្បីផ្ញើ premium emoji។",
        reply_markup=_back_button_markup(),
    )
    bot.register_next_step_handler(msg, _process_addgift)


def _process_addgift(msg):
    if not is_admin(msg.from_user.id):
        return
    try:
        text = msg.text or ""
        parts = [p.strip() for p in text.split("|")]
        name = parts[0]
        price = float(parts[1])
        typed_emoji = parts[2] if len(parts) > 2 and parts[2] else "🎁"

        # ព្យាយាមចាប់យក Premium Emoji ដោយស្វ័យប្រវត្តិពី entities នៃសារ
        auto_fallback, auto_premium_id = extract_custom_emoji_from_message(msg)
        emoji = auto_fallback if auto_fallback else typed_emoji
        premium_emoji_id = auto_premium_id

        gifts = load_gifts()
        new_id = str(max([int(k) for k in gifts.keys()] + [0]) + 1)

        emoji_warning = ""
        if premium_emoji_id:
            invalidate_emoji_cache(premium_emoji_id)
            if premium_emoji_id not in _validate_custom_emoji_ids([premium_emoji_id]):
                emoji_warning = "\n⚠️ Premium Emoji មិនត្រឹមត្រូវ — Gift នេះនឹងប្រើ fallback emoji ធម្មតា។"
                premium_emoji_id = None

        gifts[new_id] = {"name": name, "price": price, "emoji": emoji, "premium_emoji_id": premium_emoji_id}
        save_gifts(gifts)

        preview_text, preview_entities, _ = build_line_with_premium_emoji(emoji, name, premium_emoji_id)
        prefix = f"✅ បានបន្ថែម Gift #{new_id}: "
        safe_send_message(
            msg.chat.id, f"{prefix}{preview_text} (${price}){emoji_warning}",
            entities=[types.MessageEntity(type="custom_emoji", offset=_emoji_len(prefix),
                                           length=e.length, custom_emoji_id=e.custom_emoji_id) for e in preview_entities],
            reply_markup=admin_main_menu_markup(),
        )
    except Exception as e:
        log.error(f"_process_addgift error: {e}")
        bot.send_message(msg.chat.id, "❌ ទម្រង់មិនត្រឹមត្រូវ", reply_markup=admin_main_menu_markup())


# ---------- LIST GIFTS ----------
def render_gift_list_text():
    gifts = load_gifts()
    if not gifts:
        return "គ្មាន Gift ទេ", []
    header = "🎁 បញ្ជី Gift:\n\n"
    text = header
    entities = []
    offset = _emoji_len(header)
    for gid, g in gifts.items():
        fallback = g.get("emoji", "🎁")
        premium_id = g.get("premium_emoji_id")
        tag = " [premium ✨]" if premium_id else ""
        line = f"#{gid} — {fallback} {g['name']} — ${g['price']}{tag}\n"
        if premium_id:
            entities.append(types.MessageEntity(
                type="custom_emoji", offset=offset, length=_emoji_len(fallback),
                custom_emoji_id=str(premium_id),
            ))
        text += line
        offset += _emoji_len(line)
    return text, entities


@bot.message_handler(commands=["listgifts"])
def cmd_listgifts(msg):
    if not is_admin(msg.from_user.id):
        return
    text, entities = render_gift_list_text()
    safe_send_message(msg.chat.id, text, entities=entities, reply_markup=admin_main_menu_markup())


@bot.callback_query_handler(func=lambda c: c.data == "adm:list")
def cb_list_gifts(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    text, entities = render_gift_list_text()
    safe_send_message(call.message.chat.id, text, entities=entities, reply_markup=admin_main_menu_markup())


# ---------- SET / REMOVE PREMIUM EMOJI ----------
def _gift_picker_markup(prefix, only_with_emoji=False):
    gifts = load_gifts()
    markup = types.InlineKeyboardMarkup(row_width=1)
    found = False
    for gid, g in gifts.items():
        if only_with_emoji and not g.get("premium_emoji_id"):
            continue
        found = True
        label = f"{g.get('emoji','🎁')} {g['name']} — ${g['price']}"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"{prefix}:{gid}"))
    markup.add(build_button("◀️ ត្រឡប់ក្រោយ", "adm:menu"))
    return markup, found


@bot.callback_query_handler(func=lambda c: c.data == "adm:setemoji")
def cb_setemoji_menu(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    markup, found = _gift_picker_markup("adm:setemoji_pick")
    if not found:
        bot.send_message(call.message.chat.id, "គ្មាន Gift ទេ", reply_markup=admin_main_menu_markup())
        return
    bot.send_message(call.message.chat.id, "ជ្រើសរើស Gift ដែលចង់ដាក់ Premium Emoji:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:setemoji_pick:"))
def cb_setemoji_pick(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    gid = call.data.split(":", 2)[2]
    gifts = load_gifts()
    if gid not in gifts:
        bot.answer_callback_query(call.id, "រកមិនឃើញ Gift នេះទេ")
        return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        f"✍️ សូម <b>ផ្ញើ Premium Emoji ផ្ទាល់</b> សម្រាប់ '{gifts[gid]['name']}'\n"
        f"(វាយ ឬចម្លងបិទភ្ជាប់វាចូលក្នុងប្រអប់សារ ហើយផ្ញើមក — Bot នឹងចាប់យក ID ដោយស្វ័យប្រវត្តិ)\n\n"
        f"💡 ត្រូវការគណនី Telegram Premium ដើម្បីផ្ញើ premium emoji។ បើគ្មាន Premium អាចវាយ custom_emoji_id ដោយផ្ទាល់ក៏បាន។",
        reply_markup=_back_button_markup(),
    )
    bot.register_next_step_handler(msg, _process_setemoji, gid)


def _process_setemoji(msg, gid):
    if not is_admin(msg.from_user.id):
        return
    gifts = load_gifts()
    if gid not in gifts:
        bot.send_message(msg.chat.id, "រកមិនឃើញ Gift ID នេះទេ", reply_markup=admin_main_menu_markup())
        return

    fallback_char, custom_emoji_id = extract_custom_emoji_from_message(msg)

    if not custom_emoji_id:
        # fallback: admin វាយ ID ដោយដៃ (ករណីគ្មានគណនី Premium)
        typed = (msg.text or "").strip()
        if typed.isdigit():
            custom_emoji_id = typed
            fallback_char = gifts[gid].get("emoji", "🎁")
        else:
            bot.send_message(
                msg.chat.id,
                "❌ មិនអាចរកឃើញ Premium Emoji ក្នុងសារនេះទេ។ សូមផ្ញើ emoji ផ្ទាល់ម្តងទៀត "
                "(ឬវាយ custom_emoji_id ជាលេខ បើគ្មានគណនី Premium)។",
                reply_markup=admin_main_menu_markup(),
            )
            return

    invalidate_emoji_cache(custom_emoji_id)  # ត្រួតពិនិត្យថ្មីៗ កុំប្រើ cache ចាស់
    valid_ids = _validate_custom_emoji_ids([custom_emoji_id])
    if custom_emoji_id not in valid_ids:
        bot.send_message(
            msg.chat.id,
            f"❌ Emoji នេះមិនត្រឹមត្រូវ ឬលែងមានទៀតទេ។ Gift '{gifts[gid]['name']}' "
            f"នៅតែប្រើ emoji ចាស់ដដែល (មិនត្រូវបានប៉ះពាល់)។ សូមព្យាយាមម្តងទៀត។",
            reply_markup=admin_main_menu_markup(),
        )
        return

    gifts[gid]["premium_emoji_id"] = custom_emoji_id
    if fallback_char:
        gifts[gid]["emoji"] = fallback_char
    save_gifts(gifts)

    g = gifts[gid]
    prefix = "✅ បានដាក់ Premium Emoji ជូន: "
    line, entities, _ = build_line_with_premium_emoji(g.get("emoji", "🎁"), g["name"], custom_emoji_id)
    safe_send_message(msg.chat.id, f"{prefix}{line}", entities=[
        types.MessageEntity(type="custom_emoji", offset=_emoji_len(prefix) + e.offset,
                             length=e.length, custom_emoji_id=e.custom_emoji_id) for e in entities
    ], reply_markup=admin_main_menu_markup())


@bot.callback_query_handler(func=lambda c: c.data == "adm:removeemoji")
def cb_removeemoji_menu(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    markup, found = _gift_picker_markup("adm:removeemoji_pick", only_with_emoji=True)
    if not found:
        bot.send_message(call.message.chat.id, "គ្មាន Gift ណាមួយមាន Premium Emoji ទេ", reply_markup=admin_main_menu_markup())
        return
    bot.send_message(call.message.chat.id, "ជ្រើសរើស Gift ដែលចង់ដក Premium Emoji ចេញ:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:removeemoji_pick:"))
def cb_removeemoji_pick(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    gid = call.data.split(":", 2)[2]
    gifts = load_gifts()
    if gid not in gifts:
        bot.answer_callback_query(call.id, "រកមិនឃើញ Gift នេះទេ")
        return
    gifts[gid]["premium_emoji_id"] = None
    save_gifts(gifts)
    bot.answer_callback_query(call.id, "បានដក Premium Emoji ចេញ")
    bot.send_message(call.message.chat.id, f"🗑 បានដកយក Premium Emoji ចេញពី {gifts[gid]['name']}",
                      reply_markup=admin_main_menu_markup())


# ---------- REMOVE GIFT ----------
@bot.callback_query_handler(func=lambda c: c.data == "adm:removegift")
def cb_removegift_menu(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    markup, found = _gift_picker_markup("adm:removegift_pick")
    if not found:
        bot.send_message(call.message.chat.id, "គ្មាន Gift ទេ", reply_markup=admin_main_menu_markup())
        return
    bot.send_message(call.message.chat.id, "ជ្រើសរើស Gift ដែលចង់លុប:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:removegift_pick:"))
def cb_removegift_pick(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    gid = call.data.split(":", 2)[2]
    gifts = load_gifts()
    if gid not in gifts:
        bot.answer_callback_query(call.id, "រកមិនឃើញ Gift នេះទេ")
        return
    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup()
    markup.add(
        build_button("✅ បញ្ជាក់លុប", f"adm:removegift_confirm:{gid}", style="danger"),
        build_button("◀️ បោះបង់", "adm:menu"),
    )
    bot.send_message(call.message.chat.id, f"⚠️ លុប '{gifts[gid]['name']}' មែនទេ?", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:removegift_confirm:"))
def cb_removegift_confirm(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    gid = call.data.split(":", 2)[2]
    gifts = load_gifts()
    if gid not in gifts:
        bot.answer_callback_query(call.id, "រកមិនឃើញ Gift នេះទេ")
        return
    removed = gifts.pop(gid)
    save_gifts(gifts)
    bot.answer_callback_query(call.id, "បានលុប")
    bot.send_message(call.message.chat.id, f"🗑 បានលុប: {removed['name']}", reply_markup=admin_main_menu_markup())


# ---------- PENDING ORDERS ----------
def render_pending_orders(chat_id):
    orders = load_orders()
    pending = {k: v for k, v in orders.items() if v["status"] == "paid_pending_delivery"}
    if not pending:
        bot.send_message(chat_id, "✅ គ្មាន Order កំពុងរង់ចាំដាក់ Gift ទេ", reply_markup=admin_main_menu_markup())
        return
    for oid, o in pending.items():
        markup = types.InlineKeyboardMarkup()
        markup.add(build_button("✅ ដាក់ Gift រួច", f"deliver:{oid}", style="success"))
        bot.send_message(
            chat_id,
            f"🆔 {oid}\n🎁 {o['gift_name']} (${o['price']})\n👤 @{o['recipient_username']}",
            reply_markup=markup,
        )
    bot.send_message(chat_id, "◀️", reply_markup=admin_main_menu_markup())


@bot.message_handler(commands=["orders"])
def cmd_orders(msg):
    if not is_admin(msg.from_user.id):
        return
    render_pending_orders(msg.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "adm:orders")
def cb_orders(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    render_pending_orders(call.message.chat.id)


# ---------- STATS ----------
def render_stats_text():
    orders = load_orders()
    total = len(orders)
    delivered = sum(1 for o in orders.values() if o["status"] == "delivered")
    pending = sum(1 for o in orders.values() if o["status"] == "paid_pending_delivery")
    revenue = sum(o["price"] for o in orders.values() if o["status"] in ("delivered", "paid_pending_delivery"))
    return (
        f"📊 <b>ស្ថិតិ</b>\n\n"
        f"សរុប Order: {total}\n"
        f"បានដាក់រួច: {delivered}\n"
        f"កំពុងរង់ចាំ: {pending}\n"
        f"💵 ចំណូលសរុប: ${revenue:.2f}"
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(msg):
    if not is_admin(msg.from_user.id):
        return
    bot.send_message(msg.chat.id, render_stats_text(), reply_markup=admin_main_menu_markup())


@bot.callback_query_handler(func=lambda c: c.data == "adm:stats")
def cb_stats(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, render_stats_text(), reply_markup=admin_main_menu_markup())


def _global_exception_wrapper():
    while True:
        try:
            log.info("Bot polling started...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error(f"Polling crashed: {e}, restarting in 5s...")
            time.sleep(5)


def _startup_bakong_healthcheck():
    """ត្រួតពិនិត្យថា Bakong Token/Server ដំណើរការមុនពេលទទួល order — ជូនដំណឹង admin ជាមុន
    បើមានបញ្ហា (403 geo-block ឬ token ផុតកំណត់) ជំនួសឲ្យរង់ចាំ customer ជួបបញ្ហាដំបូង"""
    try:
        from bakong_khqr import KHQR
        test_client = KHQR(BAKONG_TOKEN)
        test_qr = test_client.create_qr(
            account_id=BAKONG_ACCOUNT_ID, merchant_name=MERCHANT_NAME,
            merchant_city="Phnom Penh", amount=0.01, currency="USD",
            store_label="HealthCheck", bill_number="STARTUP_TEST", static=False, expiration=1,
        )
        test_md5 = test_client.generate_md5(test_qr)
        test_client.check_payment(test_md5)  # គ្រាន់តែសាកល្បង connectivity/token
        log.info("Bakong KHQR health-check: OK")
    except Exception as e:
        err = str(e)
        log.error(f"Bakong KHQR health-check FAILED: {err}")
        reason = "403 Forbidden (server នេះប្រហែលនៅក្រៅកម្ពុជា)" if "403" in err else err
        try:
            bot.send_message(
                ADMIN_ID,
                f"🔴 <b>Bakong Health-Check បរាជ័យ</b> ពេល bot ចាប់ផ្តើម!\n\n{reason}\n\n"
                f"KHQR payment នឹងមិនដំណើរការទេ រហូតដល់កែបញ្ហានេះ។ "
                f"សូមពិនិត្យ BAKONG_TOKEN/BAKONG_ACCOUNT_ID ឬប្រើ Bakong Relay (bakongrelay.com) "
                f"បើ server នេះ host នៅក្រៅកម្ពុជា។",
            )
        except Exception:
            pass


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

    threading.Thread(target=_startup_bakong_healthcheck, daemon=True).start()
    _global_exception_wrapper()
