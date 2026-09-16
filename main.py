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
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

# --- KONFIGURACE ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GMAIL_USER = "gajdoscz@gmail.com"
GMAIL_APP_PASSWORD = "ueolepkubctpkdqn"
TELEGRAM_BOT_TOKEN = "8628786539:AAEjHL6fVdqkRHD63IEPerKvRLLZE0YnXT0"

ai_client = OpenAI(api_key=OPENAI_API_KEY)

SAVED_REMINDERS = []
VIP_WATCH_LIST = ["Gunvor", "Metrans", "Rail Force One", "Deutsche Bahn"]  
LAST_CHAT_ID = None
LAST_USER_ACTIVITY_DATE = None
SEEN_VIP_MESSAGE_IDS = set()

# Cache paměť pro průběžně sbírané maily (vyhneme se zdržování a výpadkům)
CACHED_EMAILS_DB = []

def extract_attachment_text(part):
    """Přečte Excelovou nebo CSV přílohu, ořízne ji na max 100 řádků kvůli tokenům a převede na text."""
    try:
        filename = part.get_filename()
        if not filename:
            return ""
        
        decoded_header = decode_header(filename)
        fname, encoding = decoded_header[0]
        if isinstance(fname, bytes):
            fname = fname.decode(encoding or "utf-8", errors="ignore")
        
        filename_lower = fname.lower()
        payload = part.get_payload(decode=True)
        if not payload:
            return ""

        file_bytes = io.BytesIO(payload)
        attachment_text = f"\n[PŘÍLOHA: {fname}]\n"

        if filename_lower.endswith(('.xlsx', '.xls')):
            dfs = pd.read_excel(file_bytes, sheet_name=None, dtype=str)
            for sheet_name, df in dfs.items():
                attachment_text += f"--- List: {sheet_name} ---\n"
                total_rows = len(df)
                if total_rows > 100:
                    df = df.head(100)
                    attachment_text += df.to_string(index=False) + f"\n[... tabulka zkrácena, zobrazeno prvních 100 z celkových {total_rows} řádků ...]\n"
                else:
                    attachment_text += df.to_string(index=False) + "\n"
        elif filename_lower.endswith('.csv'):
            df = pd.read_csv(file_bytes, dtype=str)
            total_rows = len(df)
            if total_rows > 100:
                df = df.head(100)
                attachment_text += df.to_string(index=False) + f"\n[... tabulka zkrácena, zobrazeno prvních 100 z celkových {total_rows} řádků ...]\n"
            else:
                attachment_text += df.to_string(index=False) + "\n"
        
        return attachment_text
    except Exception as e:
        print(f"Chyba při čtení přílohy: {e}", flush=True)
        return ""

