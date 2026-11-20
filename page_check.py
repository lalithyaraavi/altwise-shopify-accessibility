#!/usr/bin/env python3
"""
STEP 1B: Run the axe accessibility checker on two pages per store: one product page and the homepage.

Run AFTER scrape_products.py has finished (it needs data/raw/).
One-time setup in Terminal:
    pip3 install playwright
    python3 -m playwright install chromium
Then:
    python3 page_check.py

For each store that returned product data, it opens one real product page in a
hidden Chrome browser, runs axe (tools/axe.min.js), and records the problems found,
e.g. low colour contrast, images without alt text, buttons without labels.

Output:
  data/axe/<domain>.json   full list of problems for that page
  data/axe_summary.csv     one row per store with counts per problem type

Takes about 5-15 seconds per store, so it's fine to leave running in the background.
Safe to stop (Ctrl+C) and re-run: finished stores are skipped.
"""
import csv, json, os, sys
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright isn't installed. Run:\n  pip3 install playwright\n  python3 -m playwright install chromium")

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "data", "raw")
AXE_DIR = os.path.join(HERE, "data", "axe")
STATUS_CSV = os.path.join(HERE, "data", "stores_status.csv")
SUMMARY_CSV = os.path.join(HERE, "data", "axe_summary.csv")
AXE_JS = open(os.path.join(HERE, "tools", "axe.min.js"), encoding="utf-8").read()

# The rules we report as separate columns (all others are still saved in the JSON).
KEY_RULES = ["color-contrast", "image-alt", "button-name", "link-name", "label",
             "html-has-lang", "aria-allowed-attr", "list", "heading-order",
             "select-name", "role-img-alt", "svg-img-alt"]
FIELDS = (["domain", "page_type", "category", "theme_schema_name", "url", "status", "checked_at",
           "n_rule_violations", "n_elements_affected",
           "n_page_images", "n_img_no_alt_attr", "n_img_empty_alt"] + [f"rule_{r}" for r in KEY_RULES])

IMG_JS = """() => Array.from(document.querySelectorAll('img')).map(img => {
  const r = img.getBoundingClientRect();
  return {src: (img.currentSrc || img.src || '').slice(0, 300),
          alt: img.getAttribute('alt'),            // null = no alt attribute at all
          width: Math.round(r.width), height: Math.round(r.height),
          aria_hidden: img.closest('[aria-hidden="true"]') !== null};
})"""

AXE_OPTIONS = {"runOnly": {"type": "tag",
                           "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]},
               "resultTypes": ["violations"]}


def ok_stores():
    with open(STATUS_CSV) as f:
        return [r for r in csv.DictReader(f) if r["status"] == "ok"]


def load_done():
    """Finished checks, keyed by (domain, page_type). Older rows without page_type were product pages."""
    if not os.path.exists(SUMMARY_CSV):
        return {}
    with open(SUMMARY_CSV) as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        r["page_type"] = r.get("page_type") or "product"
        out[(r["domain"], r["page_type"])] = {k: r.get(k, "") for k in FIELDS}
    return out


def save_summary(rows):
    with open(SUMMARY_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows.values())


def page_url(domain, page_type):
    if page_type == "home":
        with open(os.path.join(RAW_DIR, f"{domain}.json")) as f:
            return f"https://{json.load(f)['final_domain']}/"
    return pick_product_url(domain)


def pick_product_url(domain):
    with open(os.path.join(RAW_DIR, f"{domain}.json")) as f:
        rec = json.load(f)
    products = rec["products"]
    # prefer a product that has images, so the image checks have something to test
    p = next((p for p in products if p.get("images")), products[0] if products else None)
    if not p:
        return None
    return f"https://{rec['final_domain']}/products/{p['handle']}"


def check_page(context, url):
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)  # let the theme's scripts finish drawing the page
        if not page.evaluate("typeof window.axe !== 'undefined'"):
            page.add_script_tag(content=AXE_JS)
        result = page.evaluate("opts => axe.run(document, opts)", AXE_OPTIONS)
        # Also record every image's alt text exactly as a screen reader would see it.
        result["page_images"] = page.evaluate(IMG_JS)
        return result
    finally:
        page.close()


def main():
    os.makedirs(AXE_DIR, exist_ok=True)
    stores = ok_stores()
    done = load_done()
    # two pages per store: one product page and the homepage
    todo = [(s, pt) for s in stores for pt in ("product", "home") if (s["domain"], pt) not in done]
    print(f"{len(stores)} stores x 2 pages, {len(done)} pages already checked, {len(todo)} to go.\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 900},
                                      user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 "
                                                  "StudentAccessibilityResearch/1.0"))
        context.add_init_script(AXE_JS)  # load axe into every page before the store's own scripts
        context.route("**/*", lambda route: route.abort()
                      if route.request.resource_type == "media" else route.continue_())

        for i, (s, page_type) in enumerate(todo, 1):
            domain = s["domain"]
            row = {k: "" for k in FIELDS}
            row.update(domain=domain, page_type=page_type, category=s["category"],
                       theme_schema_name=s["theme_schema_name"],
                       checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            try:
                url = page_url(domain, page_type)
                row["url"] = url or ""
                if not url:
                    row["status"] = "no_products"
                else:
                    result = check_page(context, url)
                    violations = [{"id": v["id"], "impact": v.get("impact"), "help": v.get("help"),
                                   "tags": v.get("tags", []), "n_elements": len(v.get("nodes", [])),
                                   "examples": [n.get("html", "")[:300] for n in v.get("nodes", [])[:3]]}
                                  for v in result["violations"]]
                    fname = f"{domain}.json" if page_type == "product" else f"{domain}__home.json"
                    with open(os.path.join(AXE_DIR, fname), "w") as f:
                        json.dump({"domain": domain, "page_type": page_type, "url": url, "violations": violations,
                                   "page_images": result.get("page_images", [])}, f, indent=1)
                    counts = {v["id"]: v["n_elements"] for v in violations}
                    imgs = [im for im in result.get("page_images", [])
                            if im["width"] > 1 and im["height"] > 1 and not im["aria_hidden"]]
                    row.update(status="ok", n_rule_violations=len(violations),
                               n_elements_affected=sum(counts.values()),
                               n_page_images=len(imgs),
                               n_img_no_alt_attr=sum(1 for im in imgs if im["alt"] is None),
                               n_img_empty_alt=sum(1 for im in imgs if im["alt"] is not None and not im["alt"].strip()),
                               **{f"rule_{r}": counts.get(r, 0) for r in KEY_RULES})
            except Exception as e:
                row["status"] = "error_" + type(e).__name__
            done[(domain, page_type)] = row
            save_summary(done)
            print(f"[{i}/{len(todo)}] {domain:35s} {page_type:8s} {row['status']:14s} "
                  f"problem types={row['n_rule_violations'] or 0:<3} elements={row['n_elements_affected'] or 0}")
        browser.close()

    print(f"\nDone. Summary: {SUMMARY_CSV}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to continue where you left off.")
        sys.exit(0)
