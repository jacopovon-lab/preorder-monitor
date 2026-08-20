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
                "image": _product_image(p),
            }
        if len(batch) < 250:
            break
        page += 1
    return products


def _product_image(p):
    images = p.get("images") or []
    if images and images[0].get("src"):
        return images[0]["src"]
    return None


def fetch_section_variants(session, site):
    """Ritorna {"handle:variant_id": {"title", "url", "available"}} per TUTTI i
    prodotti di una collezione (tutte le pagine), per il restock a livello di sezione."""
    parts = urlsplit(site["url"])
    base = f"{parts.scheme}://{parts.netloc}"
    collection_path = parts.path.rstrip("/")

    variants = {}
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
            for v in p["variants"]:
                title = p["title"]
                if v["title"] != "Default Title":
                    title += f" — {v['title']}"
                try:
                    vprice = float(v["price"])
                except (TypeError, ValueError):
                    vprice = None
                variants[f"{p['handle']}:{v['id']}"] = {
                    "title": title,
                    "product_title": p["title"],
                    "url": f"{base}/products/{p['handle']}",
                    "available": bool(v["available"]),
                    "price": vprice,
                    "image": _product_image(p),
                }
        if len(batch) < 250:
            break
        page += 1
    return variants


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
    image = (data.get("images") or [None])[0]
    if image and image.startswith("//"):  # l'endpoint .js usa URL senza schema
        image = "https:" + image
    variants = {}
    for v in data["variants"]:
        if wanted and wanted != str(v["id"]) and wanted not in v["title"].lower():
            continue
        variants[str(v["id"])] = {
            "title": f"{data['title']} — {v['title']}",
            "available": bool(v["available"]),
            "url": item["url"],
            # nell'endpoint .js il prezzo è in centesimi
            "price": v["price"] / 100 if isinstance(v.get("price"), (int, float)) else None,
            "image": image,
        }
    return variants
