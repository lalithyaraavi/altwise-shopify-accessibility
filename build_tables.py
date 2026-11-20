#!/usr/bin/env python3
"""
STEP 2A: Turn the raw scraped files into clean tables ready for BigQuery.

Run:  python3 build_tables.py      (re-run any time; it rebuilds everything)
Output (folder data/tables/):
  stores.csv        one row per store       (category, theme, theme type)
  products.csv      one row per product     (type, price, description length + reading ease, colour option)
  images_alt.csv    one row per sampled product image, with its real alt text
  pages.csv         one row per checked page (the denominator for page-level rates)
  page_issues.csv   one row per page x accessibility rule that failed (from axe)
  page_images.csv   one row per image on each checked product page (alt as screen readers see it)
"""
import csv, html, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
D = lambda *p: os.path.join(HERE, "data", *p)
OUT = D("tables")


def clean(s):
    """Single-line text: CSV- and BigQuery-friendly."""
    return re.sub(r"\s+", " ", str(s or "")).strip()


def strip_html(s):
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", s or "")))


def syllables(word):
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def flesch(text):
    """Flesch reading ease: higher = easier (60-70 ~ plain English, <30 ~ very hard)."""
    words = re.findall(r"[A-Za-z']+", text)
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    if len(words) < 20:
        return ""  # too short to score fairly
    syl = sum(syllables(w) for w in words)
    return round(206.835 - 1.015 * len(words) / sentences - 84.6 * syl / len(words), 1)


# Themes sold off-the-shelf (Shopify Theme Store, plus a few popular paid themes sold elsewhere).
# Many "custom" stores keep the original theme's schema name, which reveals what they were built on.
OFF_THE_SHELF = {t.lower() for t in """Dawn Refresh Sense Craft Studio Taste Crave Colorblock Ride Origin Publisher
Spotlight Trade Horizon Tinker Impulse Prestige Impact Broadcast Symmetry Empire Warehouse Motion Pipeline Focal
Atlantic Stiletto Expanse Expression Flow Streamline Blockshop Envy Local District Showcase Parallax Testament Mojave
Sleek Shapes Wonder Pitch Edge Enterprise Release Concept Combine Xclusive Fashionopolism Galleria Avenue Highlight
Atelier Minimalista Woodstock Fabric Vision Ignite Momentum Paper Habitat Reformation Cascade Canopy Context Kingdom
Lorenza Maker Modular North Pacific Providence Retina Split Sunrise Story Venue Vantage Label Venture Debut Brooklyn
Minimal Supply Narrative Boundless Simple Express Baseline Distinctive Flawless Eurus Hyper Iris Krank Huge Influence
Grid Agile Aurora Beyond Electro Emporium Erickson Essence Gain Honey Abode Aesthetic Alchemy Retro Swipe Unicorn Zest
Turbo Ella Kalles Shella Wokiee Flex Booster Debutify""".split()} | {"palo alto", "be yours", "mr parker", "split"}


def theme_info(s):
    schema = clean(s["theme_schema_name"])
    base = schema.split("(")[0].strip()
    if s["status"] != "ok":
        return "", ""
    if s["theme_store_id"]:
        return (base if base.lower() in OFF_THE_SHELF else "store theme (renamed)"), "off_the_shelf"
    if base.lower() in OFF_THE_SHELF:
        return base, "off_the_shelf_customised"
    if schema or s["theme_name"]:
        return "", "bespoke"
    return "", "unknown"


def sample_groups():
    path = os.path.join(HERE, "stores_wave2.txt")
    small = {l.split(",")[0].strip() for l in open(path) if l.strip() and not l.startswith("#")} if os.path.exists(path) else set()
    return small


def write(name, rows, fields):
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"{name:16s} {len(rows):>7,} rows")


