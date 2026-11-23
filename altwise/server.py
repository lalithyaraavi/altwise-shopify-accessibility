#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2", "httpx>=0.27", "playwright>=1.40", "pillow>=10"]
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
      media(first: 20) { nodes { ... on MediaImage { id alt createdAt image { url } } } }
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
                            "created_at": m.get("createdAt", ""),
                            "class": classify_alt(m.get("alt"), p["title"])})
    return out


def accessibility_score(imgs):
    """0-100: share of product images a screen reader can actually describe (not missing or junk).
    Returns (store score, average score across the research stores)."""
    b = BENCH["pct_unusable_by_store"]
    avg = 100 - sum(b) / len(b)
    if not imgs:
        return None, avg
    bad = sum(i["class"] in ("missing", "junk") for i in imgs)
    return round(100 * (1 - bad / len(imgs))), round(avg)


PAGE_SCAN = os.path.join(HERE, "page_scan.json")
GROUPS = {"screen_reader": "screen-reader users", "low_vision": "low-vision and older shoppers (contrast)",
          "motor": "shoppers with tremors / keyboard users", "voice": "voice-control users",
          "zoom": "shoppers who need to zoom"}


def rule_group(rule_id):
    """Same mapping as the research (analysis/step3_measure.sql)."""
    if rule_id == "color-contrast": return "low_vision"
    if rule_id == "label-content-name-mismatch": return "voice"
    if rule_id in ("target-size", "scrollable-region-focusable", "nested-interactive"): return "motor"
    if rule_id in ("meta-viewport", "meta-viewport-large"): return "zoom"
    return "screen_reader"


def scan_pages():
    """Run axe-core (same settings as the research) on the storefront homepage and one product page.
    Handles the dev-store password page using STOREFRONT_PASSWORD from .env. Saves altwise/page_scan.json."""
    from datetime import datetime, timezone
    from playwright.sync_api import sync_playwright
    axe_js = open(os.path.join(ROOT, "tools", "axe.min.js"), encoding="utf-8").read()
    opts = {"runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]},
            "resultTypes": ["violations"]}
    products = fetch_products()
    prod = next((p for p in products if p["title"] == HOOK_PRODUCT), None) or next(
        (p for p in products if p["media"]["nodes"]), products[0])
    base = f"https://{STORE}"
    urls = {"home": base + "/", "product": f"{base}/products/{prod['handle']}"}
    pages = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception:
            import subprocess
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 900})
        ctx.add_init_script(axe_js)
        page = ctx.new_page()

        def go(url):
            # Shopify can redirect mid-load (e.g. to the password page), which Playwright reports as ERR_ABORTED
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                if "ERR_ABORTED" not in str(e):
                    raise
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

        go(base + "/password")
        if page.locator("input[type=password]").count():
            pw_ = ENV.get("STOREFRONT_PASSWORD", "")
            if not pw_:
                raise RuntimeError("Your storefront is password-protected: add STOREFRONT_PASSWORD=... to .env "
                                   "(Shopify admin > Online Store > Preferences).")
            page.locator("input[type=password]").first.fill(pw_)
            page.keyboard.press("Enter")
            page.wait_for_timeout(3000)
        for kind, url in urls.items():
            go(url)
            if "/password" in page.url:
                raise RuntimeError("Still on the password page: check STOREFRONT_PASSWORD in .env.")
            page.wait_for_timeout(3000)
            if not page.evaluate("typeof window.axe !== 'undefined'"):
                page.add_script_tag(content=axe_js)
            res = page.evaluate("opts => axe.run(document, opts)", opts)
            pages.append({"page": kind, "url": url, "violations": [
                {"id": v["id"], "impact": v.get("impact"), "help": v.get("help"), "n": len(v.get("nodes", []))}
                for v in res["violations"]]})
        browser.close()
    groups = sorted({rule_group(v["id"]) for pg in pages for v in pg["violations"]})
    out = {"scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pages": pages,
           "groups_affected": groups}
    json.dump(out, open(PAGE_SCAN, "w"), indent=1)
    return out


