"""
Fortnite Item Shop Twitter Bot
Generates a styled item-shop image grouped by section and posts it to X (Twitter).
"""

import math
import os
import time
import requests
import tweepy
from PIL import Image, ImageDraw, ImageFont, ImageOps
from io import BytesIO
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ═══════════════════════════════════════════
#                 CONFIG
# ═══════════════════════════════════════════
FNBR_API_KEY          = os.getenv("FNBR_API_KEY", "").strip()
FNBR_URL              = "https://fortnite-api.com/v2/shop"

TWITTER_API_KEY       = os.getenv("TWITTER_API_KEY", "").strip()
TWITTER_API_SECRET    = os.getenv("TWITTER_API_SECRET", "").strip()
TWITTER_ACCESS_TOKEN  = os.getenv("TWITTER_ACCESS_TOKEN", "").strip()
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET", "").strip()
TWITTER_BEARER        = os.getenv("TWITTER_BEARER", "").strip()

TWITTER_HANDLE   = os.getenv("TWITTER_HANDLE", "X: @FNBRitemstore_")   # shown in image header
COLLAGE_FILENAME = "itemshop_collage.png"


def env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


ENABLE_TWEET = env_bool("ENABLE_TWEET", default=False)
SHOW_IMAGE_PREVIEW = env_bool("SHOW_IMAGE_PREVIEW", default=True)
FAIL_ON_TWEET_ERROR = env_bool("FAIL_ON_TWEET_ERROR", default=False)
TWITTER_RETRY_ATTEMPTS = max(1, int(os.getenv("TWITTER_RETRY_ATTEMPTS", "4")))
TWITTER_RETRY_BASE_SECONDS = max(1.0, float(os.getenv("TWITTER_RETRY_BASE_SECONDS", "3")))


def validate_runtime_config():
    if not FNBR_API_KEY:
        raise RuntimeError("Missing FNBR_API_KEY environment variable.")

    if ENABLE_TWEET:
        required = {
            "TWITTER_API_KEY": TWITTER_API_KEY,
            "TWITTER_API_SECRET": TWITTER_API_SECRET,
            "TWITTER_ACCESS_TOKEN": TWITTER_ACCESS_TOKEN,
            "TWITTER_ACCESS_SECRET": TWITTER_ACCESS_SECRET,
            "TWITTER_BEARER": TWITTER_BEARER,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                "Missing required Twitter environment variables: " + ", ".join(missing)
            )

# ═══════════════════════════════════════════
#           RARITY COLOUR PALETTE
# ═══════════════════════════════════════════
# Each entry: (top_colour, bottom_colour) for the tile gradient
RARITY_PALETTE = {
    "common":                ((109, 109, 118), (65,  65,  72)),
    "uncommon":              ((96,  168,  58), (55, 100,  28)),
    "rare":                  ((73,  172, 242), (25,  90, 200)),
    "epic":                  ((177,  91, 226), (90,  30, 155)),
    "legendary":             ((236, 160,  53), (150, 80,  10)),
    "mythic":                ((255, 231,  69), (190, 160, 10)),
    "icon series":           ((21,  204, 219), (5,  125, 140)),
    "dc series":             ((84,  117, 212), (35,  65, 160)),
    "marvel series":         ((197,  51,  52), (110, 15,  15)),
    "dark series":           ((200,  30, 200), (100, 10, 100)),
    "frozen series":         ((148, 223, 236), (65, 155, 185)),
    "lava series":           ((234,  95,  35), (150, 40,  10)),
    "shadow series":         ((105, 105, 110), (50,  50,  55)),
    "slurp series":          ((21,  219, 190), (8,  130, 115)),
    "gaming legends series": ((228, 196,  10), (150, 120,   5)),
    "star wars series":      ((220, 210,  60), (140, 130,  20)),
}

def rarity_gradient(rarity_str):
    key = (rarity_str or "").lower()
    return RARITY_PALETTE.get(key, RARITY_PALETTE["common"])


# ═══════════════════════════════════════════
#                FONT HELPERS
# ═══════════════════════════════════════════
_FORTNITE_FONT = os.getenv("FORTNITE_FONT_PATH", "").strip()
_CUSTOM_FONT_CANDIDATES = []
if _FORTNITE_FONT:
    _CUSTOM_FONT_CANDIDATES.append(_FORTNITE_FONT)
_CUSTOM_FONT_CANDIDATES.extend([
    "Fortnite.ttf",
    "./Fortnite.ttf",
    "./fonts/Fortnite.ttf",
])

