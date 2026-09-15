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
OPENAI_API_KEY = "sk-proj-10tudiMtXkG_qE57ZnndC12-d7c4ipi9CUJ3o4jydsOzswk0i-YNncjZbrRIV7_xePrUfT1MbT3IbKFJZ-v1K4pmhSE51T1DFzhVhuXa6KSkXgStPKbVLPe-1KMtLc9a"

GMAIL_USER = "gajdoscz@gmail.com"
GMAIL_APP_PASSWORD = "smwactngwgmfmdog"
TELEGRAM_BOT_TOKEN = "8974469854:AAFZGWFEZ_GAxmh3p0-YffZMXSs0LnzcJk8"

ai_client = OpenAI(api_key=OPENAI_API_KEY)

SAVED_NOTES = []
LAST_USER_ACTIVITY_DATE = None

def fetch_mails(days=1, mode="railtrans", keyword=None):
    print(f"Stahuji baily z Gmailu (Režim: {mode}, Dny: {days}, Klíčové slovo: {keyword})...")
    pass

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Chyba při odesílání Telegram zprávy: {e}")
        return None

def process_command(command, chat_id, is_voice=False):
    cmd = command.strip().lower()
    print(f"Zpracovávám příkaz: {cmd}")
    
    if cmd == "s1" or "s1" in cmd:
        send_telegram_message(chat_id, "Spouštím analýzu e-mailů (s1)...")
    elif cmd == "help":
        send_telegram_message(chat_id, "Dostupné příkazy:\n- s1: Spuštění analýzy e-mailů")
    else:
        send_telegram_message(chat_id, f"Neznámý příkaz: {cmd}. Napiš 'help' pro nápovědu.")

def run_telegram_bot():
    offset = 0
    print("Zjišťuji aktuální offset pro Telegram...")
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1"
        resp = requests.get(url, timeout=10).json()
        if resp.get("ok") and resp.get("result"):
            offset = resp["result"][0]["update_id"] + 1
    except Exception as e:
        print(f"Poznámka při startovním offsetu: {e}")

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
                    if message and "text" in message:
                        chat_id = message["chat"]["id"]
                        text = message["text"]
                        print(f"Přijata zpráva z Telegramu: '{text}' (chat_id: {chat_id})", flush=True)
                        process_command(text, chat_id)

        except Exception as e:
            print(f"Chyba v Telegram smyčce: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    run_telegram_bot()
