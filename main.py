import os, re, json, time, base64, traceback, datetime as dt
from urllib.parse import urljoin, urlparse
import requests, feedparser
from bs4 import BeautifulSoup

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
GKEY = os.environ["GEMINI_API_KEY"]
GKEYS = [k for k in (GKEY, os.environ.get("GEMINI_API_KEY_2", ""), os.environ.get("GEMINI_API_KEY_3", "")) if k]
GIDX = 0          # key currently in use this run
G_ALERTED = False
WKEY = os.environ.get("WEIR_API_KEY", "")
# Two tiers. BEST = strongest free models (small daily limits, used for drafting, chat, video). FAST = high-limit Flash-Lite models (used first for cheap triage).
# Each tier can be overridden with comma-separated repo variables GEMINI_BEST_MODEL / GEMINI_MODEL. A model that hits its limit (429) or is missing (404) is skipped until it resets.
def _models(var, default): return [m.strip() for m in (os.environ.get(var) or default).split(",") if m.strip()]
FAST = _models("GEMINI_MODEL", "gemini-3.1-flash-lite,gemini-2.5-flash-lite")
BEST = _models("GEMINI_BEST_MODEL", "gemini-3.5-flash,gemini-3-flash-preview,gemini-2.5-flash")
MODEL = FAST[0]
COOL = {}         # "model|keyindex" -> ISO time until which it is skipped (persisted in state.json)
SEARCH_MODEL = os.environ.get("GEMINI_SEARCH_MODEL") or "gemini-2.5-flash"   # Google Search grounding has its own free allowance per model family
UA = {"User-Agent": "Mozilla/5.0 (ai-news-bot)"}
STATE_FILE = "state.json"

# ---- settings you can change ----
DIGEST_HOURS = 3            # news batch window
SCAN_HOURS = 6              # X watchlist scan interval (credit-aware)
IDEAS_HOUR_UTC = 7          # daily idea list
WEEKLY_DAY, WEEKLY_HOUR = 6, 17   # Sunday 17:00 UTC (recap + health)
MAX_NEW_PER_SOURCE = 5
WEIR_MIN_LIKES = 100        # only watchlist posts with at least this many likes
WEIR_FREE = 5000            # free credits per month
SCAN_CAP, AGENT_CAP, HARD_CAP = 3500, 4700, 4900   # credit ceilings per use
CATEGORIES = ["Claude/Opus & coding tips", "Creative AI (3D, animation, design)",
              "AI video", "Industry", "Resource lists", "Agents", "Models & benchmarks"]

BASE_STYLE = """Write like a real person on X: plain, short, natural. No hashtags. No emoji spam (max 1, only if natural).
Do NOT start with BREAKING unless major=true and it is genuinely huge. Use ONLY facts in the source text.
Never invent personal credentials, income, earnings or results ("I made $X", "written by an expert"): write only claims the user could truthfully make. Never add a number, price, date or model name that is not in the source. Include practical details
(price, availability, who gets access) when present. Each tweet <= 280 chars; use up to 4 tweets only if needed."""
STYLE = BASE_STYLE
MUTE = ""        # "", "pause" or "quiet"
HELD = []        # messages held back while muted

def now(): return dt.datetime.now(dt.timezone.utc)
def load():
    try: return json.load(open(STATE_FILE))
    except Exception: return {}
def save(s): json.dump(s, open(STATE_FILE, "w"), indent=1)

# ---------------- Telegram ----------------
def tg(text, force=False, urgent=False, buttons=None):
    """Returns True if sent, False if held (pause/quiet hours). force=True always sends."""
    if not force and (MUTE == "pause" or (MUTE == "quiet" and not urgent)):
        HELD.append({"text": text, "buttons": buttons}); del HELD[:-40]; return False
    data = {"chat_id": TG_CHAT, "text": text[:4000], "disable_web_page_preview": "true"}
    if buttons:
        rows = buttons if isinstance(buttons[0], (list, tuple)) and isinstance(buttons[0][0], (list, tuple)) else [buttons]
        data["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]})
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=30).raise_for_status()
    return True

def tg_photo(path, caption=""):
    with open(path, "rb") as f:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_CHAT, "caption": caption[:1000]},
                      files={"photo": f}, timeout=60).raise_for_status()

# ---------------- Gemini ----------------
def _next_reset():
    n = now(); t = n.replace(hour=8, minute=5, second=0, microsecond=0)   # Gemini daily quotas reset at midnight Pacific
    return t if t > n else t + dt.timedelta(days=1)

def gpost(url, tier="best", **kw):
    """POST to Gemini with fallback across models and keys. Quotas are per model AND per Google Cloud project."""
    multi = f"models/{MODEL}:" in url
    models = list(dict.fromkeys((BEST + FAST if tier == "best" else FAST + BEST) if multi else [None]))
    tries = len(models) * len(GKEYS); last = None; global GIDX
    for m in models:
        for j in range(len(GKEYS)):
            ki = (GIDX + j) % len(GKEYS); tag = f"{m}|{ki}"
            if multi and COOL.get(tag, "") > now().isoformat(): continue
            u = url.replace(f"models/{MODEL}:", f"models/{m}:") if multi else url
            r = requests.post(u, headers={"x-goog-api-key": GKEYS[ki]}, **kw)
            if r.status_code in (429, 404) and tries > 1:
                last = r
                if multi:
                    daily = r.status_code == 404 or "PerDay" in r.text or "limit: 0" in r.text
                    COOL[tag] = (_next_reset() if daily else now() + dt.timedelta(minutes=2)).isoformat()
                    if daily: tg(f"⚠️ {m} (key {ki+1}) is out of daily quota or unavailable. Skipping it until it resets.", urgent=True)
                continue
            GIDX = ki
            return r
    return last

def gemini(prompt, image=None, tier="best"):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    parts = [{"text": prompt}]
    if image: parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image}})
    body = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    last = None
    for i in range(3):
        try:
            r = gpost(url, tier=tier, json=body, timeout=90); r.raise_for_status()
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as e:
            last = e; time.sleep(4 * (i + 1))
    raise RuntimeError(f"Gemini failed: {last}")

def gemini_search(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{SEARCH_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    r = gpost(url, json=body, timeout=120); r.raise_for_status()
    return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])

# ---------------- Weir (X data, credit-aware) ----------------
def weir(st, path, body=None, cap=HARD_CAP):
    """Credit-aware Weir call. Supports up to 3 keys (WEIR_API_KEY, _2, _3): moves to the next key when one is at the cap or out of credits."""
    keys = [k for k in (os.environ.get(n, "") for n in ("WEIR_API_KEY", "WEIR_API_KEY_2", "WEIR_API_KEY_3")) if k]
    if not keys: raise RuntimeError("WEIR_API_KEY not set")
    c = st.setdefault("credits", {}); month = now().strftime("%Y-%m")
    if c.get("month") != month: c.clear(); c.update({"month": month, "used": [0] * len(keys), "alerted": []})
    if not isinstance(c.get("used"), list): c["used"] = [int(c.get("used") or 0)]
    c["used"] += [0] * (len(keys) - len(c["used"]))
    last = None
    for i, key in enumerate(keys):
        if c["used"][i] >= cap: continue
        r = requests.request("POST" if body is not None else "GET", "https://api.weirapp.io" + path,
                             headers={"Authorization": f"Bearer {key}"}, json=body, timeout=40)
        if r.status_code == 402: c["used"][i] = WEIR_FREE; last = f"Weir key {i+1} out of credits"; continue
        r.raise_for_status()
        c["used"][i] += int(r.headers.get("x-credits-charged") or 0)
        for pct in (80, 95):
            tag = f"{i}:{pct}"
            if c["used"][i] >= WEIR_FREE * pct / 100 and tag not in c["alerted"]:
                c["alerted"].append(tag); tg(f"⚠️ Weir key {i+1} at {pct}%: {c['used'][i]}/{WEIR_FREE} credits used this month.", urgent=True)
        return r.json()
    raise RuntimeError(last or f"all {len(keys)} Weir key(s) at the {cap} credit cap")

def weir_posts(page):
    out = []
    for p in page.get("data", []):
        try:
            ts = dt.datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00"))
            u = p["author"]["username"]
            out.append({"id": str(p["id"]), "url": f"https://x.com/{u}/status/{p['id']}", "ts": ts,
                        "text": f"@{u}: {p['text']}", "likes": p.get("likeCount")})
        except Exception: continue
    return out

# ---------------- fetching news ----------------
def page_text(url):
    r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "svg"]): t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:9000]

def discover_feed(src, html):
    l = BeautifulSoup(html, "html.parser").find("link", attrs={"type": re.compile("rss|atom")})
    return urljoin(src["url"], l["href"]) if l and l.get("href") else ""

def scrape_links(src, html):
    base, out, seen_l = urlparse(src["url"]).netloc, [], set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        u = urljoin(src["url"], a["href"]).split("#")[0]; p = urlparse(u)
        if p.netloc == base and p.path.startswith(src["link_prefix"]) and p.path.rstrip("/") != src["link_prefix"].rstrip("/") and u not in seen_l:
            seen_l.add(u); out.append((u, a.get_text(" ", strip=True)[:120]))
    return out

def feed_items(url):
    r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
    return [(e.get("link"), e.get("title", "")) for e in feedparser.parse(r.content).entries[:30]]

def new_items(src, st):
    """RSS first (also auto-discovers feeds on pages); page scraping only as fallback."""
    feed = src["url"] if src["type"] == "rss" else st["feeds"].get(src["name"])
    html, items = None, []
    if src["type"] == "page" and feed is None:
        r = requests.get(src["url"], headers=UA, timeout=25); r.raise_for_status(); html = r.text
        feed = st["feeds"][src["name"]] = discover_feed(src, html)
    if feed:
        try: items = feed_items(feed)
        except Exception:
            if src["type"] == "rss": raise
    if not items and src["type"] == "page":
        if html is None:
            r = requests.get(src["url"], headers=UA, timeout=25); r.raise_for_status(); html = r.text
        items = scrape_links(src, html)
    seen = set(st["seen"]); first = src["name"] not in st["init"]
    fresh = [(u, t) for u, t in items if u and u not in seen]
    if first:
        st["init"].append(src["name"]); st["seen"].extend(u for u, _ in fresh); return []
    fresh = fresh[:MAX_NEW_PER_SOURCE]
    st["seen"].extend(u for u, _ in fresh)      # leftovers are picked up on the next run
    return fresh

# ---------------- verification, chart, drafting ----------------
def classify(title, url, text, prefs=""):
    return gemini(f"""You triage AI news from an OFFICIAL source.{prefs} Title: {title}\nURL: {url}\nText: {text}\n
Return JSON: {{"skip": true if this is not real AI news (jobs, events, marketing fluff, policy boilerplate),
"category": one of {CATEGORIES}, "topic": short lowercase key for the main model/tool/event (e.g. "gpt-6.2"),
"major": true ONLY for flagship model releases, major security breaches/incidents, or industry-shaking announcements, "release": true if this is an actual new model release or upgrade (not just an announcement of an event),
"facts": up to 8 short factual strings taken from the text}}""", tier="fast")

def nums(s): return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", s)}
def names(s): return {n.lower() for n in re.findall(r"[A-Za-z]+-?\d+(?:\.\d+)*", s)}
def verify(tweets, source):
    blob = source.replace(",", ""); low = source.lower(); t = " ".join(tweets)
    return [n for n in nums(t) if n not in blob] + [n for n in names(t) if n not in low]

def make_chart(comp, source, path="chart.png"):
    try:
        ent = [e for e in comp["entries"] if str(e.get("score", "")).replace(".", "", 1).isdigit()]
        if len(ent) < 3: return None
        blob = source.replace(",", "")
        if any(str(e["score"]) not in blob for e in ent): return None
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ent.sort(key=lambda e: -float(e["score"]))
        fig, ax = plt.subplots(figsize=(8, 4.5), facecolor="#0b0b0b"); ax.set_facecolor("#0b0b0b")
        bars = ax.bar([e["name"] for e in ent], [float(e["score"]) for e in ent], color="#3b82f6")
        for b, e in zip(bars, ent): ax.text(b.get_x() + b.get_width() / 2, b.get_height(), str(e["score"]), ha="center", va="bottom", color="white")
        ax.set_title(comp.get("metric", ""), color="white"); ax.tick_params(colors="white", labelrotation=25)
        for s in ax.spines.values(): s.set_color("#333")
        fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig); return path
    except Exception: return None

def draft_buttons(did): return [("✅ Used it", "ok:" + did), ("✏️ Edit", "ed:" + did), ("⏭ Skip", "sk:" + did)]

def draft_message(head, tweets, links, bad):
    flag = f"\n⚠️ Could not verify: {', '.join(bad)} — check before posting" if bad else ""
    return f"{head}\n\n" + "\n\n".join(tweets) + f"\n\nSources:\n{links}{flag}"

