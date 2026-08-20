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


def slim(items):
    """Copia di un dict prodotti/varianti senza il campo image: nello stato
    salvato non serve (la foto si riprende fresca a ogni run) e pesa molto."""
    return {k: {f: v for f, v in it.items() if f != "image"} for k, it in items.items()}


def linkify(p):
    return f'<a href="{html.escape(p["url"], quote=True)}">{html.escape(p["title"])}</a>'


def notify(notifications, header, p, detail=None):
    """Accoda una notifica per singolo articolo: intestazione, titolo linkato,
    riga di dettaglio (prezzo ecc.), foto se disponibile."""
    lines = [header, linkify(p)]
    if detail:
        lines.append(detail)
    notifications.append({"text": "\n".join(lines), "photo": p.get("image")})


def price_line(p):
    if p.get("price") is None:
        return None
    return f'💰 <b>{format_price(p["price"])} CHF</b>'


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
        for k in new_keys:
            notify(notifications, f"🆕 <b>{html.escape(name)}</b>", products[k],
                   price_line(products[k]))
        print(f"[{name}] {len(products)} prodotti, {len(new_keys)} nuovi")
        entry["products"] = slim(products)


def format_price(value):
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


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

        header = f"💸 <b>Sconto — {html.escape(name)}</b>"
        deals = 0
        for k, p in products.items():
            if p.get("price") is None:
                continue
            old = known.get(k)
            if old is None:
                # nuovo nella sezione: segnala solo se già visibilmente scontato
                if p.get("compare_at"):
                    deals += 1
                    notify(notifications, header, p,
                           f'<s>{format_price(p["compare_at"])}</s> → '
                           f'<b>{format_price(p["price"])} CHF</b>')
            elif old.get("price") is not None and p["price"] < old["price"]:
                pct = round((old["price"] - p["price"]) / old["price"] * 100)
                deals += 1
                notify(notifications, header, p,
                       f'{format_price(old["price"])} → '
                       f'<b>{format_price(p["price"])} CHF</b> (−{pct}%)')
        print(f"[sconti {name}] {len(products)} prodotti, {deals} sconti")
        entry["products"] = slim(products)


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

        restock_header = f"🔄 <b>Di nuovo disponibile — {html.escape(name)}</b>"
        new_products, n_restock = {}, 0
        if appearance_based:
            for k, v in variants.items():
                if k not in known:
                    n_restock += 1
                    item = items[k]
                    notify(notifications, restock_header, item, price_line(item))
        else:
            # restock raggruppati per prodotto: un messaggio con tutte le
            # varianti tornate disponibili, ciascuna col suo prezzo
            restocked_products = {}
            for k, v in variants.items():
                if not (v["available"] and known.get(k, {}).get("available") is False):
                    continue
                n_restock += 1
                handle = k.rsplit(":", 1)[0]
                p = restocked_products.setdefault(handle, {
                    "title": v.get("product_title") or v["title"],
                    "url": v["url"], "image": v.get("image"), "variants": [],
                })
                vname = v["title"]
                prefix = f'{p["title"]} — '
                if vname.startswith(prefix):
                    vname = vname[len(prefix):]
                elif vname == p["title"]:
                    vname = None
                line = f"{format_price(v['price'])} CHF" if v.get("price") is not None else ""
                if vname:
                    line = f"{vname} — {line}" if line else vname
                if line:
                    p["variants"].append(f"• {html.escape(line)}")
            for p in restocked_products.values():
                notify(notifications, restock_header, p,
                       "\n".join(p["variants"]) or None)

            # prodotto mai visto prima (nessuna variante nota): notifica 🆕;
            # una variante nuova di un prodotto già noto entra invece in silenzio
            known_handles = {k.rsplit(":", 1)[0] for k in known}
            for k, v in variants.items():
                handle = k.rsplit(":", 1)[0]
                if handle in known_handles:
                    continue
                p = new_products.setdefault(handle, {
                    "title": v.get("product_title") or v["title"],
                    "url": v["url"], "image": v.get("image"),
                    "available": False, "price": None,
                })
                p["available"] = p["available"] or v["available"]
                if v.get("price") is not None and (p["price"] is None or v["price"] < p["price"]):
                    p["price"] = v["price"]

            for p in new_products.values():
                suffix = "" if p["available"] else " ⛔ esaurito"
                detail = price_line(p)
                notify(notifications, f"🆕 <b>{html.escape(name)}</b>", p,
                       f"{detail}{suffix}" if detail else (suffix.strip() or None))

        avail = sum(1 for v in variants.values() if v["available"])
        print(
            f"[restock {name}] {len(variants)} varianti ({avail} disponibili), "
            f"{n_restock} restock, {len(new_products)} nuovi"
        )
        entry["variants"] = slim(variants)


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
            notify(notifications, "🔄 <b>Di nuovo disponibile</b>", v, price_line(v))
        avail = sum(1 for v in variants.values() if v["available"])
        print(f"[watch {name}] {avail}/{len(variants)} varianti disponibili, {len(restocked)} restock")
        entry["variants"] = slim(variants)


def _telegram_post(method, payload):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    for attempt in range(3):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=60,
        )
        if resp.status_code == 429:  # rate limit: Telegram dice quanto aspettare
            wait = (resp.json().get("parameters") or {}).get("retry_after", 5)
            print(f"Telegram 429, attendo {wait}s")
            time.sleep(wait + 1)
            continue
        return resp
    return resp


def send_telegram(item):
    """item: {"text": ..., "photo": url o None}. Con foto usa sendPhoto (la
    caption regge max 1024 caratteri); se la foto viene rifiutata ripiega sul
    messaggio di solo testo."""
    text, photo = item["text"], item.get("photo")
    if os.environ.get("DRY_RUN"):
        print(f"--- DRY_RUN, messaggio non inviato (foto: {photo or 'nessuna'}) ---")
        print(text)
        return
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    if photo and len(text) <= 1024:
        resp = _telegram_post("sendPhoto", {
            "chat_id": chat_id, "photo": photo,
            "caption": text, "parse_mode": "HTML",
        })
        if resp.ok:
            return
        print(f"sendPhoto rifiutato ({resp.status_code}), invio solo testo", file=sys.stderr)
    resp = _telegram_post("sendMessage", {
        "chat_id": chat_id, "text": text[:4000],
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })
    resp.raise_for_status()


def send_all(items):
    """Un messaggio per articolo, con pausa per il rate limit di Telegram
    (~20 messaggi/min per chat)."""
    for i, item in enumerate(items):
        if isinstance(item, str):  # gli avvisi ⚠️/✅ sono semplici stringhe
            item = {"text": item, "photo": None}
        if i:
            time.sleep(3)
        send_telegram(item)


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