def store_score(imgs):
    """Overall store accessibility score (0-100) = average of the image score and the page score.
    Page score = share of the 5 shopper groups with no barriers found by axe-core.
    Returns dict with parts, the research averages, and the share of research stores this beats."""
    img, img_avg = accessibility_score(imgs)
    scan = json.load(open(PAGE_SCAN)) if os.path.exists(PAGE_SCAN) else None
    page = round(100 * (5 - len(scan["groups_affected"])) / 5) if scan else None
    overall = round((img + page) / 2) if (img is not None and page is not None) else None
    sb, pb = BENCH.get("store_score_by_store", []), BENCH.get("page_score_by_store", [])
    beats = round(100 * sum(x < overall for x in sb) / len(sb)) if (overall is not None and sb) else None
    return {"overall": overall, "images": img, "pages": page, "scan": scan, "beats_pct": beats,
            "avg_overall": round(sum(sb) / len(sb)) if sb else None, "avg_images": img_avg,
            "avg_pages": round(sum(pb) / len(pb)) if pb else None}


_cache = {}  # media_id -> image info, so updates can be logged with context


# ---------------------------------------------------------------- MCP tools
mcp = FastMCP("altwise", instructions=(
    "AltWise helps Shopify merchants keep product images accessible. When asked to audit the store, call "
    "audit_store and reply with only the score, one suggested description per image, 1-3 short "
    "recommendations, and 'Want me to save these descriptions?'. When the merchant approves, call save_descriptions once with all of them and report the new "
    "score. If the merchant asks to check in on their store, call store_checkin (same reply shape)."))


def _image_payload(i, width=600):
    """Load a product image so the model can see it (smaller copy for speed)."""
    try:
        sep = "&" if "?" in i["url"] else "?"
        r = httpx.get(i["url"] + sep + f"width={width}", timeout=30, follow_redirects=True)
        ctype = r.headers.get("content-type", "image/jpeg")
        fmt = "png" if "png" in ctype else "webp" if "webp" in ctype else "jpeg"
        return Image(data=r.content, format=fmt)
    except Exception as e:
        return f"(couldn't load image: {e})"


