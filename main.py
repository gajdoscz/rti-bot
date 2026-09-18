import os
import time
from datetime import datetime, timedelta
import imaplib
import email
from email.header import decode_header
import email.utils
from openai import OpenAI
import requests
import re
import socket
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
import threading

# --- KONFIGURACE ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GMAIL_USER = "gajdos.cz@gmail.com"
GMAIL_APP_PASSWORD = "ueolepkubctpkdqn"
TELEGRAM_BOT_TOKEN = "8628786539:AAEjHL6fVdqkRHD63IEPerKvRLLZE0YnXT0"

ai_client = OpenAI(api_key=OPENAI_API_KEY)

VIP_WATCH_LIST = ["Gunvor", "Metrans", "Rail Force One", "Deutsche Bahn"]  
LAST_CHAT_ID = None
SEEN_VIP_MESSAGE_IDS = set()

CACHED_EMAILS_DB = []
CACHE_LOCK = threading.Lock()

# Flask webový server
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot je online a běží (Railtrans dávkový režim)!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def background_email_collector_job(days_to_fetch=2):
    """Rychle stahuje texty e-mailů do cache paměti."""
    global CACHED_EMAILS_DB
    print(f"Spouštím rychlý sběr textů e-mailů do cache (okno: {days_to_fetch} dny)...", flush=True)
    try:
        socket.setdefaulttimeout(20)
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(days=days_to_fetch)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK":
            mail.logout()
            return

        email_ids = messages[0].split()
        total_msgs = len(email_ids)
        print(f"Nalezeno celkem {total_msgs} zpráv k zpracování.", flush=True)

        new_for_vip = []

        for idx, e_id in enumerate(reversed(email_ids), 1):
            try:
                res, msg_data = mail.fetch(e_id, "(RFC822)")
                if res != "OK":
                    continue
                
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        sender = msg.get("From", "")
                        to_field = msg.get("To", "")
                        cc_field = msg.get("Cc", "")
                        
                        # Anti-loop ochrana
                        if "gajdos.cz" in sender.lower() or "gajdoscz" in sender.lower():
                            if "automatický" in msg.get("Subject", "").lower() or "report" in msg.get("Subject", "").lower():
                                continue

                        subject_header = decode_header(msg["Subject"] or "Bez předmětu")
                        subject, encoding = subject_header[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding or "utf-8", errors="ignore")
                        
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode("utf-8", errors="ignore")
                                        break
                        else:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="ignore")

                        msg_id_str = e_id.decode('utf-8')
                        email_record = {
                            "id": msg_id_str,
                            "content": f"Od: {sender} | Pro: {to_field} | Kopie: {cc_field}\nPředmět: {subject}\nObsah: {body[:1500]}\n---"
                        }
                        
                        with CACHE_LOCK:
                            if not any(item['id'] == msg_id_str for item in CACHED_EMAILS_DB):
                                CACHED_EMAILS_DB.append(email_record)
                                new_for_vip.append(email_record)

            except Exception as inner_e:
                print(f"Chyba u zprávy: {inner_e}", flush=True)
                continue

            if idx % 50 == 0 or idx == total_msgs:
                print(f"Zpracováno {idx}/{total_msgs} zpráv...", flush=True)

        if len(CACHED_EMAILS_DB) > 1500:
            with CACHE_LOCK:
                CACHED_EMAILS_DB = CACHED_EMAILS_DB[-1500:]

        mail.logout()
        print(f"Sběr dokončen. Celkem v paměti: {len(CACHED_EMAILS_DB)} zpráv.", flush=True)
        check_vip_alerts(new_for_vip)

    except Exception as e:
        print(f"Chyba při sběru: {e}", flush=True)

def check_vip_alerts(new_emails):
    global LAST_CHAT_ID, VIP_WATCH_LIST, SEEN_VIP_MESSAGE_IDS
    if not LAST_CHAT_ID or not VIP_WATCH_LIST:
        return
    
    for item in new_emails:
        msg_id = item["id"]
        if msg_id in SEEN_VIP_MESSAGE_IDS:
            continue
        
        full_text = item["content"].lower()
        for vip in VIP_WATCH_LIST:
            if vip.lower() in full_text:
                SEEN_VIP_MESSAGE_IDS.add(msg_id)
                alert_text = f"🚨 **VIP ALERT (Zpráva od '{vip}'):**\n\n{item['content'][:1000]}"
                send_telegram_message(LAST_CHAT_ID, alert_text)
                break

