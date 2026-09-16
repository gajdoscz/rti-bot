def automated_tuesday_industry_job():
    global LAST_CHAT_ID
    if not LAST_CHAT_ID: return
    print("Spouštím automatický úterní oborový report z veřejných zdrojů...", flush=True)
    send_telegram_message(LAST_CHAT_ID, "⏰ Generuji úterní oborový report (DE / AT trh) z veřejných zdrojů...")
    
    # Zde aktivně vyžádáme aktuální oborové trendy z veřejného prostoru pro GPT-4o
    prompt_industry = """
Připrav špičkový oborový report o trhu železniční nákladní dopravy v Německu a Rakousku za uplynulý týden.
Čerpej ze svých aktuálních znalostí a veřejných trendů (zahrnující weby jako Bahnblogstelle, tiskové zprávy DB InfraGO, ÖBB, Bundesnetzagentur, oborové svazy).

Zaměř se na:
1. **Infrastruktura a výluky**: Aktuální stav sítě, koridorové uzávěry (např. německé tratě, dopady na Rakousko / „Deutsches Eck“), plány DB InfraGO a ÖBB.
2. **Poplatky a energetika**: Vývoj cen energií, poplatky za dopravní cestu (Trassengebühren, rozhodnutí Bundesnetzagentur / EU soudu).
3. **Trh práce a mzdy**: Kolektivní smlouvy (EVG, GDL), mzdové požadavky a personální situace.
4. **Odborné články a zdroje**: Uveď klíčové zprávy a přidej k nim reálné online odkazy na zdroje (např. Bahnblogstelle, ÖBB Presse apod.).

Mluv věcně, profesionálně a strukturovaně pro top management.
"""
    analysis = analyze_with_openai([], prompt_industry) # Nezávislé na e-mailech, čerpá z analýzy trhu
    send_telegram_message(LAST_CHAT_ID, analysis[:4000])