def write_and_send(st, group, category, major):
    source = " ".join(" ".join(i["facts"]) + " " + i["text"] for i in group)
    srcs = "\n".join(f"- {i['title']} ({i['url']})\n  facts: {i['facts']}\n  text: {i['text'][:3500]}" for i in group)
    prompt = f"""{STYLE}\nmajor={major}. Category: {category}.
Write ONE X post (or short thread) that combines these related items:\n{srcs}\n
Return JSON: {{"tweets": [strings], "comparison": null or {{"metric": "...", "entries": [{{"name": "...", "score": number}}]}}}}
Only include comparison if the source text has at least 3 comparable scores for the same metric."""
    out = gemini(prompt); tweets = out["tweets"]; bad = verify(tweets, source)
    if bad:
        out = gemini(prompt + f"\nYour last draft used values not in the source: {bad}. Rewrite without them."); tweets = out["tweets"]; bad = verify(tweets, source)
    did = str(int(time.time() * 1000))[-9:]
    d = st.setdefault("drafts", {}); d[did] = {"tweets": tweets, "source": source[:5000], "category": category}
    for k in list(d)[:-20]: d.pop(k)
    head = ("🚨 MAJOR | " if major else "") + category
    sent = tg(draft_message(head, tweets, "\n".join(i["url"] for i in group), bad), urgent=major, buttons=draft_buttons(did))
    st["stats"]["majors" if major else "drafts"] += 1
    if not major: count_post(st)          # major news never counts against the daily limit
    if sent and out.get("comparison") and not bad:
        p = make_chart(out["comparison"], source)
        if p: tg_photo(p, "Chart for the post above")

def edit_draft(st, instruction):
    did = st.pop("editing", None); d = st.get("drafts", {}).get(did or "")
    if not d: tg("That draft has expired. Ask me again.", force=True); return
    out = gemini(f"""{STYLE}\nRewrite these tweets following the user's instruction. Use ONLY facts in SOURCE.
Instruction: {instruction}\nTweets: {json.dumps(d['tweets'])}\nSOURCE: {d['source']}\nReturn JSON: {{"tweets": [strings]}}""")
    d["tweets"] = out["tweets"]; bad = verify(out["tweets"], d["source"])
    tg(draft_message("✏️ Edited draft", out["tweets"], "", bad), force=True, buttons=draft_buttons(did))

def prefs_extra(st):
    p = st.get("prefs", [])
    return ("\nUser's standing preferences (follow for style, topics and format; they NEVER override: use only facts from the source, "
            "invent no numbers, never auto-post):\n- " + "\n- ".join(p)) if p else ""

def styles_extra(st):
    x = st.get("styles", [])
    return ("\nWriting styles the user wants to emulate (learn the patterns only; never copy anyone's wording or facts):\n- " +
            "\n- ".join(f"{a['label']}: {a['notes']}" for a in x)) if x else ""

def approved_extra(st):
    a = st.get("approved", [])[-6:]
    return ("\nTone reference only (posts the user actually used; never copy their facts):\n" + "\n---\n".join(a)) if a else ""

# ---------------- checking sources ----------------
def all_sources(st):
    base = json.load(open("sources.json")) + st.get("sources_extra", [])
    gone = {n.lower() for n in st.get("sources_removed", [])}
    return [x for x in base if x["name"].lower() not in gone]

def all_watch(st):
    base = json.load(open("watchlist.json")) + st.get("watch_extra", [])
    gone = {h.lower() for h in st.get("watch_removed", [])}
    out = []
    for h in base:
        if h.lower() not in gone and h not in out: out.append(h)
    return out

def check(st):
    for src in all_sources(st):
        try: fresh = new_items(src, st); st["fail"][src["name"]] = 0
        except Exception as e:
            n = st["fail"].get(src["name"], 0) + 1; st["fail"][src["name"]] = n
            if n == 6: tg(f"⚠️ Source failing for 30+ min: {src['name']} ({src['url']})\n{str(e)[:200]}")
            continue
        for url, title in fresh:
            try:
                text = page_text(url); c = classify(title, url, text, prefs_extra(st) + " If the preferences say to ignore a kind of news, set skip=true for it."); st["gem_fail"] = 0
                if c.get("skip"): continue
                item = {"title": title, "url": url, "text": text, "facts": c.get("facts", []),
                        "category": c.get("category", "Industry"), "topic": (c.get("topic") or title).lower(), "t": now().isoformat()}
                st["week_log"].append({k: item[k] for k in ("t", "title", "url", "category", "facts")})
                ml = st.setdefault("month_log", []); ml.append({"t": item["t"], "title": item["title"], "url": url, "category": item["category"], "facts": item["facts"][:4]}); del ml[:-500]
                if c.get("release"): st.setdefault("releases", []).append({"topic": item["topic"], "t": item["t"], "urls": [url], "bundled": False})
                if c.get("major"): write_and_send(st, [item], item["category"], True)
                else: st["buffer"].append(item)
            except Exception as e:
                if url in st["seen"]: st["seen"].remove(url)
                st["gem_fail"] = st.get("gem_fail", 0) + 1
                if st["gem_fail"] == 3: tg(f"⚠️ Bot is failing to process news (Gemini/page error): {str(e)[:250]}")

def digest(st):
    if MUTE: return False
    last = dt.datetime.fromisoformat(st["last_digest"]) if st.get("last_digest") else None
    if last and now() - last < dt.timedelta(hours=cv("digest_hours", DIGEST_HOURS)): return False
    st["last_digest"] = now().isoformat()
    cut = (now() - dt.timedelta(hours=30)).isoformat()
    buf = [i for i in st["buffer"] if i["t"] >= cut]; st["buffer"] = []
    by_cat = {}
    for i in buf: by_cat.setdefault(i["category"], {}).setdefault(i["topic"], []).append(i)
    groups = [(cat, g) for cat, topics in by_cat.items() for g in topics.values()]
    groups.sort(key=lambda cg: -(len(cg[1]) + (2 if "Models" in cg[0] else 0)))
    left = max(posts_left(st), 0)
    for cat, g in groups[left:]: st["buffer"].extend(g)          # over the daily limit: keep for tomorrow
    sent = 0
    for cat, group in groups[:left]:
        try: write_and_send(st, group, cat, False); sent += 1
        except Exception as e: st["buffer"].extend(group); tg(f"⚠️ Draft failed for '{group[0]['title']}': {str(e)[:200]}")
        time.sleep(1)
    return sent > 0

def recent(st, hours):
    cut = now() - dt.timedelta(hours=hours)
    return [x for x in st["week_log"] if dt.datetime.fromisoformat(x["t"]) >= cut]

# ---------------- quote tweets + X leads ----------------
def send_quote_options(st, ctx, note="", image=None, link="", force=False, lead=False, check=False):
    v = check_claim(ctx) if check else None
    fc = ""
    if v:
        fc = (f"FACT-CHECK RESULT: {v.get('verdict')}. Evidence: {v.get('evidence')}. Sources: {v.get('sources')}.\n"
              "Rules: supported -> agree and ADD useful context from the evidence. partly -> agree with the true part and correct the rest. contradicted -> respectfully disagree or correct, using only the evidence. "
              "unverifiable -> do not assert either way; ask for a source or frame it as your opinion. Never claim facts beyond the evidence.")
    out = gemini(f"""The user wants to QUOTE-TWEET this post from X. Post: {ctx}
User's angle/note (may be empty): {note}
{fc}
Write 4 quote-tweet options in the user's own voice. Decide from the facts whether the post is right: if it is, add to it; if it is wrong, politely contradict it; if unsure, ask a question.
Mix of: 1) agree + add (or correct, when the evidence says it is wrong), 2) a take with 1-2 sentences of context FIRST then a question, 3) funny/witty, 4) short natural reaction.
Rules: plain human tone, no hashtags, <=280 chars each. Use only facts visible in the post/screenshot or in the fact-check evidence; never invent numbers or put words in the author's mouth; opinions must read as opinions.
{('Tone reference: ' + approved_extra(st)) if st.get('approved') else ''}
Return JSON: {{"options": [{{"style": "...", "text": "..."}}]}}""", image)
    vline = ""
    if v:
        vline = f"🔍 Fact-check: {v.get('verdict')}. {v.get('evidence', '')}" + (" Sources: " + ", ".join(v.get("sources", [])) if v.get("sources") else "") + "\n\n"
    head = (f"🔎 Lead: {lead if isinstance(lead, str) else 'X post'} (not confirmed news)\n" if lead else "💬 Quote-tweet options\n") + (f"{ctx[:300]}\n{link}\n\n" if link else "\n") + vline
    tg(head + "\n\n".join(f"{i+1}) [{o['style']}]\n{o['text']}" for i, o in enumerate(out["options"])), force=force)
    st["stats"]["quotes"] += 1

def get_tweet(st, url):
    """Fallback chain for one post: FxTwitter -> Weir (search that user's recent posts)."""
    m = re.search(r"(?:x|twitter)\.com/([^/\s]+)/status/(\d+)", url)
    if not m: return None
    try:
        j = requests.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", headers=UA, timeout=20).json()["tweet"]
        return f"@{j['author']['screen_name']}: {j['text']}"
    except Exception: pass
    try:
        for p in weir_posts(weir(st, "/v1/x/search", {"fromUsers": [m.group(1)], "limit": 20}, cap=AGENT_CAP)):
            if p["id"] == m.group(2): return p["text"]
    except Exception: pass
    return None