def get_emails_from_cache(days=1, chat_id=None):
    global CACHED_EMAILS_DB
    with CACHE_LOCK:
        is_empty = len(CACHED_EMAILS_DB) == 0

    if is_empty:
        if chat_id:
            send_telegram_message(chat_id, "📥 **Cache je prázdná.** Stahuji čerstvá data...")
        background_email_collector_job(days_to_fetch=days)
        
    with CACHE_LOCK:
        return [item["content"] for item in CACHED_EMAILS_DB] if CACHED_EMAILS_DB else ["Žádné e-maily v paměti."]

def call_openai_single(text_chunk, mode_description):
    prompt = f"""
Jsi hlavní dispečerský analytik a špičkový operační asistent vrcholového manažera logistické společnosti Railtrans. 
Zpracuj níže uvedenou e-mailovou komunikaci do **podrobného a strukturovaného provozního přehledu**.

Vypíchněte konkrétní detaily:
1. **Mimořádnosti, zpoždění a problémy na tratích** (stanice, relace, čísla vlaků, důvody).
2. **Stav přeprav a obchodu** (poptávky, kapacity, partneři jako Gunvor, Metrans, DB apod.).
3. **Klíčové provozní informace a akční závěry pro manažera**.

Instrukce: {mode_description}

Data:
{text_chunk}
"""
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Jsi věcný, kritický a nekompromisní asistent pro top management."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=2500
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Chyba při OpenAI: {e}"

def analyze_with_openai(emails_text, mode_description, chat_id=None):
    combined_text = '\n'.join(emails_text)
    chunk_size = 35000  # Bezpečná velikost textu pro jeden požadavek pod limitem tokenů
    
    # Pokud se data vejdou do jedné dávky, pošleme rovnou
    if len(combined_text) <= chunk_size:
        if chat_id:
            send_telegram_message(chat_id, "🧠 **Analyzuji data pomocí OpenAI (gpt-4o)...**")
        return call_openai_single(combined_text, mode_description)
    
    # Jinak rozsekáme na dávky (chunking)
    chunks = [combined_text[i:i+chunk_size] for i in range(0, len(combined_text), chunk_size)]
    if chat_id:
        send_telegram_message(chat_id, f"📦 Objem dat je příliš velký, rozděleno do **{len(chunks)} dávek**. Zpracovávám postupně...")
    
    partial_summaries = []
    for idx, chunk in enumerate(chunks, 1):
        print(f"Zpracovávám dávku {idx}/{len(chunks)}...", flush=True)
        summary = call_openai_single(chunk, f"Část {idx}/{len(chunks)} - {mode_description}")
        partial_summaries.append(summary)
    
    if chat_id:
        send_telegram_message(chat_id, "🔗 **Syntetizuji závěrečný souhrn** ze všech dávek dohromady...")
    
    synthesis_prompt = f"Spoj a strukturuj následující dílčí analýzy (dávky 1 až {len(chunks)}) do jednoho finálního, vyčerpávajícího přehledu pro manažera:\n\n" + "\n\n--- DALŠÍ ČÁST ---\n\n".join(partial_summaries)
    
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Jsi věcný, kritický a nekompromisní asistent pro top management. Syntetizuj data do přehledného celku."},
                {"role": "user", "content": synthesis_prompt}
            ],
            temperature=0.1,
            max_tokens=3000
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Chyba při finální syntéze OpenAI: {e}"

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Chyba sendMessage: {e}", flush=True)

