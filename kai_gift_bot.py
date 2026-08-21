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
  BOT_TOKEN            - Telegram bot token
  ADMIN_ID             - Telegram user id របស់ admin (default 8266854899)
  CAMRAPIDPAY_API_KEY  - API Key ពី portal.camrapidpay.com
  MERCHANT_NAME        - ឈ្មោះហាង បង្ហាញលើសារ QR (មិនចាំបាច់ផ្ញើទៅ API ទេ)
  RENDER_EXTERNAL_URL  - (auto-set ដោយ Render) ប្រើសង់ webhook_url ជូន CamRapidPay
  DATA_DIR             - path ទុក JSON (default ./data) — ដាក់ /var/data លើ Render disk

💳 CamRapidPay KHQR (schema ដែលបានផ្ទៀងផ្ទាត់ត្រឹមត្រូវរួច — ដូចគម្រោង Kairozen ដទៃទៀត):
  - Create: POST https://pay.camrapidpay.com/api/v1/khqr/create-payments
      body: {api_key, amount, reference, webhook_url}  → response: {success, qr_code, payment_url}
  - Check:  GET  https://pay.camrapidpay.com/check-transaction-api
      params: {api_key, reference}  → response: {success, status: "success"|"paid"}
  - `reference` គឺជា order_id ខ្លួនឯង (uuid.hex[:10]) ដែលបានផ្ញើពេល create — ប្រើវាឡើងវិញ
    ដើម្បី check status ដោយមិនចាំបាច់ payment_id ដាច់ដោយឡែក
  - webhook_url ជា mandatory field ត្រូវសង់ពី RENDER_EXTERNAL_URL ស្វ័យប្រវត្តិ (Flask route
    /camrapid-webhook ទទួល push ពី CamRapidPay ដោយ log ចោលតែប៉ុណ្ណោះ — bot ប្រើ polling
    (check_khqr_status) ជាចម្បងសម្រាប់ detect ការទូទាត់)
  - Bot ធ្វើ health-check ស្វ័យប្រវត្តិពេលចាប់ផ្តើម ជូនដំណឹង Admin ភ្លាមៗបើ Key/connectivity មានបញ្ហា
  - Poll រៀងរាល់ 5 វិនាទី រហូតដល់ 10 នាទី (timeout)