def quote_help(st, m):
    text = (m.get("text") or m.get("caption") or "").strip(); image = None
    if m.get("photo"):
        fid = m["photo"][-1]["file_id"]
        fp = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile", params={"file_id": fid}, timeout=30).json()["result"]["file_path"]
        image = base64.b64encode(requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{fp}", timeout=60).content).decode()
    if not text and not image: return
    tweet = get_tweet(st, text) if "status/" in text else None
    if "status/" in text and not tweet and not image:
        tg("Couldn't read that link. Paste the tweet text or send a screenshot instead.", force=True); return
    send_quote_options(st, tweet or text or "(see screenshot)", re.sub(r"https?://\S+", "", text).strip(), image, force=True, check=bool(tweet or text))

def verified_tweet(url):
    m = re.search(r"(?:x|twitter)\.com/([^/\s]+)/status/(\d+)", url)
    if not m: return None
    j = requests.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", headers=UA, timeout=20).json().get("tweet")
    if not j or j["author"]["screen_name"].lower() != m.group(1).lower(): return None
    if now().timestamp() - j.get("created_timestamp", 0) > 24 * 3600: return None
    return {"id": m.group(2), "url": f"https://x.com/{m.group(1)}/status/{m.group(2)}", "text": f"@{j['author']['screen_name']}: {j['text']}"}

TOPIC_MIN_LIKES = 300
TOPIC_QUERIES = ["new AI model release", "AI model benchmark comparison", "same prompt two AI models side by side",
                 "I tested the new AI model", "AI agents new tool", "AI video model new", "Claude Gemini GPT Grok DeepSeek update"]

def pick_relevant(cands, limit=3):
    """Keep only posts clearly about AI and useful for the account, best first, max 2 per author, each labelled with a kind."""
    if not cands: return []
    items = [{"id": c["id"], "text": c["text"][:280]} for c in cands[:30]]
    out = gemini(f"""You pick X posts worth quote-tweeting for an AI-news account. Keep ONLY posts clearly about AI that fit one of these kinds:
news (new model release, upgrade, outage, security issue, major AI company news), comparison (side-by-side or same-prompt test of two or more AI models, benchmark results),
test (someone's hands-on test or demo of an AI model with real results), video (AI video/creative/3D demos), agents (AI agents/tools/open-source projects).
Reject everything else (cars, rockets, space, politics, general jokes, replies, anything not about AI). At most 2 posts per author. Prefer a mix of kinds.
Also set "claim": true if the post makes a specific factual claim worth fact-checking (numbers, release facts, comparisons), false for pure opinions or jokes.
Return JSON {{"picks": [{{"id": "...", "kind": "news|comparison|test|video|agents", "claim": true}}]}}, best first, at most {limit}; empty list if none qualify.
Posts: {json.dumps(items)}""", tier="fast")
    by = {c["id"]: c for c in cands}; res = []
    for p in out.get("picks", []):
        c = by.get(str(p.get("id")))
        if c: c["kind"] = str(p.get("kind", "other")); c["claim"] = bool(p.get("claim")); res.append(c)
    return res[:limit]

def scan_candidates(st):
    """X-wide AI discovery. Weir topic searches (2 rotating queries per scan) + a small watchlist search; fallback to Google Search + FxTwitter."""
    cutoff = now() - dt.timedelta(hours=24); out, notes = [], []
    i = st.get("q_idx", 0); picks = [TOPIC_QUERIES[(i + k) % len(TOPIC_QUERIES)] for k in range(2)]; st["q_idx"] = (i + 2) % len(TOPIC_QUERIES)
    for q in picks:
        try:
            ps = weir_posts(weir(st, "/v1/x/search", {"query": q, "language": "en", "minLikes": TOPIC_MIN_LIKES, "limit": 8}, cap=SCAN_CAP))
            fresh = [p for p in ps if p["ts"] >= cutoff]; out += fresh; notes.append(f"'{q}': {len(ps)} found, {len(fresh)} fresh")
        except Exception as e:
            notes.append(f"'{q}' failed: {str(e)[:60]}"); break
    try:
        ps = weir_posts(weir(st, "/v1/x/search", {"fromUsers": all_watch(st), "onlyOriginal": True, "minLikes": WEIR_MIN_LIKES, "limit": 8}, cap=SCAN_CAP))
        fresh = [p for p in ps if p["ts"] >= cutoff]; out += fresh; notes.append(f"watchlist: {len(ps)} found, {len(fresh)} fresh")
    except Exception as e:
        notes.append(f"watchlist failed: {str(e)[:60]}")
    seen_ids = set(); out = [p for p in out if not (p["id"] in seen_ids or seen_ids.add(p["id"]))]
    st["scan_note"] = "Last scan: " + " | ".join(notes)
    if out: return out, "weir"
    try:   # fallback: Google Search finds popular AI posts, FxTwitter verifies each one is real and recent
        txt = gemini_search("Using Google Search, find popular posts on X (x.com) from the last 24 hours about AI: new model releases or upgrades, side-by-side comparisons of AI models, "
                            "hands-on tests, AI video/agent demos. List the direct x.com/<user>/status/<id> URL of each post you actually found. Do not guess or invent URLs.")
    except Exception as e:
        st["scan_note"] += f" | Google fallback failed: {str(e)[:60]}"; return [], "none"
    res = []
    for u in dict.fromkeys(re.findall(r"https?://(?:x|twitter)\.com/[^/\s]+/status/\d+", txt)):
        try:
            t = verified_tweet(u)
            if t: res.append(t)
        except Exception: continue
    return res, "google"

def scan_x(st, force=False):
    if MUTE and not force: return
    last = dt.datetime.fromisoformat(st["last_scan"]) if st.get("last_scan") else None
    if not force and last and now() - last < dt.timedelta(hours=cv("scan_hours", SCAN_HOURS)): return
    st["last_scan"] = now().isoformat()
    cands, how = scan_candidates(st)
    seen = set(st.setdefault("seen_tweets", [])); sent = 0
    cands.sort(key=lambda p: -(p.get("likes") or 0))
    cands = [c for c in cands if c["id"] not in seen]
    try: cands = pick_relevant(cands, 3)
    except Exception as e: st["scan_note"] = f"lead filter failed ({str(e)[:60]})"; cands = []
    for t in cands:
        if t["id"] in seen: continue
        try:
            st["seen_tweets"].append(t["id"]); send_quote_options(st, t["text"], link=t["url"], lead=t.get("kind") or True, force=force, check=bool(t.get("claim")) and sent < 2); sent += 1
        except Exception: continue
        if sent >= 3: break
    st["seen_tweets"] = st["seen_tweets"][-500:]
    if force and not sent: tg("No AI posts worth quoting right now (try again later, or check /status for what the last scan found).", force=True)


# ---------------- web-wide research (beyond X) ----------------
WEB_HOURS = 4
WEB_DEFAULT = {"subreddits": ["LocalLLaMA", "singularity", "ClaudeAI", "OpenAI", "artificial"],
               "hn_keywords": ["AI", "LLM", "Claude", "Gemini", "GPT"], "gh_topics": ["llm", "ai-agent"]}

def web_cfg(st):
    c = st.setdefault("web", {})
    for k, v in WEB_DEFAULT.items(): c.setdefault(k, list(v))
    return c

def hn_items(st):
    ts = int((now() - dt.timedelta(hours=24)).timestamp()); out = {}
    for kw in web_cfg(st)["hn_keywords"][:6]:
        r = requests.get("https://hn.algolia.com/api/v1/search", params={"tags": "story", "query": kw, "numericFilters": f"created_at_i>{ts},points>60", "hitsPerPage": 10}, headers=UA, timeout=25)
        r.raise_for_status()
        for h in r.json().get("hits", []):
            u = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
            out[u] = {"src": "Hacker News", "title": h.get("title", ""), "url": u, "score": h.get("points", 0), "text": ""}
    return list(out.values())

def reddit_items(st):
    out = []
    for sub in web_cfg(st)["subreddits"][:8]:
        r = requests.get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit=8", headers=UA, timeout=25); r.raise_for_status()
        for e in feedparser.parse(r.content).entries[:8]:
            out.append({"src": f"Reddit r/{sub}", "title": e.get("title", ""), "url": e.get("link", ""), "score": 0, "text": ""})
    return out

def github_items(st):
    d = (now() - dt.timedelta(days=7)).strftime("%Y-%m-%d"); out = []
    for t in web_cfg(st)["gh_topics"][:4]:
        r = requests.get("https://api.github.com/search/repositories", params={"q": f"topic:{t} created:>{d}", "sort": "stars", "order": "desc", "per_page": 6},
                         headers={**UA, "Accept": "application/vnd.github+json"}, timeout=25); r.raise_for_status()
        for x in r.json().get("items", []):
            out.append({"src": "GitHub", "title": f"{x['full_name']}: {x.get('description') or ''}"[:200], "url": x["html_url"], "score": x.get("stargazers_count", 0), "text": x.get("description") or ""})
    return out

def hf_items(st):
    r = requests.get("https://huggingface.co/api/models", params={"sort": "trendingScore", "limit": 8}, headers=UA, timeout=25); r.raise_for_status()
    return [{"src": "Hugging Face (trending)", "title": m.get("id", ""), "url": "https://huggingface.co/" + m.get("id", ""), "score": m.get("likes", 0), "text": ""} for m in r.json()]

def sweep_items(st):
    """Live web research through Gemini + Google Search; keeps only items whose URL really appears in the search answer."""
    txt = gemini_search("Using Google Search, list the most notable AI developments from the last 24 hours: new model releases or upgrades, benchmark results, comparisons of AI models, "
                        "security incidents, major company or policy news, new tools. For each give a one-line title, what happened (with exact numbers if any) and the source URL.")
    out = gemini(f"""Extract items from this text. Return JSON {{"items": [{{"title": "", "url": "", "summary": ""}}]}}. Only include items whose URL is present in the text.\nTEXT:\n{txt[:7000]}""", tier="fast")
    return [{"src": "Web search", "title": i.get("title", ""), "url": i.get("url", ""), "score": 0, "text": i.get("summary", "")}
            for i in out.get("items", []) if i.get("url") and i["url"] in txt]

def pick_web(items, limit=4):
    its = [{"id": str(i), "src": x["src"], "title": x["title"][:160], "score": x["score"]} for i, x in enumerate(items[:40])]
    out = gemini(f"""You pick items for an AI-news X account to react to. Keep ONLY items clearly about AI of these kinds: news (model release/upgrade, outage, security issue, major company news),
comparison (model-vs-model tests, benchmarks), test (hands-on results), video (AI video/creative/3D), agents (agents/tools/open-source projects), resource (genuinely useful free courses/lists/tools).
Reject anything else. Prefer a mix of kinds and sources. Return JSON {{"picks": [{{"id": "...", "kind": "news|comparison|test|video|agents|resource"}}]}}, best first, at most {limit}; empty if none qualify.
Items: {json.dumps(its)}""", tier="fast")
    res = []
    for p in out.get("picks", []):
        try: x = items[int(p["id"])]; x["kind"] = str(p.get("kind", "other")); res.append(x)
        except Exception: continue
    return res[:limit]

def official_domains(st): return {urlparse(x["url"]).netloc.replace("www.", "") for x in all_sources(st)}

def web_leads(st, force=False, limit=None, quiet=False):
    if MUTE and not force: return
    if not force and posts_left(st) <= 0: return
    last = dt.datetime.fromisoformat(st["last_web"]) if st.get("last_web") else None
    if not force and last and now() - last < dt.timedelta(hours=cv("web_hours", WEB_HOURS)): return
    st["last_web"] = now().isoformat()
    notes, items = [], []
    for name, fn in (("Hacker News", hn_items), ("Reddit", reddit_items), ("GitHub", github_items), ("Hugging Face", hf_items), ("Web search", sweep_items)):
        try: got = fn(st) if name != "Web search" else fn(st); items += got; notes.append(f"{name}: {len(got)}")
        except Exception as e: notes.append(f"{name} failed: {str(e)[:40]}")
    seen = set(st.setdefault("seen_web", []))
    fresh = [x for x in items if x["url"] and x["url"] not in seen and x["title"]]
    fresh.sort(key=lambda x: -(x.get("score") or 0))
    st["web_note"] = "Last web scan: " + " | ".join(notes) + f" | new: {len(fresh)}"
    for x in fresh: st["seen_web"].append(x["url"])
    st["seen_web"] = st["seen_web"][-800:]
    try: picks = pick_web(fresh, limit or max(1, min(4, posts_left(st)))) if fresh else []
    except Exception as e: st["web_note"] += f" | filter failed: {str(e)[:50]}"; picks = []
    sent = 0
    for x in picks:
        try:
            try: body = (x["text"] + " " + page_text(x["url"]))[:6000]
            except Exception: body = x["text"]
            source = f"{x['title']} {body}"
            out = gemini(f"""{STYLE}\nSOURCE ({x['src']}): {x['title']} {x['url']}\n{body}\n
Write JSON {{"why": "one line on why this matters to AI followers", "tweets": [2 alternative draft posts in the user's style using ONLY facts in SOURCE]}}""")
            bad = verify(out["tweets"], source); did = str(int(time.time() * 1000))[-9:]
            d = st.setdefault("drafts", {}); d[did] = {"tweets": out["tweets"], "source": source[:5000], "category": x["kind"]}
            for k in list(d)[:-20]: d.pop(k)
            off = urlparse(x["url"]).netloc.replace("www.", "") in official_domains(st)
            sc = f", {x['score']} pts/stars" if x.get("score") else ""
            tg(f"🌐 Lead: {x['kind']} · {x['src']}{sc}\n{x['title'][:200]}\n{x['url']}\n" + ("✅ Official source\n" if off else "⚠️ Not confirmed news: check the link first\n") +
               f"\nWhy: {out.get('why','')}\n\n" + "\n\n".join(f"{i+1}) {t}" for i, t in enumerate(out["tweets"])) +
               (f"\n\n⚠️ Could not verify: {', '.join(bad)}" if bad else ""), force=force, buttons=draft_buttons(did))
            st["stats"]["quotes"] += 1; sent += 1; count_post(st)
        except Exception as e: st["web_note"] += f" | draft failed: {str(e)[:50]}"
    if force and not sent and not quiet: tg("No new web leads worth posting right now. " + st["web_note"], force=True)

# ---------------- learning a style from an account ----------------
def learn_style(st, handle):
    ps = weir_posts(weir(st, "/v1/x/search", {"fromUsers": [handle], "onlyOriginal": True, "limit": 15}, cap=AGENT_CAP))
    if not ps: raise RuntimeError(f"no posts found for @{handle}")
    texts = "\n---\n".join(p["text"][:280] for p in ps)
    out = gemini(f"""Analyse how @{handle} writes on X from these recent posts. Describe PATTERNS ONLY (tone, length, openers, emoji use, structure, recurring formats, kinds of AI topics). Never quote their posts.
POSTS:\n{texts}\nReturn JSON {{"style": "1-2 sentences", "tweet_types": [up to 6 short labels], "topics": [up to 6]}}""")
    label = f"@{handle} (auto)"
    x = [a for a in st.get("styles", []) if a["label"] != label]
    x.append({"label": label, "notes": (str(out.get("style", "")) + " Types: " + "; ".join(map(str, out.get("tweet_types", []))))[:350]})
    st["styles"] = x[-5:]; st.setdefault("style_refresh", {})[handle] = now().isoformat()
    return out

def ensure_styles(st):
    """Learn (and weekly refresh) the writing style of accounts in style_accounts. One per run."""
    for h in st.get("style_accounts", [])[:3]:
        last = st.get("style_refresh", {}).get(h)
        if last and now() - dt.datetime.fromisoformat(last) < dt.timedelta(days=7): continue
        try: learn_style(st, h)
        except Exception as e:
            st.setdefault("style_refresh", {})[h] = (now() - dt.timedelta(days=6)).isoformat()   # retry tomorrow
            st["web_note"] = st.get("web_note", "") + f" | style @{h} failed: {str(e)[:50]}"
        break

def t_add_style_account(st, handle, user_said):
    if not _said(user_said): return NOT_SAID
    h = (handle or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h): return {"error": "invalid X handle"}
    try: out = learn_style(st, h)
    except Exception as e: return {"error": str(e)[:200]}
    if h.lower() not in {x.lower() for x in st.setdefault("style_accounts", [])}: st["style_accounts"].append(h)
    return {"ok": f"learned @{h}'s style (patterns only) and will refresh weekly", "style": out.get("style"), "types": out.get("tweet_types")}

def t_remove_style_account(st, handle, user_said):
    if not _said(user_said): return NOT_SAID
    h = (handle or "").strip().lstrip("@")
    st["style_accounts"] = [x for x in st.get("style_accounts", []) if x.lower() != h.lower()]
    st["styles"] = [a for a in st.get("styles", []) if a["label"].lower() != f"@{h} (auto)".lower()]
    return {"ok": "removed", "style_accounts": st["style_accounts"]}

def t_subreddit(st, name, user_said, add=True):
    if not _said(user_said): return NOT_SAID
    n = re.sub(r"^/?r/", "", (name or "").strip()).strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]{2,25}", n): return {"error": "invalid subreddit name"}
    subs = web_cfg(st)["subreddits"]
    if add and n.lower() not in {x.lower() for x in subs}: subs.append(n)
    if not add: web_cfg(st)["subreddits"] = [x for x in subs if x.lower() != n.lower()]
    return {"ok": "updated", "subreddits": web_cfg(st)["subreddits"]}

