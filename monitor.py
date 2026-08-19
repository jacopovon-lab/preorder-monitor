"""Monitor multi-sito preordini + restock watch, notifiche in gruppo Telegram.

Uso: python monitor.py
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; DRY_RUN=1 per stampare invece di inviare.
"""

import html
import json
import os
import sys
import time
import traceback

import requests
import yaml

from adapters import html as html_adapter
from adapters import shopify

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
SITES_FILE = os.path.join(os.path.dirname(__file__), "sites.yml")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Con cron ogni 5 minuti: 12 fallimenti consecutivi = ~1 ora di down.
FAIL_ALERT_THRESHOLD = 12

ADAPTERS = {"shopify": shopify.fetch_products, "html": html_adapter.fetch_products}
WATCH_ADAPTERS = {"shopify": shopify.fetch_watch}
WATCH_SECTION_ADAPTERS = {"shopify": shopify.fetch_section_variants}


class CachedSession:
    """Le stesse pagine servono a più liste (es. restock + sconti sulla stessa
    sezione): ogni URL viene scaricato una sola volta per run."""

    def __init__(self, session):
        self._session = session
        self._cache = {}

    def get(self, url, params=None, **kwargs):
        key = (url, tuple(sorted((params or {}).items())))
        if key not in self._cache:
            self._cache[key] = self._session.get(url, params=params, **kwargs)
        return self._cache[key]


def skip_for_interval(entry, config_item, name):
    """True se la voce ha un `interval` (minuti) e non è ancora scaduto.
    Serve per sezioni pesanti (molte pagine HTML) da non scaricare ogni 5 min."""
    interval = config_item.get("interval")
    if not interval:
        return False
    if time.time() - entry.get("last_check", 0) < interval * 60 - 90:
        print(f"[{name}] salto: prossimo controllo tra al massimo {interval} min")
        return True
    entry["last_check"] = time.time()
    return False


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"sites": {}, "watch": {}, "sales": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        f.write("\n")


def suspicious_shrink(known, current):
    """True se la lista si è ridotta di oltre metà rispetto allo stato salvato:
    quasi sempre una pagina servita male (Wix senza SSR, anti-bot, paginazione
    interrotta), non una rimozione reale. In quel caso lo stato non va toccato,
    altrimenti al ripristino tutto ciò che 'ricompare' sembrerebbe nuovo."""
    return bool(known) and len(current) < 0.5 * len(known)


def check_failure(entry, name, kind, warnings):
    """Aggiorna il contatore fallimenti e produce l'avviso al superamento soglia."""
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    print(f"[{name}] errore ({kind}), fallimento consecutivo n. {entry['fail_count']}")
    if entry["fail_count"] == FAIL_ALERT_THRESHOLD:
        entry["alerted"] = True
        hours = FAIL_ALERT_THRESHOLD * 5 // 60
        warnings.append(
            f"⚠️ <b>{html.escape(name)}</b> non risponde da circa {hours} ora/e "
            f"({entry['fail_count']} tentativi falliti)."
        )


def check_recovery(entry, name, warnings):
    if entry.get("alerted"):
        warnings.append(f"✅ <b>{html.escape(name)}</b> risponde di nuovo.")
    entry["fail_count"] = 0
    entry["alerted"] = False


def run_sites(session, sites, state, notifications, warnings):
    for site in sites:
        name = site["name"]
        entry = state["sites"].setdefault(name, {})
        try:
            fetch = ADAPTERS[site["type"]]
            products = fetch(session, site)
        except Exception:
            print(f"[{name}] fetch fallito:\n{traceback.format_exc()}", file=sys.stderr)
            check_failure(entry, name, "sito", warnings)
            continue  # stato prodotti NON toccato: al ripristino niente falsi nuovi

        if suspicious_shrink(entry.get("products"), products):
            check_failure(entry, name, "riduzione sospetta", warnings)
            continue

        check_recovery(entry, name, warnings)

        if "products" not in entry:  # primo avvio per questo sito: silenzioso
            print(f"[{name}] primo avvio, salvo {len(products)} prodotti senza notificare")
            entry["products"] = products
            continue

        new_keys = [k for k in products if k not in entry["products"]]
        if new_keys:
            lines = [f"🆕 <b>{html.escape(name)}</b>"]
            for k in new_keys:
                p = products[k]
                lines.append(
                    f'• <a href="{html.escape(p["url"], quote=True)}">'
                    f'{html.escape(p["title"])}</a>'
                )
            notifications.append("\n".join(lines))
        print(f"[{name}] {len(products)} prodotti, {len(new_keys)} nuovi")
        entry["products"] = products


