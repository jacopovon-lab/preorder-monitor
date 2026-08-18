"""Monitor multi-sito preordini + restock watch, notifiche in gruppo Telegram.

Uso: python monitor.py
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; DRY_RUN=1 per stampare invece di inviare.
"""

import html
import json
import os
import sys
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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"sites": {}, "watch": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


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
    """Un messaggio per run; spezzato solo se supera il limite Telegram (4096)."""
    chunks, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > 4000:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks:
        send_telegram(chunk)


def main():
    with open(SITES_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    state = load_state()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    notifications, warnings = [], []
    run_sites(session, config.get("sites") or [], state, notifications, warnings)
    run_watch(session, config.get("watch") or [], state, notifications, warnings)

    blocks = notifications + warnings
    if blocks:
        send_all(blocks)
        print(f"Inviate {len(blocks)} notifiche")
    else:
        print("Nessuna novità")

    save_state(state)


if __name__ == "__main__":
    main()
