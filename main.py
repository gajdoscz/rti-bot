def background_email_collector_job(days_to_fetch=7):
    """Průběžně stahuje a parsuje maily do interní cache paměti na pozadí."""
    global CACHED_EMAILS_DB
    print(f"Spouštím sběr e-mailů do cache (okno: {days_to_fetch} dnů)...", flush=True)
    try:
        socket.setdefaulttimeout(15)
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(days=days_to_fetch)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK":
            mail.logout()
            return

        email_ids = messages[0].split()
        new_collected = []
        total_msgs = len(email_ids)
        print(nalezeno {total_msgs} zpráv k Zpracování., flush=True)

        for idx, e_id in enumerate(reversed(email_ids), 1):
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            if res != "OK":
                continue
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    sender = msg.get("From", "")
                    to_field = msg.get("To", "")
                    cc_field = msg.get("Cc", "")
                    
                    if "sales.de@railtrans.eu" in sender.lower() or "gajdoscz@gmail.com" in sender.lower():
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
                    email_record = {
                        "id": msg_id_str,
                        "content": f"Od: {sender} | Pro: {to_field} | Kopie: {cc_field}\nPředmět: {subject}\nObsah: {body[:600]}...\n{attachments_text}\n---"
                    }
                    
                    if not any(item['id'] == msg_id_str for item in CACHED_EMAILS_DB):
                        new_collected.append(email_record)

            if idx % 20 == 0:
                print(f"Zpracováno {idx}/{total_msgs} zpráv...", flush=True)

        CACHED_EMAILS_DB.extend(new_collected)
        if len(CACHED_EMAILS_DB) > 1200:
            CACHED_EMAILS_DB = CACHED_EMAILS_DB[-1200:]

        mail.logout()
        print(f"Cache aktualizována. Celkem v paměti: {len(CACHED_EMAILS_DB)} zpráv.", flush=True)
        check_vip_alerts(new_collected)

    except Exception as e:
        print(f"Chyba při průběžném sběru: {e}", flush=True)