@mcp.tool()
def audit_store() -> list:
    """Audit the store's accessibility. Returns the store accessibility score and EVERY product image that
    needs a description, each with its picture. Reply in exactly this shape and nothing else:
    (1) the score in 2-3 short lines, (2) a numbered list, one line per image:
    product name - what a screen reader hears now - your suggested description,
    (3) "Other ways to improve": the recommendations given below, as 1-3 short bullets,
    (4) one closing question: "Want me to save these descriptions?". When the merchant agrees, call
    save_descriptions once with ALL of them (it saves them and returns the new score)."""
    products = fetch_products()
    imgs = images_of(products)
    for i in imgs:
        _cache[i["media_id"]] = i
    if not imgs:
        return [f"{STORE}: no product images to audit."]
    sc = store_score(imgs)
    bad = sorted((i for i in imgs if i["class"] in ("missing", "junk", "minimal")),
                 key=lambda i: i.get("created_at", ""), reverse=True)
    if sc["overall"] is not None:
        score = (f"STORE ACCESSIBILITY SCORE: {sc['overall']}/100 (average Shopify store in my research: "
                 f"{sc['avg_overall']}/100)\n"
                 f"  Product images: {sc['images']}/100 (share a screen reader can describe)\n"
                 f"  Pages: {sc['pages']}/100 (shopper groups with no barriers"
                 + (": issue for " + ", ".join(GROUPS[g] for g in sc["scan"]["groups_affected"]) if sc["scan"]["groups_affected"] else "")
                 + ")")
    else:
        score = f"IMAGE ACCESSIBILITY SCORE: {sc['images']}/100 (average store: {sc['avg_images']}/100)"
    ADVICE = {
        "low_vision": "Some text is too faint to read (low colour contrast). In Online Store > Themes > Customize > "
                      "Theme settings > Colors, darken text and button colours against their backgrounds.",
        "screen_reader": "Some buttons, links or form fields have no label a screen reader can announce. Add clear "
                         "labels (for example, name icon-only buttons like 'Search' or 'Cart').",
        "motor": "Some buttons or links are too small or too close together to tap easily. Make tap targets at "
                 "least 24x24 pixels.",
        "voice": "Some buttons' accessible names don't match their visible text, so voice-control users can't "
                 "say them. Make the label match what's shown.",
        "zoom": "The page stops shoppers from zooming in. Remove 'user-scalable=no' / maximum-scale from the "
                "theme's viewport setting.",
    }
    recs = []
    if sc.get("scan"):
        recs += [ADVICE[g] for g in sc["scan"]["groups_affected"] if g in ADVICE]
    else:
        recs.append("Run scan_storefront to check the store's pages for barriers (contrast, labels, tap size).")
    scores_ = [x for x in (flesch(pr["description"]) for pr in products) if x is not None]
    hard_ = sum(x < 30 for x in scores_)
    if hard_:
        recs.append(f"{hard_} product descriptions are very hard to read. Shorter sentences and plain words help "
                    "everyone, including shoppers using screen readers.")
    recs.append("Going forward, add a description whenever you upload a product photo (AltWise can remind you).")
    out = [score + "\n\nOTHER WAYS TO IMPROVE (recommendations):\n" + "\n".join(f"- {r}" for r in recs[:3])]
    out[0] += (f"\n\n{len(bad)} of {len(imgs)} product images need a description (newest first). "
               "For each, look closely at the picture and write a specific description a shopper could picture: "
               "product type first, then main colours, graphics or pattern, and any text printed on it "
               "(e.g. 'Snowboard with a navy base, a moonlit pine forest on top and \"Liquid\" in white script'). "
               "Under 125 characters, no 'image of', don't repeat the product name word for word.")
    for k, i in enumerate(bad, 1):
        cur = i["alt"] if i["alt"] else "(nothing, no description)"
        out.append(f"Image {k}: '{i['product_title']}' | media_id {i['media_id']} | screen reader hears now: {cur}")
    # All pictures in numbered contact sheets (one image per 12 products), so every image fits in one reply
    out += _contact_sheets(bad)
    return out


