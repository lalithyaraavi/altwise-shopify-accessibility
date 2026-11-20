#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2", "httpx>=0.27"]
# ///
"""
AltWise: an accessibility audit + image-description fixer for a Shopify store, as an MCP server.

It connects Claude (Desktop) to your Shopify store through the Admin API and offers tools to:
  - audit_store                 -> report card: missing/junk alt text, hard-to-read descriptions,
                                   benchmarked against a 401-store research sample
  - get_images_needing_alt      -> shows Claude the images that need a description
  - update_image_alt            -> saves an approved description into Shopify (and logs it)
  - get_research_images /       -> evaluation mode: AI describes 100 real images from the research
    save_ai_description            sample, so the descriptions can be hand-scored for accuracy

Credentials are read from ../.env  (SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET).

Command-line helpers (run in Terminal from the project folder):
    uv run --script altwise/server.py --test           # check the connection + print an audit
    uv run --script altwise/server.py --make-demo      # make the demo store look like a typical store
    uv run --script altwise/server.py --restore-demo   # put the original alt text back
"""
import csv, json, os, re, sys, time
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP, Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API_VERSION = "2026-04"
FIX_LOG = os.path.join(HERE, "fix_log.csv")
BENCH = json.load(open(os.path.join(HERE, "benchmark.json")))


# ---------------------------------------------------------------- credentials + API
def load_env(path=os.path.join(ROOT, ".env")):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
STORE = re.sub(r"^https?://", "", ENV["SHOPIFY_STORE"]).strip("/")
_token = {"value": None, "expires": 0.0}


def get_token():
    """Client credentials grant: swap the app's ID + secret for an access token (valid ~24h)."""
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    r = httpx.post(f"https://{STORE}/admin/oauth/access_token", timeout=30, data={
        "grant_type": "client_credentials",
        "client_id": ENV["SHOPIFY_CLIENT_ID"],
        "client_secret": ENV["SHOPIFY_CLIENT_SECRET"]})
    if r.status_code != 200:
        raise RuntimeError(f"Couldn't get an access token ({r.status_code}): {r.text[:300]}")
    j = r.json()
    _token["value"] = j["access_token"]
    _token["expires"] = time.time() + int(j.get("expires_in", 86399)) - 300
    return _token["value"]