def format_price(value):
    return f"{value:.2f}".rstrip("0").rstrip(".")


def run_sales(session, sections, state, notifications, warnings):
    """Sezioni sorvegliate per SCONTI: notifica cali di prezzo rispetto al run
    precedente e prodotti che compaiono già con prezzo barrato."""
    for section in sections:
        name = section["name"]
        entry = state["sales"].setdefault(name, {})
        if skip_for_interval(entry, section, f"sconti {name}"):
            continue
        try:
            fetch = ADAPTERS[section["type"]]
            products = fetch(session, section)
        except Exception:
            print(f"[sconti {name}] fetch fallito:\n{traceback.format_exc()}", file=sys.stderr)
            check_failure(entry, f"sconti: {name}", "sconti", warnings)
            continue

        if suspicious_shrink(entry.get("products"), products):
            check_failure(entry, f"sconti: {name}", "riduzione sospetta", warnings)
            continue

        check_recovery(entry, f"sconti: {name}", warnings)

        known = entry.get("products")
        if known is None:
            print(f"[sconti {name}] primo avvio, salvo {len(products)} prodotti senza notificare")
            entry["products"] = products
            continue

        deals = []
        for k, p in products.items():
            if p.get("price") is None:
                continue
            old = known.get(k)
            if old is None:
                # nuovo nella sezione: segnala solo se già visibilmente scontato
                if p.get("compare_at"):
                    deals.append(
                        f'• <a href="{html.escape(p["url"], quote=True)}">'
                        f'{html.escape(p["title"])}</a>: '
                        f'<s>{format_price(p["compare_at"])}</s> → '
                        f'<b>{format_price(p["price"])} CHF</b>'
                    )
            elif old.get("price") is not None and p["price"] < old["price"]:
                pct = round((old["price"] - p["price"]) / old["price"] * 100)
                deals.append(
                    f'• <a href="{html.escape(p["url"], quote=True)}">'
                    f'{html.escape(p["title"])}</a>: '
                    f'{format_price(old["price"])} → '
                    f'<b>{format_price(p["price"])} CHF</b> (−{pct}%)'
                )
        if deals:
            notifications.append(f"💸 <b>Sconti — {html.escape(name)}</b>\n" + "\n".join(deals))
        print(f"[sconti {name}] {len(products)} prodotti, {len(deals)} sconti")
        entry["products"] = products


def run_watch_sections(session, sections, state, notifications, warnings):
    """Sezioni intere sorvegliate per RESTOCK: notifica ogni variante che passa
    da esaurita a disponibile, su tutti i prodotti della sezione."""
    for section in sections:
        name = section["name"]
        entry = state["watch_sections"].setdefault(name, {})
        if skip_for_interval(entry, section, f"restock {name}"):
            continue
        appearance_based = section["type"] not in WATCH_SECTION_ADAPTERS
        try:
            if appearance_based:
                # Siti HTML che elencano solo i disponibili (esauriti nascosti o
                # filtrati via URL): una comparsa nella lista = disponibile.
                items = ADAPTERS[section["type"]](session, section)
                variants = {
                    k: {"title": p["title"], "url": p["url"], "available": True}
                    for k, p in items.items()
                }
            else:
                variants = WATCH_SECTION_ADAPTERS[section["type"]](session, section)
        except Exception:
            print(f"[restock {name}] fetch fallito:\n{traceback.format_exc()}", file=sys.stderr)
            check_failure(entry, f"restock: {name}", "restock", warnings)
            continue

        if suspicious_shrink(entry.get("variants"), variants):
            check_failure(entry, f"restock: {name}", "riduzione sospetta", warnings)
            continue

        check_recovery(entry, f"restock: {name}", warnings)

        known = entry.get("variants")
        if known is None:
            print(f"[restock {name}] primo avvio, salvo {len(variants)} varianti senza notificare")
            entry["variants"] = variants
            continue

        new_products = {}
        if appearance_based:
            restocked = [v for k, v in variants.items() if k not in known]
        else:
            restocked = [
                v for k, v in variants.items()
                if v["available"] and known.get(k, {}).get("available") is False
            ]
            # prodotto mai visto prima (nessuna variante nota): notifica 🆕;
            # una variante nuova di un prodotto già noto entra invece in silenzio
            known_handles = {k.rsplit(":", 1)[0] for k in known}
            for k, v in variants.items():
                handle = k.rsplit(":", 1)[0]
                if handle in known_handles:
                    continue
                p = new_products.setdefault(handle, {
                    "title": v.get("product_title") or v["title"],
                    "url": v["url"],
                    "available": False,
                })
                p["available"] = p["available"] or v["available"]

        if new_products:
            lines = [f"🆕 <b>{html.escape(name)}</b>"]
            for p in new_products.values():
                suffix = "" if p["available"] else " (esaurito)"
                lines.append(
                    f'• <a href="{html.escape(p["url"], quote=True)}">'
                    f'{html.escape(p["title"])}</a>{suffix}'
                )
            notifications.append("\n".join(lines))

        if restocked:
            lines = [f"🔄 <b>Di nuovo disponibile — {html.escape(name)}</b>"]
            for v in restocked:
                lines.append(
                    f'• <a href="{html.escape(v["url"], quote=True)}">'
                    f'{html.escape(v["title"])}</a>'
                )
            notifications.append("\n".join(lines))
        avail = sum(1 for v in variants.values() if v["available"])
        print(
            f"[restock {name}] {len(variants)} varianti ({avail} disponibili), "
            f"{len(restocked)} restock, {len(new_products)} nuovi"
        )
        entry["variants"] = variants


