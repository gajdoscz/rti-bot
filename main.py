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
TELEGRAM_BOT_TOKEN = "8628786539:AAH..."  # Tvůj platný token od @rti2_bot

ai_client = OpenAI(api_key=OPENAI_API_KEY)

# Úložiště pro připomínky a úkoly v paměti
SAVED_REMINDERS = []
# Uložíme si poslední známý chat_id pro automatické pondělní zprávy
LAST_CHAT_ID = None

def fetch_gmail_messages(days=1, keyword=None):
    """Stáhne e-maily z Gmailu za zadaný počet dnů (až 90) pomocí IMAP."""
    print(f"Stahuji e-maily z Gmailu (Dny: {days}, Klíčové slovo: {keyword})...", flush=True)
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
                            content_type = part.get_content_type()
                            content_disposition = str(part.get("Content-Disposition"))
                            if "attachment" not in content_disposition and content_type == "text/plain":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body = payload.decode("utf-8", errors="ignore")
                                    break
                    else:
                        payload = msg.get(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")

                    emails_data.append(f"Od: {sender}\nPředmět: {subject}\nObsah: {body[:500]}...\n---")

        mail.logout()
        return emails_data
    except Exception as e:
        print(f"Chyba při komunikaci s Gmail IMAP: {e}", flush=True)
        return []

def analyze_with_openai(emails_text, mode_description):
    """Propojení vnitřního světa RTI, trhu a soukromých záležitostí přes OpenAI."""
    print("Odesílám data do OpenAI k analýze...", flush=True)
    prompt = f"""
Jsi hlavní výkonný asistent a strategický poradce vrcholového manažera. Proveď hloubkovou analýzu propojující interní svět RTI s vnějším tržním prostředím (energetika, komodity, futures, tržní rizika).
Režim / Zaměření: {mode_description}

E-maily k analýze:
{'\n'.join(emails_text)}

Připrav strukturovaný exekutivní výstup v češtině rozdělený do těchto sekcí:
1. **🏢 Svět RTI / Interní operativa** (klíčové firemní úkoly, rozhodnutí, dispečink, termíny)
2. **📈 Tržní kontext & Externí vlivy** (energetika, futures kontrakty Cal, cenové trendy, dodavatelé)
3. **🏠 Soukromé záležitosti** (rodina, auto/servis, osobní termíny)
4. **⚠️ Strategická rizika a urgentní upozornění**
"""
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Jsi špičkový strategický asistent pro vrcholový management."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=1800
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Chyba při generování analýzy: {e}"

def transcribe_voice_message(file_id):
    """Přepis hlasové zprávy přes Whisper API."""
    try:
        file_info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        resp = requests.get(file_info_url, timeout=10).json()
        if not resp.get("ok"):
            return None
        
        file_path = resp["result"]["file_path"]
        audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        audio_bytes = requests.get(audio_url, timeout=15).content
        
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "voice_note.oga"
        
        transcript = ai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        return transcript.text
    except Exception as e:
        print(f"Chyba při přepisu hlasu: {e}", flush=True)
        return None

def text_to_speech(text):
    """Převede text na hlasovou zprávu (mp3) pomocí OpenAI TTS API."""
    try:
        # Ořízneme text pro řeč, ať to netrvá moc dlouho (max 500 znaků)
        short_text = text[:500] + ("..." if len(text) > 500 else "")
        response = ai_client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input=short_text
        )
        return io.BytesIO(response.content)
    except Exception as e:
        print(f"Chyba při generování TTS: {e}", flush=True)
        return None

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=15)
        return response.json()
    except Exception as e:
        print(f"Chyba při odesílání zprávy: {e}", flush=True)
        return None

def send_telegram_voice(chat_id, audio_io):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice"
    files = {"voice": ("reply.mp3", audio_io.getvalue())}
    data = {"chat_id": chat_id}
    try:
        requests.post(url, data=data, files=files, timeout=20)
    except Exception as e:
        print(f"Chyba při odesílání hlasové zprávy: {e}", flush=True)

