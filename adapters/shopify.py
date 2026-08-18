"""Adapter Shopify: usa gli endpoint JSON pubblici dello storefront."""

from urllib.parse import urlsplit


def fetch_products(session, site):
    """Ritorna {handle: {"title": ..., "url": ...}} per una collezione.

    site["url"] è la pagina della collezione, es.
    https://shop.ch/collections/pre-order (query string ignorata).
    """
    parts = urlsplit(site["url"])
    base = f"{parts.scheme}://{parts.netloc}"
    collection_path = parts.path.rstrip("/")

    products = {}
    page = 1
    while True:
        resp = session.get(
            f"{base}{collection_path}/products.json",
            params={"limit": 250, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()["products"]
        if not batch:
            break
        for p in batch:
            price = compare_at = None
            for v in p["variants"]:
                try:
                    vp = float(v["price"])
                except (TypeError, ValueError):
                    continue
                if price is None or vp < price:
                    price = vp
                    try:
                        ca = float(v.get("compare_at_price") or 0)
                    except (TypeError, ValueError):
                        ca = 0
                    compare_at = ca if ca > vp else None
            products[p["handle"]] = {
                "title": p["title"],
                "url": f"{base}/products/{p['handle']}",
                "price": price,
                "compare_at": compare_at,
            }
        if len(batch) < 250:
            break
        page += 1
    return products


def fetch_watch(session, item):
    """Ritorna {variant_id: {"title": ..., "available": bool}} per un prodotto.

    item["url"] è la pagina prodotto; usa l'endpoint /products/<handle>.js.
    item["variant"] (opzionale) filtra per ID o per sottostringa del nome.
    """
    parts = urlsplit(item["url"])
    base = f"{parts.scheme}://{parts.netloc}"
    handle = parts.path.rstrip("/").split("/")[-1]

    resp = session.get(f"{base}/products/{handle}.js", timeout=30)
    resp.raise_for_status()
    data = resp.json()

    wanted = str(item.get("variant", "")).strip().lower()
    variants = {}
    for v in data["variants"]:
        if wanted and wanted != str(v["id"]) and wanted not in v["title"].lower():
            continue
        variants[str(v["id"])] = {
            "title": f"{data['title']} — {v['title']}",
            "available": bool(v["available"]),
            "url": item["url"],
        }
    return variants