def background_email_collector_job():
    """Průběžně každých 15 minut stahuje a parsuje maily do interní paměti (Cache)."""
    global CACHED_EMAILS_DB
    print("Spouštím průběžný sběr e-mailů do paměti na pozadí...", flush=True)
    try:
        socket.setdefaulttimeout(15)
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(days=4)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK":
            mail.logout()
            return

        email_ids = messages[0].split()
        new_collected = []

        for e_id in reversed(email_ids):
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            if res != "OK":
                continue
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    sender = msg.get("From", "")
                    to_field = msg.get("To", "")
                    cc_field = msg.get("Cc", "")
                    
                    # 🛡️ ANTI-LOOP OCHRANA: Ignorujeme vlastní odeslané zprávy nebo sales adresses
                    full_envelope = f"{sender} {to_field} {cc_field}".lower()
                    if "sales.de@railtrans.eu" in sender.lower() or "gajdoscz@gmail.com" in sender.lower():
                        # Pokud to posíláme my ven, ignorujeme to, aby se to necyklilo
                        if "automatický" in msg.get("Subject", "").lower() or "report" in msg.get("Subject", "").lower():
                            continue

                    subject_header = decode_header(msg["Subject"] or "Bez předmětu")
                    subject, encoding = subject_header[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8", errors="ignore")
                    
                    body = ""
                    attachments_text = ""

                    if msg.is_multipart():
                        for part in msg.walk():
                            content_disposition = str(part.get("Content-Disposition", ""))
                            if part.get_content_type() == "text/plain" and "attachment" not in content_disposition:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body = payload.decode("utf-8", errors="ignore")
                            elif "attachment" in content_disposition or part.get_filename():
                                attachments_text += extract_attachment_text(part)
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")

                    msg_id_str = e_id.decode('utf-8')
                    # Zkontrolujeme, zda už v cache není
                    if not any(item['id'] == msg_id_str for item in CACHED_EMAILS_DB):
                        email_record = {
                            "id": msg_id_str,
                            "date": msg.get("Date"),
                            "sender": sender,
                            "to": to_field,
                            "subject": subject,
                            "content": f"Od: {sender} | Pro: {to_field} | Kopie: {cc_field}\nPředmět: {subject}\nObsah: {body[:600]}...\n{attachments_text}\n---"
                        }
                        new_collected.append(email_record)

        # Přidáme do paměti a udržujeme maximálně posledních 300 položek
        CACHED_EMAILS_DB.extend(new_collected)
        if len(CACHED_EMAILS_DB) > 300:
            CACHED_EMAILS_DB = CACHED_EMAILS_DB[-300:]

        mail.logout()
        print(f"Průběžný sběr dokončen. Celkem v paměti: {len(CACHED_EMAILS_DB)} zpráv.", flush=True)

        # Spustíme i VIP check na nově nasbírané
        check_vip_alerts(new_collected)

    except Exception as e:
        print(f"Chyba při průběžném sběru: {e}", flush=True)

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
                alert_text = f"🚨 **VIP ALERT (Nová zpráva od '{vip}'):**\n\n{item['content'][:1000]}"
                send_telegram_message(LAST_CHAT_ID, alert_text)
                break

def get_emails_from_cache(days=1, recipient_filter=None, exclude_filter=None):
    """Vrátí vyfiltrované e-maily z lokální cache paměti podle dnů a filtrů."""
    cutoff = datetime.now() - timedelta(days=days)
    filtered = []
    
    for item in CACHED_EMAILS_DB:
        # Filtrování podle adresátů / exkluzí
        env = f"{item['sender']} {item['to']}".lower()
        if recipient_filter and recipient_filter.lower() not in env:
            continue
        if exclude_filter and exclude_filter.lower() in env:
            continue
        filtered.append(item["content"])
        
    return filtered if filtered else ["Žádné odpovídající e-maily v paměti za toto období."]

def analyze_with_openai(emails_text, mode_description):
    print("Odesílám data do OpenAI (gpt-4o)...", flush=True)
    prompt = f"""
Jsi špičkový operační dispečer a obchodní asistent vrcholového manažera v německé logistické společnosti. 
Tvým úkolem je zpracovat nasbíranou e-mailovou komunikaci **včetně tabulkových příloh (Excel)** do **maximálně podrobného, přesného a strukturovaného přehledu**.

Režim a instrukce: {mode_description}

E-maily k analýze:
{'\n'.join(emails_text)}
"""
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Jsi věcný, kritický a nekompromisní asistent pro top management."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=3000
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Chyba při OpenAI: {e}"

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Chyba sendMessage: {e}", flush=True)

def process_command(command, chat_id):
    global LAST_CHAT_ID, LAST_USER_ACTIVITY_DATE, VIP_WATCH_LIST
    LAST_CHAT_ID = chat_id
    LAST_USER_ACTIVITY_DATE = datetime.now().date()
    
    cmd = command.strip().lower()
    print(f"Zpracovávám příkaz: {cmd}", flush=True)
    
    if "help" in cmd or "pomoc" in cmd:
        send_telegram_message(chat_id, "Příkazy:\n- **sales1** až **sales7**: Sales přehled z cache paměti\n- **a1** až **a90**: Abweichung\n- **r1** až **r90**: Provoz Railtrans\n- **s1** až **s90**: Soukromé a ostatní\n- **pondeli**: Podklad za 168h\n- **vip**, **pridejvip [jméno]**, **smazvip [jméno]**")
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
    if days > 90: days = 90

    # SALES Z CACHE PAMĚTI (Podpora sales1 až sales7)
    if "sales" in cmd:
        sales_days = days if nums else 1
        if sales_days > 7: sales_days = 7
        
        send_telegram_message(chat_id, f"📈 Generuji sales přehled z cache za posledních {sales_days} dnů...")
        emails = get_emails_from_cache(days=sales_days, recipient_filter="sales.de@railtrans.eu")
        analysis = analyze_with_openai(emails, f"Sales přehled za {sales_days} dnů z Excel příloh a e-mailů: Seskup poptávky podle zákazníků, relace a spočítej celkový počet poptávaných vlaků.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    elif cmd.startswith('a'):
        send_telegram_message(chat_id, f"⚠️ Vyhledávám Abweichungen za {days} dnů...")
        emails = get_emails_from_cache(days=days)
        analysis = analyze_with_openai(emails, f"Dispečerský přehled Abweichungen za {days} dnů.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    elif cmd.startswith('r'):
        target_days = 1 if days == 1 else days
        send_telegram_message(chat_id, f"🚆 Generuji provozní přehled Railtrans za {target_days} dny...")
        emails = get_emails_from_cache(days=target_days)
        analysis = analyze_with_openai(emails, f"Provozní přehled Railtrans za {target_days} dnů.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    elif "pondeli" in cmd or "tyden" in cmd:
        send_telegram_message(chat_id, f"📅 Připravuji podklady za posledních 168 hodin...")
        emails = get_emails_from_cache(days=7)
        analysis = analyze_with_openai(emails, "Podklady pro poradu za 168 hodin: Problémy, spory, příběhy vlaků.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    elif cmd.startswith('s'):
        target_days = 1 if days == 1 else days
        send_telegram_message(chat_id, f"🔍 Generuji soukromý/ostatní přehled (mimo Railtrans)...")
        emails = get_emails_from_cache(days=target_days, exclude_filter="railtrans.eu")
        analysis = analyze_with_openai(emails, f"Soukromý přehled mimo Railtrans za {target_days} dnů.")
        send_telegram_message(chat_id, analysis[:4000])
        return

    else:
        send_telegram_message(chat_id, f"Neznámý příkaz: {cmd}. Napiš 'help'.")

# --- VEČERNÍ ŠPIČKOVÝ EXEKUTIVNÍ SOUHRN (18:00) S DOPORUČENÍMI ---
def automated_evening_executive_report_job():
    global LAST_CHAT_ID
    if not LAST_CHAT_ID: return
    
    print("Spouštím večerní exekutivní report s doporučeními...", flush=True)
    emails = get_emails_from_cache(days=1)
    
    prompt_executive = """
Připrav večerní exekutivní souhrn pro vrcholového manažera (Michala) za uplynulý den. 
Mluv lidsky, věcně a profesionálně (tykání, přímé oslovení).

Struktura reportu:
1. **Shrnutí situace**: Co dnes bylo nejdůležitější (problémy, více-náklady, provozní stavy).
2. **Kategorizované závěry**: Roztřiď události do logických oblastí (např. Problémy/Mimořádnosti, Obchod a poptávky, Důležité zprávy a schůzky).
3. **⚠️ Akční kroky & Doporučené reakce**: U každého kritického bodu jasně zformuluj, jak by měl manažer reagovat a jaký má být další krok (např. „Odpovědět firmě X ohledně stolu“, „Ověřit více-náklady na vlaku Y“).

Nezahrnuj technické detaily, pište čistě pro manažerské rozhodování.
"""
    analysis = analyze_with_openai(emails, prompt_executive)
    send_telegram_message(LAST_CHAT_ID, f"🌙 **Večerní exekutivní přehled:**\n\n{analysis[:4000]}" )

def run_telegram_bot():
    cleaned_token = TELEGRAM_BOT_TOKEN.strip()
    print("Inicializuji APScheduler s průběžným sběrem a večerním reportem...", flush=True)
    try:
        scheduler = BackgroundScheduler()
        # Průběžný sběr pošty každých 15 minut
        scheduler.add_job(background_email_collector_job, 'interval', minutes=15)
        # Večerní exekutivní report v 18:00
        scheduler.add_job(automated_evening_executive_report_job, 'cron', hour=18, minute=0)
        scheduler.start()
        
        # Okamžitě při startu spustíme jeden sběr, ať má cache data
        background_email_collector_job()
    except Exception as e:
        print(f"Chyba při startu scheduleru: {e}", flush=True)

    offset = None
    try:
        init_url = f"https://api.telegram.org/bot{cleaned_token}/getUpdates?timeout=1"
        init_resp = requests.get(init_url, timeout=5).json()
        if init_resp.get("ok") and init_resp.get("result"):
            offset = init_resp["result"][-1]["update_id"] + 1
    except Exception:
        pass

    while True:
        try:
            url = f"https://api.telegram.org/bot{cleaned_Token if 'cleaned_Token' in locals() else cleaned_token}/getUpdates?timeout=10"
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