def process_command(command, chat_id, is_voice=False):
    global LAST_CHAT_ID
    LAST_CHAT_ID = chat_id
    
    cmd = command.strip().lower()
    print(f"Zpracovávám příkaz: {cmd}", flush=True)
    
    nums = re.findall(r'\d+', cmd)
    days = int(nums[0]) if nums else 1
    if days > 90:
        days = 90

    # Správa připomínek
    if "pripomen" in cmd or "úkol" in cmd or "zapis" in cmd:
        SAVED_REMINDERS.append(command)
        reply_text = f"✅ Zapsal jsem si připomínku: '{command}'"
        send_telegram_message(chat_id, reply_text)
        if is_voice:
            audio = text_to_speech(reply_text)
            if audio: send_telegram_voice(chat_id, audio)
        return

    if "ukoly" in cmd or "pripominky" in cmd:
        if not SAVED_REMINDERS:
            reply_text = "Nemáš žádné uložené připomínky."
        else:
            reply_text = "📋 **Tvoje aktivní připomínky:**\n" + "\n".join([f"- {r}" for r in SAVED_REMINDERS])
        send_telegram_message(chat_id, reply_text)
        if is_voice:
            audio = text_to_speech(reply_text)
            if audio: send_telegram_voice(chat_id, audio)
        return

    # Reporty
    if "pondeli" in cmd or "weekly" in cmd:
        reply_text = f"📅 Generuji pravidelný pondělní komplexní report (RTI + Trh + Soukromé) za {days} dnů..."
        send_telegram_message(chat_id, reply_text)
        emails = fetch_gmail_messages(days=days)
        analysis = analyze_with_openai(emails or ["Žádné maily."], "Pravidelný pondělní reporting propojující RTI a trh.")
        if len(analysis) > 4000: analysis = analysis[:4000] + "\n\n*(Zpráva zkrácena)*"
        send_telegram_message(chat_id, analysis)
        if is_voice:
            audio = text_to_speech("Pondělní report byl vygenerován a odeslán v textu.")
            if audio: send_telegram_voice(chat_id, audio)

    elif "r" in cmd:
        send_telegram_message(chat_id, f"🚆 Generuji provozní dispečerský report (Railtrans/Trh) za {days} dnů...")
        emails = fetch_gmail_messages(days=days)
        analysis = analyze_with_openai(emails or ["Žádné maily."], f"Provozní přehled Railtrans v kontextu trhu za {days} dnů.")
        if len(analysis) > 4000: analysis = analysis[:4000] + "\n\n*(Zpráva zkrácena)*"
        send_telegram_message(chat_id, analysis)

    elif "s" in cmd or days > 0:
        send_telegram_message(chat_id, f"🔍 Generuji exekutivní souhrn (RTI + Trh + Soukromé) za {days} dnů...")
        emails = fetch_gmail_messages(days=days)
        analysis = analyze_with_openai(emails or ["Žádné maily."], f"Exekutivní přehled (RTI, Trh & Soukromé) za {days} dnů.")
        if len(analysis) > 4000: analysis = analysis[:4000] + "\n\n*(Zpráva zkrácena)*"
        send_telegram_message(chat_id, analysis)
        if is_voice:
            audio = text_to_speech("Exekutivní souhrn byl úspěšně připraven.")
            if audio: send_telegram_voice(chat_id, audio)

    elif "help" in cmd or "pomoc" in cmd:
        help_text = (
            "Dostupné příkazy:\n"
            "- **s1 až s90**: Exekutivní přehled (RTI + Trh + Soukromé)\n"
            "- **r1 až r90**: Provozní přehled (Railtrans)\n"
            "- **pondeli**: Týdenní report\n"
            "- **připomeň [text]**: Uložení připomínky\n"
            "- **úkoly**: Výpis uložených připomínek"
        )
        send_telegram_message(chat_id, help_text)
    else:
        send_telegram_message(chat_id, f"Neznámý příkaz: {cmd}. Napiš 'help'.")

def automated_monday_job():
    """Automatická úloha spouštěná každé pondělí v 9:00 na pozadí."""
    if not LAST_CHAT_ID:
        print("Automatické pondělní hlášení: Neznám LAST_CHAT_ID (napiš botovi jako první).", flush=True)
        return
    print("Spouštím automatický pondělní report v 9:00...", flush=True)
    send_telegram_message(LAST_CHAT_ID, "⏰ **Automatický pondělní report (9:00)**\nStahuji e-maily za posledních 7 dnů...")
    emails = fetch_gmail_messages(days=7)
    analysis = analyze_with_openai(emails or ["Žádné maily."], "Automatický týdenní pondělní report.")
    if len(analysis) > 4000: analysis = analysis[:4000] + "\n\n*(Zpráva zkrácena)*"
    send_telegram_message(LAST_CHAT_ID, analysis)

def run_scheduler():
    """Plánovač úloh běžící na pozadí."""
    scheduler = BackgroundScheduler()
    # Nastavení: Každé pondělí v 09:00 ráno
    scheduler.add_job(automated_monday_job, 'cron', day_of_week='mon', hour=9, minute=0)
    scheduler.start()

def run_telegram_bot():
    # Spuštění plánovače na pozadí
    run_scheduler()

    offset = 0
    print("Zjišťuji aktuální offset pro Telegram...", flush=True)
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1"
        resp = requests.get(url, timeout=10).json()
        if resp.get("ok") and resp.get("result"):
            offset = resp["result"][0]["update_id"] + 1
    except Exception as e:
        print(f"Poznámka při startovním offsetu: {e}", flush=True)

    print(f"Telegram bot běží a naslouchá příkazům (Startovní offset: {offset})...", flush=True)

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url, timeout=35)
            data = response.json()

            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        chat_id = message["chat"]["id"]
                        text_to_process = None
                        is_voice = False
                        
                        if "text" in message:
                            text_to_process = message["text"]
                        elif "voice" in message:
                            file_id = message["voice"]["file_id"]
                            send_telegram_message(chat_id, "🎙️ Poslouchám hlasovou zprávu...")
                            text_to_process = transcribe_voice_message(file_id)
                            is_voice = True
                            if text_to_process:
                                send_telegram_message(chat_id, f"🗣️ Rozpoznáno: *{text_to_process}*")
                        
                        if text_to_process:
                            process_command(text_to_process, chat_id, is_voice=is_voice)

        except Exception as e:
            print(f"Chyba v Telegram smyčce: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    run_telegram_bot()