def gql(query, variables=None):
    r = httpx.post(f"https://{STORE}/admin/api/{API_VERSION}/graphql.json", timeout=60,
                   headers={"X-Shopify-Access-Token": get_token()},
                   json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError("Shopify API error: " + json.dumps(j["errors"])[:500])
    return j["data"]


PRODUCTS_Q = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle productType description
      media(first: 20) { nodes { ... on MediaImage { id alt image { url } } } }
    }
  }
}"""


def fetch_products():
    products, cursor = [], None
    while True:
        d = gql(PRODUCTS_Q, {"cursor": cursor})["products"]
        products += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return products
        cursor = d["pageInfo"]["endCursor"]


# ---------------------------------------------------------------- the research metrics
def classify_alt(alt, title):
    """Same 4-level rules as the research (SQL view `images_classified`)."""
    a = (alt or "").strip(); al = a.lower(); tl = (title or "").lower().strip()
    if a == "":
        return "missing"
    if (re.search(r"\.(jpe?g|png|webp|gif|avif)$", al)
            or re.search(r"(::|\|\||option\[|^img[_-]?\d|^dsc|pdp\d?|_main\b|^colou?r:|^size:|#colou?r_)", al)
            or re.search(r"^(featured|slideshow|image|img|photo|picture|thumbnail|banner|default|product)( ?\d+)?$", al)
            or not re.search(r"[A-Za-z]{3}", a)
            or (not re.search(r"\s", a) and re.search(r"[-_]", a))):
        return "junk"
    if al == tl or len(re.findall(r"[A-Za-z]+", a)) <= 3 or al in tl:
        return "minimal"
    return "descriptive"


def flesch(text):
    words = re.findall(r"[A-Za-z']+", text or "")
    if len(words) < 20:
        return None
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    def syl(w):
        w = re.sub(r"[^a-z]", "", w.lower()); g = re.findall(r"[aeiouy]+", w)
        return max(1, len(g) - (1 if w.endswith("e") and len(g) > 1 else 0))
    return round(206.835 - 1.015 * len(words) / sentences - 84.6 * sum(map(syl, words)) / len(words), 1)


def images_of(products):
    out = []
    for p in products:
        for m in p["media"]["nodes"]:
            if m and m.get("id"):
                out.append({"product_id": p["id"], "product_title": p["title"], "media_id": m["id"],
                            "alt": m.get("alt") or "", "url": (m.get("image") or {}).get("url", ""),
                            "class": classify_alt(m.get("alt"), p["title"])})
    return out


_cache = {}  # media_id -> image info, so updates can be logged with context


# ---------------------------------------------------------------- MCP tools
mcp = FastMCP("altwise")


@mcp.tool()
def audit_store() -> str:
    """Audit the connected Shopify store's accessibility: image descriptions (alt text) and
    product-description readability, benchmarked against a research sample of 401 Shopify stores."""
    products = fetch_products()
    imgs = images_of(products)
    for i in imgs:
        _cache[i["media_id"]] = i
    n = len(imgs)
    if n == 0:
        return f"{STORE}: {len(products)} products, but no product images to audit."
    counts = {c: sum(i["class"] == c for i in imgs) for c in ["descriptive", "minimal", "junk", "missing"]}
    pct_missing = 100 * counts["missing"] / n
    pct_unusable = 100 * (counts["missing"] + counts["junk"]) / n
    b = BENCH["pct_missing_by_store"]
    worse_than = 100 * sum(x < pct_missing for x in b) / len(b)
    scores = [s for s in (flesch(p["description"]) for p in products) if s is not None]
    hard = sum(s < 30 for s in scores)
    junk_examples = [f'"{i["alt"]}" ({i["product_title"]})' for i in imgs if i["class"] == "junk"][:3]

    lines = [
        f"ACCESSIBILITY REPORT: {STORE}",
        f"Products: {len(products)}   Product images: {n}",
        "",
        "Image descriptions (what screen-reader users hear):",
        f"  descriptive {counts['descriptive']} | minimal {counts['minimal']} | junk {counts['junk']} | missing {counts['missing']}",
        f"  {pct_missing:.0f}% of images have NO description; {pct_unusable:.0f}% are missing or junk.",
        "",
        f"Benchmark ({BENCH['n_stores']} Shopify stores studied, average {BENCH['avg_pct_missing']}% missing):",
        f"  This store has more missing descriptions than {worse_than:.0f}% of stores in the study.",
    ]
    if junk_examples:
        lines += ["", "Junk alt text examples (not real descriptions):"] + [f"  - {e}" for e in junk_examples]
    if scores:
        lines += ["", f"Product descriptions: {len(scores)} long enough to score; {hard} are very hard to read (Flesch < 30)."]
    lines += ["", "Next step: call get_images_needing_alt to review the images and write descriptions."]
    return "\n".join(lines)


@mcp.tool()
def get_images_needing_alt(limit: int = 5, include: str = "missing,junk") -> list:
    """Return up to `limit` product images whose alt text is in the given classes
    (comma-separated: missing, junk, minimal), each with its image so you can see it.
    Write a description for each, show them to the user for approval, then call update_image_alt."""
    wanted = {c.strip() for c in include.split(",")}
    imgs = [i for i in images_of(fetch_products()) if i["class"] in wanted]
    out = [f"{len(imgs)} images need a description; showing {min(limit, len(imgs))}.\n"
           "Guidelines: describe what is visible (item, colour, material, key details, setting), "
           "in under 125 characters, without starting with 'image of'."]
    for k, i in enumerate(imgs[:limit], 1):
        _cache[i["media_id"]] = i
        out.append(f"Image {k}: product '{i['product_title']}' | media_id {i['media_id']} | "
                   f"current alt: '{i['alt']}' ({i['class']})")
        if i["url"]:
            try:
                sep = "&" if "?" in i["url"] else "?"
                r = httpx.get(i["url"] + sep + "width=600", timeout=30, follow_redirects=True)
                ctype = r.headers.get("content-type", "image/jpeg")
                fmt = "png" if "png" in ctype else "webp" if "webp" in ctype else "jpeg"
                out.append(Image(data=r.content, format=fmt))
            except Exception as e:
                out.append(f"(couldn't load image: {e})")
    return out


@mcp.tool()
def update_image_alt(media_id: str, new_alt: str) -> str:
    """Save an approved description (alt text) for one product image into Shopify.
    Only call this after the user has approved the text."""
    d = gql("""mutation($files: [FileUpdateInput!]!) {
                 fileUpdate(files: $files) { files { id alt } userErrors { field message } }
               }""", {"files": [{"id": media_id, "alt": new_alt}]})["fileUpdate"]
    if d["userErrors"]:
        return "Shopify rejected the update: " + json.dumps(d["userErrors"])
    info = _cache.get(media_id, {})
    new_file = not os.path.exists(FIX_LOG)
    with open(FIX_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["saved_at", "product_title", "media_id", "image_url", "old_alt", "old_class", "new_alt"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), info.get("product_title", ""),
                    media_id, info.get("url", ""), info.get("alt", ""), info.get("class", ""), new_alt])
    return f"Saved to Shopify: {media_id} -> \"{new_alt}\""


# ---------------------------------------------------------------- Step 6: AI quality evaluation
SAMPLE_CSV = os.path.join(HERE, "rating_sample.csv")      # 100 real product images with NO alt text,
AI_CSV = os.path.join(HERE, "ai_descriptions.csv")        # 1 random image from each of 100 stores (seed 42)


def _done_ids():
    if not os.path.exists(AI_CSV):
        return set()
    return {r["sample_id"] for r in csv.DictReader(open(AI_CSV))}


def _fetch_image(url):
    sep = "&" if "?" in url else "?"
    r = httpx.get(url + sep + "width=600", timeout=30, follow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "image/jpeg")
    return Image(data=r.content, format="png" if "png" in ctype else "webp" if "webp" in ctype else "jpeg")


@mcp.tool()
def get_research_images(n: int = 10) -> list:
    """EVALUATION MODE. Return the next `n` images (not yet described) from the research sample:
    100 real product images that had no alt text, one from each of 100 Shopify stores.
    Write one alt-text description per image following the guidelines, then call
    save_ai_description for each. Do not ask the user for approval in evaluation mode."""
    done = _done_ids()
    todo = [r for r in csv.DictReader(open(SAMPLE_CSV)) if r["sample_id"] not in done]
    out = [f"{len(done)} of 100 described so far; {len(todo)} remaining. Showing {min(n, len(todo))}.\n"
           "Guidelines: describe what is visible (item, colour, material, key details, setting), "
           "in under 125 characters, without starting with 'image of'."]
    for r in todo[:n]:
        out.append(f"{r['sample_id']} | product: '{r['product_title']}' | type: '{r['product_type']}' | store category: {r['category']}")
        try:
            out.append(_fetch_image(r["image_src"]))
        except Exception as e:
            out.append(f"(couldn't load image {r['sample_id']}: {e}) - save the description as 'IMAGE_UNAVAILABLE'")
    return out


@mcp.tool()
def save_ai_description(sample_id: str, description: str) -> str:
    """EVALUATION MODE. Record the AI-written description for one research-sample image."""
    sample = {r["sample_id"]: r for r in csv.DictReader(open(SAMPLE_CSV))}
    if sample_id not in sample:
        return f"Unknown sample_id {sample_id}"
    if sample_id in _done_ids():
        return f"{sample_id} already saved; skipping."
    r = sample[sample_id]
    new_file = not os.path.exists(AI_CSV)
    with open(AI_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["sample_id", "domain", "category", "product_title", "image_src", "ai_description",
                        "n_chars", "accurate", "useful", "concise", "error_type", "notes"])
        w.writerow([sample_id, r["domain"], r["category"], r["product_title"], r["image_src"],
                    description, len(description), "", "", "", "", ""])
    return f"Saved {sample_id} ({len(_done_ids())}/100)"


# ---------------------------------------------------------------- demo setup (command line only)
DEMO_BACKUP = os.path.join(HERE, "demo_backup.json")


def set_alt(media_id, alt):
    d = gql("""mutation($files: [FileUpdateInput!]!) {
                 fileUpdate(files: $files) { userErrors { message } } }""",
            {"files": [{"id": media_id, "alt": alt}]})["fileUpdate"]
    if d["userErrors"]:
        raise RuntimeError(d["userErrors"])


HOOK_PRODUCT = "The Complete Snowboard"


def make_demo():
    """Make the demo store look like a typical store from the research (~half of images with no
    description, a couple with junk text). Saves the original alt text first so it can be restored."""
    imgs = sorted(images_of(fetch_products()), key=lambda i: i["media_id"])
    if not os.path.exists(DEMO_BACKUP):
        json.dump({i["media_id"]: i["alt"] for i in imgs}, open(DEMO_BACKUP, "w"), indent=1)
        print(f"Backed up original alt text for {len(imgs)} images -> altwise/demo_backup.json")
    # The video opens on this product, so its main image always gets the file-name junk text
    hook = next((i for i in imgs if i["product_title"] == HOOK_PRODUCT), None)
    if hook:
        set_alt(hook["media_id"], "IMG_4821.jpg")
        imgs = [i for i in imgs if i is not hook]
    n_missing = round((len(imgs) + 1) * 0.47)
    junk = ["PDP2 Option[Main] LIFESTYLE"]
    for k, i in enumerate(imgs):
        if k < n_missing:
            set_alt(i["media_id"], "")
        elif k < n_missing + len(junk):
            set_alt(i["media_id"], junk[k - n_missing])
    print(f"Demo store ready: {n_missing} images with no description, {len(junk) + bool(hook)} with junk text"
          f"{f' (incl. {HOOK_PRODUCT} = IMG_4821.jpg)' if hook else ''}.")


def restore_demo():
    backup = json.load(open(DEMO_BACKUP))
    for media_id, alt in backup.items():
        set_alt(media_id, alt)
    print(f"Restored original alt text for {len(backup)} images.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        print("Getting access token...", end=" "); get_token(); print("OK")
        print(audit_store())
    elif "--make-demo" in sys.argv:
        make_demo(); print(); print(audit_store())
    elif "--restore-demo" in sys.argv:
        restore_demo()
    else:
        mcp.run()