def _contact_sheets(items, per_sheet=12, tile=300, cols=4):
    """Download each product image and lay them out in numbered grids, so the model can see them all."""
    from io import BytesIO
    from PIL import Image as PILImage, ImageDraw
    sheets = []
    for start in range(0, len(items), per_sheet):
        chunk = items[start:start + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        sheet = PILImage.new("RGB", (cols * tile, rows * tile), "white")
        draw = ImageDraw.Draw(sheet)
        for j, i in enumerate(chunk):
            x, y = (j % cols) * tile, (j // cols) * tile
            try:
                sep = "&" if "?" in i["url"] else "?"
                r = httpx.get(i["url"] + sep + f"width={tile}", timeout=30, follow_redirects=True)
                im = PILImage.open(BytesIO(r.content)).convert("RGB")
                im.thumbnail((tile - 10, tile - 10))
                sheet.paste(im, (x + (tile - im.width) // 2, y + (tile - im.height) // 2))
            except Exception:
                draw.text((x + 20, y + tile // 2), "(couldn't load)", fill="black")
            n = start + j + 1
            draw.rectangle([x + 4, y + 4, x + 44, y + 34], fill="#004c3f")
            draw.text((x + 12, y + 10), str(n), fill="white")
        buf = BytesIO(); sheet.save(buf, format="JPEG", quality=85)
        sheets.append(f"Contact sheet: images {start + 1}-{start + len(chunk)} (number in the top-left of each tile).")
        sheets.append(Image(data=buf.getvalue(), format="jpeg"))
    return sheets


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
            out.append(_image_payload(i))
    return out


@mcp.tool()
def scan_storefront() -> str:
    """Scan the live storefront (homepage + a product page) with axe-core, the same checker used in the
    research, to find barriers for screen-reader, low-vision, motor, voice and zoom users. Takes ~30 s.
    Then call audit_store to see the full store accessibility score."""
    import subprocess
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--scan-pages"], capture_output=True,
                       text=True, timeout=300)
    if r.returncode != 0:
        with open(os.path.join(HERE, "scan_error.log"), "w") as f:
            f.write(r.stdout + "\n----\n" + r.stderr)
        return "Scan failed (full log in altwise/scan_error.log): " + (r.stderr or r.stdout)[-800:]
    scan = json.load(open(PAGE_SCAN))
    n = sum(len(p["violations"]) for p in scan["pages"])
    return (f"Scanned {len(scan['pages'])} pages: {n} types of accessibility issues. Groups affected: "
            + (", ".join(GROUPS[g] for g in scan["groups_affected"]) or "none") + ". Now call audit_store.")


@mcp.tool()
def store_checkin(hours: int = 24) -> list:
    """Check in on the store (what the AltWise notification asks merchants to say). Same as audit_store:
    returns the store accessibility score and EVERY product image that needs a description, with a note
    about photos uploaded in the last `hours`. Reply in exactly this shape and nothing else:
    (1) one line on the new upload(s) if any, (2) the score in 2-3 short lines, (3) a numbered list, one line
    per image: product name - what a screen reader hears now - your suggested description,
    (4) "Other ways to improve": the recommendations given, as 1-3 short bullets,
    (5) one closing question: "Want me to save these descriptions?". When the merchant agrees, call
    save_descriptions once with ALL of them (it saves them and returns the new score)."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    def is_new(i):
        try:
            return datetime.fromisoformat(i["created_at"].replace("Z", "+00:00")) >= cutoff
        except Exception:
            return False
    new_bad = [i for i in images_of(fetch_products()) if is_new(i) and i["class"] in ("missing", "junk")]
    note = ("NEW UPLOADS WITH NO DESCRIPTION: " + ", ".join(sorted({i["product_title"] for i in new_bad}))
            + "\n\n") if new_bad else ""
    out = audit_store()
    out[0] = note + out[0]
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


@mcp.tool()
def save_descriptions(descriptions: list[dict[str, str]]) -> str:
    """Save several approved descriptions at once, then return the new store accessibility score.
    `descriptions` is a list of {"media_id": "...", "alt": "..."}. Only call after the merchant approves.
    Reply with the before and after score in one or two lines."""
    before = store_score(images_of(fetch_products()))
    saved, failed = 0, []
    for d in descriptions:
        mid, alt = d.get("media_id", ""), (d.get("alt") or "").strip()
        if not mid or not alt:
            continue
        r = update_image_alt(mid, alt)
        if r.startswith("Saved"):
            saved += 1
        else:
            failed.append(mid)
    after = store_score(images_of(fetch_products()))
    key = "overall" if after["overall"] is not None else "images"
    msg = (f"Saved {saved} new descriptions to Shopify.\n"
           f"NEW STORE ACCESSIBILITY SCORE: {after[key]}/100 (was {before[key]}/100; "
           f"average store: {after['avg_overall'] if key == 'overall' else after['avg_images']}/100)\n"
           f"  Product images: {after['images']}/100 (was {before['images']}/100)")
    if failed:
        msg += f"\nCouldn't save {len(failed)}: {', '.join(failed)}"
    return msg


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


HOOK_PRODUCT = "The Collection Snowboard: Liquid"


def make_demo():
    """Make the demo store look like a typical store from the research: about half the images with
    no description, plus junk and minimal ones like those found in real stores. The rest keep their
    descriptions. Saves the original alt text first so it can be restored."""
    import random
    imgs = sorted(images_of(fetch_products()), key=lambda i: i["media_id"])
    if not os.path.exists(DEMO_BACKUP):
        json.dump({i["media_id"]: i["alt"] for i in imgs}, open(DEMO_BACKUP, "w"), indent=1)
        print(f"Backed up original alt text for {len(imgs)} images -> altwise/demo_backup.json")
    backup = json.load(open(DEMO_BACKUP))
    n = len(imgs)
    # The video opens on this product, so its main image always gets the file-name junk text
    hook = next((i for i in imgs if i["product_title"] == HOOK_PRODUCT), None)
    rest = [i for i in imgs if i is not hook]
    random.Random(7).shuffle(rest)
    # Mix based on the research: typical store ~47% missing; junk and minimal text are common too
    n_missing = round(n * 0.47)
    n_junk = max(0, round(n * 0.17) - (1 if hook else 0))
    n_minimal = round(n * 0.17)
    junk_examples = ["PDP2 Option[Main] LIFESTYLE", "Color:Ice;", "snowboard-main-01", "DSC_0193", "product 2"]
    counts = {"missing": 0, "junk": 0, "minimal": 0, "descriptive": 0}
    if hook:
        set_alt(hook["media_id"], "IMG_4821.jpg"); counts["junk"] += 1
    for k, i in enumerate(rest):
        if k < n_missing:
            set_alt(i["media_id"], ""); counts["missing"] += 1
        elif k < n_missing + n_junk:
            set_alt(i["media_id"], junk_examples[(k - n_missing) % len(junk_examples)]); counts["junk"] += 1
        elif k < n_missing + n_junk + n_minimal:
            set_alt(i["media_id"], i["product_title"]); counts["minimal"] += 1   # just the product name
        else:
            set_alt(i["media_id"], backup.get(i["media_id"], i["alt"])); counts["descriptive"] += 1
    print("Demo store ready: " + ", ".join(f"{v} {k}" for k, v in counts.items())
          + (f" (incl. {HOOK_PRODUCT} = IMG_4821.jpg)" if hook else "") + ".")


def restore_demo():
    backup = json.load(open(DEMO_BACKUP))
    for media_id, alt in backup.items():
        set_alt(media_id, alt)
    print(f"Restored original alt text for {len(backup)} images.")


def notify(title, message):
    """Show a macOS notification (falls back to printing on other systems)."""
    import subprocess
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'],
                       check=False, capture_output=True)
    except FileNotFoundError:
        pass
    print(f"[{title}] {message}")


def watch(every=20):
    """Sidekick-style proactive reminders: check the store every `every` seconds and notify the
    merchant, without being asked, when a newly uploaded product image has no usable description."""
    import time
    seen = {i["media_id"] for i in images_of(fetch_products())}
    print(f"AltWise is watching {STORE} for new product images (Ctrl+C to stop)...")
    while True:
        time.sleep(every)
        try:
            imgs = images_of(fetch_products())
        except Exception as e:
            print("check failed, retrying:", e); continue
        new = [i for i in imgs if i["media_id"] not in seen]
        seen |= {i["media_id"] for i in imgs}
        bad = [i for i in new if i["class"] in ("missing", "junk")]
        if bad:
            sc = store_score(imgs)
            score, avg = (sc["overall"], sc["avg_overall"]) if sc["overall"] is not None else (sc["images"], sc["avg_images"])
            names = ", ".join(sorted({i["product_title"] for i in bad}))
            notify("AltWise: new photo has no description",
                   f"{names}: shoppers using screen readers can't see it. "
                   f"Accessibility score {score}/100 (avg store {avg}). Open Claude and say 'Check in on my store'.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        print("Getting access token...", end=" "); get_token(); print("OK")
        print(audit_store())
    elif "--make-demo" in sys.argv:
        make_demo(); print(); print(audit_store())
    elif "--restore-demo" in sys.argv:
        restore_demo()
    elif "--scan-pages" in sys.argv:
        r = scan_pages(); print("Groups affected:", r["groups_affected"])
    elif "--watch" in sys.argv:
        watch()
    else:
        mcp.run()
