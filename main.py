import os
import time
from datetime import datetime, timedelta
import imaplib
import email
from email.header import decode_header
from openai import OpenAI
import requests
import re
import io
from apscheduler.schedulers.background import BackgroundScheduler

# --- KONFIGURACE ---
OPENAI_API_KEY = "sk-proj-10tudiMtXkG_qE57ZnndC12-d7c4ipi9CUJ3o4jydsOzswk0i-YNncjZbrRIV7_xePrUfT1MbT3IbKFJZ-v1K4pmhSE51T1DFzhVhuXa6KSkXgStPKbVLPe-1KMtLc9a"

GMAIL_USER = "gajdoscz@gmail.com"
GMAIL_APP_PASSWORD = "smwactngwgmfmdog"
TELEGRAM_BOT_TOKEN = "8628786539:AAG9kPZQyC1knyZapa3OgDB3weisvxfnno"

ai_client = OpenAI(api_key=OPENAI_API_KEY)

SAVED_REMINDERS = []
LAST_CHAT_ID = None

def fetch_gmail_messages(days=1, keyword=None):
    print(f"Stahuji e-maily z Gmailu (Dny: {days})...", flush=True)
    emails_data = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        search_criteria = f'(SINCE "{since_date}")'
        if keyword:
            search_criteria = f'(SINCE "{since_date}" TEXT "{keyword}")'

        status, messages = mail.search(None, search_criteria)
        if status != "OK":
            return []

        email_ids = messages[0].split()
        for e_id in email_ids[-40:]:
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            if res != "OK":
                continue
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject_header = decode_header(msg["Subject"] or "Bez předmětu")
                    subject, encoding = subject_header[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8", errors="ignore")
                    
                    sender = msg.get("From", "Neznámý")
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

                    emails_data.append(f"Od: {sender}\nPředmět: {subject}\nObsah: {body[:500]}...\n---")

        mail.logout()
        return emails_data
    except Exception as e:
        print(f"Chyba při IMAP: {e}", flush=True)
        return []

def analyze_with_openai(emails_text, mode_description):
    prompt = f"""
Jsi hlavní výkonný asistent a strategický poradce vrcholového manažera. Proveď analýzu propojující interní svět RTI s vnějším tržním prostředím.
Režim: {mode_description}

E-maily:
{'\n'.join(emails_text)}

Výstup rozdělen do sekcí:
1. **🏢 Svět RTI / Interní operativa**
2. **📈 Tržní kontext & Externí vlivy**
3. **🏠 Soukromé záležitosti**
4. **⚠️ Strategická rizika a urgentní upozornění**
"""
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Jsi strategický asistent pro management."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=1500
        )
        return response.choices[0].message.content
    except Exception as e:
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
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=15)
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
    global LAST_CHAT_ID
    LAST_CHAT_ID = chat_id
    
    cmd = command.strip().lower()
    print(f"Zpracovávám příkaz: {cmd}", flush=True)
    
    nums = re.findall(r'\d+', cmd)
    days = int(nums[0]) if nums else 1
    if days > 90: days = 90

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

    if "pondeli" in cmd or "weekly" in cmd:
        send_telegram_message(chat_id, f"📅 Generuji pondělní report za {days} dnů...")
        emails = fetch_gmail_messages(days=days)
        analysis = analyze_with_openai(emails or ["Žádné maily."], "Pondělní reporting RTI & Trh.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    elif "r" in cmd:
        send_telegram_message(chat_id, f"🚆 Generuji provozní report Railtrans za {days} dnů...")
        emails = fetch_gmail_messages(days=days)
        analysis = analyze_with_openai(emails or ["Žádné maily."], "Provozní přehled Railtrans.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    elif "s" in cmd or days > 0:
        send_telegram_message(chat_id, f"🔍 Generuji exekutivní report za {days} dnů...")
        emails = fetch_gmail_messages(days=days)
        analysis = analyze_with_openai(emails or ["Žádné maily."], "Exekutivní přehled RTI, Trh & Soukromé.")
        send_telegram_message(chat_id, analysis[:4000])
        if is_voice:
            audio = text_to_speech("Exekutivní souhrn je hotový.")
            if audio: send_telegram_voice(chat_id, audio)
        return

    elif "help" in cmd or "pomoc" in cmd:
        send_telegram_message(chat_id, "Příkazy: s1-s90, r1-r90, pondeli, připomeň [text], úkoly")
    else:
        send_telegram_message(chat_id, f"Neznámý příkaz: {cmd}. Napiš 'help'.")

def automated_monday_job():
    if not LAST_CHAT_ID: return
    send_telegram_message(LAST_CHAT_ID, "⏰ Automatický pondělní report...")
    emails = fetch_gmail_messages(days=7)
    analysis = analyze_with_openai(emails or ["Žádné maily."], "Automatický týdenní report.")
    send_telegram_message(LAST_CHAT_ID, analysis[:4000])

def run_telegram_bot():
    print("Inicializuji APScheduler...", flush=True)
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(automated_monday_job, 'cron', day_of_week='mon', hour=9, minute=0)
        scheduler.start()
        print("Scheduler úspěšně spuštěn.", flush=True)
    except Exception as e:
        print(f"Chyba při startu scheduleru: {e}", flush=True)

    offset = 0
    print("Vstupuji do hlavní smyčky Telegram getUpdates...", flush=True)

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=25"
            response = requests.get(url, timeout=30)
            data = response.json()

            if not data.get("ok"):
                print(f"Telegram API vrátilo chybu: {data}", flush=True)
                time.sleep(5)
                continue

            results = data.get("result", [])
            if results:
                print(f"Přijato {len(results)} nových aktualizací z Telegramu!", flush=True)

            for update in results:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    chat_id = message["chat"]["id"]
                    text_to_process = None
                    is_voice = False
                    
                    if "text" in message:
                        text_to_process = message["text"]
                        print(f"Přijat text od uživatele: {text_to_process}", flush=True)
                    elif "voice" in message:
                        print(f"Přijata hlasová zpráva, stahuji...", flush=True)
                        send_telegram_message(chat_id, "🎙️ Zpracovávám hlasovku...")
                        text_to_process = transcribe_voice_message(message["voice"]["file_id"])
                        is_voice = True
                    
                    if text_to_process:
                        print(f"Spouštím process_command pro: {text_to_process}", flush=True)
                        process_command(text_to_process, chat_id, is_voice=is_voice)

        except Exception as e:
            print(f"CHYBA V HLAVNÍ SMYČCE: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    run_telegram_bot()
