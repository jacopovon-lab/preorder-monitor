"""Adapter HTML generico: scraping con selettori CSS definiti in sites.yml."""

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


def fetch_products(session, site):
    """Ritorna {chiave: {"title": ..., "url": ...}}.

    Config in sites.yml:
      selector        card prodotto (o direttamente l'<a> se contiene già titolo+link)
      title_selector  (opzionale) elemento col titolo dentro la card
      link_selector   (opzionale) <a> dentro la card
      price_selector  (opzionale) elemento col prezzo dentro la card (per sezioni sconti)
      page_param      (opzionale) parametro di paginazione (es. "p", "page"):
                      scarica le pagine successive finché trova prodotti nuovi
      max_pages       (opzionale, default 50) tetto di sicurezza alla paginazione

    La chiave univoca è il path dell'URL prodotto.
    """
    products = {}
    page_param = site.get("page_param")
    for page in range(1, site.get("max_pages", 50) + 1):
        url = site["url"]
        if page_param and page > 1:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{page_param}={page}"
        page_products = _fetch_page(session, site, url)
        # pagina vuota o già vista (i siti spesso ripetono l'ultima pagina): fine
        if not page_products or all(k in products for k in page_products):
            break
        products.update(page_products)
        if not page_param:
            break
    return products


def _fetch_page(session, site, url):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products = {}
    for card in soup.select(site["selector"]):
        link_sel = site.get("link_selector")
        link_el = card.select_one(link_sel) if link_sel else card
        if link_el is None or not link_el.get("href"):
            continue
        url = urljoin(site["url"], link_el["href"]).split("?")[0]

        title_sel = site.get("title_selector")
        title_el = card.select_one(title_sel) if title_sel else card
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        price = None
        price_sel = site.get("price_selector")
        if price_sel:
            price_el = card.select_one(price_sel)
            if price_el:
                price = _parse_price(price_el.get_text())

        key = urlsplit(url).path
        products[key] = {"title": title, "url": url, "price": price, "compare_at": None}
    return products


def _parse_price(text):
    """Estrae il primo numero da testi tipo 'CHF 29.90', '29,90 €', "1'299.00"."""
    cleaned = text.replace("'", "").replace("’", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d{1,2})?", cleaned)
    return float(match.group()) if match else None
