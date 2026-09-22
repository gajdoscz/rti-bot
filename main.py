import email
import email.utils
import imaplib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from openai import OpenAI


# VĹˇechny citlivĂ© Ăşdaje se naÄŤĂ­tajĂ­ pouze z Render Environment Variables.
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
IMAP_MAILBOX = os.environ.get("IMAP_MAILBOX", "INBOX")
MAX_CACHE_EMAILS = int(os.environ.get("MAX_CACHE_EMAILS", "10000"))

try:
    ALLOWED_CHAT_IDS = {
        int(x.strip()) for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
        if x.strip()
    }
except ValueError:
    ALLOWED_CHAT_IDS = set()

VIP_WATCH_LIST = [
    x.strip() for x in os.environ.get(
        "VIP_WATCH_LIST",
        "Rapant,Vavrak,Antalikova,Marcekova,Kolarik,Horinek,Tomecek,Machac,Hindra,Krajcovic,Plevova",
    ).split(",") if x.strip()
]

DATA_DIR = Path(os.environ.get("RTI_BOT_DATA_DIR", "/opt/render/project/src/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "email_cache.json"
CACHE_LOCK = threading.Lock()
COLLECT_LOCK = threading.Lock()
LAST_CHAT_ID = None

try:
    with CACHE_FILE.open("r", encoding="utf-8") as f:
        CACHED_EMAILS_DB = json.load(f)
    if not isinstance(CACHED_EMAILS_DB, list):
        CACHED_EMAILS_DB = []
except (FileNotFoundError, json.JSONDecodeError, OSError):
    CACHED_EMAILS_DB = []

app = Flask(__name__)
ai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def save_cache():
    with CACHE_LOCK:
        snapshot = list(CACHED_EMAILS_DB)
    tmp = CACHE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False)
    tmp.replace(CACHE_FILE)


def decode_mime(value):
    if not value:
        return ""
    result = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result).strip()


def message_body(msg):
    parts = msg.walk() if msg.is_multipart() else [msg]
    plain, html = [], []
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            plain.append(text)
        elif part.get_content_type() == "text/html":
            html.append(re.sub(r"<[^>]+>", " ", text))
    text = "\n".join(plain or html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:12000]


def is_relevant_inbox_message(msg):
    """Reject newsletters, bulk mail and automated noise."""
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return False
    if msg.get("Precedence", "").lower() in {"bulk", "list", "junk"}:
        return False
    subject = decode_mime(msg.get("Subject", "")).lower()
    if any(word in subject for word in ("unsubscribe", "newsletter", "abmeldung")):
        return False
    return True