Deploy: Render Web Service (ត្រូវការ public URL សម្រាប់ webhook_url — មិនអាចជា
Background Worker សុទ្ធទេ ព្រោះ CamRapidPay push webhook មកវិញ)
Persistence: JSON files ក្នុង DATA_DIR (gifts.json, orders.json)
"""

import os
import json
import time
import uuid
import logging
import threading
import hashlib
from datetime import datetime
from decimal import Decimal

import requests
import telebot
from telebot import types

# -------------------- CONFIG --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8266854899"))
CAMRAPIDPAY_API_KEY = os.environ.get("CAMRAPIDPAY_API_KEY", "")
CAMRAPID_CREATE_URL = os.environ.get(
    "CAMRAPID_CREATE_URL", "https://pay.camrapidpay.com/api/v1/khqr/create-payments"
)
CAMRAPID_CHECK_URL = os.environ.get(
    "CAMRAPID_CHECK_URL", "https://pay.camrapidpay.com/check-transaction-api"
)
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Kai Gift Shop")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
DATA_DIR = os.environ.get("DATA_DIR", "./data")

os.makedirs(DATA_DIR, exist_ok=True)
GIFTS_FILE = os.path.join(DATA_DIR, "gifts.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
GLOBAL_EMOJI_FILE = os.path.join(DATA_DIR, "global_premium_emoji.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kai_gift_bot")

if not BOT_TOKEN:
    raise RuntimeError("សូមកំណត់ BOT_TOKEN environment variable")


# -------------------- ERROR MONITORING (ជូនដំណឹង Admin ពេលមាន Error ច្រើនដងក្នុងរយៈពេលខ្លី) --------------------
# គំនិត: error តែម្តងម្កាល (ឧ. user វាយខុស format) មិនចាំបាច់រំខាន Admin ទេ — ប៉ុន្តែបើ
# error កើតឡើងញឹកញាប់ក្នុងរយៈពេលខ្លី (ជាទូទៅមានន័យថា bot/API មានបញ្ហាជាប្រព័ន្ធ ឧ.
# CamRapidPay គាំង, bug ថ្មី ។ល។) bot នឹងផ្ញើសារជូនដំណឹង Admin ស្វ័យប្រវត្តិភ្លាមៗ ព្រម
# ព័ត៌មាន error ចុងក្រោយៗ ដើម្បីជួយ debug លឿន។ មាន cooldown ជៀសវាងសារហៀរច្រើនពេក។
ERROR_ALERT_THRESHOLD = int(os.environ.get("ERROR_ALERT_THRESHOLD", "5"))       # ចំនួន error អប្បបរមាមុននឹងជូនដំណឹង
ERROR_ALERT_WINDOW_SEC = int(os.environ.get("ERROR_ALERT_WINDOW_SEC", "300"))   # រយៈពេលរាប់ (5 នាទី)
ERROR_ALERT_COOLDOWN_SEC = int(os.environ.get("ERROR_ALERT_COOLDOWN_SEC", "600"))  # ចន្លោះពេលអប្បបរមារវាងសារ (10 នាទី)

_error_events = []        # list នៃ (timestamp, source, error_text) ក្នុងបង្អួច window បច្ចុប្បន្ន
_last_error_alert_at = 0.0
_error_lock = threading.Lock()


def _record_error(source: str, exc: Exception):
    """កត់ត្រា error មួយពី source ណាមួយ (ឧ. \"handler\", \"create_khqr\", \"polling\")។
    បើចំនួន error ក្នុងបង្អួច ERROR_ALERT_WINDOW_SEC វិនាទីចុងក្រោយ ≥ ERROR_ALERT_THRESHOLD
    bot នឹងផ្ញើសារជូនដំណឹង Admin ស្វ័យប្រវត្តិ (មិនលើសពី ១ដងក្នុងរយៈពេល
    ERROR_ALERT_COOLDOWN_SEC ដើម្បីកុំឲ្យសារហៀរច្រើនពេក)។"""
    global _last_error_alert_at
    now = time.time()
    err_text = f"{type(exc).__name__}: {exc}"
    log.error(f"[{source}] {err_text}")
    snapshot = None
    with _error_lock:
        _error_events.append((now, source, err_text))
        while _error_events and _error_events[0][0] < now - ERROR_ALERT_WINDOW_SEC:
            _error_events.pop(0)
        recent_count = len(_error_events)
        if recent_count >= ERROR_ALERT_THRESHOLD and (now - _last_error_alert_at) > ERROR_ALERT_COOLDOWN_SEC:
            _last_error_alert_at = now
            snapshot = list(_error_events)
    if snapshot:
        _send_error_alert(len(snapshot), snapshot)


def _send_error_alert(count, snapshot):
    lines = [
        "🚨 <b>Bot កំពុងជួប Error ច្រើនហួសប្រមាណ!</b>",
        "",
        f"📊 {count} errors ក្នុងរយៈពេល {ERROR_ALERT_WINDOW_SEC // 60} នាទីចុងក្រោយ",
        "",
        "🔎 <b>Error ថ្មីៗ:</b>",
    ]
    for ts, source, err_text in snapshot[-5:]:
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        short = err_text if len(err_text) <= 150 else err_text[:150] + "…"
        lines.append(f"• {t} [{source}] {short}")
    lines.append("")
    lines.append("💡 សូមពិនិត្យ Render logs ដើម្បីមើលលម្អិត ឬវាយ /errorlog")
    try:
        bot.send_message(ADMIN_ID, "\n".join(lines))
    except Exception as e:
        log.error(f"[_send_error_alert] failed to notify admin: {e}")


class _LoggingExceptionHandler(telebot.ExceptionHandler):
    """ចាប់ Exception ណាមួយកើតឡើងក្នុង message/callback handler ណាមួយ (បើគ្មាន handler
    នេះ pyTelegramBotAPI នឹងលេប exception ចោលស្ងាត់ៗ ធ្វើឲ្យ user ចុច button ហើយគ្មានអ្វី
    កើតឡើងសោះ)។ ត្រង់នេះ log ចេញ Render logs ជានិច្ច ព្រមទាំងកត់ត្រាទុកសម្រាប់ជូនដំណឹង
    Admin ស្វ័យប្រវត្តិបើកើតច្រើនដង (មើល _record_error)។ bot នៅតែបន្តដំណើរការធម្មតា
    សម្រាប់ update បន្ទាប់ (មិនគាំង) ព្រោះ return True។"""
    def handle(self, exception):
        import traceback
        traceback.print_exc()
        _record_error("handler", exception)
        return True


bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", exception_handler=_LoggingExceptionHandler())

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
    """ដក custom_emoji entity ដែល id ខូចចេញតែម្នាក់ឯង, ទុក id ត្រឹមត្រូវនៅដដែល។
    Entity ប្រភេទដទៃទៀត (ឧ. bold ដែលប្រើក្នុងសារ DELIVERED) មិនមាន custom_emoji_id
    ទេ ដូច្នេះត្រូវឲ្យវាឆ្លងកាត់ដោយផ្ទាល់ កុំយកទៅ validate ជាមួយ custom_emoji_id=None
    (បើមិនដូច្នេះ វានឹងត្រូវដកចេញខុសដោយសារ None មិនផ្គូផ្គងនឹង str(None))។"""
    if not entities:
        return entities
    emoji_entities = [e for e in entities if e.type == "custom_emoji" and e.custom_emoji_id]
    other_entities = [e for e in entities if not (e.type == "custom_emoji" and e.custom_emoji_id)]
    if not emoji_entities:
        return entities
    ids = list({e.custom_emoji_id for e in emoji_entities})
    valid_ids = _validate_custom_emoji_ids(ids)
    kept = [e for e in emoji_entities if e.custom_emoji_id in valid_ids]
    if len(kept) != len(emoji_entities):
        log.info(f"filter_valid_entities: dropped {len(emoji_entities)-len(kept)} invalid custom_emoji entity(ies)")
    return other_entities + kept


def invalidate_emoji_cache(custom_emoji_id):
    _emoji_validity_cache.pop(str(custom_emoji_id), None)


def safe_send_message(chat_id, text, entities=None, reply_markup=None, **kwargs):
    """ផ្ញើសារជាមួយ premium emoji entities, ដក id ខូចចេញតែម្នាក់ឯង, fallback ពេញលេញបើនៅតែបរាជ័យ។
    ត្រង់នេះក៏បន្ថែម global premium emoji (ដាក់តាម /setupemoji) ទៅ glyph ដទៃទៀតក្នុង text ដែរ
    (ឧ. 🆕 👤 👉 ក្នុងសារជូនដំណឹង order) — បើគ្មាន add_global_emoji_entities() ទេ glyph ទាំងនោះ
    នឹងមិនដាក់ Premium ទេ ព្រោះការហៅ bot.send_message ជាមួយ entities= ធ្វើឲ្យ monkey-patch
    global premium_text() (ដែលធម្មតាដោះស្រាយ glyph ទាំងនេះ) skip ខ្លួនវាឯង។"""
    entities = add_global_emoji_entities(text, entities)
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
    entities = add_global_emoji_entities(text, entities)
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


# -------------------- GLOBAL PREMIUM EMOJI (Setup Emoji — អនុវត្តគ្រប់កន្លែង) --------------------
# ខុសពី premium_emoji_id ក្នុង gifts.json (ជាក់លាក់ក្នុងមួយ Gift) — ត្រង់នេះជា "global
# map" មួយ ដែល admin ភ្ជាប់ glyph ធម្មតា (ឧ. ✅ ❌ 🔙 ➕ ➖ 🎁 💵 ។ល។) ទៅនឹង Premium Emoji
# ID ម្តង ចាប់ពីនោះទៅ *គ្រប់ទីកន្លែង* ក្នុង Bot (ប៊ូតុង admin panel, សារបញ្ជាក់ order,
# សារជូនដំណឹង ។ល។) ដែលមាន glyph នោះ នឹងបង្ហាញ icon premium ស្វ័យប្រវត្តិ — មិនចាំបាច់
# កំណត់ម្តងមួយៗដូច per-gift emoji ទេ។
EMOJI_CATEGORIES = [
    ("✅", "✅ ជោគជ័យ / បញ្ជាក់"),
    ("❌", "❌ បោះបង់ / បដិសេធ"),
    ("◀️", "◀️ ត្រឡប់ក្រោយ"),
    ("🔙", "🔙 ត្រឡប់ក្រោយ (ផ្សេង)"),
    ("➕", "➕ បន្ថែម"),
    ("➖", "➖ បន្ថយ"),
    ("🎁", "🎁 Gift"),
    ("💵", "💵 តម្លៃ/ប្រាក់"),
    ("💳", "💳 ការទូទាត់"),
    ("📦", "📦 Order"),
    ("📋", "📋 បញ្ជី"),
    ("📊", "📊 ស្ថិតិ"),
    ("🛠", "🛠 Admin Panel"),
    ("🗑", "🗑 លុប/ដក"),
    ("⏳", "⏳ កំពុងរង់ចាំ"),
    ("⌛", "⌛ ផុតកំណត់"),
    ("⚠️", "⚠️ ប្រុងប្រយ័ត្ន"),
    ("🚨", "🚨 បន្ទាន់ (Admin alert)"),
    ("🔔", "🔔 ជូនដំណឹង"),
    ("📢", "📢 Broadcast"),
    ("📨", "📨 សំណើ/សារ"),
    ("🔁", "🔁 ព្យាយាមម្តងទៀត"),
    ("☎️", "☎️ ទំនាក់ទំនង"),
    ("👉", "👉 ចង្អុលបង្ហាញ"),
    ("👋", "👋 សួស្តី"),
    ("👥", "👥 អ្នកប្រើប្រាស់"),
    ("🏠", "🏠 ម៉ឺនុយចម្បង"),
    ("📱", "📱 ស្កេន QR"),
    ("😀", "😀 Setup Premium Emoji"),
    ("✏️", "✏️ កែ/បញ្ចូលព័ត៌មាន"),
    ("👤", "👤 អ្នកប្រើប្រាស់ម្នាក់"),
    ("ℹ️", "ℹ️ ព័ត៌មាន"),
    ("🔎", "🔎 ស្វែងរក/Debug"),
    ("✨", "✨ ការណែនាំ/Tips"),
    ("🙏", "🙏 អរគុណ"),
    ("🎉", "🎉 អបអរ"),
    ("🔗", "🔗 តំណភ្ជាប់"),
    ("★", "★ Premium badge"),
]


def get_emoji_map():
    return _load(GLOBAL_EMOJI_FILE, {})


def save_emoji_map(m):
    _save(GLOBAL_EMOJI_FILE, m)


def emoji_icon_for(text):
    """រកមើលថាតើ text (ជាធម្មតាជា label ប៊ូតុង) មាន glyph ណាមួយក្នុង global map រួច
    — return (glyph, custom_emoji_id) ដំបូងដែលរកឃើញ, ឬ (None, None) បើគ្មាន។"""
    m = get_emoji_map()
    if not m:
        return None, None
    for glyph in sorted(m.keys(), key=len, reverse=True):
        if glyph and glyph in text:
            icon_id = m[glyph].get("custom_emoji_id")
            if icon_id:
                return glyph, icon_id
    return None, None


def _strip_glyph(text, glyph):
    """លុប glyph ធម្មតាចេញពី label (ព្រោះ icon premium បង្ហាញជំនួសរួចហើយ) — បើលុបហើយ
    label ក្លាយជាទទេ រក្សា text ដើមទុក ដើម្បីកុំឲ្យ Telegram បដិសេធ button text ទទេ។"""
    if not glyph:
        return text
    cleaned = text.replace(glyph, "", 1)
    cleaned = " ".join(cleaned.split())
    return cleaned if cleaned else text


def premium_text(text):
    """ជំនួស glyph ធម្មតា (ឧ. ✅) ដោយ HTML <tg-emoji> tag នៅគ្រប់ទីកន្លែងក្នុង text
    (សម្រាប់សារ parse_mode=HTML ធម្មតា — មិនអនុវត្តលើសារដែលប្រើ entities ផ្ទាល់ខ្លួន
    ដូចជា catalog/order message ដែលមាន per-gift premium emoji រួចស្រាប់ទេ)។ ប្រើ
    placeholder token ជាមុនសិន រួច replace ត្រឡប់ជា HTML នៅចុងក្រោយតែម្តង ដើម្បីកុំឲ្យ
    វគ្គបន្ទាប់ replace ត្រូវលើ tag ដែលបានបញ្ចូលរួច (ជៀសវាង nested/broken tag)។"""
    if not text:
        return text
    m = get_emoji_map()
    if not m:
        return text
    items = sorted(m.items(), key=lambda kv: len(kv[0]), reverse=True)
    placeholders = {}
    for i, (glyph, info) in enumerate(items):
        icon_id = info.get("custom_emoji_id")
        if not icon_id or not glyph or glyph not in text:
            continue
        token = f"\x00PE{i}\x00"
        text = text.replace(glyph, token)
        placeholders[token] = f'<tg-emoji emoji-id="{icon_id}">{glyph}</tg-emoji>'
    for token, tag_html in placeholders.items():
        text = text.replace(token, tag_html)
    return text


def _find_glyph_offsets(text, glyph):
    """រកទីតាំង (utf16_offset, utf16_length) គ្រប់ occurrence នៃ glyph ក្នុង text។"""
    offsets = []
    start = 0
    glyph_len = _emoji_len(glyph)
    while True:
        idx = text.find(glyph, start)
        if idx == -1:
            break
        offset = _emoji_len(text[:idx])
        offsets.append((offset, glyph_len))
        start = idx + len(glyph)
    return offsets


def _ranges_overlap(a_off, a_len, b_off, b_len):
    return a_off < b_off + b_len and b_off < a_off + a_len


def add_global_emoji_entities(text, entities=None):
    """បន្ថែម custom_emoji entities សម្រាប់ glyph ណាមួយក្នុង global emoji map ដែលមាននៅ
    ក្នុង text — ដោយមិនជាន់ (overlap) លើ entity ដែលមានស្រាប់ (ឧ. per-gift premium emoji)។

    មូលហេតុត្រូវការមុខងារនេះ: បើសារណាមួយផ្ញើដោយ entities= ផ្ទាល់ខ្លួន (ឧ. per-gift
    emoji ក្នុង catalog/order message), _should_skip_global_emoji() នៅក្នុង monkey-patch
    bot.send_message នឹង skip premium_text() ទាំងអស់ — ធ្វើឲ្យ glyph សកលដទៃទៀតក្នុងសារ
    ដដែល (ឧ. 🆕 👤 👉 ដែល admin បានដាក់ Premium តាម /setupemoji) មិនដាក់ Premium ទេ សូម្បី
    តែបានកំណត់រួចក៏ដោយ — នេះជាមូលហេតុ 'កន្លែងខ្លះដាក់ emoji អត់ជាប់'។ ត្រង់នេះបំពេញចន្លោះ
    នោះដោយបន្ថែម entity ដោយផ្ទាល់សម្រាប់ glyph ទាំងនោះ ជំនួសឲ្យពឹងផ្អែក premium_text()។"""
    entities = list(entities) if entities else []
    m = get_emoji_map()
    if not m or not text:
        return entities
    existing_ranges = [(e.offset, e.length) for e in entities]
    for glyph, info in sorted(m.items(), key=lambda kv: len(kv[0]), reverse=True):
        icon_id = info.get("custom_emoji_id")
        if not icon_id or not glyph or glyph not in text:
            continue
        for offset, length in _find_glyph_offsets(text, glyph):
            if any(_ranges_overlap(offset, length, ro, rl) for ro, rl in existing_ranges):
                continue
            entities.append(types.MessageEntity(
                type="custom_emoji", offset=offset, length=length, custom_emoji_id=str(icon_id),
            ))
            existing_ranges.append((offset, length))
    return entities


def _is_entity_parse_error(exc):
    """រកមើលថាតើ exception នេះទាក់ទងនឹង tg-emoji/entity ដែរឬអត់ (ឧ. "can't parse
    entities" ឬ "ENTITY_TEXT_INVALID" ព្រោះ custom_emoji_id លែងមាន) — ករណីណាក៏ដោយ
    គួរតែ retry ដោយអត្ថបទធម្មតា ជាជាងឲ្យសារបាត់សោះ។"""
    return "entit" in str(exc).lower()


def _should_skip_global_emoji(kwargs):
    """សារដែលហៅ entities=... ផ្ទាល់ខ្លួន (ឧ. safe_send_message ជាមួយ per-gift premium
    emoji, parse_mode=None) មិនត្រូវអនុវត្ត global premium_text() ជាន់ទៀតទេ — បើមិន
    ដូច្នេះ tag HTML នឹងលេចចេញជាអត្ថបទដើមដោយសារ parse_mode=None។"""
    return bool(kwargs.get("entities")) or ("parse_mode" in kwargs and kwargs.get("parse_mode") is None)


# --- Auto-apply premium_text() លើសារគ្រប់ប្រភេទដែល bot ផ្ញើ (monkey-patch) ---
_orig_send_message = bot.send_message
_orig_reply_to = bot.reply_to
_orig_edit_message_text = bot.edit_message_text
_orig_edit_message_caption = bot.edit_message_caption
_orig_send_photo = bot.send_photo


def _patched_send_message(chat_id, text=None, *args, **kwargs):
    if _should_skip_global_emoji(kwargs):
        return _orig_send_message(chat_id, text, *args, **kwargs)
    try:
        return _orig_send_message(chat_id, premium_text(text), *args, **kwargs)
    except Exception as e:
        if _is_entity_parse_error(e):
            log.warning(f"[premium_text] entity parse failed, retrying plain text: {e}")
            return _orig_send_message(chat_id, text, *args, **kwargs)
        raise


def _patched_reply_to(message, text=None, *args, **kwargs):
    if _should_skip_global_emoji(kwargs):
        return _orig_reply_to(message, text, *args, **kwargs)
    try:
        return _orig_reply_to(message, premium_text(text), *args, **kwargs)
    except Exception as e:
        if _is_entity_parse_error(e):
            log.warning(f"[premium_text] entity parse failed, retrying plain text: {e}")
            return _orig_reply_to(message, text, *args, **kwargs)
        raise


def _patched_edit_message_text(text=None, *args, **kwargs):
    if _should_skip_global_emoji(kwargs):
        return _orig_edit_message_text(text, *args, **kwargs)
    try:
        return _orig_edit_message_text(premium_text(text), *args, **kwargs)
    except Exception as e:
        if _is_entity_parse_error(e):
            log.warning(f"[premium_text] entity parse failed, retrying plain text: {e}")
            return _orig_edit_message_text(text, *args, **kwargs)
        raise


def _patched_edit_message_caption(caption=None, *args, **kwargs):
    if _should_skip_global_emoji(kwargs):
        return _orig_edit_message_caption(caption, *args, **kwargs)
    try:
        return _orig_edit_message_caption(premium_text(caption), *args, **kwargs)
    except Exception as e:
        if _is_entity_parse_error(e):
            log.warning(f"[premium_text] entity parse failed, retrying plain caption: {e}")
            return _orig_edit_message_caption(caption, *args, **kwargs)
        raise


def _patched_send_photo(chat_id, photo, caption=None, *args, **kwargs):
    if _should_skip_global_emoji(kwargs):
        return _orig_send_photo(chat_id, photo, caption, *args, **kwargs)
    try:
        return _orig_send_photo(chat_id, photo, premium_text(caption), *args, **kwargs)
    except Exception as e:
        if _is_entity_parse_error(e):
            log.warning(f"[premium_text] entity parse failed, retrying plain caption: {e}")
            return _orig_send_photo(chat_id, photo, caption, *args, **kwargs)
        raise


bot.send_message = _patched_send_message
bot.reply_to = _patched_reply_to
bot.edit_message_text = _patched_edit_message_text
bot.edit_message_caption = _patched_edit_message_caption
bot.send_photo = _patched_send_photo


def build_button(text, callback_data=None, icon_custom_emoji_id=None, style=None, url=None):
    """
    សង់ InlineKeyboardButton ជាមួយ icon_custom_emoji_id/style (Bot API 9.4+)។
    - icon_custom_emoji_id ដែលបញ្ជូនមកផ្ទាល់ (ឧ. per-gift premium emoji) មានអាទិភាពជាងគេ។
    - បើគ្មានបញ្ជូនមក ស្វែងរកក្នុង global emoji map (កំណត់ដោយ 😀 ដាក់ Premium Emoji ក្នុង
      Admin Panel / /setupemoji) ថាតើ text នេះមាន glyph ណាមួយត្រូវគ្នាដែរឬអត់ — បើមាន
      ប្រើ icon នោះស្វ័យប្រវត្តិ ព្រមទាំងលុប glyph ធម្មតាចេញ (កុំបង្ហាញស្ទួន)។
    - url: ប្រសិនបើកំណត់, ធ្វើប៊ូតុងបើក link ជំនួស callback_data (ឧ. "🔗 បើកទំព័រទូទាត់")។
    style ត្រូវតែជាមួយក្នុងចំណោម 3 តម្លៃប៉ុណ្ណោះ (verified ពី Telegram Bot API):
      - "primary" = ខៀវ  (សកម្មភាពសំខាន់/default)
      - "success" = បៃតង (បញ្ជាក់/OK/positive action)
      - "danger"  = ក្រហម (លុប/បោះបង់/destructive action)
    បើ telebot version មិន support field ទាំងនេះ វា fallback ទៅប៊ូតុងធម្មតា
    ដោយស្វ័យប្រវត្តិ (គ្មាន crash) ដោយសារ try/except ខាងក្រោម។
    """
    icon_id = str(icon_custom_emoji_id) if icon_custom_emoji_id else None
    glyph = None
    if not icon_id:
        glyph, auto_id = emoji_icon_for(text)
        if glyph and auto_id:
            icon_id = str(auto_id)
    label = _strip_glyph(text, glyph) if (glyph and icon_id) else text
    attempts = []
    if style and icon_id:
        attempts.append({"style": style, "icon_custom_emoji_id": icon_id})
    if icon_id:
        attempts.append({"icon_custom_emoji_id": icon_id})
    if style:
        attempts.append({"style": style})
    for extra in attempts:
        use_text = label if "icon_custom_emoji_id" in extra else text
        try:
            return types.InlineKeyboardButton(text=use_text, callback_data=callback_data, url=url, **extra)
        except TypeError:
            continue
    return types.InlineKeyboardButton(text=text, callback_data=callback_data, url=url)


# -------------------- STATE (in-memory step tracking) --------------------
# user_id -> {"step": "...", "gift_id": "...", "recipient_username": "..."}
user_state = {}


def is_admin(uid):
    return uid == ADMIN_ID


# -------------------- KHQR (CamRapidPay) --------------------
# schema ដែលបានផ្ទៀងផ្ទាត់ត្រឹមត្រូវរួច (ដូចគម្រោង Kairozen ដទៃទៀតទាំងអស់)៖
#   POST {CAMRAPID_CREATE_URL}  body: {api_key, amount, reference, webhook_url}
#        → {success, qr_code, payment_url}
#   GET  {CAMRAPID_CHECK_URL}   params: {api_key, reference}
#        → {success, status: "success" | "paid"}
_camrapidpay_warned = False  # ជៀសវាងផ្ញើសារព្រមានដដែលៗច្រើនដង
_last_camrapid_error = ""    # debug: error ចុងក្រោយ — មើលបានតាម /testpay


def _webhook_url():
    """CamRapidPay តម្រូវ webhook_url ជា mandatory field ពេល create — ត្រូវជា URL
    សាធារណៈពិតប្រាកដ (Render ដាក់ RENDER_EXTERNAL_URL ស្វ័យប្រវត្តិ)។"""
    if not RENDER_EXTERNAL_URL:
        return ""
    return f"{RENDER_EXTERNAL_URL}/camrapid-webhook"


def _notify_admin_camrapidpay_issue(reason: str):
    """ជូនដំណឹង admin តែម្តងគត់ក្នុងមួយ run លើបញ្ហា CamRapidPay (auth/connectivity)"""
    global _camrapidpay_warned
    if _camrapidpay_warned:
        return
    _camrapidpay_warned = True
    try:
        bot.send_message(
            ADMIN_ID,
            f"⚠️ <b>CamRapidPay មានបញ្ហា</b>\n\n{reason}\n\n"
            f"💡 សូមប្រើ /testpay ដើម្បីមើល error លម្អិត។",
        )
    except Exception:
        pass


def create_khqr(amount_usd, order_id):
    """បង្កើត KHQR តាម CamRapidPay។ ត្រឡប់ dict {"qr_string", "payment_url", "md5",
    "created_at"} ឬ None បើបរាជ័យ។ `md5` ត្រង់នេះស្មើ order_id ខ្លួនឯង (=reference
    ដែលបានផ្ញើពេល create) ព្រោះ CamRapidPay មិន return payment id ដាច់ដោយឡែកទេ —
    ត្រូវប្រើ reference ដដែលនេះឡើងវិញពេល check_khqr_status()។"""
    global _last_camrapid_error
    if not CAMRAPIDPAY_API_KEY:
        _last_camrapid_error = "CAMRAPIDPAY_API_KEY មិនបានកំណត់ក្នុង environment variables"
        log.error(f"[create_khqr] {_last_camrapid_error}")
        _notify_admin_camrapidpay_issue(_last_camrapid_error)
        return None
    webhook_url = _webhook_url()
    if not webhook_url:
        _last_camrapid_error = "RENDER_EXTERNAL_URL មិនទាន់កំណត់ — CamRapidPay តម្រូវ webhook_url"
        log.error(f"[create_khqr] {_last_camrapid_error}")
        _notify_admin_camrapidpay_issue(_last_camrapid_error)
        return None
    try:
        resp = requests.post(
            CAMRAPID_CREATE_URL,
            json={
                "api_key": CAMRAPIDPAY_API_KEY,
                "amount": round(float(amount_usd), 2),
                "reference": order_id,
                "webhook_url": webhook_url,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        try:
            data = resp.json()
        except Exception:
            _last_camrapid_error = f"HTTP {resp.status_code} (non-JSON): {resp.text[:300]}"
            log.error(f"[create_khqr] {_last_camrapid_error}")
            _notify_admin_camrapidpay_issue(_last_camrapid_error)
            return None
        if not data.get("success"):
            _last_camrapid_error = f"HTTP {resp.status_code}: {data}"
            log.error(f"[create_khqr] failed: {_last_camrapid_error}")
            _notify_admin_camrapidpay_issue(_last_camrapid_error)
            return None
        qr_string = data.get("qr_code", "")
        payment_url = data.get("payment_url", "")
        if not qr_string:
            _last_camrapid_error = f"2xx ប៉ុន្តែគ្មាន qr_code: {data}"
            log.error(f"[create_khqr] {_last_camrapid_error}")
            _notify_admin_camrapidpay_issue(_last_camrapid_error)
            return None
        log.info(f"[create_khqr] OK reference={order_id}")
        return {
            "qr_string": qr_string,
            "payment_url": payment_url,
            "md5": order_id,
            "created_at": time.time(),
        }
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        _last_camrapid_error = f"{type(e).__name__}: {e}"
        log.error(f"[create_khqr] transient error: {_last_camrapid_error}")
        _notify_admin_camrapidpay_issue(_last_camrapid_error)
        _record_error("create_khqr", e)
        return None
    except Exception as e:
        _last_camrapid_error = f"{type(e).__name__}: {e}"
        log.error(f"[create_khqr] error: {_last_camrapid_error}")
        _notify_admin_camrapidpay_issue(_last_camrapid_error)
        _record_error("create_khqr", e)
        return None


def check_khqr_status(payment_id):
    """payment_id ត្រង់នេះជា reference (=order_id) ដែលបានផ្ញើពេល create_khqr()។
    ត្រឡប់ (is_paid, next_delay_sec)។"""
    global _last_camrapid_error
    try:
        resp = requests.get(
            CAMRAPID_CHECK_URL,
            params={"api_key": CAMRAPIDPAY_API_KEY, "reference": payment_id},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        data = resp.json()
        is_paid = bool(data.get("success")) and str(data.get("status", "")).lower() in ("success", "paid")
        return is_paid, 5
    except Exception as e:
        _last_camrapid_error = f"{type(e).__name__}: {e}"
        log.error(f"[check_khqr_status] error: {_last_camrapid_error}")
        _record_error("check_khqr_status", e)
        return False, 5


def poll_payment(order_id, payment_id, timeout_sec=600, created_at=None):
    """Background thread: poll រៀងរាល់ ~5s រហូតបានលុយ ឬ timeout (10 នាទី)"""
    start = created_at or time.time()
    while time.time() - start < timeout_sec:
        orders = load_orders()
        order = orders.get(order_id)
        if not order or order.get("status") != "awaiting_payment":
            return  # cancelled/changed elsewhere
        is_paid, next_delay = check_khqr_status(payment_id)
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
        premium_id = g.get("premium_emoji_id")
        # បើមាន premium_emoji_id, icon នឹងបង្ហាញផ្ទាល់ខ្លួនរួចហើយ —
        # កុំដាក់ fallback emoji ក្នុង text ទៀត ដើម្បីកុំឲ្យបង្ហាញជាន់គ្នា (🌹🌹)
        if premium_id:
            btn_text = f"{g['name']} — ${g['price']}"
        else:
            btn_text = f"{g.get('emoji', '🎁')} {g['name']} — ${g['price']}"
        markup.add(build_button(
            btn_text, f"pick:{gid}",
            icon_custom_emoji_id=premium_id,
            style="primary",
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
    kb = None
    if qr_data.get("payment_url"):
        kb = types.InlineKeyboardMarkup()
        kb.add(build_button("🔗 បើកទំព័រទូទាត់", url=qr_data["payment_url"], style="primary"))
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
                    f"🏪 {MERCHANT_NAME}\n"
                    f"🆔 Order: <code>{order_id}</code>\n\n"
                    f"⏳ QR នេះមានសុពលភាព 10 នាទី",
            reply_markup=kb,
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            f"💳 KHQR String:\n<code>{qr_data['qr_string']}</code>\n\nOrder: {order_id}",
            reply_markup=kb,
        )

    t = threading.Thread(
        target=poll_payment,
        args=(order_id, qr_data["md5"]),
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
        # ការពារ premium emoji ដើម (per-gift + global) កុំឲ្យបាត់ពេល edit —
        # bot.edit_message_text ធម្មតាមិនស្គាល់ entities ចាស់ទេ លុះត្រាតែបញ្ជូនមកវិញ
        # ដោយផ្ទាល់ (call.message.entities), បើមិនធ្វើបែបនេះ Premium emoji ក្នុងសារនេះ
        # នឹងធ្លាក់ត្រឡប់ទៅ fallback unicode វិញភ្លាមៗពេលចុច "ដាក់ Gift រួច"។
        orig_entities = list(call.message.entities or [])
        suffix_prefix = "\n\n✅ "
        bold_word = "DELIVERED"
        new_text = call.message.text + suffix_prefix + bold_word
        bold_offset = _emoji_len(call.message.text) + _emoji_len(suffix_prefix)
        entities = orig_entities + [types.MessageEntity(
            type="bold", offset=bold_offset, length=_emoji_len(bold_word),
        )]
        safe_edit_message_text(new_text, call.message.chat.id, call.message.message_id, entities=entities)
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
        build_button("📋 បញ្ជី Gift", "adm:list", style="primary"),
    )
    markup.add(
        build_button("😀 Premium Emoji (មួយៗ Gift)", "adm:setemoji", style="primary"),
        build_button("🗑 ដក Emoji Gift", "adm:removeemoji", style="danger"),
    )
    markup.add(build_button("🎭 Setup Emoji (គ្រប់កន្លែង)", "adm:setupemoji", style="primary"))
    markup.add(
        build_button("❌ លុប Gift", "adm:removegift", style="danger"),
        build_button("📦 Order កំពុងរង់ចាំ", "adm:orders", style="primary"),
    )
    markup.add(build_button("📊 ស្ថិតិ", "adm:stats", style="primary"))
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
    m.add(build_button("◀️ ត្រឡប់ក្រោយ", "adm:menu", style="primary"))
    return m


# ---------- SETUP EMOJI (GLOBAL — គ្រប់កន្លែង) ----------
def _encode_glyph(glyph):
    return glyph.encode("utf-8").hex()


def _decode_glyph(hex_str):
    return bytes.fromhex(hex_str).decode("utf-8")


def global_emoji_setup_kb():
    m = get_emoji_map()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for glyph, label in EMOJI_CATEGORIES:
        is_set = glyph in m
        mark = "✅" if is_set else "⬜"
        style = "success" if is_set else "primary"
        btn_text = f"{mark} {label}"
        try:
            btn = types.InlineKeyboardButton(
                btn_text, callback_data=f"gemoji_pick_{_encode_glyph(glyph)}", style=style
            )
        except TypeError:
            btn = types.InlineKeyboardButton(btn_text, callback_data=f"gemoji_pick_{_encode_glyph(glyph)}")
        kb.add(btn)
    kb.add(build_button("◀️ ត្រឡប់ក្រោយ", "adm:menu", style="primary"))
    return kb


@bot.message_handler(commands=["setupemoji"])
def cmd_setupemoji(msg):
    if not is_admin(msg.from_user.id):
        return
    bot.send_message(
        msg.chat.id,
        "🎭 <b>Setup Premium Emoji (គ្រប់កន្លែង)</b>\n\n"
        "ជ្រើសរើសប្រភេទខាងក្រោម រួចផ្ញើ Premium Emoji ពិត (ត្រូវការ Telegram Premium) "
        "ដើម្បីភ្ជាប់ icon នោះទៅគ្រប់ប៊ូតុង/សារក្នុង Bot ទាំងមូលដែលមាន glyph ធម្មតានេះ:",
        reply_markup=global_emoji_setup_kb(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "adm:setupemoji")
def cb_admin_setupemoji(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            "🎭 <b>Setup Premium Emoji (គ្រប់កន្លែង)</b>\n\n"
            "ជ្រើសរើសប្រភេទខាងក្រោម រួចផ្ញើ Premium Emoji ពិត (ត្រូវការ Telegram Premium) "
            "ដើម្បីភ្ជាប់ icon នោះទៅគ្រប់ប៊ូតុង/សារក្នុង Bot ទាំងមូលដែលមាន glyph ធម្មតានេះ:",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            reply_markup=global_emoji_setup_kb(),
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            "🎭 <b>Setup Premium Emoji (គ្រប់កន្លែង)</b>\n\nជ្រើសរើសប្រភេទខាងក្រោម:",
            reply_markup=global_emoji_setup_kb(),
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("gemoji_"))
def cb_global_emoji_setup(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    data = call.data
    chat_id = call.message.chat.id

    if data == "gemoji_close":
        bot.edit_message_text(
            "🎭 បិទ Setup Emoji។ ប្រើ /setupemoji ម្តងទៀតបើត្រូវការ។",
            chat_id=chat_id, message_id=call.message.message_id,
        )

    elif data.startswith("gemoji_pick_"):
        glyph = _decode_glyph(data[len("gemoji_pick_"):])
        label = next((l for g, l in EMOJI_CATEGORIES if g == glyph), f"Icon {glyph}")
        msg = bot.send_message(
            chat_id,
            f"📨 សូមផ្ញើ <b>Premium Emoji ពិត</b> សម្រាប់ប្រភេទ:\n{label}\n\n"
            f"(ត្រូវជា custom emoji ពិតៗ ដែលអ្នកមាន Telegram Premium ចុចផ្ញើ មិនមែន emoji ធម្មតាទេ)",
        )
        bot.register_next_step_handler(msg, _global_emoji_capture_step, glyph, label)

    elif data.startswith("gemoji_clear_"):
        glyph = _decode_glyph(data[len("gemoji_clear_"):])
        label = next((l for g, l in EMOJI_CATEGORIES if g == glyph), f"Icon {glyph}")
        m = get_emoji_map()
        m.pop(glyph, None)
        save_emoji_map(m)
        bot.edit_message_text(
            f"🗑 លុប icon premium សម្រាប់ {label} រួចហើយ។",
            chat_id=chat_id, message_id=call.message.message_id,
            reply_markup=global_emoji_setup_kb(),
        )

    bot.answer_callback_query(call.id)


def _global_emoji_capture_step(msg, glyph, label):
    if not is_admin(msg.from_user.id):
        return
    entities = msg.entities or []
    ce = next((e for e in entities if e.type == "custom_emoji"), None)
    if not ce:
        kb = types.InlineKeyboardMarkup()
        kb.add(build_button("🔁 ព្យាយាមម្តងទៀត", f"gemoji_pick_{_encode_glyph(glyph)}", style="primary"))
        kb.add(build_button("◀️ ត្រឡប់ក្រោយ", "adm:setupemoji", style="primary"))
        bot.send_message(
            msg.chat.id,
            "❌ រកមិនឃើញ Premium Emoji ក្នុងសារនេះទេ។\nសូមផ្ញើ Premium Emoji ពិត (មិនមែន emoji ធម្មតា) ម្តងទៀត:",
            reply_markup=kb,
        )
        return
    emoji_char = msg.text[ce.offset: ce.offset + ce.length]
    m = get_emoji_map()
    m[glyph] = {"custom_emoji_id": ce.custom_emoji_id, "emoji": emoji_char}
    save_emoji_map(m)
    bot.send_message(
        msg.chat.id,
        f"✅ <b>{label}</b>\n\nបានភ្ជាប់ Premium Emoji {emoji_char} ទៅ glyph <code>{glyph}</code> រួចហើយ។\n"
        f"ចាប់ពីនេះទៅ គ្រប់ប៊ូតុង/សារណាដែលមាន {glyph} នឹងបង្ហាញ icon premium ថែមទៀត។",
        reply_markup=global_emoji_setup_kb(),
    )


# ---------- ADD GIFT (wizard: ឈ្មោះ -> តម្លៃ -> emoji, ម្តងមួយជំហាន) ----------
# ចាស់: admin ត្រូវវាយបញ្ចូល "ឈ្មោះ | តម្លៃ | emoji" ក្នុងសារតែមួយ — បើវាយខុសទម្រង់ ឬ
# ភ្លេច pipe មួយ ត្រូវចាប់ផ្តើមឡើងវិញទាំងអស់។ ថ្មី: សួរម្តងមួយជំហាន, validate រៀងខ្លួន
# (បើខុស សួរឡើងវិញតែជំហាននោះ មិនបាត់ព័ត៌មានដែលបានវាយរួច), emoji អាចរំលងបានផងដែរ។
def _cancel_markup():
    m = types.InlineKeyboardMarkup()
    m.add(build_button("❌ បោះបង់", "addgift_cancel", style="danger"))
    return m


def _clear_addgift_step(chat_id):
    try:
        bot.clear_step_handler_by_chat_id(chat_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "addgift_cancel")
def cb_addgift_cancel(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    _clear_addgift_step(call.message.chat.id)
    _addgift_drafts.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "បានបោះបង់")
    bot.send_message(call.message.chat.id, "❌ បានបោះបង់ការបន្ថែម Gift", reply_markup=admin_main_menu_markup())


@bot.callback_query_handler(func=lambda c: c.data == "adm:addgift")
def cb_addgift(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "➕ <b>បន្ថែម Gift ថ្មី</b> (1/3)\n\n✍️ តើ Gift នេះឈ្មោះអ្វី?\nឧទាហរណ៍: <code>Rose</code>",
        reply_markup=_cancel_markup(),
    )
    bot.register_next_step_handler(msg, _addgift_step_name)


def _addgift_step_name(msg):
    if not is_admin(msg.from_user.id):
        return
    name = (msg.text or "").strip()
    if not name:
        m = bot.send_message(
            msg.chat.id,
            "❌ ឈ្មោះមិនអាចទទេបានទេ សូមវាយម្តងទៀត (1/3):",
            reply_markup=_cancel_markup(),
        )
        bot.register_next_step_handler(m, _addgift_step_name)
        return
    m = bot.send_message(
        msg.chat.id,
        f"✅ ឈ្មោះ: {name}\n\n💵 <b>Gift ថ្មី</b> (2/3)\n\nតម្លៃប៉ុន្មាន? (ដុល្លារ)\nឧទាហរណ៍: <code>2.5</code>",
        reply_markup=_cancel_markup(),
    )
    bot.register_next_step_handler(m, _addgift_step_price, {"name": name})


# ដើម្បីឲ្យប៊ូតុង "ប្រើ Default" (callback_query) ដឹងពី name/price ដែលបានវាយរួច
# (draft) — register_next_step_handler ដាក់ draft ជូនតែ message handler ប៉ុណ្ណោះ, មិន
# ជូន callback_query handler ទេ ដូច្នេះត្រូវទុក draft ក្នុង dict បណ្តោះអាសន្ននេះ keyed
# តាម admin user_id (សុវត្ថិភាព ព្រោះមានតែ admin ម្នាក់គត់អាចប្រើ flow នេះ)។
_addgift_drafts = {}


def _addgift_step_price(msg, draft):
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip().replace("$", "")
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError("price must be positive")
    except ValueError:
        m = bot.send_message(
            msg.chat.id,
            f"❌ តម្លៃមិនត្រឹមត្រូវ (\"{raw}\") — សូមវាយជាលេខវិជ្ជមាន (ឧ. 2.5) (2/3):",
            reply_markup=_cancel_markup(),
        )
        bot.register_next_step_handler(m, _addgift_step_price, draft)
        return
    draft["price"] = price
    _addgift_drafts[msg.from_user.id] = draft
    kb = types.InlineKeyboardMarkup()
    kb.add(build_button("🎁 ប្រើ Default (🎁)", "addgift_default_emoji", style="primary"))
    kb.add(build_button("❌ បោះបង់", "addgift_cancel", style="danger"))
    m = bot.send_message(
        msg.chat.id,
        f"✅ តម្លៃ: ${price:.2f}\n\n😀 <b>Gift ថ្មី</b> (3/3)\n\n"
        f"ផ្ញើ Emoji ធម្មតា (ឧ. 🎂) ឬ <b>Premium Emoji</b> ពិត (វាយ/ចម្លងបិទភ្ជាប់ផ្ទាល់) សម្រាប់ Gift នេះ — "
        f"ឬចុច \"ប្រើ Default\" ខាងក្រោមដើម្បីរំលង:",
        reply_markup=kb,
    )
    bot.register_next_step_handler(m, _addgift_step_emoji, draft)


@bot.callback_query_handler(func=lambda c: c.data == "addgift_default_emoji")
def cb_addgift_default_emoji(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "អ្នកគ្មានសិទ្ធិ")
        return
    _clear_addgift_step(call.message.chat.id)
    draft = _addgift_drafts.pop(call.from_user.id, None)
    if not draft or "name" not in draft or "price" not in draft:
        bot.answer_callback_query(call.id, "សូមចាប់ផ្តើមម្តងទៀត")
        bot.send_message(call.message.chat.id, "❌ Session ផុតកំណត់ សូម /admin ម្តងទៀត", reply_markup=admin_main_menu_markup())
        return
    bot.answer_callback_query(call.id)
    _finalize_addgift(call.message.chat.id, draft["name"], draft["price"], "🎁", None)


def _addgift_step_emoji(msg, draft):
    if not is_admin(msg.from_user.id):
        return
    _addgift_drafts.pop(msg.from_user.id, None)
    typed = (msg.text or "").strip()
    auto_fallback, auto_premium_id = extract_custom_emoji_from_message(msg)
    emoji = auto_fallback if auto_fallback else (typed or "🎁")
    _finalize_addgift(msg.chat.id, draft["name"], draft["price"], emoji, auto_premium_id)


def _finalize_addgift(chat_id, name, price, emoji, premium_emoji_id):
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
        chat_id, f"{prefix}{preview_text} (${price:.2f}){emoji_warning}",
        entities=[types.MessageEntity(type="custom_emoji", offset=_emoji_len(prefix),
                                       length=e.length, custom_emoji_id=e.custom_emoji_id) for e in preview_entities],
        reply_markup=admin_main_menu_markup(),
    )


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
        markup.add(build_button(label, f"{prefix}:{gid}", style="primary"))
    markup.add(build_button("◀️ ត្រឡប់ក្រោយ", "adm:menu", style="primary"))
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
        build_button("◀️ បោះបង់", "adm:menu", style="primary"),
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


@bot.message_handler(commands=["testpay"])
def cmd_testpay(msg):
    """Admin-only: សាកល្បង CamRapidPay ដោយបង្កើត QR $0.01 ផ្ទាល់ — ប្រើដើម្បីផ្ទៀងផ្ទាត់
    API Key/Secret និង field schema មុនចាប់ផ្តើមលក់ពិត។"""
    if not is_admin(msg.from_user.id):
        return
    bot.send_message(msg.chat.id, "⏳ កំពុងសាកល្បង CamRapidPay...")
    test_order_id = "TESTPAY_" + uuid.uuid4().hex[:8]
    qr_data = create_khqr(0.01, test_order_id)
    if not qr_data:
        bot.send_message(
            msg.chat.id,
            "❌ បរាជ័យបង្កើត QR — មើល Render Logs សម្រាប់ error លម្អិត "
            "(ប្រហែល field name ខុស ឬ API Key/Secret មិនត្រឹមត្រូវ)។",
        )
        return
    bot.send_message(
        msg.chat.id,
        f"✅ បង្កើត QR ជោគជ័យ!\n\n"
        f"🆔 reference: <code>{qr_data['md5']}</code>\n"
        f"🔗 payment_url: {qr_data.get('payment_url') or '(គ្មាន)'}\n\n"
        f"💳 QR String:\n<code>{qr_data['qr_string']}</code>\n\n"
        f"សូមស្កេន QR នេះទូទាត់ $0.01 ដើម្បីសាកល្បង auto-detect (រង់ចាំ ~10-15 វិនាទីរួច /checkpay)",
    )


@bot.message_handler(commands=["checkpay"])
def cmd_checkpay(msg):
    """Admin-only: check ស្ថានភាព payment_id ចុងក្រោយពី /testpay (paste payment_id ជា argument)"""
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(msg.chat.id, "សូមប្រើទម្រង់: /checkpay <payment_id>")
        return
    is_paid, _ = check_khqr_status(parts[1].strip())
    bot.send_message(msg.chat.id, f"ស្ថានភាព: {'✅ PAID' if is_paid else '⏳ មិនទាន់ទូទាត់ (UNPAID)'}")


@bot.message_handler(commands=["errorlog"])
def cmd_errorlog(msg):
    """Admin-only: មើល error ថ្មីៗដែល bot បានកត់ត្រា (ដូចគ្នានឹងព័ត៌មានប្រើសម្រាប់ជូន
    ដំណឹងស្វ័យប្រវត្តិពេល error ច្រើនហួសប្រមាណ) ដោយមិនចាំបាច់ចូល Render logs ទេ។"""
    if not is_admin(msg.from_user.id):
        return
    with _error_lock:
        now = time.time()
        recent = [e for e in _error_events if e[0] >= now - ERROR_ALERT_WINDOW_SEC]
    if not recent:
        bot.send_message(
            msg.chat.id,
            f"✅ គ្មាន Error ណាមួយកត់ត្រាក្នុងរយៈពេល {ERROR_ALERT_WINDOW_SEC // 60} នាទីចុងក្រោយទេ។",
        )
        return
    lines = [f"🔎 <b>Error {len(recent)} ក្នុងរយៈពេល {ERROR_ALERT_WINDOW_SEC // 60} នាទីចុងក្រោយ</b>", ""]
    for ts, source, err_text in recent[-15:]:
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        short = err_text if len(err_text) <= 150 else err_text[:150] + "…"
        lines.append(f"• {t} [{source}] {short}")
    bot.send_message(msg.chat.id, "\n".join(lines))


def _global_exception_wrapper():
    while True:
        try:
            log.info("Bot polling started...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error(f"Polling crashed: {e}, restarting in 5s...")
            _record_error("polling", e)
            time.sleep(5)


def _startup_camrapidpay_healthcheck():
    """ត្រួតពិនិត្យថា CamRapidPay Key/Server ដំណើរការមុនពេលទទួល order — ជូនដំណឹង admin ជាមុន
    បើមានបញ្ហា (auth/schema/connectivity) ជំនួសឲ្យរង់ចាំ customer ជួបបញ្ហាដំបូង"""
    try:
        qr_data = create_khqr(0.01, "STARTUP_TEST_" + uuid.uuid4().hex[:8])
        if not qr_data:
            raise RuntimeError("create_khqr ត្រឡប់ None — មើល error លម្អិតខាងលើ log")
        is_paid, _ = check_khqr_status(qr_data["md5"])  # គ្រាន់តែសាកល្បង connectivity/auth
        log.info(f"CamRapidPay health-check: OK (is_paid={is_paid})")
    except Exception as e:
        err = str(e)
        log.error(f"CamRapidPay health-check FAILED: {err}")
        try:
            bot.send_message(
                ADMIN_ID,
                f"🔴 <b>CamRapidPay Health-Check បរាជ័យ</b> ពេល bot ចាប់ផ្តើម!\n\n{err}\n\n"
                f"KHQR payment នឹងមិនដំណើរការទេ រហូតដល់កែបញ្ហានេះ។ "
                f"សូមពិនិត្យ CAMRAPIDPAY_API_KEY និង RENDER_EXTERNAL_URL។",
            )
        except Exception:
            pass


if __name__ == "__main__":
    # Flask keep-alive + webhook endpoint សម្រាប់ Render Web Service
    try:
        from flask import Flask, request as flask_request
        app = Flask(__name__)

        @app.route("/")
        def home():
            return "Kai Gift Bot is running"

        @app.route("/camrapid-webhook", methods=["POST", "GET"])
        def camrapid_webhook():
            # CamRapidPay ហៅ endpoint នេះពេលទូទាត់ជោគជ័យ។ bot ប្រើ polling
            # (check_khqr_status) ជាចម្បងរួចហើយ ដូច្នេះទីនេះគ្រាន់តែ log ចោល និង
            # return 200 ដើម្បីបំពេញលក្ខខណ្ឌ webhook_url ដែល CamRapidPay តម្រូវ។
            try:
                log.info(f"[camrapid_webhook] {flask_request.get_json(silent=True) or flask_request.args}")
            except Exception:
                pass
            return {"success": True}, 200

        threading.Thread(
            target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))),
            daemon=True,
        ).start()
    except ImportError:
        pass

    threading.Thread(target=_startup_camrapidpay_healthcheck, daemon=True).start()
    _global_exception_wrapper()