def main():
    os.makedirs(OUT, exist_ok=True)
    status = {r["domain"]: r for r in csv.DictReader(open(D("stores_status.csv")))}
    smaller = sample_groups()

    stores, products = [], []
    for domain, s in status.items():
        base_theme, theme_family = theme_info(s)
        stores.append({
            "domain": domain, "category": s["category"],
            "sample_group": "smaller" if domain in smaller else "big_brand",
            "status": s["status"],
            "final_domain": s["final_domain"], "n_products": s["n_products"] or "",
            "theme_name": clean(s["theme_name"]), "theme_schema_name": clean(s["theme_schema_name"]),
            "theme_store_id": s["theme_store_id"],
            # Shopify Theme Store themes have a theme_store_id; custom-built themes don't
            "theme_type": ("standard" if s["theme_store_id"] else
                           "custom" if (s["theme_name"] or s["theme_schema_name"]) else
                           "unknown") if s["status"] == "ok" else "",
            "base_theme": base_theme, "theme_family": theme_family,
        })
        raw = D("raw", f"{domain}.json")
        if s["status"] != "ok" or not os.path.exists(raw):
            continue
        for p in json.load(open(raw))["products"]:
            prices = [float(v.get("price") or 0) for v in p.get("variants", [])] or [0]
            desc = strip_html(p.get("body_html"))
            color_opt = next((o for o in p.get("options", [])
                              if o.get("name", "").strip().lower() in ("color", "colour", "colors", "colours")), None)
            products.append({
                "domain": domain, "product_id": p["id"], "handle": p.get("handle", ""),
                "title": clean(p.get("title")), "product_type": clean(p.get("product_type")),
                "n_images": len(p.get("images", [])), "n_variants": len(p.get("variants", [])),
                "min_price": min(prices), "max_price": max(prices),
                "is_placeholder": int(max(prices) == 0 or not p.get("images")),
                "desc_words": len(desc.split()), "desc_reading_ease": flesch(desc),
                "has_color_option": int(color_opt is not None),
                "color_values": clean(" | ".join(color_opt.get("values", []))) if color_opt else "",
            })
    write("stores.csv", stores, list(stores[0].keys()))
    write("products.csv", products, list(products[0].keys()) if products else ["domain"])

    images = []
    if os.path.isdir(D("alt")):
        for fn in sorted(os.listdir(D("alt"))):
            for r in json.load(open(D("alt", fn)))["images"]:
                images.append({k: (clean(v) if isinstance(v, str) or v is None else v)
                               for k, v in r.items() if k != "image_src"}
                              | {"alt_is_null": int(r["alt"] is None)})
    write("images_alt.csv", images, ["domain", "category", "product_handle", "product_title",
                                     "product_type", "image_position", "image_file", "alt", "alt_is_null"])

    issues, page_imgs = [], []
    if os.path.isdir(D("axe")):
        for fn in sorted(os.listdir(D("axe"))):
            a = json.load(open(D("axe", fn)))
            page_type = a.get("page_type", "product")
            for v in a["violations"]:
                level = next((t for t in v["tags"] if re.fullmatch(r"wcag2\d?a{1,2}", t)), "")
                issues.append({"domain": a["domain"], "page_type": page_type, "url": a["url"], "rule_id": v["id"],
                               "impact": v.get("impact") or "", "wcag_level": level,
                               "n_elements": v["n_elements"], "help": clean(v.get("help"))})
            for im in a.get("page_images", []):
                page_imgs.append({"domain": a["domain"], "page_type": page_type, "image_file": clean(im["src"]).split("?")[0].rsplit("/", 1)[-1],
                                  "alt": clean(im["alt"]) if im["alt"] is not None else "",
                                  "alt_attr_missing": int(im["alt"] is None),
                                  "width": im["width"], "height": im["height"],
                                  "aria_hidden": int(im["aria_hidden"])})
    # every page that was checked (including pages with zero issues) = the denominator for "% of pages failing"
    pages = []
    if os.path.exists(D("axe_summary.csv")):
        for r in csv.DictReader(open(D("axe_summary.csv"))):
            if r.get("status") == "ok":
                pages.append({"domain": r["domain"], "page_type": r.get("page_type") or "product",
                              "url": r["url"], "n_rule_violations": r["n_rule_violations"],
                              "n_elements_affected": r["n_elements_affected"]})
    write("pages.csv", pages, ["domain", "page_type", "url", "n_rule_violations", "n_elements_affected"])
    write("page_issues.csv", issues, ["domain", "page_type", "url", "rule_id", "impact", "wcag_level", "n_elements", "help"])
    write("page_images.csv", page_imgs, ["domain", "page_type", "image_file", "alt", "alt_attr_missing",
                                         "width", "height", "aria_hidden"])


if __name__ == "__main__":
    main()
