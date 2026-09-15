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
import io
import socket
from apscheduler.schedulers.background import BackgroundScheduler

# --- KONFIGURACE ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GMAIL_USER = "gajdoscz@gmail.com"
GMAIL_APP_PASSWORD = "ueolepkubctpkdqn"
TELEGRAM_BOT_TOKEN = "8628786539:AAEjHL6fVdqkRHD63IEPerKvRLLZE0YnXT0"

ai_client = OpenAI(api_key=OPENAI_API_KEY)

SAVED_REMINDERS = []
LAST_CHAT_ID = None
LAST_USER_ACTIVITY_DATE = None  # Sledování aktivity pro denní 18:00 report

def fetch_gmail_messages(days=1, keyword=None, exclude_keyword=None):
    print(f"Stahuji e-maily z Gmailu (okno: {days} dnů, klíčové slovo: {keyword}, exkluze: {exclude_keyword})...", flush=True)
    emails_data = []
    try:
        socket.setdefaulttimeout(15)

        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        search_days = max(days, 2)
        since_date = (datetime.now() - timedelta(days=search_days)).strftime("%d-%b-%Y")
        
        if keyword:
            search_criteria = f'(SINCE "{since_date}" TEXT "{keyword}")'
        else:
            search_criteria = f'(SINCE "{since_date}")'

        status, messages = mail.search(None, search_criteria)
        if status != "OK":
            print("Vyhledávání v Gmailu nevrátilo OK statut.", flush=True)
            return []

        email_ids = messages[0].split()
        total_found = len(email_ids)
        print(f"Nalezeno {total_found} kandidátů, aplikuji filtr...", flush=True)

        cutoff_time = datetime.now() - timedelta(days=days)
        matched_count = 0

        for idx, e_id in enumerate(email_ids):
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            if res != "OK":
                continue
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    msg_date_header = msg.get("Date")
                    if msg_date_header:
                        try:
                            msg_dt = email.utils.parsedate_to_datetime(msg_date_header)
                            if msg_dt.tzinfo is not None:
                                msg_dt = msg_dt.astimezone().replace(tzinfo=None)
                            if msg_dt < cutoff_time:
                                continue
                        except Exception:
                            pass

                    sender = msg.get("From", "")
                    
                    # Exkluze (např. pro 's' vyřadíme @railtrans.eu)
                    if exclude_keyword and exclude_keyword.lower() in sender.lower():
                        continue

                    matched_count += 1
                    subject_header = decode_header(msg["Subject"] or "Bez předmětu")
                    subject, encoding = subject_header[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8", errors="ignore")
                    
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

                    emails_data.append(f"Od: {sender}\nPředmět: {subject}\nObsah: {body[:600]}...\n---")

        mail.logout()
        print(f"Úspěšně filtrováno: {matched_count} e-mailů.", flush=True)
        return emails_data
    except Exception as e:
        print(f"Chyba při IMAP stahování: {e}", flush=True)
        return []

def analyze_with_openai(emails_text, mode_description):
    print("Odesílám data do OpenAI, čekám na vygenerování reportu...", flush=True)
    prompt = f"""
Jsi špičkový exekutivní asistent vrcholového manažera v německé logistické a železniční společnosti. 
Dej mi maximálně stručný, věcný a nekompromisní přehled formou odrážek. Žádné obecné fráze. Uváděj konkrétní jména, firmy, čísla vlaků a události.
Režim: {mode_description}

E-maily:
{'\n'.join(emails_text)}
"""
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Jsi věcný a nekompromisní asistent pro management."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=2000
        )
        print("Analýza od OpenAI byla úspěšně dokončena.", flush=True)
        return response.choices[0].message.content
    except Exception as e:
        print(f"Chyba při OpenAI: {e}", flush=True)
        return f"Chyba při OpenAI: {e}"

def transcribe_voice_message(file_id):
    try:
        file_info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        resp = requests.get(file_info_url, timeout=10).json()
        if not resp.get("ok"): return None
        
        file_path = resp["result"]["file_path"]
        audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        audio_bytes = requests.get(audio_url, timeout=15).content
        
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "voice_note.oga"
        
        transcript = ai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        return transcript.text
    except Exception as e:
        print(f"Chyba Whisper: {e}", flush=True)
        return None

def text_to_speech(text):
    try:
        short_text = text[:400]
        response = ai_client.audio.speech.create(model="tts-1", voice="alloy", input=short_text)
        return io.BytesIO(response.content)
    except Exception as e:
        print(f"Chyba TTS: {e}", flush=True)
        return None

def send_telegram_message(chat_id, text):
    print("Odesílám hotovou zprávu zpět do Telegramu...", flush=True)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=15)
        print("Zpráva byla úspěšně doručena do Telegramu.", flush=True)
    except Exception as e:
        print(f"Chyba sendMessage: {e}", flush=True)

