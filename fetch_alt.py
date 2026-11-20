#!/usr/bin/env python3
"""
STEP 1C: Collect real alt text for a random sample of products per store.

Why: the main feed (products.json) never includes alt text for product images,
so we use the single-product format (/products/<handle>.js), which does.

Run:  python3 fetch_alt.py
Output: data/alt/<domain>.json  (one row per image: product, image, alt text)
Safe to stop (Ctrl+C) and re-run.
"""
import json, os, random, sys, time
from scrape_products import fetch, img_key, HERE, RAW_DIR, PAUSE_SECONDS

ALT_DIR = os.path.join(HERE, "data", "alt")
PRODUCTS_PER_STORE = 10
SEED = 42  # fixed seed = the same random sample every time you run it


def real_products(products):
    """Skip placeholders (e.g. $0 'Content' entries with no images)."""
    out = []
    for p in products:
        prices = [float(v.get("price") or 0) for v in p.get("variants", [])]
        if p.get("images") and max(prices or [0]) > 0:
            out.append(p)
    return out


def main():
    os.makedirs(ALT_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(RAW_DIR) if f.endswith(".json"))
    todo = [f for f in files if not os.path.exists(os.path.join(ALT_DIR, f))]
    print(f"{len(files)} stores, {len(files) - len(todo)} already done, {len(todo)} to go.\n")

    for i, fname in enumerate(todo, 1):
        rec = json.load(open(os.path.join(RAW_DIR, fname)))
        prods = real_products(rec["products"])
        rng = random.Random(f"{SEED}-{rec['domain']}")
        sample = rng.sample(prods, min(PRODUCTS_PER_STORE, len(prods)))
        rows, failed = [], 0
        for p in sample:
            try:
                _, js = fetch(f"https://{rec['final_domain']}/products/{p['handle']}.js")
                for m in json.loads(js).get("media", []):
                    if m.get("media_type") != "image":
                        continue
                    rows.append({"domain": rec["domain"], "category": rec["category"],
                                 "product_handle": p["handle"], "product_title": p.get("title", ""),
                                 "product_type": p.get("product_type", ""),
                                 "image_position": m.get("position"), "image_file": img_key(m.get("src")),
                                 "image_src": m.get("src"), "alt": m.get("alt")})
            except Exception:
                failed += 1
            time.sleep(PAUSE_SECONDS / 2)
        with open(os.path.join(ALT_DIR, fname), "w") as f:
            json.dump({"domain": rec["domain"], "n_products_sampled": len(sample),
                       "n_failed": failed, "images": rows}, f)
        empty = sum(1 for r in rows if not (r["alt"] or "").strip())
        print(f"[{i}/{len(todo)}] {rec['domain']:35s} products={len(sample):<3} images={len(rows):<4} empty_alt={empty}")

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Re-run to continue.")
        sys.exit(0)