def collect_emails(days_to_fetch=2):
    if not COLLECT_LOCK.acquire(blocking=False):
        print("SbÄ›r uĹľ bÄ›ĹľĂ­, druhĂ˝ sbÄ›r pĹ™eskoÄŤen.", flush=True)
        return 0
    added = 0
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        status, _ = mail.select(IMAP_MAILBOX, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Nelze otevĹ™Ă­t mailbox {IMAP_MAILBOX}")
        since = (datetime.now(timezone.utc) - timedelta(days=days_to_fetch)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError("Gmail search selhal")
        ids = data[0].split()
        print(f"Nalezeno {len(ids)} zprĂˇv za poslednĂ­ch {days_to_fetch} dnĹŻ.", flush=True)
        for index, msg_id in enumerate(reversed(ids), 1):
            try:
                status, parts = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = next((p[1] for p in parts if isinstance(p, tuple) and isinstance(p[1], bytes)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                if not is_relevant_inbox_message(msg):
                    continue
                sender = decode_mime(msg.get("From", ""))
                subject = decode_mime(msg.get("Subject", "Bez pĹ™edmÄ›tu")) or "Bez pĹ™edmÄ›tu"
                date = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                if date is None:
                    date = datetime.now(timezone.utc)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                stable_id = (msg.get("Message-ID") or "").strip() or msg_id.decode("ascii", errors="ignore")
                record = {
                    "id": stable_id,
                    "imap_id": msg_id.decode("ascii", errors="ignore"),
                    "date": date.astimezone(timezone.utc).isoformat(),
                    "content": f"Od: {sender}\nPro: {decode_mime(msg.get('To', ''))}\nPĹ™edmÄ›t: {subject}\nObsah: {message_body(msg)}",
                }
                with CACHE_LOCK:
                    known = {item.get("id") for item in CACHED_EMAILS_DB}
                    if stable_id not in known:
                        CACHED_EMAILS_DB.append(record)
                        added += 1
                if index % 25 == 0:
                    save_cache()
                    print(f"ZpracovĂˇno {index}/{len(ids)}, novÄ› uloĹľeno {added}.", flush=True)
            except Exception as exc:
                print(f"Chyba u zprĂˇvy: {exc}", flush=True)
        with CACHE_LOCK:
            CACHED_EMAILS_DB.sort(key=lambda x: x.get("date", ""))
            if len(CACHED_EMAILS_DB) > MAX_CACHE_EMAILS:
                del CACHED_EMAILS_DB[:-MAX_CACHE_EMAILS]
        save_cache()
        print(f"SbÄ›r dokonÄŤen. Celkem v cache: {len(CACHED_EMAILS_DB)} zprĂˇv.", flush=True)
        return added
    except Exception as exc:
        print(f"Chyba pĹ™i sbÄ›ru Gmailu: {exc}", flush=True)
        return added
    finally:
        try:
            if mail:
                mail.logout()
        except Exception:
            pass
        COLLECT_LOCK.release()


def cached_emails(days=1):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with CACHE_LOCK:
        rows = list(CACHED_EMAILS_DB)
    result = []
    for row in rows:
        try:
            if datetime.fromisoformat(row["date"]) >= cutoff:
                result.append(row["content"])
        except Exception:
            result.append(row.get("content", ""))
    return result or ["Za zvolenĂ© obdobĂ­ nejsou v cache ĹľĂˇdnĂ© e-maily."]


def send_telegram(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000]}, timeout=20,
    )


def ask_openai(emails, prompt, days):
    if not ai_client:
        return "OPENAI_API_KEY nenĂ­ nastavenĂ˝."
    source = "\n\n---\n\n".join(emails[-250:])
    instruction = f"""Jsi seniornĂ­ CEO analytik RTI DE/AT. PiĹˇ ÄŤesky a pracuj pouze s dodanĂ˝mi e-maily.
Vynech automatickĂ© odpovÄ›di, newslettery, rutinnĂ­ Track & Trace a nerelevantnĂ­ zprĂˇvy.
Nic si nevymĂ˝Ĺˇlej. VytvoĹ™ struÄŤnĂ˝ manaĹľerskĂ˝ report za poslednĂ­ch {days} dnĹŻ.
UveÄŹ: shrnutĂ­, kritickĂˇ rizika, provoz/lokomotivy, finance a vĂ­cenĂˇklady, obchod,
energii, personĂˇl/bezpeÄŤnost/prĂˇvo, Ăşkoly s termĂ­ny a co pouze sledovat.

UĹľivatelskĂ˝ dotaz nebo poĹľadavek: {prompt}

E-MAILY:
{source}"""
    response = ai_client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        messages=[{"role": "system", "content": "Jsi pĹ™esnĂ˝ analytik pro CEO."}, {"role": "user", "content": instruction}],
        temperature=0.1, max_tokens=3500,
    )
    return response.choices[0].message.content or "Bez vĂ˝sledku."


def process_command(command, chat_id):
    global LAST_CHAT_ID
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        return
    LAST_CHAT_ID = chat_id
    cmd = command.strip().lower()
    if cmd in {"help", "pomoc"}:
        send_telegram(chat_id, "PĹ™Ă­kazy: r1 aĹľ r10, status, vip, critical, finance, lokomotivy. Lze napsat i vlastnĂ­ dotaz.")
        return
    if cmd == "status":
        with CACHE_LOCK:
            total = len(CACHED_EMAILS_DB)
        send_telegram(chat_id, f"DatabĂˇze obsahuje {total} unikĂˇtnĂ­ch pĹ™Ă­chozĂ­ch mailĹŻ. Zdroj: {IMAP_MAILBOX}.")
        return
    if cmd == "vip":
        send_telegram(chat_id, "SledovanĂˇ jmĂ©na: " + ", ".join(VIP_WATCH_LIST))
        return
    days = 1
    match = re.fullmatch(r"r(\d+)", cmd)
    if match:
        days = min(max(int(match.group(1)), 1), 10)
        send_telegram(chat_id, f"ZaÄŤĂ­nĂˇm report za poslednĂ­ch {days} dnĹŻâ€¦")
        collect_emails(days)
        send_telegram(chat_id, ask_openai(cached_emails(days), "VytvoĹ™ kompletnĂ­ manaĹľerskĂ˝ vĂ˝tah.", days))
        return
    query = cmd
    send_telegram(chat_id, f"HledĂˇm v pĹ™Ă­chozĂ­ch e-mailech: {command}â€¦")
    send_telegram(chat_id, ask_openai(cached_emails(7), query, 7))


def scheduled_collect():
    collect_emails(2)


def telegram_loop():
    global LAST_CHAT_ID
    requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", params={"drop_pending_updates": "true"}, timeout=10)
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_collect, "interval", minutes=15, max_instances=1, coalesce=True)
    scheduler.start()
    threading.Thread(target=scheduled_collect, daemon=True).start()
    offset = None
    while True:
        try:
            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = offset
            data = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params=params, timeout=30).json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                if message.get("text"):
                    process_command(message["text"], int(message["chat"]["id"]))
        except Exception as exc:
            print(f"Chyba Telegram smyÄŤky: {exc}", flush=True)
            time.sleep(5)


@app.get("/")
def health():
    return "RTI Mail CEO bot je online.", 200


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print(f"Bot je online. Cache: {CACHE_FILE}", flush=True)
    telegram_loop()