_BOLD    = _CUSTOM_FONT_CANDIDATES + ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_REGULAR = _CUSTOM_FONT_CANDIDATES + ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]

def load_font(size, bold=False):
    for path in (_BOLD if bold else _REGULAR):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


# ═══════════════════════════════════════════
#               IMAGE HELPERS
# ═══════════════════════════════════════════
def fetch_image(url):
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception as exc:
        print(f"  ! Image failed: {exc}")
        return None


def _item_image_url(item):
    """Return the best available image URL for any item kind."""
    kind = item.get("_kind")
    if kind == "track":
        return item.get("albumArt")
    images = item.get("images") or {}
    if kind in ("car", "instrument"):
        return images.get("large") or images.get("small")
    # br items
    return images.get("featured") or images.get("icon") or images.get("smallIcon")


def prefetch_images(sections_dict):
    """Download all item icons in parallel and return a {url: Image} cache."""
    urls = set()
    for items in sections_dict.values():
        for item in items:
            url = _item_image_url(item)
            if url:
                urls.add(url)

    cache = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        future_to_url = {pool.submit(fetch_image, url): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            cache[url] = future.result()
    return cache


def make_gradient(width, height, top_c, bot_c):
    """Create a solid vertical gradient PIL image without numpy."""
    grad_col = Image.new("RGB", (1, height))
    for row in range(height):
        t = row / max(height - 1, 1)
        r = int(top_c[0] + (bot_c[0] - top_c[0]) * t)
        g = int(top_c[1] + (bot_c[1] - top_c[1]) * t)
        b = int(top_c[2] + (bot_c[2] - top_c[2]) * t)
        grad_col.putpixel((0, row), (r, g, b))
    return grad_col.resize((width, height), Image.Resampling.NEAREST)


# ═══════════════════════════════════════════
#                  THEME
# ═══════════════════════════════════════════
BG_TOP          = (129, 176, 246)
BG_BOTTOM       = (63, 116, 214)
HEADER_BG       = (116, 168, 244)
HEADER_ACCENT   = (232, 243, 255)
TITLE_COLOR     = (255, 255, 255)
DATE_COLOR      = (230, 239, 255)
HANDLE_COLOR    = (225, 236, 255)
SECTION_BG      = (54, 103, 192)
SECTION_HEADER  = (69, 122, 208)
SECTION_TEXT    = (255, 255, 255)
SHOW_SECTION_OVERLAY = False
ITEM_NAME_COLOR = (255, 255, 255, 255)
PRICE_COLOR     = (255, 255, 255, 255)
VBUCKS_COIN_OUTER = (220, 240, 255, 255)
VBUCKS_COIN_INNER = (140, 200, 255, 255)
VBUCKS_COIN_TEXT  = (255, 255, 255, 255)


# ═══════════════════════════════════════════
#              LAYOUT CONSTANTS
# ═══════════════════════════════════════════
CANVAS_W    = 3820
CANVAS_H    = 4096
OUTER_PAD   = 24
SEC_PAD     = 10       # padding inside section block
SEC_TITLE_H = 40
HEADER_H    = 110
ITEM_W      = 208
ITEM_H      = 274
ICON_SZ     = 150
ICON_PAD_X  = 6
ICON_TOP_PAD = 4
ICON_BOTTOM_GAP = 8
IMAGE_FIT_MODE = "contain"  # full image visible for all item types.
CORNER_R    = 10
SEC_GAP     = 16
ITEM_GAP    = 8
NUM_COLS    = 4
COL_GAP     = 16

INNER_W = CANVAS_W - 2 * OUTER_PAD


def col_width():
    return (INNER_W - (NUM_COLS - 1) * COL_GAP) // NUM_COLS


def items_per_col_row():
    return max(1, col_width() // (ITEM_W + ITEM_GAP))


def section_height(n):
    ipr   = items_per_col_row()
    nrows = math.ceil(n / ipr)
    return SEC_TITLE_H + SEC_PAD + nrows * (ITEM_H + ITEM_GAP) + SEC_PAD


def draw_vbucks_coin(base_img, x, y, size):
    coin = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    coin_draw = ImageDraw.Draw(coin)

    coin_draw.ellipse([0, 0, size - 1, size - 1], fill=VBUCKS_COIN_OUTER)
    inset = max(2, size // 7)
    coin_draw.ellipse([inset, inset, size - 1 - inset, size - 1 - inset], fill=VBUCKS_COIN_INNER)

    f_coin = load_font(max(8, size - 6), bold=True)
    text = "V"
    text_w = int(coin_draw.textlength(text, font=f_coin))
    text_h = max(1, f_coin.size)
    coin_draw.text(((size - text_w) // 2, (size - text_h) // 2 - 1), text, font=f_coin, fill=VBUCKS_COIN_TEXT)

    base_img.paste(coin, (x, y), coin)


def fit_text_font(draw, text, start_size, max_w, max_h, bold=False, min_size=10):
    size = start_size
    while size >= min_size:
        font = load_font(size, bold=bold)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        tw = right - left
        th = bottom - top
        if tw <= max_w and th <= max_h:
            return font, tw, th
        size -= 1

    font = load_font(min_size, bold=bold)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return font, (right - left), (bottom - top)


def pick_image_fit_mode(item):
    """Pick image fit strategy by item kind unless explicitly overridden."""
    if IMAGE_FIT_MODE in ("contain", "cover"):
        return IMAGE_FIT_MODE
    kind = item.get("_kind")
    if kind in ("track", "car", "instrument"):
        return "contain"
    return "cover"


# ═══════════════════════════════════════════
#               ITEM TILE
# ═══════════════════════════════════════════
def build_tile(item, img_cache=None):
    rarity_raw = item.get("rarity") or "common"
    if isinstance(rarity_raw, dict):
        rarity_id = rarity_raw.get("value") or rarity_raw.get("id") or rarity_raw.get("name") or "common"
    else:
        rarity_id = str(rarity_raw)
    top_c, drk_c = rarity_gradient(rarity_id)

    # Gradient background
    bg   = make_gradient(ITEM_W, ITEM_H, top_c, drk_c)
    tile = bg.convert("RGBA")

    draw = ImageDraw.Draw(tile)

    # Icon
    icon_box_w = ITEM_W - (ICON_PAD_X * 2)
    icon_box_h = ITEM_H - 58
    img_url = _item_image_url(item)
    if img_url:
        icon = (img_cache.get(img_url) if img_cache else None) or fetch_image(img_url)
        if icon:
            fit_mode = pick_image_fit_mode(item)
            if fit_mode == "cover":
                fitted = ImageOps.fit(icon, (icon_box_w, icon_box_h), method=Image.Resampling.LANCZOS)
            else:
                fitted = ImageOps.contain(icon, (icon_box_w, icon_box_h), method=Image.Resampling.LANCZOS)
            ix = (ITEM_W - fitted.width) // 2
            iy = ICON_TOP_PAD + (icon_box_h - fitted.height) // 2
            tile.paste(fitted, (ix, iy), fitted)

    # Name
    f_name  = load_font(22, bold=True)
    f_price = load_font(20, bold=True)

    name    = (item.get("name") or "").upper()
    max_w   = ITEM_W - 6
    while name and draw.textlength(name, font=f_name) > max_w:
        name = name[:-1]
    if name != ((item.get("name") or "").upper()):
        name = name.rstrip(".,- ") + "..."

    name_y = ICON_TOP_PAD + icon_box_h + ICON_BOTTOM_GAP
    name_w = int(draw.textlength(name, font=f_name))
    draw.text(((ITEM_W - name_w) // 2, name_y), name,
              font=f_name, fill=ITEM_NAME_COLOR)

    # Price
    price = item.get("price")
    if price is not None:
        try:
            price_str = f"{int(price):,}"
        except (ValueError, TypeError):
            price_str = str(price)
        coin_sz = 18
        gap = 4
        p_w = int(draw.textlength(price_str, font=f_price))
        total_w = coin_sz + gap + p_w
        start_x = (ITEM_W - total_w) // 2
        price_y = name_y + 24
        draw_vbucks_coin(tile, start_x, price_y + 1, coin_sz)
        draw.text((start_x + coin_sz + gap, price_y),
                  price_str, font=f_price, fill=PRICE_COLOR)

    # Rounded-corner mask
    mask = Image.new("L", (ITEM_W, ITEM_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, ITEM_W - 1, ITEM_H - 1], radius=CORNER_R, fill=255
    )
    tile.putalpha(mask)
    return tile


# ═══════════════════════════════════════════
#               FULL CANVAS
# ═══════════════════════════════════════════
def build_canvas(sections_dict, date_str, img_cache=None):
    cw   = col_width()
    ipr  = items_per_col_row()
    secs = [(name, items) for name, items in sections_dict.items() if items]

    # Greedy: assign each section to the shortest column
    col_cursors  = [0] * NUM_COLS          # current y per column
    col_sections = [[] for _ in range(NUM_COLS)]
    for sec_name, items in secs:
        sh      = section_height(len(items))
        shortest = min(range(NUM_COLS), key=lambda c: col_cursors[c])
        col_sections[shortest].append((sec_name, items, sh))
        col_cursors[shortest] += sh + SEC_GAP

    content_h = max(col_cursors) if col_cursors else 0
    total_h   = HEADER_H + OUTER_PAD + content_h + OUTER_PAD

    canvas = make_gradient(CANVAS_W, max(CANVAS_H, total_h), BG_TOP, BG_BOTTOM).convert("RGB")
    draw   = ImageDraw.Draw(canvas)

    # ── Header ───────────────────────────────────────────────
    draw.rectangle([0, 0, CANVAS_W, HEADER_H], fill=HEADER_BG)

    title_txt = "FORTNITE ITEM SHOP"
    date_txt = date_str.upper()
    handle_txt = TWITTER_HANDLE

    header_pad_x = OUTER_PAD
    header_pad_y = 8
    header_gap = 14
    header_bottom = HEADER_H - 6

    # Fit handle first, then reserve room on the right for it.
    f_handle, hw, hh = fit_text_font(
        draw,
        handle_txt,
        start_size=26,
        max_w=max(140, CANVAS_W // 4),
        max_h=HEADER_H - 2 * header_pad_y,
        bold=False,
        min_size=12,
    )
    handle_x = CANVAS_W - hw - header_pad_x
    handle_y = header_pad_y + (HEADER_H - 2 * header_pad_y - hh) // 2

    left_max_w = max(200, handle_x - header_pad_x - 24)
    title_max_h = max(20, int((HEADER_H - 2 * header_pad_y - header_gap) * 0.62))
    date_max_h = max(14, HEADER_H - 2 * header_pad_y - header_gap - title_max_h)

    f_title, tw, th = fit_text_font(
        draw,
        title_txt,
        start_size=78,
        max_w=left_max_w,
        max_h=title_max_h,
        bold=True,
        min_size=18,
    )
    f_date, dw, dh = fit_text_font(
        draw,
        date_txt,
        start_size=40,
        max_w=left_max_w,
        max_h=date_max_h,
        bold=False,
        min_size=12,
    )

    title_x = header_pad_x
    title_y = header_pad_y
    date_x = header_pad_x
    date_y = title_y + th + header_gap
    if date_y + dh > header_bottom:
        date_y = max(title_y + th + 2, header_bottom - dh)

    draw.text((title_x, title_y), title_txt, font=f_title, fill=TITLE_COLOR)
    draw.text((date_x, date_y), date_txt, font=f_date, fill=DATE_COLOR)
    draw.text((handle_x, handle_y), handle_txt, font=f_handle, fill=HANDLE_COLOR)

    # ── Columns ──────────────────────────────────────────────
    f_sec = load_font(28, bold=True)

    for ci in range(NUM_COLS):
        cx = OUTER_PAD + ci * (cw + COL_GAP)
        cy = HEADER_H + OUTER_PAD

        for sec_name, items, sh in col_sections[ci]:
            sx0 = cx
            sx1 = cx + cw

            if SHOW_SECTION_OVERLAY:
                draw.rounded_rectangle([sx0, cy, sx1, cy + sh],          radius=8, fill=SECTION_BG)
                draw.rounded_rectangle([sx0, cy, sx1, cy + SEC_TITLE_H], radius=8, fill=SECTION_HEADER)
            draw.text((sx0 + 12, cy + 7), sec_name.upper(), font=f_sec, fill=SECTION_TEXT)

            ix = sx0 + SEC_PAD
            iy = cy + SEC_TITLE_H + SEC_PAD
            for idx, item in enumerate(items):
                print(f"  - {item.get('name', '?')}")
                tile = build_tile(item, img_cache)
                canvas.paste(tile, (ix, iy), tile)
                ix += ITEM_W + ITEM_GAP
                if (idx + 1) % ipr == 0:
                    ix  = sx0 + SEC_PAD
                    iy += ITEM_H + ITEM_GAP

            cy += sh + SEC_GAP

    return canvas


# ═══════════════════════════════════════════
#             SHOP DATA HELPERS
# ═══════════════════════════════════════════
def fetch_shop_data():
    resp = requests.get(FNBR_URL, headers={"x-api-key": FNBR_API_KEY}, timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]


def group_by_section(shop):
    buckets = defaultdict(list)
    seen_ids = set()

    def _add(label, item):
        item_id = item.get("id")
        if item_id and item_id in seen_ids:
            return
        if item_id:
            seen_ids.add(item_id)
        buckets[label].append(item)

    for entry in shop.get("entries", []):
        price = entry.get("finalPrice") or entry.get("regularPrice")
        label = (
            (entry.get("layout") or {}).get("name")
            or entry.get("layoutId")
            or "Daily Items"
        )
        for item in entry.get("brItems", []):
            item = dict(item)
            item["price"] = price
            item["_kind"] = "br"
            _add(label, item)
        for item in entry.get("tracks", []):
            item = dict(item)
            item["price"] = price
            item["_kind"] = "track"
            item.setdefault("name", item.get("title", ""))
            _add(label, item)
        for item in entry.get("cars", []):
            item = dict(item)
            item["price"] = price
            item["_kind"] = "car"
            _add(label, item)
        for item in entry.get("instruments", []):
            item = dict(item)
            item["price"] = price
            item["_kind"] = "instrument"
            _add(label, item)
    return buckets


# ═══════════════════════════════════════════
#             TWITTER POSTING
# ═══════════════════════════════════════════
def is_retryable_twitter_error(exc):
    status_code = getattr(exc, "status_code", None)
    if status_code in (429, 500, 502, 503, 504):
        return True

    msg = str(exc).lower()
    retry_signals = (
        "service unavailable",
        "temporarily unavailable",
        "too many requests",
        "timed out",
        "timeout",
        "bad gateway",
        "gateway timeout",
        "connection reset",
        "503",
    )
    return any(signal in msg for signal in retry_signals)


def run_with_retries(action_name, fn):
    for attempt in range(1, TWITTER_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            can_retry = is_retryable_twitter_error(exc)
            if attempt >= TWITTER_RETRY_ATTEMPTS or not can_retry:
                raise

            delay = TWITTER_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            print(
                f"{action_name} failed ({exc}). "
                f"Retrying in {delay:.1f}s ({attempt}/{TWITTER_RETRY_ATTEMPTS})..."
            )
            time.sleep(delay)


def tweet_image(image_path, date_str):
    auth = tweepy.OAuth1UserHandler(
        TWITTER_API_KEY, TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET,
    )
    v1 = tweepy.API(auth)
    v2 = tweepy.Client(
        bearer_token=TWITTER_BEARER,
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_SECRET,
    )

    print("Uploading media to Twitter...")
    media = run_with_retries(
        "Media upload",
        lambda: v1.media_upload(filename=image_path),
    )

    body = f"Fortnite Item Shop - {date_str}\n#Fortnite #ItemShop #FNItemShop"
    resp = run_with_retries(
        "Tweet creation",
        lambda: v2.create_tweet(text=body, media_ids=[media.media_id_string]),
    )
    print(f"Tweet posted! ID: {resp.data['id']}")


# ═══════════════════════════════════════════
#                  MAIN
# ═══════════════════════════════════════════
def main():
    validate_runtime_config()

    print("  Fetching Fortnite item shop...")
    shop = fetch_shop_data()

    raw_date = shop.get("date", "")
    try:
        date_str = datetime.strptime(raw_date[:10], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_str = datetime.now().strftime("%B %d, %Y")

    print(f"   Date : {date_str}")

    sections = group_by_section(shop)
    print(f"   Sections ({len(sections)}): {', '.join(sections)}")

    print("\nPre-fetching item images in parallel...")
    img_cache = prefetch_images(sections)
    print(f"   Cached {len(img_cache)} images")

    print("\nBuilding image...")
    img = build_canvas(sections, date_str, img_cache)
    img.save(COLLAGE_FILENAME, format="PNG", optimize=True)
    print(f"   Saved -> {COLLAGE_FILENAME}")
    if SHOW_IMAGE_PREVIEW:
        img.show()

    if ENABLE_TWEET:
        print("Posting to Twitter...")
        try:
            tweet_image(COLLAGE_FILENAME, date_str)
        except Exception as exc:
            print(f"Twitter posting failed after retries: {exc}")
            if FAIL_ON_TWEET_ERROR:
                raise
    else:
        print("Twitter post skipped (set ENABLE_TWEET=true to enable posting).")


if __name__ == "__main__":
    main()

