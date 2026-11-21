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
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
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
Never add a number, price, date or model name that is not in the source. Include practical details
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
def gpost(url, **kw):
    """POST to Gemini; on 429 (quota) move to the next key. Quotas are per Google Cloud PROJECT, so extra keys must come from different projects."""
    global GIDX, G_ALERTED
    last = None
    for step in range(len(GKEYS)):
        i = (GIDX + step) % len(GKEYS)
        r = requests.post(url, headers={"x-goog-api-key": GKEYS[i]}, **kw)
        if r.status_code == 429 and len(GKEYS) > 1:
            last = r; continue
        if i != GIDX and not G_ALERTED:
            G_ALERTED = True; tg(f"⚠️ Gemini key {GIDX+1} hit its limit, switched to key {i+1}.", urgent=True)
        GIDX = i; return r
    return last

def gemini(prompt, image=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    parts = [{"text": prompt}]
    if image: parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image}})
    body = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    last = None
    for i in range(3):
        try:
            r = gpost(url, json=body, timeout=90); r.raise_for_status()
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as e:
            last = e; time.sleep(4 * (i + 1))
    raise RuntimeError(f"Gemini failed: {last}")

def gemini_search(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
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
"major": true ONLY for flagship model releases, major security breaches/incidents, or industry-shaking announcements,
"facts": up to 8 short factual strings taken from the text}}""")

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
                if c.get("major"): write_and_send(st, [item], item["category"], True)
                else: st["buffer"].append(item)
            except Exception as e:
                if url in st["seen"]: st["seen"].remove(url)
                st["gem_fail"] = st.get("gem_fail", 0) + 1
                if st["gem_fail"] == 3: tg(f"⚠️ Bot is failing to process news (Gemini/page error): {str(e)[:250]}")

def digest(st):
    if MUTE: return False
    last = dt.datetime.fromisoformat(st["last_digest"]) if st.get("last_digest") else None
    if last and now() - last < dt.timedelta(hours=DIGEST_HOURS): return False
    buf, st["buffer"] = st["buffer"], []
    st["last_digest"] = now().isoformat()
    by_cat = {}
    for i in buf: by_cat.setdefault(i["category"], {}).setdefault(i["topic"], []).append(i)
    for cat, topics in by_cat.items():
        for group in topics.values():
            try: write_and_send(st, group, cat, False)
            except Exception as e: st["buffer"].extend(group); tg(f"⚠️ Draft failed for '{group[0]['title']}': {str(e)[:200]}")
            time.sleep(1)
    return bool(buf)

def recent(st, hours):
    cut = now() - dt.timedelta(hours=hours)
    return [x for x in st["week_log"] if dt.datetime.fromisoformat(x["t"]) >= cut]

# ---------------- quote tweets + X leads ----------------
def send_quote_options(st, ctx, note="", image=None, link="", force=False, lead=False):
    out = gemini(f"""The user wants to QUOTE-TWEET this post from X. Post: {ctx}