def process_command(command, chat_id):
    global LAST_CHAT_ID, VIP_WATCH_LIST
    LAST_CHAT_ID = chat_id
    
    cmd = command.strip().lower()
    print(f"Zpracovávám příkaz: {cmd}", flush=True)
    
    if "help" in cmd or "pomoc" in cmd:
        send_telegram_message(chat_id, "Dostupné příkazy:\n- **r1** až **r10**: Provozní přehled Railtrans za 1 až 10 dnů\n- **vip**: Zobrazit sledovaná VIP\n- **pridejvip [jméno]**: Přidat VIP\n- **smazvip [jméno]**: Smazat VIP")
        return

    if cmd.startswith("pridejvip"):
        name_to_add = command.replace("pridejvip", "").strip()
        if name_to_add and name_to_add not in VIP_WATCH_LIST:
            VIP_WATCH_LIST.append(name_to_add)
        send_telegram_message(chat_id, f"✅ Přidal jsem '{name_to_add}' do VIP hlídání.")
        return

    if cmd.startswith("smazvip"):
        name_to_rem = command.replace("smazvip", "").strip()
        if name_to_rem in VIP_WATCH_LIST:
            VIP_WATCH_LIST.remove(name_to_rem)
        send_telegram_message(chat_id, f"🗑️ Odebral jsem '{name_to_rem}' z VIP hlídání.")
        return

    if cmd == "vip":
        list_str = "\n".join([f"- {n}" for n in VIP_WATCH_LIST])
        send_telegram_message(chat_id, f"📋 **Sledovaní VIP:**\n{list_str}")
        return

    nums = re.findall(r'\d+', cmd)
    days = int(nums[0]) if nums else 1
    if days > 10: days = 10

    if cmd.startswith('r'):
        send_telegram_message(chat_id, f"🔍 Zahajuji zpracování: Hledám e-maily za posledních {days} dnů...")
        emails = get_emails_from_cache(days=days, chat_id=chat_id)
        analysis = analyze_with_openai(emails, f"Provozní přehled Railtrans za {days} dnů: Seskup události, mimořádnosti a provozní stav.", chat_id=chat_id)
        send_telegram_message(chat_id, analysis[:4000])
        return
    else:
        send_telegram_message(chat_id, f"Neznámý příkaz: {cmd}. Napiš 'help'.")

def automated_morning_railtrans_job():
    global LAST_CHAT_ID
    if not LAST_CHAT_ID: 
        return
    print("Spouštím automatické ranní shrnutí Railtrans v 8:00...", flush=True)
    send_telegram_message(LAST_CHAT_ID, "🌅 **Dobré ráno! Spouštím automatické ranní shrnutí provozu Railtrans za posledních 24 hodin...**")
    emails = get_emails_from_cache(days=1, chat_id=LAST_CHAT_ID)
    analysis = analyze_with_openai(emails, "Ranní dispečerský přehled Railtrans za posledních 24 hodin: Klíčové události, zpoždění, problémy a stav vlaků.", chat_id=LAST_CHAT_ID)
    send_telegram_message(LAST_CHAT_ID, analysis[:4000])

def run_telegram_bot():
    cleaned_token = TELEGRAM_BOT_TOKEN.strip()
    print("Inicializuji APScheduler a Flask server...", flush=True)
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(automated_morning_railtrans_job, 'cron', hour=8, minute=0)
        scheduler.add_job(background_email_collector_job, 'interval', minutes=15)
        scheduler.start()
    except Exception as e:
        print(f"Chyba při startu scheduleru: {e}", flush=True)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    init_collector_thread = threading.Thread(target=background_email_collector_job, kwargs={"days_to_fetch": 2}, daemon=True)
    init_collector_thread.start()

    offset = None
    try:
        init_url = f"https://api.telegram.org/bot{cleaned_token}/getUpdates?timeout=1"
        init_resp = requests.get(init_url, timeout=5).json()
        if init_resp.get("ok") and init_resp.get("result"):
            offset = init_resp["result"][-1]["update_id"] + 1
    except Exception:
        pass

    print("Bot je plně online a poslouchá Telegram (Railtrans dávkový režim)...", flush=True)

    while True:
        try:
            url = f"https://api.telegram.org/bot{cleaned_token}/getUpdates?timeout=10"
            if offset:
                url += f"&offset={offset}"

            response = requests.get(url, timeout=15)
            data = response.json()

            if not data.get("ok"):
                if data.get("error_code") == 409:
                    time.sleep(5)
                    continue
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message and "text" in message:
                    chat_id = message["chat"]["id"]
                    text_to_process = message["text"]
                    process_command(text_to_process, chat_id)

        except Exception as e:
            print(f"CHYBA V HLAVNÍ SMYČCE: {e}", flush=True)
            time.sleep(5)
        
        time.sleep(2)

if __name__ == "__main__":
    run_telegram_bot()