# ---------------- settings, daily budget, helpers ----------------
TZ_OFFSET = 1                 # hours from UTC for "today" (Nigeria = 1); change with: "set tz_offset to 0"
DAILY_MAX, DAILY_MIN = 8, 2   # original posts per day (news batches, web leads, repo roundups, custom sections)
CFGV = {}                     # chat-changed settings (state.json "cfg")

def cv(k, d): return CFGV.get(k, d)

def day_key(st): return (now() + dt.timedelta(hours=cv("tz_offset", TZ_OFFSET))).strftime("%Y-%m-%d")

def _daily(st):
    d = st.setdefault("daily", {})
    if d.get("day") != day_key(st): d.clear(); d.update({"day": day_key(st), "n": 0, "min_done": False})
    return d

def posts_left(st): return int(cv("daily_max", DAILY_MAX)) - _daily(st)["n"]
def count_post(st): _daily(st)["n"] += 1

SETTINGS = {"daily_max": (1, 12), "daily_min": (0, 6), "digest_hours": (1, 12), "scan_hours": (2, 24), "web_hours": (2, 24), "tz_offset": (-12, 14),
            "ideas_hour_utc": (0, 23), "release_hour_utc": (0, 23), "bundle_hour_utc": (0, 23), "catchup_hour_utc": (0, 23),
            "sleep_start_utc": (0, 23), "sleep_end_utc": (0, 23)}

def t_set_setting(st, key, value, user_said):
    if not _said(user_said): return NOT_SAID
    if key not in SETTINGS: return {"error": f"allowed settings: {', '.join(SETTINGS)}"}
    c = st.setdefault("cfg", {})
    if str(value).lower() == "off" and key.startswith("sleep"): c[key] = "off"; return {"ok": f"{key} = off"}
    try: v = int(value)
    except Exception: return {"error": "value must be a whole number"}
    lo, hi = SETTINGS[key]
    if not lo <= v <= hi: return {"error": f"{key} must be between {lo} and {hi}"}
    c[key] = v; return {"ok": f"{key} = {v}", "cfg": c}

def tg_doc(path, caption=""):
    with open(path, "rb") as f:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_CHAT, "caption": caption[:900]}, files={"document": f}, timeout=90).raise_for_status()

def send_draft(st, head, tweets, links, bad, source, category, urgent=False, count=True, force=False):
    did = str(int(time.time() * 1000))[-9:]
    d = st.setdefault("drafts", {}); d[did] = {"tweets": tweets, "source": source[:5000], "category": category}
    for k in list(d)[:-20]: d.pop(k)
    msg = draft_message(head, tweets, links, bad)
    sent = tg(msg, urgent=urgent, force=force, buttons=draft_buttons(did))
    if count: count_post(st)
    return sent

# ---------------- fact-check for quote tweets ----------------
def check_claim(text):
    try:
        ans = gemini_search("Fact-check this X post using reliable sources. Say clearly whether it is supported, partly true, contradicted, or cannot be verified. "
                            "Give the key evidence and source URLs.\nPOST: " + text[:500])
        v = gemini(f"""From this fact-check write JSON {{"verdict": "supported|partly|contradicted|unverifiable", "evidence": "1-2 sentences", "sources": [source URLs that appear in the text]}}.
TEXT:\n{ans[:4000]}""", tier="fast")
        v["sources"] = [u for u in v.get("sources", []) if u in ans][:2]
        return v
    except Exception:
        return None

# ---------------- release preview, evening bundle, benchmark comparison ----------------
def benchmark_blob(st, topic):
    parts = []
    try: parts.append(page_text("https://artificialanalysis.ai/leaderboards/models")[:5000])
    except Exception: pass
    try:
        parts.append(gemini_search(f"Current benchmark scores (Artificial Analysis Intelligence Index, LMArena, or the company's own published benchmarks) for {topic} compared with the top "
                                   "flagship AI models (GPT, Claude, Gemini, Grok, DeepSeek, Kimi). Give model names with exact scores and source URLs.")[:5000])
    except Exception: pass
    blob = " ".join(parts); comp = None
    if blob.strip():
        try:
            o = gemini(f"""From TEXT return JSON {{"comparison": null or {{"metric": "", "entries": [{{"name": "", "score": 0}}]}}}}.
Use ONLY scores literally present in TEXT, for ONE metric, at least 3 models including {topic}. Otherwise null.\nTEXT:\n{blob[:9000]}""", tier="fast")
            comp = o.get("comparison")
        except Exception: pass
    return blob, comp

def release_watch(st, force=False):
    """Once a day: preview post for launches/events the COMPANY has officially announced for today or the next 2 days."""
    if not force and (MUTE or now().hour < cv("release_hour_utc", 6) or st.get("last_release_watch") == day_key(st)): return
    st["last_release_watch"] = day_key(st)
    if posts_left(st) <= 0 and not force: return
    try:
        txt = gemini_search("Using Google Search, find AI model releases, product launches or livestream events that the COMPANY ITSELF has officially announced for today or the next 2 days "
                            "(official blog, official account or event page). Exclude leaks and rumors. For each give company, what is announced, date/time and the official source URL.")
        out = gemini(f"""Extract JSON {{"events": [{{"company": "", "what": "", "when": "", "url": ""}}]}}. Only items whose URL is present in the text.\nTEXT:\n{txt[:6000]}""", tier="fast")
        ev = [e for e in out.get("events", []) if e.get("url") and e["url"] in txt][:3]
        if not ev:
            if force: tg("No officially announced launches or events found for the next 2 days.", force=True)
            return
        d = gemini(f"""{STYLE}\nWrite ONE 'what to expect today' preview post (or a very short thread) using ONLY these officially announced facts.
Say clearly it is announced and not yet released. No rumors, no guessed specs or benchmarks.\nEVENTS: {json.dumps(ev)}\nSOURCE: {txt[:5000]}\nReturn JSON {{"tweets": [strings]}}""")
        bad = verify(d["tweets"], txt)
        send_draft(st, "🗓 What to expect (officially announced)", d["tweets"], "\n".join(e["url"] for e in ev), bad, txt, "Release preview", force=force)
    except Exception as e:
        st["web_note"] = st.get("web_note", "") + f" | release watch failed: {str(e)[:50]}"

def release_bundle(st, force=False):
    """Once a day in the evening: everything known about today's releases, with a benchmark comparison chart when real numbers exist."""
    n = now()
    if not force and (MUTE or n.hour < cv("bundle_hour_utc", 17) or st.get("last_bundle") == day_key(st)): return
    st["last_bundle"] = day_key(st)
    cut = (n - dt.timedelta(hours=24)).isoformat()
    todo = [r for r in st.get("releases", []) if (force or not r.get("bundled")) and r["t"] >= cut][:2]
    if force and not todo: tg("No major releases in the last 24 hours to round up.", force=True)
    for r in todo:
        r["bundled"] = True
        key = (r["topic"].split() or [""])[0].lower()
        facts = [x for x in st.get("month_log", []) if key and key in (x["title"] + " " + " ".join(x["facts"])).lower()][-10:]
        blob, comp = benchmark_blob(st, r["topic"])
        source = json.dumps(facts) + " " + blob
        try:
            d = gemini(f"""{STYLE}\nWrite an evening roundup (one post or a thread of 3-5 posts) about {r['topic']}: what was released, price/availability, and how it compares with top models.
Use ONLY facts in SOURCE. If benchmark numbers are missing say so, do not guess.\nSOURCE: {source[:9000]}\nReturn JSON {{"tweets": [strings]}}""")
            bad = verify(d["tweets"], source)
            send_draft(st, f"🌆 Evening roundup: {r['topic']}", d["tweets"], "\n".join(r.get("urls", [])), bad, source, "Release roundup", force=force)
            if comp and not bad:
                p = make_chart(comp, source)
                if p: tg_photo(p, "Benchmark chart for the roundup (numbers checked against the sources)")
        except Exception as e:
            st["web_note"] = st.get("web_note", "") + f" | bundle failed: {str(e)[:50]}"

# ---------------- repo roundups ("repo section") ----------------
REPO_THEMES = ["AI agents", "AI video editing and motion graphics", "3D and animation", "UI and UX design with AI", "local LLM tools", "voice and audio AI", "coding agents", "RAG and data tools"]

