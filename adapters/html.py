"""Adapter HTML generico: scraping con selettori CSS definiti in sites.yml."""

from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


def fetch_products(session, site):
    """Ritorna {chiave: {"title": ..., "url": ...}}.

    Config in sites.yml:
      selector        card prodotto (o direttamente l'<a> se contiene già titolo+link)
      title_selector  (opzionale) elemento col titolo dentro la card
      link_selector   (opzionale) <a> dentro la card

    La chiave univoca è il path dell'URL prodotto.
    """
    resp = session.get(site["url"], timeout=30)
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

        key = urlsplit(url).path
        products[key] = {"title": title, "url": url}
    return products
