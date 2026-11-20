#!/usr/bin/env python3
"""
STEP 1A: Collect product data + theme name from Shopify stores.

Run in Terminal from this folder:
    python3 scrape_products.py

For each store in stores.txt it:
  1. checks the store's robots.txt and skips it if /products.json is disallowed
  2. downloads /products.json (up to 250 products)
  3. downloads the homepage and reads the theme name from the page code
  3b. checks 3 products' alt text in a second format, to validate the main feed
  4. waits a moment before the next store (to be polite)

Output:
  data/raw/<domain>.json   one file per store (products + theme)
  data/stores_status.csv   one row per store: did it work, how many products, which theme

Safe to stop (Ctrl+C) and re-run: stores already saved are skipped.
Uses only Python's built-in libraries, so nothing to install.
"""
import csv, json, os, re, sys, time
import urllib.request, urllib.error, urllib.robotparser, urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "data", "raw")
STATUS_CSV = os.path.join(HERE, "data", "stores_status.csv")
UA = "Mozilla/5.0 (compatible; StudentAccessibilityResearch/1.0)"
PAUSE_SECONDS = 1.5
TIMEOUT = 20
FIELDS = ["domain", "category", "status", "final_domain", "n_products", "n_images",
          "n_images_with_alt_field", "check_images", "check_alt_null", "check_alt_text",
          "theme_name", "theme_schema_name", "theme_store_id", "scraped_at"]


def fetch(url):
    """Download a URL and return (final_url, text). Raises on errors."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.geturl(), resp.read().decode("utf-8", errors="replace")


def robots_allows(domain, path="/products.json"):
    """True if robots.txt allows us to fetch `path` (or if there is no robots.txt)."""
    rp = urllib.robotparser.RobotFileParser()
    try:
        _, text = fetch(f"https://{domain}/robots.txt")
    except Exception:
        return True  # no robots.txt reachable -> nothing disallowed
    rp.parse(text.splitlines())
    return rp.can_fetch(UA, f"https://{domain}{path}")


def img_key(src):
    """'https://cdn.shopify.com/.../files/A12_SIDE.png?v=17' -> 'a12_side.png'"""
    return (src or "").split("?")[0].rsplit("/", 1)[-1].lower()


THEME_RE = re.compile(r"Shopify\.theme\s*=\s*(\{.*?\})\s*;", re.S)


def parse_theme(html):
    """Shopify pages contain `Shopify.theme = {...};` with the theme's name and id."""
    m = THEME_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def load_stores():
    stores = []
    with open(os.path.join(HERE, "stores.txt")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            domain, _, category = line.partition(",")
            stores.append((domain.strip().lower(), category.strip() or "unknown"))
    return stores


def load_done():
    if not os.path.exists(STATUS_CSV):
        return {}
    with open(STATUS_CSV) as f:
        return {row["domain"]: row for row in csv.DictReader(f)}


def save_status(rows):
    with open(STATUS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows.values())


def scrape_store(domain, category):
    row = {k: "" for k in FIELDS}
    row.update(domain=domain, category=category,
               scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    if not robots_allows(domain):
        row["status"] = "skipped_robots"
        return row, None

    try:
        final_url, text = fetch(f"https://{domain}/products.json?limit=250")
        data = json.loads(text)
        products = data["products"]
    except urllib.error.HTTPError as e:
        row["status"] = f"http_{e.code}"
        return row, None
    except (json.JSONDecodeError, KeyError, TypeError):
        row["status"] = "not_shopify"
        return row, None
    except Exception as e:
        row["status"] = "error_" + type(e).__name__
        return row, None

    final_domain = urllib.parse.urlparse(final_url).netloc
    time.sleep(PAUSE_SECONDS / 2)
    theme = {}
    try:
        _, html = fetch(f"https://{final_domain}/")
        theme = parse_theme(html)
    except Exception:
        pass

    # Validation: the main feed seems to drop "alt" when it's empty. Check 3 products
    # against the single-product format (/products/<handle>.js), which always shows "alt".
    check = []
    for p in [p for p in products if p.get("images")][:3]:
        time.sleep(PAUSE_SECONDS / 2)
        try:
            _, js = fetch(f"https://{final_domain}/products/{p['handle']}.js")
            media = json.loads(js).get("media", [])
            # the two formats use different ids for the same image, so match on the file name
            feed_alt = {img_key(im.get("src")): im.get("alt", "<no field>") for im in p.get("images", [])}
            for m in media:
                if m.get("media_type") == "image":
                    check.append({"handle": p["handle"], "media_id": m.get("id"),
                                  "js_alt": m.get("alt"),
                                  "feed_alt": feed_alt.get(img_key(m.get("src")), "<not matched>")})
        except Exception:
            pass

    row.update(status="ok", final_domain=final_domain,
               n_products=len(products),
               n_images=sum(len(p.get("images", [])) for p in products),
               # some stores' feeds leave out the "alt" field entirely; record how many images have it
               n_images_with_alt_field=sum(1 for p in products for im in p.get("images", []) if "alt" in im),
               theme_name=theme.get("name", ""),
               theme_schema_name=theme.get("schema_name", ""),
               theme_store_id=theme.get("theme_store_id", "") or "",
               check_images=len(check),
               check_alt_null=sum(1 for c in check if not (c["js_alt"] or "").strip()),
               check_alt_text=sum(1 for c in check if (c["js_alt"] or "").strip()))
    record = {"domain": domain, "final_domain": final_domain, "category": category,
              "scraped_at": row["scraped_at"], "theme": theme, "alt_check": check,
              "products": products}
    return row, record


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    stores = load_stores()
    done = load_done()
    todo = [(d, c) for d, c in stores if d not in done]
    print(f"{len(stores)} stores in list, {len(done)} already done, {len(todo)} to go.\n")

    for i, (domain, category) in enumerate(todo, 1):
        row, record = scrape_store(domain, category)
        if record:
            with open(os.path.join(RAW_DIR, f"{domain}.json"), "w") as f:
                json.dump(record, f)
        done[domain] = row
        save_status(done)
        theme = row["theme_schema_name"] or row["theme_name"] or "-"
        print(f"[{i}/{len(todo)}] {domain:35s} {row['status']:16s} "
              f"products={row['n_products'] or 0:<4} theme={theme}")
        time.sleep(PAUSE_SECONDS)

    ok = sum(1 for r in done.values() if r["status"] == "ok")
    print(f"\nDone. {ok} of {len(done)} stores returned product data.")
    print(f"Summary: {STATUS_CSV}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to continue where you left off.")
        sys.exit(0)