def repo_roundup(st, theme=None, n=15, force=False, quiet=False):
    if not force and (MUTE or posts_left(st) <= 0): return
    i = st.get("repo_i", 0); theme = theme or REPO_THEMES[i % len(REPO_THEMES)]
    if theme == REPO_THEMES[i % len(REPO_THEMES)]: st["repo_i"] = i + 1
    since = (now() - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    r = requests.get("https://api.github.com/search/repositories", params={"q": f"{theme} in:name,description,topics stars:>150 pushed:>{since} archived:false", "sort": "stars", "order": "desc", "per_page": 40},
                     headers={**UA, "Accept": "application/vnd.github+json"}, timeout=25); r.raise_for_status()
    repos = {x["full_name"]: {"url": x["html_url"], "desc": (x.get("description") or "")[:160]} for x in r.json().get("items", []) if x.get("description") and not x.get("archived")}
    if len(repos) < 8:
        if not quiet: tg(f"Couldn't find enough good repos for '{theme}'.", force=True)
        return
    avail = json.dumps([{"name": k, "desc": v["desc"]} for k, v in list(repos.items())[:40]])
    o = gemini(f"""{STYLE}\nBuild a GitHub repo roundup post on the theme '{theme}' from ONLY these real repos: {avail}
Pick the {n} most useful and varied. Return JSON {{"hook": "1-2 punchy lines that fit THIS theme, no income claims, no invented credentials. Do not reuse an opener from this list: {st.get('hooks', [])[-6:]}", "intro": ["2-3 very short lines"], "picks": [{{"name": "owner/repo", "line": "max 9 words describing what it does, based on its description"}}],
"flow": "a short arrow chain like Idea -> build -> test -> ship", "closing": "one short line asking people to bookmark it"}}""")
    lines, used = [], []
    for p in o.get("picks", []):
        if p.get("name") in repos and p["name"] not in used:
            used.append(p["name"]); lines.append(f"{len(used):02d} {p['name'].split('/')[1]} → {p.get('line', '')}\n{repos[p['name']]['url']}")
    if len(lines) < 5:
        if not quiet: tg("The roundup didn't come out right (too few valid repos). Try again.", force=True)
        return
    text = f"{o.get('hook', '')}\n\n" + "\n".join(o.get("intro", [])[:3]) + "\n\n" + "\n".join(lines) + f"\n\n{o.get('flow', '')}\n\n{o.get('closing', '')}"
    vtext = " ".join([str(o.get("hook", "")), " ".join(map(str, o.get("intro", []))), " ".join(str(p.get("line", "")) for p in o.get("picks", []) if p.get("name") in repos), str(o.get("flow", "")), str(o.get("closing", ""))])
    bad = verify([vtext], " ".join(v["desc"] + " " + k for k, v in repos.items()) + " " + avail)
    st["last_repo"] = day_key(st)
    st.setdefault("hooks", []).append(str(o.get("hook", ""))[:80]); del st["hooks"][:-10]
    head = f"📦 Repo roundup: {theme} ({len(lines)} repos, all checked on GitHub)"
    if len(text) > 3500:
        half = len(lines) // 2; part1 = text[:text.index(lines[half])]; part2 = text[len(part1):]
        send_draft(st, head + " (part 1)", [part1], "", bad, avail, "Repo roundup", force=force)
        tg("Part 2:\n\n" + part2, force=force)
    else: send_draft(st, head, [text], "", bad, avail, "Repo roundup", force=force)

def repo_weekly(st):
    n = now()
    if n.weekday() != 2 or n.hour < 10 or st.get("last_repo") == day_key(st): return
    try: repo_roundup(st)
    except Exception as e: st["web_note"] = st.get("web_note", "") + f" | repo roundup failed: {str(e)[:50]}"

# ---------------- monthly article ----------------
def article_draft(st, topic=None, force=False):
    log = [x for x in st.get("month_log", [])][-120:]
    if not topic:
        if not log:
            if force: tg("I don't have enough collected news yet to pick an article topic. Give me one: ask in chat, e.g. 'draft an article about AI video tools'.", force=True)
            return
        t = gemini(f"""Pick the single best topic for a detailed, practical article for an AI audience from this month's verified news. Return JSON {{"topic": "", "angle": ""}}.\nNEWS: {json.dumps([{'t': x['title'], 'c': x['category']} for x in log])[:6000]}""", tier="fast")
        topic = f"{t.get('topic', '')}: {t.get('angle', '')}"
    key = (topic.split() or [""])[0].lower()
    urls = [x["url"] for x in log if key and key in (x["title"] + " " + " ".join(x["facts"])).lower()][-5:]
    texts = []
    for u in urls:
        try: texts.append(f"[{u}] " + page_text(u)[:4000])
        except Exception: pass
    try: texts.append("[web search] " + gemini_search(f"Key verified facts and current context for an article about: {topic}. Include source URLs.")[:5000])
    except Exception: pass
    source = " ".join(texts) + " " + json.dumps([x for x in log if key and key in x["title"].lower()])[:3000]
    outline = gemini(f"""{STYLE}\nOutline a detailed practical article on: {topic}. Audience: AI-curious builders. Use only the SOURCE for facts.
Return JSON {{"title": "", "subtitle": "", "hook": "2-3 short paragraphs with an opener that fits this topic and is NOT one of: {st.get('hooks', [])[-6:]}", "sections": [{{"heading": "", "points": ["..."]}}], "closing": "short practical takeaway + a rule to remember"}} with 5-7 sections.\nSOURCE: {source[:9000]}""")
    parts = [f"# {outline.get('title', topic)}\n\n*{outline.get('subtitle', '')}*\n\n{outline.get('hook', '')}"]; unverified = []
    for i, sec in enumerate(outline.get("sections", [])[:7], 1):
        body = gemini(f"""{STYLE}\nWrite section {i} of the article '{outline.get('title', '')}': '{sec.get('heading', '')}'. Points to cover: {sec.get('points')}.
150-250 words, practical, use short paragraphs and numbered steps or a small code/example block where it helps. Facts, numbers, prices and claims ONLY from SOURCE; general how-to advice is fine.
Never invent credentials, income or results.\nSOURCE: {source[:9000]}\nReturn JSON {{"text": ""}}""")
        txt = body.get("text", ""); bad = verify([txt], source)
        if bad: unverified.append(f"section {i}: {', '.join(bad)}")
        parts.append(f"## {i:02d} {sec.get('heading', '')}\n\n{txt}")
    parts.append(f"## Takeaway\n\n{outline.get('closing', '')}")
    path = "article.md"; open(path, "w").write("\n\n".join(parts))
    st["last_article"] = now().strftime("%Y-%m")
    st.setdefault("hooks", []).append(str(outline.get("hook", ""))[:80]); del st["hooks"][:-10]
    tg_doc(path, f"📝 Article draft: {outline.get('title', topic)}\nReview every claim before posting. " + ("⚠️ Unverified numbers: " + "; ".join(unverified) if unverified else "Numbers checked against the sources."))
    count_post(st)

def article_monthly(st):
    n = now()
    if MUTE or (n + dt.timedelta(hours=cv("tz_offset", TZ_OFFSET))).day != 1 or n.hour < 10 or st.get("last_article") == n.strftime("%Y-%m"): return
    try: article_draft(st)
    except Exception as e: st["web_note"] = st.get("web_note", "") + f" | article failed: {str(e)[:50]}"

# ---------------- custom sections you define in chat ----------------
def run_sections(st):
    if MUTE: return
    n = now()
    for sec in st.get("sections", []):
        if n.hour < int(sec.get("hour_utc", 9)): continue
        last = dt.datetime.fromisoformat(sec["last_iso"]) if sec.get("last_iso") else None
        gap = {"daily": 0.9, "weekly": 6.9, "monthly": 27.9}.get(sec.get("every", "weekly"), 6.9)
        if last and (n - last).total_seconds() < gap * 86400: continue
        if posts_left(st) <= 0: continue
        try:
            txt = agent_run(st, f"Scheduled section '{sec['name']}'. The user pre-approved drafting for this section, so write the draft now. Instructions: {sec['instructions']}. "
                                "Use tools for every fact, label leads as leads, keep it short for a phone, no invented numbers.")
            sec["last_iso"] = n.isoformat()
            did = str(int(time.time() * 1000))[-9:]; d = st.setdefault("drafts", {}); d[did] = {"tweets": [txt], "source": "", "category": sec["name"]}
            tg(f"🧩 {sec['name']}\n\n{txt}", buttons=[("✅ Used it", "ok:" + did), ("⏭ Skip", "sk:" + did)]); count_post(st)
        except Exception as e:
            st["web_note"] = st.get("web_note", "") + f" | section {sec['name']} failed: {str(e)[:50]}"

def t_add_section(st, name, instructions, every, hour_utc, user_said):
    if not _said(user_said): return NOT_SAID
    secs = st.setdefault("sections", [])
    name = (name or "").strip()[:40]; every = (every or "weekly").lower()
    if not name or not (instructions or "").strip(): return {"error": "need a name and instructions"}
    if every not in ("daily", "weekly", "monthly"): return {"error": "every must be daily, weekly or monthly"}
    if len(secs) >= 6 and name.lower() not in {s["name"].lower() for s in secs}: return {"error": "6 custom sections max. Remove one first."}
    hr = 9 if hour_utc in (None, "") else max(0, min(23, int(hour_utc)))
    st["sections"] = [s for s in secs if s["name"].lower() != name.lower()] + [{"name": name, "instructions": instructions.strip()[:600], "every": every, "hour_utc": hr}]
    return {"ok": f"added section '{name}' ({every}, about {hr}:00 UTC). It counts toward the daily post limit.", "sections": [s["name"] for s in st["sections"]]}

def t_remove_section(st, name, user_said):
    if not _said(user_said): return NOT_SAID
    st["sections"] = [s for s in st.get("sections", []) if s["name"].lower() != (name or "").lower()]
    return {"ok": "removed", "sections": [s["name"] for s in st["sections"]]}

def ensure_minimum(st):
    """Around the catch-up hour, if fewer than the minimum daily posts were drafted, pull in the best web leads."""
    d = _daily(st); need = int(cv("daily_min", DAILY_MIN)) - d["n"]
    if MUTE or now().hour < cv("catchup_hour_utc", 17) or d.get("min_done") or need <= 0: return
    d["min_done"] = True
    web_leads(st, force=True, limit=need, quiet=True)

# ---------------- agent ----------------
AGENT_SYS = """You are the user's AI-news editor for his personal X account. You only RECOMMEND; the user decides and posts himself.
Rules: use tools for every fact; never state a fact, number, or tweet that no tool returned. Only official-source items from get_recent_news count as confirmed news.
search_x and search_web results are LEADS (unverified): always label them as leads. Never draft until the user confirms a pick (e.g. 'draft 2'), unless he directly asks for a draft.
You can change the bot's own settings with save_preference, add/remove_watch_account, add/remove_source and remove_preference, but ONLY when the user directly asks in his latest message (copy his exact words into user_said), then confirm what you changed. Preferences can never override the safety rules (facts only from sources, no invented numbers, no auto-posting). You can also learn an X account's writing style (add_style_account) and manage Reddit sources (add/remove_subreddit), under the same rule. You can also change numeric settings (set_setting) and add recurring custom sections (add_section) the user describes in plain words. For anything the tools cannot do, say the user must ask Claude to add a new feature. Plain human tone, no hashtags. When asked what to post or try, include same-prompt model showdowns (one exact prompt, two models, screenshots side by side, short punchy caption) alongside news picks. Keep replies short enough for a phone screen. When recommending, rank the best 3 with one line of why each will land and a suggested angle, then end with: Reply 'draft 1/2/3' to continue."""
TOOLS = [{"function_declarations": [
 {"name": "get_recent_news", "description": "Verified news items the bot found on official sources (title, url, category, facts).",
  "parameters": {"type": "object", "properties": {"hours": {"type": "integer"}, "category": {"type": "string"}}}},
 {"name": "search_x", "description": "Search recent X posts on a topic (costs credits; use sparingly). Results are unverified LEADS, not confirmed news.",
  "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
 {"name": "search_web", "description": "Google search for background or to check a claim. Unverified leads only.",
  "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
 {"name": "read_page", "description": "Read the text of a web page URL.",
  "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
 {"name": "draft_post", "description": "Draft X post(s) from source URLs. Code verifies numbers and model names against the sources and reports anything unverified.",
  "parameters": {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}, "angle": {"type": "string"}}, "required": ["urls"]}},
 {"name": "save_preference", "description": "Save a standing preference about what/how to draft (tweet types, topics, tone, things to ignore). Only when the user explicitly asks in his latest message.", "parameters": {"type": "object", "properties": {"rule": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["rule","user_said"]}},
 {"name": "add_style_account", "description": "Learn the writing style (patterns only) of an X account and refresh it weekly. Only when the user asks.", "parameters": {"type": "object", "properties": {"handle": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["handle", "user_said"]}},
 {"name": "remove_style_account", "description": "Stop learning an X account's style. Only when the user asks.", "parameters": {"type": "object", "properties": {"handle": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["handle", "user_said"]}},
 {"name": "add_subreddit", "description": "Add a subreddit to the web research scan. Only when the user asks.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["name", "user_said"]}},
 {"name": "remove_subreddit", "description": "Remove a subreddit from the web research scan. Only when the user asks.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["name", "user_said"]}},
 {"name": "set_setting", "description": "Change a bot setting: daily_max, daily_min, digest_hours, scan_hours, web_hours, tz_offset, ideas_hour_utc, release_hour_utc, bundle_hour_utc, catchup_hour_utc, sleep_start_utc, sleep_end_utc. Only when the user asks.", "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["key","value","user_said"]}},
 {"name": "add_section", "description": "Add a custom recurring content section the user describes (name, instructions, every daily/weekly/monthly, hour_utc). Only when the user asks.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "instructions": {"type": "string"}, "every": {"type": "string"}, "hour_utc": {"type": "integer"}, "user_said": {"type": "string"}}, "required": ["name","instructions","every","user_said"]}},
 {"name": "remove_section", "description": "Remove a custom section by name. Only when the user asks.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["name","user_said"]}},
 {"name": "draft_repo_roundup", "description": "Draft a GitHub repo roundup post (numbered list of real repos) on a theme and send it to the user. Use when the user asks for one.", "parameters": {"type": "object", "properties": {"theme": {"type": "string"}}, "required": []}},
 {"name": "draft_article", "description": "Draft a detailed article on a topic and send it as a file. Use when the user asks for one.", "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": []}},
 {"name": "remove_style", "description": "Remove a saved writing style by number (1-based). Only when the user asks.", "parameters": {"type": "object", "properties": {"index": {"type": "integer"}, "user_said": {"type": "string"}}, "required": ["index", "user_said"]}},
 {"name": "list_settings", "description": "Show saved preferences, the X watchlist and the news sources.", "parameters": {"type": "object", "properties": {}, "required": []}},
 {"name": "remove_preference", "description": "Remove a saved preference by its number (1-based). Only when the user asks.", "parameters": {"type": "object", "properties": {"index": {"type": "integer"}, "user_said": {"type": "string"}}, "required": ["index","user_said"]}},
 {"name": "add_watch_account", "description": "Add an X account to the watchlist. Only when the user asks.", "parameters": {"type": "object", "properties": {"handle": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["handle","user_said"]}},
 {"name": "remove_watch_account", "description": "Remove an X account from the watchlist. Only when the user asks.", "parameters": {"type": "object", "properties": {"handle": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["handle","user_said"]}},
 {"name": "add_source", "description": "Add an official news source by URL (RSS or blog/news page). Only when the user asks.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "url": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["url","user_said"]}},
 {"name": "remove_source", "description": "Remove a news source by name. Only when the user asks.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "user_said": {"type": "string"}}, "required": ["name","user_said"]}},
 {"name": "quote_options", "description": "4 quote-tweet options for a post's text.",
  "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}]

def t_get_recent_news(st, hours=24, category=""):
    items = recent(st, int(hours or 24))
    if category: items = [x for x in items if category.lower() in x["category"].lower()]
    return items[-25:]

def t_search_x(st, query, limit=10):
    page = weir(st, "/v1/x/search", {"query": query, "language": "en", "limit": min(int(limit or 10), 15)}, cap=AGENT_CAP)
    return {"label": "LEADS: unverified X posts, not confirmed news",
            "posts": [{"text": p["text"], "likes": p["likes"], "url": p["url"]} for p in weir_posts(page)]}

def t_draft_post(urls, angle=""):
    texts = []
    for u in urls[:4]:
        try: texts.append((u, page_text(u)))
        except Exception as e: return {"error": f"could not read {u}: {e}"}
    source = " ".join(t for _, t in texts)
    srcs = "\n".join(f"- {u}\n  text: {t[:3500]}" for u, t in texts)
    prompt = f"{STYLE}\nAngle: {angle}\nWrite ONE X post (or short thread) from:\n{srcs}\nReturn JSON: {{\"tweets\": [strings]}}"
    tweets = gemini(prompt)["tweets"]; bad = verify(tweets, source)
    if bad:
        tweets = gemini(prompt + f"\nLast draft used values not in the source: {bad}. Rewrite without them.")["tweets"]; bad = verify(tweets, source)
    return {"tweets": tweets, "unverified": bad, "sources": urls}

def t_quote_options(text):
    return gemini(f"""Quote-tweet options in the user's voice for this X post: {text}
4 options: funny, informative add-on, context-first take then question, short reaction. No hashtags, <=280 chars, no invented facts.
Return JSON: {{"options": [{{"style": "", "text": ""}}]}}""")

CURRENT_MSG = ""
def _said(q): return bool(q) and q.strip().lower() in CURRENT_MSG.lower()
NOT_SAID = {"error": "Only change settings when the user explicitly asked in this message. Put his exact words in user_said."}

def t_save_pref(st, rule, user_said):
    if not _said(user_said): return NOT_SAID
    p = st.setdefault("prefs", []); rule = (rule or "").strip()[:300]
    if not rule: return {"error": "empty rule"}
    if rule in p: return {"ok": "already saved", "preferences": p}
    if len(p) >= 20: return {"error": "20 preferences max. Remove one first."}
    p.append(rule); return {"ok": "saved", "preferences": p}

def t_remove_style(st, index, user_said):
    if not _said(user_said): return NOT_SAID
    x = st.get("styles", [])
    if not 1 <= int(index) <= len(x): return {"error": "no such style number"}
    return {"ok": "removed", "removed": x.pop(int(index) - 1)["label"]}

def t_list_prefs(st):
    return {"styles": [a["label"] for a in st.get("styles", [])], "sections": [s["name"] for s in st.get("sections", [])], "settings": st.get("cfg", {}), "style_accounts": st.get("style_accounts", []), "subreddits": web_cfg(st)["subreddits"], "preferences": st.get("prefs", []), "watchlist": all_watch(st), "sources": [x["name"] for x in all_sources(st)]}

def t_remove_pref(st, index, user_said):
    if not _said(user_said): return NOT_SAID
    p = st.get("prefs", [])
    if not 1 <= int(index) <= len(p): return {"error": "no such preference number"}
    gone = p.pop(int(index) - 1); return {"ok": "removed", "removed": gone, "preferences": p}

def t_add_watch(st, handle, user_said):
    if not _said(user_said): return NOT_SAID
    h = (handle or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h): return {"error": "invalid X handle"}
    st["watch_removed"] = [x for x in st.get("watch_removed", []) if x.lower() != h.lower()]
    if h.lower() not in {x.lower() for x in all_watch(st)}: st.setdefault("watch_extra", []).append(h)
    return {"ok": "watching", "watchlist": all_watch(st)}

def t_remove_watch(st, handle, user_said):
    if not _said(user_said): return NOT_SAID
    h = (handle or "").strip().lstrip("@")
    st["watch_extra"] = [x for x in st.get("watch_extra", []) if x.lower() != h.lower()]
    st.setdefault("watch_removed", []).append(h)
    return {"ok": "removed", "watchlist": all_watch(st)}

def t_add_source(st, name, url, user_said):
    if not _said(user_said): return NOT_SAID
    if not (url or "").startswith("https://"): return {"error": "URL must start with https://"}
    name = (name or urlparse(url).netloc).strip()[:40]; src = None
    try:
        if feed_items(url): src = {"name": name, "type": "rss", "url": url}
    except Exception: pass
    if not src:
        try:
            r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
            feed = discover_feed({"url": url}, r.text)
            if feed and feed_items(feed): src = {"name": name, "type": "rss", "url": feed}
            else:
                prefix = urlparse(url).path or "/"
                if scrape_links({"url": url, "link_prefix": prefix}, r.text): src = {"name": name, "type": "page", "url": url, "link_prefix": prefix}
        except Exception: pass
    if not src: return {"error": "couldn't find any posts at that URL. Try the site's blog/news page or its RSS link."}
    st["sources_removed"] = [x for x in st.get("sources_removed", []) if x.lower() != name.lower()]
    st["sources_extra"] = [x for x in st.get("sources_extra", []) if x["name"].lower() != name.lower()] + [src]
    return {"ok": "added (existing posts are skipped on its first run)", "source": src}

def t_remove_source(st, name, user_said):
    if not _said(user_said): return NOT_SAID
    st["sources_extra"] = [x for x in st.get("sources_extra", []) if x["name"].lower() != (name or "").lower()]
    st.setdefault("sources_removed", []).append(name)
    return {"ok": "removed", "sources": [x["name"] for x in all_sources(st)]}

def run_tool(st, name, a):
    if name == "save_preference": return t_save_pref(st, a.get("rule"), a.get("user_said"))
    if name == "set_setting": return t_set_setting(st, a.get("key"), a.get("value"), a.get("user_said"))
    if name == "add_section": return t_add_section(st, a.get("name"), a.get("instructions"), a.get("every"), a.get("hour_utc"), a.get("user_said"))
    if name == "remove_section": return t_remove_section(st, a.get("name"), a.get("user_said"))
    if name == "draft_repo_roundup":
        repo_roundup(st, a.get("theme") or None, force=True); return {"ok": "roundup sent to the user"}
    if name == "draft_article":
        article_draft(st, a.get("topic") or None, force=True); return {"ok": "article sent to the user as a file"}
    if name == "add_style_account": return t_add_style_account(st, a.get("handle"), a.get("user_said"))
    if name == "remove_style_account": return t_remove_style_account(st, a.get("handle"), a.get("user_said"))
    if name == "add_subreddit": return t_subreddit(st, a.get("name"), a.get("user_said"), True)
    if name == "remove_subreddit": return t_subreddit(st, a.get("name"), a.get("user_said"), False)
    if name == "remove_style": return t_remove_style(st, a.get("index", 0), a.get("user_said"))
    if name == "list_settings": return t_list_prefs(st)
    if name == "remove_preference": return t_remove_pref(st, a.get("index", 0), a.get("user_said"))
    if name == "add_watch_account": return t_add_watch(st, a.get("handle"), a.get("user_said"))
    if name == "remove_watch_account": return t_remove_watch(st, a.get("handle"), a.get("user_said"))
    if name == "add_source": return t_add_source(st, a.get("name"), a.get("url"), a.get("user_said"))
    if name == "remove_source": return t_remove_source(st, a.get("name"), a.get("user_said"))
    if name == "get_recent_news": return t_get_recent_news(st, a.get("hours", 24), a.get("category", ""))
    if name == "search_x": return t_search_x(st, a["query"], a.get("limit", 10))
    if name == "search_web": return gemini_search(a["query"])[:5000]
    if name == "read_page": return page_text(a["url"])
    if name == "draft_post": return t_draft_post(a.get("urls", []), a.get("angle", ""))
    if name == "quote_options": return t_quote_options(a["text"])
    return {"error": "unknown tool"}

def trim(res):
    s = json.dumps(res, default=str)
    return json.loads(s) if len(s) <= 6000 else {"truncated": s[:6000]}

def agent_run(st, user_text):
    global CURRENT_MSG
    CURRENT_MSG = user_text
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    contents = [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in st.get("chat", [])[-10:]]
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    final = ""
    for _ in range(6):
        body = {"system_instruction": {"parts": [{"text": AGENT_SYS + prefs_extra(st) + styles_extra(st) + approved_extra(st)}]}, "contents": contents, "tools": TOOLS}
        r = gpost(url, json=body, timeout=120); r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not calls: final = "".join(p.get("text", "") for p in parts).strip(); break
        contents.append({"role": "model", "parts": parts}); resp = []
        for c in calls:
            try: res = run_tool(st, c["name"], c.get("args", {}))
            except Exception as e: res = {"error": str(e)[:300]}
            resp.append({"functionResponse": {"name": c["name"], "response": {"result": trim(res)}}})
        contents.append({"role": "user", "parts": resp})
    if not final: final = "I couldn't finish that. Try asking again in a simpler way."
    st["chat"] = (st.get("chat", []) + [{"role": "user", "text": user_text}, {"role": "model", "text": final}])[-12:]
    st["stats"]["chats"] += 1
    return final

def recommend(st):
    txt = agent_run(st, "Automatic review: use get_recent_news for the last 6 hours and recommend the 3 best items to post on my account. Do not draft yet.")
    tg("🧠 Top picks\n\n" + txt)

# ---------------- ideas, weekly, health ----------------
def ideas(st, force=False):
    n = now()
    if not force and (MUTE or n.hour != cv("ideas_hour_utc", IDEAS_HOUR_UTC) or st.get("last_ideas") == n.date().isoformat()): return
    st["last_ideas"] = n.date().isoformat()
    log = recent(st, 24)
    if not log:
        if force: tg("No verified news in the last 24h yet, so no ideas right now.", force=True)
        return
    trend = ""
    try:   # one cheap Weir search for trending comparison formats (about 10 credits/day)
        page = weir(st, "/v1/x/search", {"query": "same prompt comparison AI model vs", "minLikes": 200, "limit": 8, "language": "en"}, cap=SCAN_CAP)
        trend = "\n".join(p["text"][:220] for p in weir_posts(page))
    except Exception: pass
    out = gemini(f"""From today's verified AI news below, give ideas for an AI-focused X account. Only use these facts, invent no numbers or capabilities.
News: {json.dumps(log)[:6000]}
Return JSON: {{"try_this": [3 short project/content ideas (3D/animation demos, free tool tests, first-test posts)],
"takes": [3 tweet drafts that could spark replies: start with 1-2 sentences of real context from the news, THEN a question or opinion. Plain human tone, no hashtags]}}""")
    tg("💡 Daily ideas\n\nTRY THIS:\n" + "\n".join(f"{i+1}. {x}" for i, x in enumerate(out["try_this"])) +
       "\n\nTAKES TO POST:\n" + "\n\n".join(f"{i+1}) {x}" for i, x in enumerate(out["takes"])), force=force)

def health(st, reset=False, force=False):
    f = [k for k, v in st["fail"].items() if v >= 6]; c = st.get("credits", {}); s = st["stats"]
    tg(f"📊 Bot health\nDrafts sent: {s['drafts']} (major {s['majors']}) | quote options: {s['quotes']} | agent chats: {s['chats']}\n"
       f"Failing sources: {', '.join(f) or 'none'}\nWeir credits this month: {', '.join(f'key {i+1}: {u}/{WEIR_FREE}' for i, u in enumerate(c.get('used') or [0]))}\n"
       f"Status: {'paused' if st.get('paused') else 'running'}" + (f"\nNote: {st['scan_note']}" if st.get("scan_note") else ""), force=force)
    if reset: st["stats"] = {"drafts": 0, "majors": 0, "quotes": 0, "chats": 0}; st.pop("scan_note", None)

def weekly(st):
    n = now(); last = st.get("last_weekly")
    if MUTE or n.weekday() != WEEKLY_DAY or n.hour < WEEKLY_HOUR: return
    if last and n - dt.datetime.fromisoformat(last) < dt.timedelta(days=6): return
    st["last_weekly"] = n.isoformat()
    log = recent(st, 24 * 7)
    if log:
        src = json.dumps(log)
        out = gemini(f"""{STYLE}\nWrite a 'this week in AI' recap thread (3-5 tweets) from ONLY this verified list of the week's news:\n{src[:9000]}
Return JSON: {{"tweets": [strings]}}""")
        bad = verify(out["tweets"], src)
        tg("🗓 Weekly recap\n\n" + "\n\n".join(out["tweets"]) + (f"\n⚠️ Could not verify: {', '.join(bad)}" if bad else ""))
    health(st, reset=True)

# ---------------- video understanding ----------------
GBASE = "https://generativelanguage.googleapis.com"

def _video_json(prompt, file_part, key=None):
    body = {"contents": [{"parts": [file_part, {"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}}
    if key:      # uploaded file: must be read with the same key (project) that uploaded it
        r = None
        for m in dict.fromkeys(BEST + FAST):
            r = requests.post(f"{GBASE}/v1beta/models/{m}:generateContent", headers={"x-goog-api-key": key}, json=body, timeout=300)
            if r.status_code not in (429, 404): break
    else:
        r = gpost(f"{GBASE}/v1beta/models/{MODEL}:generateContent", json=body, timeout=300)
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])

def gemini_video(prompt, data=None, mime="video/mp4", url=None):
    """Analyse a video: either raw bytes (uploaded via the Gemini Files API) or a public YouTube URL."""
    if url: return _video_json(prompt, {"file_data": {"file_uri": url}})
    h = {"x-goog-api-key": GKEYS[GIDX]}   # uploaded files belong to this key's project, so use the same key to read them
    r = requests.post(GBASE + "/upload/v1beta/files", headers={**h, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                      "X-Goog-Upload-Header-Content-Length": str(len(data)), "X-Goog-Upload-Header-Content-Type": mime,
                      "Content-Type": "application/json"}, json={"file": {"display_name": "tg-video"}}, timeout=60)
    r.raise_for_status()
    r = requests.post(r.headers["x-goog-upload-url"], headers={"Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0",
                      "X-Goog-Upload-Command": "upload, finalize"}, data=data, timeout=600)
    r.raise_for_status(); f = r.json()["file"]
    try:
        for _ in range(60):
            if f.get("state") == "ACTIVE": break
            if f.get("state") == "FAILED": raise RuntimeError("Gemini could not process that video")
            time.sleep(5); f = requests.get(f"{GBASE}/v1beta/{f['name']}", headers=h, timeout=30).json()
        else: raise RuntimeError("video is still processing, try a shorter clip")
        return _video_json(prompt, {"file_data": {"mime_type": mime, "file_uri": f["uri"]}}, GKEYS[GIDX])
    finally:
        try: requests.delete(f"{GBASE}/v1beta/{f['name']}", headers=h, timeout=20)
        except Exception: pass

YT_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-]+[^\s]*")
FILE_RE = re.compile(r"https?://[^\s]+\.(?:mp4|mov|webm)(?:\?[^\s]*)?", re.I)

def analyze_video(st, note, data=None, mime="video/mp4", url=None):
    out = gemini_video(f"""You are watching a screen recording of X (Twitter) posts and/or an AI-related video. The user's note: {note or 'none'}.
Describe patterns ONLY. Do not reproduce anyone's tweets word for word.
Return JSON: {{"summary": "2-3 sentences on what the video shows", "style": "how the posts are written (tone, length, openers, emoji use, structure) in 1-2 sentences",
"tweet_types": [up to 6 short labels, e.g. 'model showdown with side-by-side screenshots', 'hands-on test with usage stats'],
"topics": [up to 8 AI topics/models/tools shown],
"handles": [X usernames you can clearly read on screen, without @, max 8, empty if none or unsure],
"formats_to_try": [3 post formats the user could copy the approach of]}}""", data, mime, url)
    vid = str(int(time.time() * 1000))[-9:]; vn = st.setdefault("video_notes", {}); vn[vid] = out
    for k in list(vn)[:-10]: vn.pop(k)
    hs = [h for h in map(str, out.get("handles", [])) if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h)][:8]; out["handles"] = hs
    rows = [[("💾 Save this style", "vs:" + vid), ("📝 Draft from it", "vd:" + vid)]]
    rows.append(([("👀 Watch these accounts", "vw:" + vid)] if hs else []) + [("🗑 Ignore", "vx:" + vid)])
    tg("🎬 What I saw\n\n" + str(out.get("summary", "")) + "\n\nStyle: " + str(out.get("style", "")) +
       "\n\nTweet types:\n- " + "\n- ".join(map(str, out.get("tweet_types", []))) + "\n\nTopics: " + ", ".join(map(str, out.get("topics", []))) +
       ("\n\nAccounts I could read: " + ", ".join("@" + h for h in hs) + " (check these, I can misread names)" if hs else "") +
       "\n\nWhat next?", force=True, buttons=rows)

def video_help(st, m):
    v = m.get("video") or m.get("video_note") or m.get("document") or {}
    if (v.get("file_size") or 0) > 20 * 1024 * 1024:
        tg("That video is over 20 MB and Telegram won't let me download it. Options: trim it to about 60-90 seconds, send it as a video (not as a file), "
           "upload it to YouTube (unlisted works) and send me the link, or send a direct link to an .mp4 file.", force=True); return
    tg("🎬 Got it. Watching the video now, this can take a minute or two.", force=True)
    fp = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile", params={"file_id": v["file_id"]}, timeout=30).json()["result"]["file_path"]
    data = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{fp}", timeout=120).content
    analyze_video(st, (m.get("caption") or "").strip(), data, v.get("mime_type") or "video/mp4")

def video_link_help(st, txt):
    note = re.sub(r"https?://\S+", "", txt).strip()
    y = YT_RE.search(txt)
    if y:
        tg("🎬 Watching that YouTube video, this can take a minute or two.", force=True); analyze_video(st, note, url=y.group(0)); return
    f = FILE_RE.search(txt)
    tg("🎬 Downloading and watching that video, this can take a few minutes.", force=True)
    r = requests.get(f.group(0), headers=UA, stream=True, timeout=60); r.raise_for_status()
    size = int(r.headers.get("content-length") or 0)
    if size > 150 * 1024 * 1024: tg("That file is over 150 MB. Send a shorter clip or a YouTube link.", force=True); return
    data = r.content
    if len(data) > 150 * 1024 * 1024: tg("That file is over 150 MB. Send a shorter clip or a YouTube link.", force=True); return
    analyze_video(st, note, data, r.headers.get("content-type", "video/mp4").split(";")[0] or "video/mp4")

def video_button(st, act, vid):
    n = st.get("video_notes", {}).get(vid)
    if not n: tg("That video analysis expired. Send the video again.", force=True); return
    if act == "vx": st["video_notes"].pop(vid, None); tg("🗑 Ignored.", force=True)
    elif act == "vw":
        added = []
        for h in n.get("handles", [])[:8]:
            if h.lower() not in {x.lower() for x in all_watch(st)}: st.setdefault("watch_extra", []).append(h); added.append(h)
            st["watch_removed"] = [x for x in st.get("watch_removed", []) if x.lower() != h.lower()]
        tg("👀 Now watching: " + (", ".join("@" + h for h in added) or "nothing new (already on the watchlist)") + ". Their strong recent posts will show up as leads with quote options.", force=True)
    elif act == "vs":
        x = st.setdefault("styles", [])
        x.append({"label": str(n.get("summary", "style"))[:40], "notes": (str(n.get("style", "")) + " Types: " + "; ".join(map(str, n.get("tweet_types", []))))[:350]})
        del x[:-5]; tg("💾 Style saved. I'll use it when drafting (patterns only, never copying wording). Say 'remove style 1' to delete one.", force=True)
    elif act == "vd":
        msg = (f"The user sent a video showing this style: {n.get('style')}. Tweet types: {n.get('tweet_types')}. Topics: {n.get('topics')}. "
               "Use get_recent_news (and search_x or search_web if news is thin) to find fresh info on these topics, then write 3 draft posts in that style. "
               "Use only facts from tools, label leads as leads, never copy anyone's wording.")
        tg("📝 Drafts in that style\n\n" + agent_run(st, msg), force=True)


# ---------------- comparison ideas (its own section) ----------------
CMP_KINDS = ["a code animation in one HTML file (canvas or Three.js)", "a 3D scene", "a UI/UX landing page design", "a small playable game", "a physics simulation",
             "a data visualization", "a text-to-video clip (for AI video models)", "an image or SVG illustration", "a coding challenge with a visible result"]

def comparison_ideas(st, force=False, kind=None):
    """Separate section: random same-prompt model showdown ideas the user can run, record side by side and post."""
    if not force and (MUTE or now().hour < cv("ideas_hour_utc", IDEAS_HOUR_UTC) or st.get("last_cmp") == day_key(st)): return
    st["last_cmp"] = day_key(st)
    models = sorted({(r.get("topic") or "") for r in st.get("releases", [])} - {""})[-8:]
    titles = [x["title"] for x in st.get("month_log", [])][-40:]
    trend = ""
    try:
        page = weir(st, "/v1/x/search", {"query": "same prompt comparison AI models side by side", "minLikes": 150, "limit": 6, "language": "en"}, cap=SCAN_CAP)
        trend = "\n".join(p["text"][:200] for p in weir_posts(page))
    except Exception: pass
    used = st.setdefault("cmp_used", [])
    o = gemini(f"""Invent 4 DIFFERENT, fresh ideas for a 'same prompt, several AI models' comparison that the user can run himself, record side by side (for example a 2x2 grid video or screenshots) and post.
Vary the kinds across: {', '.join(CMP_KINDS)}.{(' Focus on: ' + kind + '.') if kind else ''} At least one idea should be visual and eye-catching (animals, nature, space, cities, weather, motion).
Models: choose 2-4 models that were in the recent news below or are well-known flagships (Claude, GPT, Gemini, Grok, DeepSeek, Kimi). Recent model topics: {models}. Recent news titles: {titles}.
Examples of what already goes viral on X (inspiration only, do not copy): {trend[:1500] or 'none available'}
Do NOT repeat these earlier prompts: {used[-20:]}
Return JSON {{"ideas": [{{"title": "", "kind": "", "prompt": "the exact prompt to paste into every model", "models": ["..."],
"how": "how to run and record it in 1-2 sentences (e.g. ask each for one self-contained HTML file, open it full screen, screen-record, stitch into a grid)",
"why": "what difference people will notice / why it could spread", "caption": "a tweet caption template like: A vs B vs C.\\n\\n>[what surprised you]\\n>[who won on what]"}}]}}
Never claim what a model can or cannot do; the user will test it.""")
    ideas_ = o.get("ideas", [])[:4]
    if not ideas_:
        if force: tg("I couldn't come up with ideas right now, try again.", force=True)
        return
    for x in ideas_: used.append(str(x.get("prompt", ""))[:120])
    del used[:-40]
    tg("🆚 Comparison ideas (run them yourself, then post)\n\n" + "\n\n".join(
        f"{i+1}) {x.get('title', '')} ({x.get('kind', '')})\nModels: {', '.join(map(str, x.get('models', [])))}\nPrompt: {x.get('prompt', '')}\nHow: {x.get('how', '')}\nWhy it could spread: {x.get('why', '')}\nCaption: {x.get('caption', '')}"
        for i, x in enumerate(ideas_)), force=force)

# ---------------- rumor watch (its own section, always labelled unverified) ----------------
def rumor_watch(st, force=False):
    if not force and (MUTE or now().hour < cv("release_hour_utc", 6) + 1 or st.get("last_rumor") == day_key(st) or posts_left(st) <= 0): return
    st["last_rumor"] = day_key(st)
    try:
        txt = gemini_search("Using Google Search, find credible rumors or leaks (from reputable tech outlets or well-known leakers) about upcoming AI model releases or AI products expected in the next few weeks. "
                            "For each give what is rumored, who reported it and the source URL. These are rumors, say so.")
        out = gemini(f"""Extract JSON {{"rumors": [{{"what": "", "reported_by": "", "url": ""}}]}}. Only items whose URL is present in the text.\nTEXT:\n{txt[:6000]}""", tier="fast")
        seen = set(st.setdefault("seen_web", []))
        new = [r for r in out.get("rumors", []) if r.get("url") and r["url"] in txt and r["url"] not in seen][:3]
        if not new:
            if force: tg("No new credible rumors found right now.", force=True)
            return
        st["seen_web"].extend(r["url"] for r in new)
        d = gemini(f"""{STYLE}\nWrite ONE post about these RUMORS. Frame every rumor as unverified, e.g. 'Rumors say ..., though this is not verified or confirmed', credit who reported it, and never state a rumor as fact.
Use ONLY the information below.\nRUMORS: {json.dumps(new)}\nSOURCE: {txt[:5000]}\nReturn JSON {{"tweets": [strings]}}""")
        tw = [t if ("rumor" in t.lower() or "reportedly" in t.lower() or "unverified" in t.lower()) else "Unverified rumor: " + t for t in d["tweets"]]
        send_draft(st, "🕵️ Rumor watch (unverified, labelled in the post)", tw, "\n".join(r["url"] for r in new), verify(tw, txt), txt, "Rumor watch", force=force)
    except Exception as e:
        st["web_note"] = st.get("web_note", "") + f" | rumor watch failed: {str(e)[:50]}"
        if force: tg(f"Rumor watch failed: {str(e)[:120]}", force=True)

# ---------------- slash commands ----------------
BOT_COMMANDS = [("menu", "Open the menu"), ("news", "Latest verified AI news"), ("leads", "X leads with quote options"), ("web", "Web leads (HN, Reddit, GitHub...)"),
                ("compare", "Comparison ideas to run and post"), ("repo", "GitHub repo roundup post"), ("article", "Draft a detailed article"),
                ("releases", "Preview of officially announced launches"), ("rumors", "Rumor watch (labelled unverified)"), ("evening", "Evening release roundup"),
                ("ideas", "Daily ideas and takes"), ("quote", "How to get quote-tweet options"), ("styles", "My saved writing styles"),
                ("settings", "My settings"), ("status", "Bot health and credits"), ("pause", "Pause messages"), ("resume", "Resume messages")]
CMD_MAP = {"/news": "news", "/leads": "leads", "/web": "web", "/releases": "preview", "/rumors": "rumors", "/evening": "bundle", "/ideas": "ideas",
           "/quote": "quote", "/styles": "styles", "/learn": "learnhow"}

def register_commands(st):
    if st.get("cmds_v") == 2: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/setMyCommands", json={"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]}, timeout=20)
        st["cmds_v"] = 2
    except Exception: pass

def command(st, txt):
    parts = txt.split(maxsplit=1); cmd = parts[0].lower().split("@")[0]; arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/pause": st["paused"] = True; tg("⏸ Paused. I'll keep collecting news and hold messages. /resume to continue.", force=True)
    elif cmd == "/resume": st["paused"] = False; tg("▶️ Resumed. Held messages will arrive shortly.", force=True)
    elif cmd == "/status": health(st, force=True)
    elif cmd in ("/settings", "/prefs"): show_settings(st)
    elif cmd == "/repo": repo_roundup(st, arg or None, force=True)
    elif cmd == "/article": article_draft(st, arg or None, force=True)
    elif cmd == "/compare": comparison_ideas(st, force=True, kind=arg or None)
    elif cmd in CMD_MAP: menu_action(st, CMD_MAP[cmd])
    else: show_menu()

# ---------------- Telegram inbox: commands, buttons, chat ----------------
def in_sleep():
    """Sleep window (UTC hours). Default 23-06 UTC = 12am-7am Nigeria time (UTC+1). Set SLEEP_START_UTC / SLEEP_END_UTC to change; both 'off' disables."""
    a, b = str(cv("sleep_start_utc", os.environ.get("SLEEP_START_UTC") or "23")), str(cv("sleep_end_utc", os.environ.get("SLEEP_END_UTC") or "6"))
    if not (a.isdigit() and b.isdigit()): return False
    h, s0, e = now().hour, int(a), int(b)
    return (s0 <= h < e) if s0 < e else (h >= s0 or h < e)

def mute_state(st):
    if st.get("paused"): return "pause"
    qs, qe = os.environ.get("QUIET_START_UTC", ""), os.environ.get("QUIET_END_UTC", "")
    if qs.isdigit() and qe.isdigit():
        h, s, e = now().hour, int(qs), int(qe)
        if (s <= h < e) if s < e else (h >= s or h < e): return "quiet"
    return ""

MENUS = {
 "home": [[("📰 News", "m:sub_news"), ("🔎 Leads", "m:sub_leads")],
          [("🆚 Comparison ideas", "m:compare"), ("📦 Repo roundup", "m:repo")],
          [("📝 Article", "m:article"), ("🗓 Releases & rumors", "m:sub_rel")],
          [("💡 Daily ideas", "m:ideas"), ("🎬 Learn a style", "m:sub_learn")],
          [("⚙️ Settings & control", "m:sub_ctl")]],
 "sub_news": [[("📰 All latest", "m:news"), ("🧠 Top picks", "m:picks")],
              [("Claude/Opus", "m:cat0"), ("Creative AI", "m:cat1"), ("AI video", "m:cat2")],
              [("Industry", "m:cat3"), ("Resources", "m:cat4"), ("Agents", "m:cat5")],
              [("Models & benchmarks", "m:cat6"), ("⬅️ Back", "m:home")]],
 "sub_leads": [[("🔎 X leads now", "m:leads"), ("🌐 Web leads now", "m:web")], [("💬 Quote help", "m:quote"), ("⬅️ Back", "m:home")]],
 "sub_rel": [[("🗓 Preview now", "m:preview"), ("🕵️ Rumor watch", "m:rumors")], [("🌆 Evening roundup", "m:bundle"), ("⬅️ Back", "m:home")]],
 "sub_learn": [[("How to teach me", "m:learnhow"), ("My saved styles", "m:styles")], [("⬅️ Back", "m:home")]],
 "sub_ctl": [[("⚙️ My settings", "m:settings"), ("📊 Status", "m:status")],
             [("⏸ Pause", "m:pause"), ("▶️ Resume", "m:resume")], [("⬅️ Back", "m:home")]]}

def show_settings(st):
    p = st.get("prefs", []); sy = st.get("styles", [])
    tg("⚙️ My settings\n\nPreferences:\n" + ("\n".join(f"{i+1}. {x}" for i, x in enumerate(p)) or "(none yet)") +
       "\n\nSaved styles:\n" + ("\n".join(f"{i+1}. {x['label']}" for i, x in enumerate(sy)) or "(none yet)") +
       "\n\nLearning style from: " + (", ".join("@" + h for h in st.get("style_accounts", [])) or "(none)") +
       "\n\nWatching: " + ", ".join("@" + h for h in all_watch(st)) +
       "\n\nReddit: " + ", ".join("r/" + x for x in web_cfg(st)["subreddits"]) +
       "\n\nCustom sections: " + (", ".join(f"{x['name']} ({x['every']})" for x in st.get("sections", [])) or "(none)") +
       "\n\nSettings: daily posts " + str(cv("daily_min", DAILY_MIN)) + "-" + str(cv("daily_max", DAILY_MAX)) + ", changed: " + (str(st.get("cfg")) or "none") +
       "\n\nSources: " + ", ".join(x["name"] for x in all_sources(st)) +
       "\n\nTo change anything, just tell me. Examples: 'from now on write threads for AI video news', 'add @handle to my watchlist', 'remove preference 2', 'remove style 1'.", force=True)

def show_menu(name="home"):
    tg({"home": "What do you want to do? You can also just type to chat with me."}.get(name, "Choose:"), force=True, buttons=MENUS[name])

def menu_action(st, what):
    if what == "home" or what.startswith("sub_"): show_menu(what)
    elif what == "news": tg(agent_run(st, "Give me a short summary of the most important verified AI news from the last 12 hours, grouped by category."), force=True)
    elif what.startswith("cat"):
        cat = CATEGORIES[int(what[3:])]
        tg(f"📰 {cat}\n\n" + agent_run(st, f"Using get_recent_news, summarise the verified news in the category '{cat}' from the last 24 hours. If there is none, say so."), force=True)
    elif what == "picks": tg("🧠 Top picks\n\n" + agent_run(st, "Use get_recent_news for the last 12 hours and recommend the 3 best items to post. Do not draft yet."), force=True)
    elif what == "ideas": ideas(st, force=True)
    elif what == "leads": scan_x(st, force=True)
    elif what == "web": web_leads(st, force=True)
    elif what == "repo": repo_roundup(st, force=True)
    elif what == "compare": comparison_ideas(st, force=True)
    elif what == "preview": release_watch(st, force=True)
    elif what == "rumors": rumor_watch(st, force=True)
    elif what == "bundle": release_bundle(st, force=True)
    elif what == "article": article_draft(st, force=True)
    elif what == "quote": tg("Send me a tweet link, the tweet text, or a screenshot (add your angle if you have one) and I'll write 4 quote-tweet options.", force=True)
    elif what == "learnhow": tg("Send me a screen recording (under 20 MB, about 60-90 seconds works best) of the tweets or videos you want me to learn from. Add a caption if you want, like 'this is how he tweets about model launches'. I'll describe the style and topics, then you can save the style or have me draft from it.", force=True)
    elif what == "styles":
        sy = st.get("styles", [])
        tg("💾 Saved styles\n\n" + ("\n".join(f"{i+1}. {x['label']}: {x['notes']}" for i, x in enumerate(sy)) or "(none yet, send me a video)"), force=True)
    elif what == "settings": show_settings(st)
    elif what == "status": health(st, force=True)
    elif what == "pause": st["paused"] = True; tg("⏸ Paused. /resume to continue.", force=True)
    elif what == "resume": st["paused"] = False; tg("▶️ Resumed.", force=True)

def on_button(st, q):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery", data={"callback_query_id": q["id"]}, timeout=15)
    act, _, did = (q.get("data") or "").partition(":"); d = st.get("drafts", {}).get(did)
    if act == "m": menu_action(st, did); return
    if act in ("vs", "vd", "vx", "vw"): video_button(st, act, did); return
    if act == "ok" and d:
        st.setdefault("approved", []).append("\n".join(d["tweets"])); st["approved"] = st["approved"][-8:]
        tg("✅ Saved as a tone example. Post it from X when ready.", force=True)
    elif act == "ed" and d:
        st["editing"] = did; tg("What should I change? Reply with your instruction.", force=True)
    elif act == "sk":
        st.get("drafts", {}).pop(did, None); tg("⏭ Skipped.", force=True)
    else: tg("That draft has expired.", force=True)

def inbox(st):
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params={"offset": st.get("tg_offset", 0), "timeout": 0}, timeout=30).json()
    for u in r.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        q = u.get("callback_query"); m = u.get("message") or (q or {}).get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TG_CHAT): continue
        try:
            if q: on_button(st, q); continue
            txt = (m.get("text") or m.get("caption") or "").strip()
            if txt.startswith("/"): command(st, txt)
            elif m.get("video") or m.get("video_note") or (m.get("document") or {}).get("mime_type", "").startswith("video/"): video_help(st, m)
            elif YT_RE.search(txt) or FILE_RE.search(txt): video_link_help(st, txt)
            elif m.get("photo") or "status/" in txt: quote_help(st, m)
            elif st.get("editing") and txt: edit_draft(st, txt)
            elif txt: tg(agent_run(st, txt), force=True)
        except Exception as e: tg(f"⚠️ Couldn't handle that message: {str(e)[:200]}", force=True)

def safe(st, name, fn, *a):
    """Run one scheduled job; on failure note it and alert once per job per day instead of crashing the whole run."""
    try: return fn(*a)
    except Exception as e:
        st["web_note"] = st.get("web_note", "") + f" | {name} failed: {str(e)[:60]}"
        je = st.setdefault("job_err", {})
        if je.get(name) != day_key(st):
            je[name] = day_key(st); tg(f"⚠️ {name} failed today: {str(e)[:150]}")

def main():
    global MUTE, STYLE, HELD, COOL, CFGV
    st = load()
    for k, v in {"seen": [], "init": [], "buffer": [], "week_log": [], "fail": {}, "feeds": {}, "drafts": {},
                 "approved": [], "held": [], "style_accounts": ["Octoberry"], "stats": {"drafts": 0, "majors": 0, "quotes": 0, "chats": 0}}.items(): st.setdefault(k, v)
    CFGV = st.setdefault("cfg", {})
    COOL.update({k: v for k, v in st.setdefault("cool", {}).items() if v > now().isoformat()}); st["cool"] = COOL
    register_commands(st)
    HELD = st["held"]; STYLE = BASE_STYLE + prefs_extra(st) + styles_extra(st) + approved_extra(st); MUTE = mute_state(st)
    try:
        inbox(st); MUTE = mute_state(st)          # /resume or button presses may change state
        if in_sleep():
            check(st)     # sleep window: only official-source checks, so major news still comes through at any hour; no batches, scans or searches
        else:
            check(st)
            if digest(st): safe(st, "top picks", recommend, st)
            for name, fn in (("release preview", release_watch), ("rumor watch", rumor_watch), ("X leads", scan_x), ("web leads", web_leads), ("repo roundup", repo_weekly),
                             ("minimum posts", ensure_minimum), ("custom sections", run_sections), ("style learning", ensure_styles), ("daily ideas", ideas),
                             ("comparison ideas", comparison_ideas), ("weekly recap", weekly), ("monthly article", article_monthly), ("evening roundup", release_bundle)):
                safe(st, name, fn, st)       # one failing job never stops the others
        if not MUTE and HELD:
            held = list(HELD); HELD.clear()
            for h in held[:15]: tg(h["text"], buttons=h.get("buttons"))
            if len(held) > 15: tg(f"…and {len(held) - 15} older held messages were dropped.")
    except Exception:
        tg("⚠️ Bot crashed:\n" + traceback.format_exc()[-600:], force=True)
    mcut = (now() - dt.timedelta(days=31)).isoformat(); st["month_log"] = [x for x in st.get("month_log", []) if x["t"] >= mcut]
    st["releases"] = st.get("releases", [])[-30:]
    cut = now() - dt.timedelta(days=8)
    st["week_log"] = [x for x in st["week_log"] if dt.datetime.fromisoformat(x["t"]) >= cut]
    st["seen"] = st["seen"][-3000:]
    save(st)

if __name__ == "__main__":
    main()