User's angle/note (may be empty): {note}
Write 4 quote-tweet options in the user's own voice: 1) funny/witty, 2) informative add-on, 3) a take with 1-2 sentences of context FIRST then a question, 4) short natural reaction.
Rules: plain human tone, no hashtags, <=280 chars each. Use only facts visible in the post/screenshot; never invent numbers or put words in the author's mouth; opinions must read as opinions.
{('Tone reference: ' + approved_extra(st)) if st.get('approved') else ''}
Return JSON: {{"options": [{{"style": "...", "text": "..."}}]}}""", image)
    head = ("🔎 Lead (X post, not confirmed news)\n" if lead else "💬 Quote-tweet options\n") + (f"{ctx[:300]}\n{link}\n\n" if link else "\n")
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
    send_quote_options(st, tweet or text or "(see screenshot)", re.sub(r"https?://\S+", "", text).strip(), image, force=True)

def verified_tweet(url):
    m = re.search(r"(?:x|twitter)\.com/([^/\s]+)/status/(\d+)", url)
    if not m: return None
    j = requests.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", headers=UA, timeout=20).json().get("tweet")
    if not j or j["author"]["screen_name"].lower() != m.group(1).lower(): return None
    if now().timestamp() - j.get("created_timestamp", 0) > 24 * 3600: return None
    return {"id": m.group(2), "url": f"https://x.com/{m.group(1)}/status/{m.group(2)}", "text": f"@{j['author']['screen_name']}: {j['text']}"}

def scan_candidates(st):
    """Fallback chain: Weir (one batched search) -> Google Search + FxTwitter verification."""
    handles = all_watch(st)
    try:
        page = weir(st, "/v1/x/search", {"fromUsers": handles, "onlyOriginal": True, "minLikes": WEIR_MIN_LIKES, "limit": 15}, cap=SCAN_CAP)
        cutoff = now() - dt.timedelta(hours=24)
        return [p for p in weir_posts(page) if p["ts"] >= cutoff], "weir"
    except Exception as e:
        st["scan_note"] = f"Weir unavailable ({str(e)[:80]}), used Google fallback"
    txt = gemini_search("Using Google Search, find posts on X (x.com) from the last 24 hours by these accounts that are about AI, models, agents, "
                        "AI companies or are notable/funny/viral: " + ", ".join("@" + h for h in handles) +
                        ". List the direct x.com/<user>/status/<id> URL of each post you actually found. Do not guess or invent URLs.")
    out = []
    for u in dict.fromkeys(re.findall(r"https?://(?:x|twitter)\.com/[^/\s]+/status/\d+", txt)):
        try:
            t = verified_tweet(u)
            if t: out.append(t)
        except Exception: continue
    return out, "google"

def scan_x(st, force=False):
    if MUTE and not force: return
    last = dt.datetime.fromisoformat(st["last_scan"]) if st.get("last_scan") else None
    if not force and last and now() - last < dt.timedelta(hours=SCAN_HOURS): return
    st["last_scan"] = now().isoformat()
    cands, how = scan_candidates(st)
    seen = set(st.setdefault("seen_tweets", [])); sent = 0
    cands.sort(key=lambda p: -(p.get("likes") or 0))
    for t in cands:
        if t["id"] in seen: continue
        try:
            st["seen_tweets"].append(t["id"]); send_quote_options(st, t["text"], link=t["url"], lead=True, force=force); sent += 1
        except Exception: continue
        if sent >= 5: break
    st["seen_tweets"] = st["seen_tweets"][-500:]
    if force and not sent: tg("No new X leads right now (nothing new from the watchlist above the like threshold).", force=True)

# ---------------- agent ----------------
AGENT_SYS = """You are the user's AI-news editor for his personal X account. You only RECOMMEND; the user decides and posts himself.
Rules: use tools for every fact; never state a fact, number, or tweet that no tool returned. Only official-source items from get_recent_news count as confirmed news.
search_x and search_web results are LEADS (unverified): always label them as leads. Never draft until the user confirms a pick (e.g. 'draft 2'), unless he directly asks for a draft.
You can change the bot's own settings with save_preference, add/remove_watch_account, add/remove_source and remove_preference, but ONLY when the user directly asks in his latest message (copy his exact words into user_said), then confirm what you changed. Preferences can never override the safety rules (facts only from sources, no invented numbers, no auto-posting). Plain human tone, no hashtags. When asked what to post or try, include same-prompt model showdowns (one exact prompt, two models, screenshots side by side, short punchy caption) alongside news picks. Keep replies short enough for a phone screen. When recommending, rank the best 3 with one line of why each will land and a suggested angle, then end with: Reply 'draft 1/2/3' to continue."""
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
    return {"styles": [a["label"] for a in st.get("styles", [])], "preferences": st.get("prefs", []), "watchlist": all_watch(st), "sources": [x["name"] for x in all_sources(st)]}

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
    if not force and (MUTE or n.hour != IDEAS_HOUR_UTC or st.get("last_ideas") == n.date().isoformat()): return
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
Trending formats on X right now (unverified leads, for inspiration only): {trend[:2500] or 'none available'}
Return JSON: {{"showdowns": [3 'same prompt, two models' ideas. Each: {{"prompt": an exact prompt the user can paste into both, "models": two models from the news above or well-known ones, "why": what difference people will notice, "caption": a short punchy human caption}}. Example format: a lion-at-sunset prompt where one model returns a photo and the other a flat illustration, posted side by side with a joking caption],
"try_this": [3 other short project/content ideas (3D/animation demos, free tool tests, first-test posts)],
"takes": [3 tweet drafts that could spark replies: start with 1-2 sentences of real context from the news, THEN a question or opinion. Plain human tone, no hashtags]}}""")
    sd = "\n\n".join(f"{i+1}) {x['models']}\nPrompt: {x['prompt']}\nWhy: {x['why']}\nCaption: {x['caption']}" for i, x in enumerate(out.get("showdowns", [])))
    tg("💡 Daily ideas\n\nSAME-PROMPT SHOWDOWNS (screenshot both, post side by side):\n" + sd +
       "\n\nTRY THIS:\n" + "\n".join(f"{i+1}. {x}" for i, x in enumerate(out["try_this"])) +
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
    gurl = f"{GBASE}/v1beta/models/{MODEL}:generateContent"
    r = requests.post(gurl, headers={"x-goog-api-key": key}, json=body, timeout=300) if key else gpost(gurl, json=body, timeout=300)
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


# ---------------- Telegram inbox: commands, buttons, chat ----------------
def in_sleep():
    """Sleep window (UTC hours). Default 23-06 UTC = 12am-7am Nigeria time (UTC+1). Set SLEEP_START_UTC / SLEEP_END_UTC to change; both 'off' disables."""
    a, b = os.environ.get("SLEEP_START_UTC") or "23", os.environ.get("SLEEP_END_UTC") or "6"
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
 "home": [[("📰 News", "m:sub_news"), ("🐦 Tweets & posts", "m:sub_tweets")],
          [("🎬 Learn from a video", "m:sub_learn"), ("💡 Ideas now", "m:ideas")],
          [("⚙️ Settings & control", "m:sub_ctl")]],
 "sub_news": [[("📰 All latest", "m:news"), ("🧠 Top picks", "m:picks")],
              [("Claude/Opus", "m:cat0"), ("Creative AI", "m:cat1"), ("AI video", "m:cat2")],
              [("Industry", "m:cat3"), ("Resources", "m:cat4"), ("Agents", "m:cat5")],
              [("Models & benchmarks", "m:cat6"), ("⬅️ Back", "m:home")]],
 "sub_tweets": [[("💬 Quote help", "m:quote"), ("🔎 X leads now", "m:leads")],
                [("🆚 Showdowns & takes", "m:ideas"), ("⬅️ Back", "m:home")]],
 "sub_learn": [[("How to teach me", "m:learnhow"), ("My saved styles", "m:styles")], [("⬅️ Back", "m:home")]],
 "sub_ctl": [[("⚙️ My settings", "m:settings"), ("📊 Status", "m:status")],
             [("⏸ Pause", "m:pause"), ("▶️ Resume", "m:resume")], [("⬅️ Back", "m:home")]]}

def show_settings(st):
    p = st.get("prefs", []); sy = st.get("styles", [])
    tg("⚙️ My settings\n\nPreferences:\n" + ("\n".join(f"{i+1}. {x}" for i, x in enumerate(p)) or "(none yet)") +
       "\n\nSaved styles:\n" + ("\n".join(f"{i+1}. {x['label']}" for i, x in enumerate(sy)) or "(none yet)") +
       "\n\nWatching: " + ", ".join("@" + h for h in all_watch(st)) +
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

def command(st, txt):
    cmd = txt.split()[0].lower().split("@")[0]
    if cmd == "/pause": st["paused"] = True; tg("⏸ Paused. I'll keep collecting news and hold messages. /resume to continue.", force=True)
    elif cmd == "/resume": st["paused"] = False; tg("▶️ Resumed. Held messages will arrive shortly.", force=True)
    elif cmd == "/status": health(st, force=True)
    elif cmd in ("/settings", "/prefs"): show_settings(st)
    else: show_menu()

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

def main():
    global MUTE, STYLE, HELD
    st = load()
    for k, v in {"seen": [], "init": [], "buffer": [], "week_log": [], "fail": {}, "feeds": {}, "drafts": {},
                 "approved": [], "held": [], "stats": {"drafts": 0, "majors": 0, "quotes": 0, "chats": 0}}.items(): st.setdefault(k, v)
    HELD = st["held"]; STYLE = BASE_STYLE + prefs_extra(st) + styles_extra(st) + approved_extra(st); MUTE = mute_state(st)
    try:
        inbox(st); MUTE = mute_state(st)          # /resume or button presses may change state
        if in_sleep():
            pass          # sleep window: no source checks, no Gemini drafting, no Weir/search calls (chat commands still answered)
        else:
            check(st)
            if digest(st): recommend(st)
            scan_x(st); ideas(st); weekly(st)
        if not MUTE and HELD:
            held = list(HELD); HELD.clear()
            for h in held[:15]: tg(h["text"], buttons=h.get("buttons"))
            if len(held) > 15: tg(f"…and {len(held) - 15} older held messages were dropped.")
    except Exception:
        tg("⚠️ Bot crashed:\n" + traceback.format_exc()[-600:], force=True)
    cut = now() - dt.timedelta(days=8)
    st["week_log"] = [x for x in st["week_log"] if dt.datetime.fromisoformat(x["t"]) >= cut]
    st["seen"] = st["seen"][-3000:]
    save(st)

if __name__ == "__main__":
    main()