def send_telegram_voice(chat_id, audio_io):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
    files = {"voice": ("reply.mp3", audio_io.getvalue())}
    data = {"chat_id": chat_id}
    try:
        requests.post(url, data=data, files=files, timeout=20)
    except Exception as e:
        print(f"Chyba sendVoice: {e}", flush=True)

def process_command(command, chat_id, is_voice=False):
    global LAST_CHAT_ID, LAST_USER_ACTIVITY_DATE
    LAST_CHAT_ID = chat_id
    LAST_USER_ACTIVITY_DATE = datetime.now().date()  # Zaznamenáme aktivitu uživatele
    
    cmd = command.strip().lower()
    print(f"Zpracovávám příkaz: {cmd}", flush=True)
    
    if "help" in cmd or "pomoc" in cmd:
        send_telegram_message(chat_id, "Příkazy:\n- **a1** až **a90**: Abweichung (mimořádnosti)\n- **r1** až **r90**: Provoz Railtrans\n- **s1** až **s90**: Soukromé a ostatní (vše kromě @railtrans.eu)\n- **pondeli** (nebo týden): Podklad pro poradu za 168h (problémy, spory, trasy, DB InfraGO výluky)\n- **připomeň [text]**, **úkoly**")
        if is_voice:
            audio = text_to_speech("Tady je nápověda k příkazům.")
            if audio: send_telegram_voice(chat_id, audio)
        return

    if "pripomen" in cmd or "úkol" in cmd or "zapis" in cmd:
        SAVED_REMINDERS.append(command)
        reply = f"✅ Zapsal jsem si připomínku: '{command}'"
        send_telegram_message(chat_id, reply)
        if is_voice:
            audio = text_to_speech(reply)
            if audio: send_telegram_voice(chat_id, audio)
        return

    if "ukoly" in cmd or "pripominky" in cmd:
        reply = "📋 **Aktivní připomínky:**\n" + ("\n".join([f"- {r}" for r in SAVED_REMINDERS]) if SAVED_REMINDERS else "Žádné.")
        send_telegram_message(chat_id, reply)
        if is_voice:
            audio = text_to_speech(reply)
            if audio: send_telegram_voice(chat_id, audio)
        return

    nums = re.findall(r'\d+', cmd)
    days = int(nums[0]) if nums else 1
    if days > 90: days = 90

    # 1. ABWEICHUNG (Mimořádnosti)
    if cmd.startswith('a'):
        send_telegram_message(chat_id, f"⚠️ Vyhledávám Abweichungen za {days} dnů...")
        emails = fetch_gmail_messages(days=days, keyword="Abweichung")
        analysis = analyze_with_openai(emails or ["Žádné Abweichung e-maily nenalezeny."], f"Dispečerský přehled Abweichungen za {days} dnů.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    # 2. RAILTRANS (Pouze @railtrans.eu nebo klíčové slovo railtrans)
    elif cmd.startswith('r'):
        target_days = 1 if days == 1 else days
        send_telegram_message(chat_id, f"🚆 Generuji provozní přehled Railtrans za {target_days} dny...")
        emails = fetch_gmail_messages(days=target_days, keyword="Railtrans")
        analysis = analyze_with_openai(emails or ["Žádné maily od Railtrans."], f"Provozní přehled Railtrans za {target_days} dnů.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    # 3. PONDĚLNÍ PORADA / TÝDENNÍ SYSTÉMOVÝ SOUHRN (168 hodin)
    elif "pondeli" in cmd or "weekly" in cmd or "tyden" in cmd:
        send_telegram_message(chat_id, f"📅 Připravuji podklady pro pondělní poradu za posledních 168 hodin (problémy, spory, trasy, výluky)...")
        # Stáhneme vše za 7 dnů (168h) a zaměříme se na provoz, trasy a DB InfraGO výluky
        emails = fetch_gmail_messages(days=7, keyword=None)
        
        prompt_mode = """
PODKLADY PRO PONDĚLNÍ PORADU (za posledních 168 hodin / 7 dnů):
Proveď hloubkovou analýzu e-mailů s důrazem na:
1. **🔥 Problémy, spory a nutné reakce** (Kdo s kým se dohaduje, kde hoří bota, co vyžaduje okamžitý manažerský zásah).
2. **🛤️ Trasy & Koridory (Kudy jezdíme)** (Identifikuj z e-mailů klíčové trasy, tratě a přepravní relace, na kterých se pohybujeme).
3. **🚧 Výhled provozu a výluky (DB InfraGO apod.)** (Vyhledej zmínky o výlukách, omezeních, změnách jízdních řádů, tiskových zprávách správců infrastruktury či dodavatelů a shrň, co nás čeká příští týden).
"""
        analysis = analyze_with_openai(emails or ["Žádné maily."], prompt_mode)
        send_telegram_message(chat_id, analysis[:4000])
        return

    # 4. SOUKROMÉ A OSTATNÍ (Vše kromě @railtrans.eu)
    elif cmd.startswith('s') or days > 0:
        target_days = 1 if days == 1 else days
        send_telegram_message(chat_id, f"🔍 Generuji soukromý/ostatní přehled (vše kromě Railtrans)...")
        emails = fetch_gmail_messages(days=target_days, keyword=None, exclude_keyword="railtrans.eu")
        analysis = analyze_with_openai(emails or ["Žádné maily."], f"Soukromý a ostatní přehled (mimo Railtrans) za {target_days} dnů.")
        send_telegram_message(chat_id, analysis[:4000])
        if is_voice:
            audio = text_to_speech("Přehled je hotový.")
            if audio: send_telegram_voice(chat_id, audio)
        return

    else:
        send_telegram_message(chat_id, f"Neznámý příkaz: {cmd}. Napiš 'help'.")

def automated_daily_18_job():
    """Automatický denní report v 18:00, pokud se uživatel celý den neozval."""
    global LAST_USER_ACTIVITY_DATE, LAST_CHAT_ID
    if not LAST_CHAT_ID: return
    
    today = datetime.now().date()
    if LAST_USER_ACTIVITY_DATE != today:
        print("Uživatel se dnes neozval, spouštím automatický report v 18:00...", flush=True)
        send_telegram_message(LAST_CHAT_ID, "⏰ Automatický denní report (žádná dnešní aktivita na Telegramu):")
        emails = fetch_gmail_messages(days=1, keyword=None)
        analysis = analyze_with_openai(emails or ["Žádné maily."], "Automatický denní přehled při neaktivitě.")
        send_telegram_message(LAST_CHAT_ID, analysis[:4000])
    else:
        print("Uživatel byl dnes aktivní, automatický 18:00 report se vynechává.", flush=True)

def automated_monday_job():
    if not LAST_CHAT_ID: return
    send_telegram_message(LAST_CHAT_ID, "⏰ Automatický pondělní týdenní report (168h)...")
    emails = fetch_gmail_messages(days=7, keyword=None)
    analysis = analyze_with_openai(emails or ["Žádné maily."], "Automatický týdenní souhrn pro poradu (problémy, trasy, výluky).")
    send_telegram_message(LAST_CHAT_ID, analysis[:4000])

def run_telegram_bot():
    cleaned_token = TELEGRAM_BOT_TOKEN.strip()
    print(f"DEBUG Token - délka: {len(cleaned_token)}, začátek: {cleaned_token[:10]}", flush=True)

    print("Inicializuji APScheduler...", flush=True)
    try:
        scheduler = BackgroundScheduler()
        # Pondělní porada v 9:00
        scheduler.add_job(automated_monday_job, 'cron', day_of_week='mon', hour=9, minute=0)
        # Denní kontrola v 18:00 (pokud nebyla aktivita)
        scheduler.add_job(automated_daily_18_job, 'cron', hour=18, minute=0)
        scheduler.start()
        print("Scheduler úspěšně spuštěn (včetně denního 18:00 hídače).", flush=True)
    except Exception as e:
        print(f"Chyba při startu scheduleru: {e}", flush=True)

    print("Vstupuji do hlavní smyčky Telegram getUpdates...", flush=True)
    processed_update_ids = set()

    while True:
        try:
            url = f"https://api.telegram.org/bot{cleaned_token}/getUpdates?timeout=10"
            response = requests.get(url, timeout=15)
            data = response.json()

            if not data.get("ok"):
                err_code = data.get("error_code")
                if err_code == 409:
                    print("Konflikt instancí (409), čekám na uvolnění linky...", flush=True)
                    time.sleep(5)
                    continue
                print(f"Telegram API chyba: {data}", flush=True)
                time.sleep(5)
                continue

            results = data.get("result", [])
            for update in results:
                update_id = update["update_id"]
                if update_id in processed_update_ids:
                    continue
                
                processed_update_ids.add(update_id)
                if len(processed_update_ids) > 100:
                    processed_update_ids.pop()

                message = update.get("message")
                if message:
                    chat_id = message["chat"]["id"]
                    text_to_process = None
                    is_voice = False
                    
                    if "text" in message:
                        text_to_process = message["text"]
                        print(f"--> PŘIJAT TEXT OD UŽIVATELE: {text_to_process}", flush=True)
                    elif "voice" in message:
                        print(f"--> Přijata hlasová zpráva, stahuji...", flush=True)
                        send_telegram_message(chat_id, "🎙️ Zpracovávám hlasovku...")
                        text_to_process = transcribe_voice_message(message["voice"]["file_id"])
                        is_voice = True
                    
                    if text_to_process:
                        print(f"Spouštím process_command pro: {text_to_process}", flush=True)
                        process_command(text_to_process, chat_id, is_voice=is_voice)

        except Exception as e:
            print(f"CHYBA V HLAVNÍ SMYČCE: {e}", flush=True)
            time.sleep(5)
        
        time.sleep(2)

if __name__ == "__main__":
    run_telegram_bot()
