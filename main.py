import os
import time
from datetime import datetime, timedelta
import imaplib
import email
from email.header import decode_header
from openai import OpenAI
import requests
import threading

# --- KONFIGURACE ---
OPENAI_API_KEY = "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"

GMAIL_USER = "gajdos.cz@gmail.com"
GMAIL_APP_PASSWORD = "smwactgnwgmfmdog"
TELEGRAM_BOT_TOKEN = "8974469854:AAErqTuS4F0gTbGikw-RxAYbhU3Zw-23DhU" 

ai_client = OpenAI(api_key=OPENAI_API_KEY)

SAVED_NOTES = []
LAST_USER_ACTIVITY_DATE = None

def fetch_mails(days=1, mode="railtrans", keyword=None):
    print(f"Stahuji maily z Gmailu (Režim: {mode}, Dny: {days}, Klíčové slovo: {keyword})...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select('INBOX') 
        
        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        search_query = f'(SINCE "{since_date}")'
        
        status, messages = mail.search(None, search_query)
        if status != 'OK' or not messages[0]:
            mail.logout()
            return []

        email_ids = messages[0].split()
        mails_data = []

        for e_id in email_ids:
            res, msg_data = mail.fetch(e_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    subject = "Bez předmětu"
                    try:
                        raw_subj, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(raw_subj, bytes):
                            enc = encoding if encoding and encoding.lower() != 'unknown-8bit' else 'utf-8'
                            subject = raw_subj.decode(enc, errors="ignore")
                        else:
                            subject = str(raw_subj)
                    except Exception:
                        pass
                    
                    from_ = msg.get("From", "Neznámý odesílatel")
                    
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body = payload.decode("utf-8", errors="ignore")
                                break
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")

                    full_text = f"{subject} {body} {from_}".lower()

                    if keyword and keyword.lower() not in full_text:
                        continue

                    if not keyword:
                        is_railtrans = any(domain in from_.lower() for domain in ["railtrans.eu", "bls", "forwardis", "metrans"])
                        if mode == "railtrans" and not is_railtrans:
                            continue
                        if mode == "ostatni" and is_railtrans:
                            continue

                    mails_data.append({
                        "from": from_,
                        "subject": subject,
                        "body": body[:500]
                    })
                    
        mail.logout()
        return mails_data
    except Exception as e:
        print(f"Chyba při stahování z Gmailu: {e}")
        return []

def analyze_with_ai(mails_data, mode, days, keyword=None, extra_notes=None):
    if not mails_data and not extra_notes:
        return f"Žádné maily k zobrazení (Období: posledních {days} dnů)."

    mails_text = ""
    for i, m in enumerate(mails_data, 1):
        mails_text += f"\n--- EMAIL {i} ---\nOd: {m['from']}\nPředmět: {m['subject']}\nObsah: {m['body']}\n"

    notes_text = ""
    if extra_notes:
        notes_text = "\n--- ULOŽENÉ POZNÁMKY A TRH (DE/AT) ---\n" + "\n".join(extra_notes)

    target_desc = f"vyhledávání výrazu '{keyword}'" if keyword else ("Railtrans & Operativa" if mode == "railtrans" else "Ostatní záležitosti")

    prompt = f"""
Jsi osobní výkonný exekutivní asistent vrcholového manažera. Analyzuj následující podklady za posledních {days} dnů ({target_desc}) a vytvoř exekutivní přehled podle této struktury:

1. 🚨 KLÍČOVÉ UDÁLOSTI A PROBLÉMY:
   - Co nejdůležitějšího se nachází v datech a na trhu (DE/AT dráhy), co vyžaduje pozornost.
2. ✅ TO-DO LIST PRO MICHALA:
   - Konkrétní úkoly a akce k provedení formou přehledného checklistu (skvělé pro porady).
3. 👥 KONTEXT A DĚNÍ:
   - Stručný přehled souvislostí a zpráv z trhu.

Data k analýze:
{mails_text}
{notes_text}
"""

    response = ai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Jsi expertní exekutivní asistent."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(url, json=payload)
    print(f"Odeslána Telegram zpráva na chat_id {chat_id}, status: {resp.status_code}")

def download_telegram_file(file_path):
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    resp = requests.get(url)
    if resp.status_code == 200:
        audio_filename = "voice_note.ogg"
        with open(audio_filename, "wb") as f:
            f.write(resp.content)
        return audio_filename
    return None

def process_command(text, chat_id, is_voice=False):
    global LAST_USER_ACTIVITY_DATE
    LAST_USER_ACTIVITY_DATE = datetime.now().date()

    text_lower = text.strip().lower()
    print(f"-> Zpracovávám příkaz: '{text}' pro chat_id: {chat_id}")
    
    if text_lower in ["help", "/help"]:
        help_text = (
            "🤖 *RTI Assistant - Plná výbava*\n\n"
            "• *r* nebo *r[dny]* = Railtrans (např. `r`, `r7`)\n"
            "• *s* nebo *s[dny]* = Ostatní/soukromé (např. `s`, `s3`)\n"
            "• *t [slovo] [dny]* = Hledání tématu (např. `t exrtiw075 30`)\n"
            "• *save [text]* = Uložení poznámky / novinky z trhu (DE/AT) pro pondělní briefing\n"
            "🎙️ *Hlasové poznámky:* Můžeš poslat i audio zprávu!"
        )
        send_telegram_message(chat_id, help_text)
        return

    if text_lower.startswith("save "):
        note_content = text[5:]
        SAVED_NOTES.append(note_content)
        send_telegram_message(chat_id, f"📝 Poznámka uložena do pondělního briefingu:\n_{note_content}_")
        return

    parts = text_lower.split()
    if parts and parts[0] == "t" and len(parts) >= 2:
        keyword = parts[1]
        days = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 30
        
        send_telegram_message(chat_id, f"Hledám '{keyword}' za posledních {days} dnů... 🔍")
        mails = fetch_mails(days=days, keyword=keyword)
        summary = analyze_with_ai(mails, "search", days, keyword=keyword)
        send_telegram_message(chat_id, summary)
        return

    cmd = parts[0] if parts else "r"
    mode = "railtrans"
    if cmd.startswith("s"):
        mode = "ostatni"
    
    days = 1
    digits = "".join(filter(str.isdigit, cmd))
    if digits:
        days = int(digits)

    voice_prefix = "🎙️ *(Zpracováno z hlasové zprávy)*\n\n" if is_voice else ""
    send_telegram_message(chat_id, f"Generuji přehled ({mode}, období: {days} dnů)... ⏳")
    
    mails = fetch_mails(days=days, mode=mode)
    summary = analyze_with_ai(mails, mode, days)
    send_telegram_message(chat_id, voice_prefix + summary)

def background_scheduler(chat_id):
    global LAST_USER_ACTIVITY_DATE
    last_monday_briefing = None
    last_evening_check = None

    while True:
        try:
            now = datetime.now()
            
            if now.weekday() == 0 and now.hour == 9 and now.minute == 0:
                if last_monday_briefing != now.date():
                    print("Spouštím pondělní briefing...")
                    send_telegram_message(chat_id, "📅 *Pondělní porada-ready briefing (Railtrans & Trh DE/AT)* 🚀")
                    mails = fetch_mails(days=7, mode="railtrans")
                    summary = analyze_with_ai(mails, "railtrans", 7, extra_notes=SAVED_NOTES)
                    send_telegram_message(chat_id, summary)
                    SAVED_NOTES.clear()
                    last_monday_briefing = now.date()

            if now.hour == 18 and now.minute == 0:
                if last_evening_check != now.date():
                    if LAST_USER_ACTIVITY_DATE != now.date():
                        print("Spouštím večerní pojistku...")
                        send_telegram_message(chat_id, "🌙 *Večerní automatický přehled (celý den bez dotazu)*:")
                        m_rail = fetch_mails(days=1, mode="railtrans")
                        sum_rail = analyze_with_ai(m_rail, "railtrans", 1)
                        send_telegram_message(chat_id, "🚂 *Railtrans (za 24h):*\n" + sum_rail)
                        
                        m_oth = fetch_mails(days=1, mode="ostatni")
                        sum_oth = analyze_with_ai(m_oth, "ostatni", 1)
                        send_telegram_message(chat_id, "📂 *Ostatní / Soukromé (za 24h):*\n" + sum_oth)
                    
                    last_evening_check = now.date()
        except Exception as e:
            print(f"Chyba v scheduleru: {e}")

        time.sleep(30)

def run_telegram_bot():
    print("🤖 Zjišťuji aktuální offset pro Telegram...")
    # Vyčištění staré fronty a získání nejnovějšího ID zprávy
    init_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1"
    try:
        init_resp = requests.get(init_url, timeout=10).json()
        offset = 0
        if init_resp.get("ok") and init_resp.get("result"):
            offset = init_resp["result"][-1]["update_id"] + 1
    except Exception as e:
        print(f"Varování při inicializaci offsetu: {e}")
        offset = 0

    print(f"🤖 Telegram bot běží a naslouchá příkazům (Startovní offset: {offset})...")
    
    target_chat_id = "5209676333" 
    scheduler_thread = threading.Thread(target=background_scheduler, args=(target_chat_id,), daemon=True)
    scheduler_thread.start()

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url, timeout=35)
            data = response.json()
            
            if data.get("ok"):
                for result in data.get("result", []):
                    offset = result["update_id"] + 1
                    msg = result.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    
                    if not chat_id:
                        continue

                    if "text" in msg:
                        text = msg["text"]
                        print(f">>> [TEXT] Přijat příkaz od uživatele: '{text}' (chat_id: {chat_id})")
                        process_command(text, chat_id)
                    
                    elif "voice" in msg or "audio" in msg:
                        print(f">>> [AUDIO] Přijata hlasová zpráva od chat_id: {chat_id}")
                        file_id = msg["voice"]["file_id"] if "voice" in msg else msg["audio"]["file_id"]
                        file_info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
                        file_info_resp = requests.get(file_info_url).json()
                        
                        if file_info_resp.get("ok"):
                            file_path = file_info_resp["result"]["file_path"]
                            audio_file = download_telegram_file(file_path)
                            
                            if audio_file:
                                send_telegram_message(chat_id, "🎙️ Přepisuji hlasovou zprávu přes Whisper...")
                                with open(audio_file, "rb") as f:
                                    transcript = ai_client.audio.transcriptions.create(
                                        model="whisper-1",
                                        file=f
                                    )
                                transcribed_text = transcript.text
                                send_telegram_message(chat_id, f"📝 *Rozpoznáno:* _{transcribed_text}_")
                                os.remove(audio_file)
                                process_command(transcribed_text, chat_id, is_voice=True)

        except Exception as e:
            print(f"Chyba v Telegram smyčce: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_telegram_bot()