def run_watch(session, watch_items, state, notifications, warnings):
    for item in watch_items:
        name = item["name"]
        entry = state["watch"].setdefault(name, {})
        try:
            fetch = WATCH_ADAPTERS[item["type"]]
            variants = fetch(session, item)
        except Exception:
            print(f"[watch {name}] fetch fallito:\n{traceback.format_exc()}", file=sys.stderr)
            check_failure(entry, f"watch: {name}", "watch", warnings)
            continue

        check_recovery(entry, f"watch: {name}", warnings)

        if not variants:
            print(f"[watch {name}] nessuna variante corrisponde al filtro", file=sys.stderr)
            continue

        known = entry.get("variants")
        restocked = []
        if known is not None:
            for vid, v in variants.items():
                was = known.get(vid, {}).get("available")
                if v["available"] and was is False:
                    restocked.append(v)
        else:
            print(f"[watch {name}] primo avvio, salvo stato senza notificare")

        for v in restocked:
            notifications.append(
                f'🔄 <b>Di nuovo disponibile</b>\n'
                f'• <a href="{html.escape(v["url"], quote=True)}">'
                f'{html.escape(v["title"])}</a>'
            )
        avail = sum(1 for v in variants.values() if v["available"])
        print(f"[watch {name}] {avail}/{len(variants)} varianti disponibili, {len(restocked)} restock")
        entry["variants"] = variants


def send_telegram(text):
    if os.environ.get("DRY_RUN"):
        print("--- DRY_RUN, messaggio non inviato ---")
        print(text)
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def send_all(blocks):
    """Un messaggio per sito/sezione; un blocco oltre il limite Telegram (4096)
    viene spezzato per righe, senza tagliare un prodotto a metà."""
    first = True
    for block in blocks:
        chunks, current = [], ""
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 4000:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        for chunk in chunks:
            if not first:
                time.sleep(3)  # rate limit Telegram: ~20 messaggi/min per chat
            send_telegram(chunk)
            first = False


def main():
    with open(SITES_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    state = load_state()
    plain = requests.Session()
    plain.headers["User-Agent"] = USER_AGENT
    session = CachedSession(plain)

    notifications, warnings = [], []
    state.setdefault("sales", {})
    state.setdefault("watch_sections", {})
    run_sites(session, config.get("sites") or [], state, notifications, warnings)
    run_sales(session, config.get("sales") or [], state, notifications, warnings)
    run_watch_sections(session, config.get("watch_sections") or [], state, notifications, warnings)
    run_watch(session, config.get("watch") or [], state, notifications, warnings)

    blocks = notifications + warnings
    if blocks:
        configured = os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
        if not configured and not os.environ.get("DRY_RUN"):
            # Senza secrets non fallire il run: lo stato non viene salvato,
            # così le novità restano in coda e partiranno al primo run configurato.
            print(f"Secrets Telegram mancanti: {len(blocks)} notifiche in coda, stato non salvato")
            return
        send_all(blocks)
        print(f"Inviate {len(blocks)} notifiche")
    else:
        print("Nessuna novità")

    save_state(state)


if __name__ == "__main__":
    main()
