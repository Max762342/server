"""
Sychos Hub — Oracle Server v5.0
Zentraler Server: Login & Register (50 Start-Credits), Credit-System (2-4 dynamisch),
KI-Chat, Chat-Verwaltung (inkl. Umbenennen), Admin-API (Online-User, Credits vergeben).
Serviert zudem das Frontend:  /        -> Sychos Hub   /admin/  -> Admin-Panel
Nur Python-Stdlib. Start:  python oracle_server.py
"""
import os, sys, json, time, uuid, threading, mimetypes, zlib, base64
import sqlite3, urllib.request, urllib.parse, hashlib, hmac, re, math
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer as HTTPServerCls
except ImportError:
    HTTPServerCls = HTTPServer
from urllib.parse import urlparse, unquote

# pythonw (versteckter Always-On-Modus) hat kein stdout/stderr -> auf devnull
if getattr(sys, "stdout", None) is None:
    sys.stdout = open(os.devnull, "w")
if getattr(sys, "stderr", None) is None:
    sys.stderr = open(os.devnull, "w")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get("ORACLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORACLE_PORT", "7777"))
# Offizielle Sychos-Domains (Zusatz-Domains) + kanonische Hauptdomain
SYCHOS_DOMAINS = ("sychos.com", "sychos.de", "sychos.global")
SYCHOS_HOME = "https://sychos.com"
DB_PATH = os.environ.get("ORACLE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sychos.db"))
# Provider-Keys: fest im Server hinterlegt (kein manuelles Eintragen noetig).
# Env GEMINI_API_KEY / GROQ_API_KEY / CLINE_API_KEY koennen sie bei Bedarf ueberschreiben.
# NIEMALS im Frontend, in Logs oder Fehlermeldungen ausgeben.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JC_fpFIoudZSPJyi0oLHbEpFZXt-PdJPBDobUt76TtOQ")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
CLINE_API_KEY = os.environ.get("CLINE_API_KEY", "sk_44b7a08f5bbad92391cf22535ff37373db50457d90979e1df3e5d992fb835381")
# Echtes Zahlen: Stripe Secret Key (leer = Test-Modus, keine echte Abbuchung)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

# ── Merchant of Record (Paddle / Lemon Squeezy): der Anbieter ist der offizielle
#    Verkaeufer und fuehrt ALLE Steuern weltweit ab (USt / VAT / Sales-Tax). ──
def _kv(s):
    return dict(p.split("=", 1) for p in (s or "").split(",") if "=" in p)

PADDLE_CLIENT_TOKEN = os.environ.get("PADDLE_CLIENT_TOKEN", "live_c3597782b58fb3e865fe78fd68e")
PADDLE_WEBHOOK_SECRET = os.environ.get("PADDLE_WEBHOOK_SECRET",
    "pdl_ntfset_01m39q5bhx7fhebrevjn9w1py4_tF6BDSRxpcITbS3jbdrdDll2/DLxKOzG")
PADDLE_PRICES = _kv(os.environ.get("PADDLE_PRICES",
    "basic=pri_01m3818vm4r281eddpyg4a09g1,"
    "pro=pri_01m381a3nb4yfs4vqy4q7c77t9,"
    "max=pri_01m3817vz42r9xn2azg3vc5hkw,"
    "business=pri_01m3816q322wk6jve5jx5vrg9j,"
    "test=pri_01m381373a8gpxz6b2sdxnagxx"))                    # plan=pri_xxx
LEMONSQUEEZY_STORE = os.environ.get("LEMONSQUEEZY_STORE", "")               # Shop-Subdomain
LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")
LEMONSQUEEZY_VARIANTS = _kv(os.environ.get("LEMONSQUEEZY_VARIANTS", ""))    # plan=variant_id
PADDLE_LINKS = _kv(os.environ.get("PADDLE_LINKS", ""))    # plan=Payment-Link-URL (direkt zu Paddle)

START_CREDITS = 50.0
START_TOKENS = 25000.0   # Start-Bonus-Tokens fuer neue Accounts
# Passive Gutschrift: alle 7 Stunden bekommen alle User +25 Credits
CREDIT_REGEN_SECONDS = 7 * 3600
CREDIT_REGEN_AMOUNT = 25.0
ONLINE_WINDOW = 300
MAX_MSG_LEN = 8000
MAX_TITLE_LEN = 80
SEND_RATE_LIMIT = 20
LOGIN_RATE_LIMIT = 120     # Login-Versuche pro Minute pro IP (grosszuegig – kein "kann nicht mehr anmelden")
REGISTER_RATE_LIMIT = 12   # Registrierungen pro Minute pro IP (eigener Bucket!)
PWFAIL_LIMIT = 8           # echte Fehlversuche pro Konto ...
PWFAIL_WINDOW = 600        # ... innerhalb 10 Minuten -> dann kurz gesperrt
MAX_ACCOUNTS_PER_IP = int(os.environ.get("MAX_ACCOUNTS_PER_IP", "10"))

# Zentrale Credit-Preise je KI-Stufe (serverseitig verbindlich)
STRENGTHS = {
    "low":    {"name": "Low",    "max_tokens": 512,  "temp": 0.3, "cost": 1},
    "medium": {"name": "Medium", "max_tokens": 2048, "temp": 0.7, "cost": 2},
    "high":   {"name": "High",   "max_tokens": 4096, "temp": 1.0, "cost": 4},
    "extra":  {"name": "Extra",  "max_tokens": 8192, "temp": 1.3, "cost": 6},
}

# KI-Modelle: Anbieter + Kostenfaktor (Preis = Stufen-Cost x Faktor)
MODELS = {
    "gemini-3.6-flash": {"name": "Gemini 3.6 Flash", "provider": "gemini", "factor": 1.0, "vision": True},
    "gemini-2.5-flash": {"name": "Gemini 2.5 Flash", "provider": "gemini", "factor": 1.0, "vision": True},
    "gemini-2.5-lite":  {"name": "Gemini 2.5 Lite (schnell)", "provider": "gemini", "factor": 0.6, "vision": True},
    "gemini-3.6-pro":   {"name": "Gemini 3.6 Pro", "provider": "gemini", "factor": 1.5, "paid": True, "vision": True},
    "gpt-oss-120b":     {"name": "GPT-OSS 120B", "provider": "groq", "factor": 1.0,
                         "api_model": "openai/gpt-oss-120b"},
    "gpt-oss-20b":      {"name": "GPT-OSS 20B", "provider": "groq", "factor": 0.7,
                         "api_model": "openai/gpt-oss-20b"},
    "qwen3-27b":        {"name": "Qwen3 27B", "provider": "groq", "factor": 0.9,
                         "api_model": "qwen/qwen3.8-27b"},
    "llama-3.3-70b":    {"name": "Llama 3.3 70B", "provider": "groq", "factor": 0.8, "vision": True,
                         "api_model": "llama-3.3-70b-versatile"},
    "llama-3.1-8b":     {"name": "Llama 3.1 8B (schnell)", "provider": "groq", "factor": 0.4,
                         "api_model": "llama-3.1-8b-instant"},
    "qwen3-32b":        {"name": "Qwen3 32B", "provider": "groq", "factor": 0.8, "vision": True,
                         "api_model": "qwen/qwen3-32b"},
    "deepseek-r1-70b":  {"name": "DeepSeek R1 70B", "provider": "groq", "factor": 0.9,
                         "api_model": "deepseek-r1-distill-llama-70b"},
    "kimi-k2":          {"name": "Kimi K2", "provider": "groq", "factor": 1.0, "paid": True, "vision": True,
                         "api_model": "moonshotai/kimi-k2-instruct"},
    "nemotron-ultra":   {"name": "Nemotron Ultra 550B", "provider": "cline", "factor": 0.6,
                         "api_model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    "nemotron-super":   {"name": "Nemotron Super 120B", "provider": "cline", "factor": 0.5,
                         "api_model": "nvidia/nemotron-3-super-120b-a12b:free"},
    "qwen3-27b-free":   {"name": "Qwen3.8 27B", "provider": "cline", "factor": 0.4,
                         "api_model": "qwen/qwen3.8-27b:free"},
    "gemma-4-31b":      {"name": "Gemma 4 31B", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "google/gemma-4-31b-it:free"},
    "north-mini-code":  {"name": "North Mini Code", "provider": "cline", "factor": 0.3,
                         "api_model": "cohere/north-mini-code:free"},
    "llama-3.3-70b-free": {"name": "Llama 3.3 70B (free)", "provider": "cline", "factor": 0.5, "vision": True,
                         "api_model": "meta-llama/llama-3.3-70b-instruct"},
    "deepseek-v3-free": {"name": "DeepSeek V3 (free)", "provider": "cline", "factor": 0.5,
                         "api_model": "deepseek/deepseek-chat-v3-0324:free"},
    "deepseek-r1-free": {"name": "DeepSeek R1 (free)", "provider": "cline", "factor": 0.6,
                         "api_model": "deepseek/deepseek-r1-0528:free"},
    "qwen3-235b-free":  {"name": "Qwen3 235B (free)", "provider": "cline", "factor": 0.7, "paid": True, "vision": True,
                         "api_model": "qwen/qwen3-235b-a22b-thinking-2507"},
    "mistral-small-free": {"name": "Mistral Small 3.1 (free)", "provider": "cline", "factor": 0.4, "vision": True,
                         "api_model": "mistralai/mistral-small-3.1-24b-instruct"},
    "phi-4-free":       {"name": "Phi-4 (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "microsoft/phi-4:free"},
    "gemma-3-27b-free": {"name": "Gemma 3 27B (free)", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "google/gemma-3-27b-it"},
    "mimo-v26-pro":     {"name": "MiMo v2.6 Pro", "provider": "cline", "factor": 0.7, "paid": True, "vision": True,
                         "api_model": "xiaomi/mimo-v2.6-pro"},
    "mimo-v26-flash":   {"name": "MiMo v2.6 Flash", "provider": "cline", "factor": 0.5, "paid": True, "vision": True,
                         "api_model": "xiaomi/mimo-v2.6-flash"},
    "qwen3-vl-32b":     {"name": "Qwen3 VL 32B", "provider": "cline", "factor": 0.6, "vision": True,
                         "api_model": "qwen/qwen3-vl-32b-instruct"},
    "glm-5v":           {"name": "GLM 5V", "provider": "cline", "factor": 0.6, "paid": True, "vision": True,
                         "api_model": "z-ai/glm-5v-turbo"},
    "mimo-v26-turbo":   {"name": "MiMo v2.6 Turbo", "provider": "cline", "factor": 0.6, "paid": True, "vision": True,
                         "api_model": "xiaomi/mimo-v2.6-pro-ultraspeed"},
    "minimax-m2":       {"name": "MiniMax M2.7", "provider": "cline", "factor": 0.9, "paid": True, "vision": True,
                         "api_model": "minimax/minimax-m2.7"},
    "qwen3-max":        {"name": "Qwen3 Max Thinking", "provider": "cline", "factor": 0.9, "paid": True, "vision": True,
                         "api_model": "qwen/qwen3-max-thinking"},
    "glm-53-air":       {"name": "GLM 5.3 Flash", "provider": "cline", "factor": 0.5, "paid": True,
                         "api_model": "z-ai/glm-5.3-flash"},
    "deepseek-v4-flash": {"name": "DeepSeek V4 Flash", "provider": "cline", "factor": 0.5, "paid": True, "vision": True,
                         "api_model": "deepseek/deepseek-v4-flash-vision-exp"},
    "kimi-k3":          {"name": "Kimi K3", "provider": "cline", "factor": 1.0, "paid": True, "vision": True,
                         "api_model": "moonshotai/kimi-k3"},
    "deepseek-v4-pro":  {"name": "DeepSeek V4 Pro", "provider": "cline", "factor": 0.8, "paid": True,
                         "api_model": "deepseek/deepseek-v4-pro"},
    "glm-53":           {"name": "GLM 5.3", "provider": "cline", "factor": 0.8, "paid": True, "vision": True,
                         "api_model": "z-ai/glm-5.3"},
    "glm-53-prime":     {"name": "GLM 5.3 Prime", "provider": "cline", "factor": 1.0, "paid": True, "vision": True,
                         "api_model": "z-ai/glm-5.3-prime"},
    "kimi-k2-thinking": {"name": "Kimi K2 Thinking", "provider": "cline", "factor": 1.0, "paid": True,
                         "api_model": "moonshotai/kimi-k2-thinking"},
    "claude-sonnet-5":  {"name": "Claude Sonnet 5", "provider": "cline", "factor": 1.2, "paid": True, "vision": True,
                         "api_model": "anthropic/claude-sonnet-5"},
    "grok-4.7":         {"name": "Grok 4.7", "provider": "cline", "factor": 1.2, "paid": True, "vision": True,
                         "api_model": "x-ai/grok-4.7"},
    "qwen3.7-max":      {"name": "Qwen3.7 Max", "provider": "cline", "factor": 1.2, "paid": True, "vision": True,
                         "api_model": "qwen/qwen3.7-max"},
    "claude-opus-5":    {"name": "Claude Opus 5", "provider": "cline", "factor": 1.5, "paid": True, "vision": True,
                         "api_model": "anthropic/claude-opus-5"},
    "minimax-m3":       {"name": "MiniMax M3", "provider": "cline", "factor": 1.5, "paid": True, "vision": True,
                         "api_model": "minimax/minimax-m3"},
}

# ── Pro Modell unabhaengig: Faehigkeiten + Planbedingungen ──
#   plan      – noetiger Plan; ein hoeherer Plan darf ALLES darunter (Max kann auch Pro-Modelle)
#   temp      – nur an Modelle senden, die "temperature" unterstuetzen (Free-Modelle lehnen ab)
#   reasoning – Denkprozess ("Thinking") wird live gesendet und angezeigt
PLAN_RANK = {"free": 0, "test": 1, "basic": 2, "pro": 3, "max": 4, "ultra": 5, "business": 6}
MODEL_PLAN = {
    "gemini-2.5-lite": "free", "llama-3.1-8b": "free", "gpt-oss-20b": "free",
    "qwen3-27b-free": "free", "gemma-3-27b-free": "free", "phi-4-free": "free",
    "mistral-small-free": "free", "north-mini-code": "free", "llama-3.3-70b-free": "free",
    "gemma-4-31b": "free", "deepseek-v3-free": "free",
    "gemini-2.5-flash": "basic", "llama-3.3-70b": "basic", "qwen3-32b": "basic",
    "deepseek-r1-free": "basic", "nemotron-super": "basic", "glm-53-air": "basic",
    "gemini-3.6-flash": "pro", "gpt-oss-120b": "pro", "deepseek-r1-70b": "pro",
    "qwen3-27b": "pro", "deepseek-v4-flash": "pro", "qwen3-vl-32b": "pro", "glm-5v": "pro",
    "gemini-3.6-pro": "max", "kimi-k2": "max", "mimo-v26-flash": "max",
    "glm-53": "max", "deepseek-v4-pro": "max", "qwen3-235b-free": "max",
    "minimax-m2": "max", "qwen3-max": "max", "glm-53-prime": "max", "kimi-k2-thinking": "max",
    "claude-sonnet-5": "max", "grok-4.7": "max",
    "nemotron-ultra": "ultra", "mimo-v26-pro": "ultra", "kimi-k3": "ultra",
    "mimo-v26-turbo": "ultra", "qwen3.7-max": "ultra", "claude-opus-5": "ultra", "minimax-m3": "ultra",
}
REASONING_MODELS = {"deepseek-r1-70b", "deepseek-r1-free", "nemotron-ultra", "glm-53", "glm-53-prime",
                    "kimi-k3", "qwen3-max", "qwen3-235b-free", "kimi-k2-thinking", "claude-opus-5"}
API_RATE_PER_MIN = {"free": 5, "test": 10, "basic": 20, "pro": 40, "max": 100, "ultra": 200, "business": 500}

def supports_temp(api_model):
    """Jedes Modell bekommt nur Parameter, die es UNTERSTUETZT:
    :free- und Reasoning-/Thinking-Modelle lehnen 'temperature' ab (HTTP 400/500)."""
    am = (api_model or "").lower()
    return ":free" not in am and not any(t in am for t in ("thinking", "reasoning", "-r1", "/deepseek-r1"))

def plan_ok(user_plan, need):
    """Hoeherer Plan enthaellt alles darunter (Max darf auch Pro-Modelle nutzen)."""
    return PLAN_RANK.get(user_plan or "free", 0) >= PLAN_RANK.get(need or "free", 0)

for _id, _m in MODELS.items():
    _m["plan"] = MODEL_PLAN.get(_id, "free")
    _m["temp"] = supports_temp(_m.get("api_model") or _id)
    _m["reasoning"] = _id in REASONING_MODELS

# ── Token- & Plan-System (wie Cline): Nutzung in Tokens, Limits in 3 Fenstern ──
TOKEN_WINDOWS = {
    "5h":    {"seconds": 5 * 3600,       "label": "5 Stunden"},
    "week":  {"seconds": 7 * 24 * 3600,  "label": "Woche"},
    "month": {"seconds": 30 * 24 * 3600, "label": "Monat"},
}
PLANS = {
    "free":     {"name": "Free",     "price": 0.0,    "desc": "Zum Ausprobieren",
                 "t5": 40000,    "tweek": 250000,    "tmonth": 750000},
    "test":     {"name": "Test",     "price": 0.10,   "desc": "Zahlung testen – echte Abbuchung",
                 "t5": 5000,     "tweek": 25000,     "tmonth": 75000},
    "basic":    {"name": "Basic",    "price": 6.99,   "desc": "Fuer den Einstieg",
                 "t5": 150000,   "tweek": 1200000,   "tmonth": 4000000},
    "pro":      {"name": "Pro",      "price": 12.99,  "desc": "Fuer jeden Tag",
                 "t5": 400000,   "tweek": 3500000,   "tmonth": 12000000},
    "max":      {"name": "Max",      "price": 39.99,  "desc": "Viel los",
                 "t5": 1200000,  "tweek": 10000000,  "tmonth": 40000000},
    "ultra":    {"name": "Ultra",    "price": 99.99,  "desc": "Fast ohne Grenzen",
                 "t5": 3000000,  "tweek": 25000000,  "tmonth": 100000000},
    "business": {"name": "Business", "price": 199.99, "desc": "Fuer Teams & Firmen",
                 "t5": 8000000,  "tweek": 60000000,  "tmonth": 250000000},
}
PLAN_ORDER = ["free", "test", "basic", "pro", "max", "ultra", "business"]
SUPER_ADMIN_EMAIL = "admin@sychos.net"   # nur dieser Account darf Admin-Rechte vergeben/entziehen
PLAN_DAYS = 30   # ein gekaufter Plan gilt 30 Tage (monatlich kuendbar)
PROMO_CODES = {"release": 0.20}   # Rabattcode "Release" = -20 %

def effective_plan(user):
    """Aktiver Plan: gekaufter Plan (bis plan_until) > Admin-Paid (= Pro) > free."""
    if not user:
        return "free"
    p = user.get("plan") or "free"
    if p != "free":
        until = user.get("plan_until", 0) or 0
        if until and time.time() > until:
            p = "free"
        else:
            return p
    paid_until = user.get("paid_until", 0) or 0
    if user.get("is_paid") and (not paid_until or time.time() <= paid_until):
        return "pro"
    return "free"

def plan_limits(plan):
    p = PLANS.get(plan, PLANS["free"])
    return {"5h": p["t5"], "week": p["tweek"], "month": p["tmonth"]}

def usage_state(uid, plan):
    """Drei Fenster (5h / Woche / Monat): Verbrauch, Limit und Reset-Zeit."""
    now = time.time()
    lim = plan_limits(plan)
    out = {}
    db = get_db()
    brow = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
    bonus = (brow["bonus_tokens"] if brow else 0) or 0
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
            db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                       (uid, win, start, used))
        out[win] = {"used": used, "limit": lim[win] + bonus,
                    "remaining": max(0.0, lim[win] + bonus - used),
                    "resets_at": start + info["seconds"]}
    db.commit(); db.close()
    return out

def add_usage(uid, tokens):
    """Verbrauchte Tokens in alle drei Fenster eintragen (Fenster laufen automatisch ab)."""
    now = time.time()
    db = get_db()
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
        db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                   (uid, win, start, used + max(0.0, float(tokens))))
    db.commit(); db.close()

def est_tokens(history):
    """Grobe Vorab-Schaetzung (Input + Bilder + Reserve fuer die Antwort)."""
    def _clen(c):
        if isinstance(c, str):
            return len(c)
        n = 0
        for p in c or []:
            n += len(p.get("text") or "") + (1800 if p.get("type") == "image" else 0)
        return n
    chars = sum(_clen(m.get("content")) for m in history)
    return int(chars / 4) + 900

def limit_block(uid, plan, est):
    """None = ok, sonst (win, state) des ersten ueberzogenen Fensters."""
    st = usage_state(uid, plan)
    for win in ("5h", "week", "month"):
        if st[win]["used"] + est > st[win]["limit"]:
            return win, st[win], st
    return None

def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h:
        return "%d Std %d Min" % (h, m)
    return "%d Min" % max(1, m)

def strength_cost(strength, model_id):
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    f = MODELS.get(model_id, {}).get("factor", 1.0)
    return max(1, int(round(s["cost"] * f)))

request_times = []
LOAD_LOCK = threading.Lock()

def note_request():
    with LOAD_LOCK:
        now = time.time()
        recent = [t for t in request_times if now - t < 60]
        recent.append(now)
        request_times[:] = recent

def load_factor():
    """Auslastungs-Aufschlag: viele Anfragen pro Minute -> hoehere Kosten."""
    with LOAD_LOCK:
        n = len(request_times)
    if n >= 240:
        return 2.5
    if n >= 120:
        return 2.0
    if n >= 60:
        return 1.5
    if n >= 30:
        return 1.25
    return 1.0

def message_cost(strength, model_id):
    """Endgueltiger Credit-Preis inkl. Auslastungs-Aufschlag."""
    return max(1, int(round(strength_cost(strength, model_id) * load_factor())))

RATE = {}
RATE_LOCK = threading.Lock()

def rate_ok(key, limit, window=60):
    now = time.time()
    with RATE_LOCK:
        lst = [t for t in RATE.get(key, []) if now - t < window]
        if len(lst) >= limit:
            RATE[key] = lst
            return False
        lst.append(now)
        RATE[key] = lst
        return True

START_TIME = time.time()
# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def hash_pw(pw):
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000)
    return "pbkdf2$%s$%s" % (salt, dk.hex())

def verify_pw(pw, stored):
    if stored.startswith("pbkdf2$"):
        _, salt, dk = stored.split("$", 2)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()
        return hmac.compare_digest(calc, dk)
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)

def new_token():
    return uuid.uuid4().hex + os.urandom(16).hex()

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            credits REAL DEFAULT 50.0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT DEFAULT '',
            ban_until REAL DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_paid INTEGER DEFAULT 0,
            reg_ip TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            last_login REAL DEFAULT 0,
            last_seen REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            title TEXT DEFAULT 'Neuer Chat',
            model TEXT DEFAULT 'gemini-3.6-flash',
            created_at REAL DEFAULT 0,
            FOREIGN KEY (uid) REFERENCES users(uid)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_keys (
            uid TEXT NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            PRIMARY KEY (uid, provider)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    row = db.execute("SELECT uid FROM users WHERE is_admin=1").fetchone()
    if not row:
        uid = str(uuid.uuid4())
        # Admin-Passwort: Env ORACLE_ADMIN_PASSWORD, sonst "Lenamax5745"
        pw = os.environ.get("ORACLE_ADMIN_PASSWORD", "Lenamax5745")
        db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                   (uid, "admin@sychos.net", hash_pw(pw), "Sychos", 99999, 1, time.time()))
        db.commit()
    try:
        db.execute("ALTER TABLE messages ADD COLUMN images TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    db.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            uid TEXT NOT NULL,
            win TEXT NOT NULL,
            start REAL DEFAULT 0,
            tokens REAL DEFAULT 0,
            PRIMARY KEY (uid, win)
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            key TEXT UNIQUE,
            created_at REAL DEFAULT 0,
            revoked INTEGER DEFAULT 0
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            plan TEXT NOT NULL,
            code TEXT DEFAULT '',
            price REAL DEFAULT 0,
            created_at REAL DEFAULT 0
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            read INTEGER DEFAULT 0
        );""")
    for col in ("last_login REAL DEFAULT 0", "last_seen REAL DEFAULT 0",
                "ban_reason TEXT DEFAULT ''", "ban_until REAL DEFAULT 0",
                "is_paid INTEGER DEFAULT 0", "reg_ip TEXT DEFAULT ''",
                "paid_until REAL DEFAULT 0", "last_credit REAL DEFAULT 0",
                "plan TEXT DEFAULT 'free'", "plan_until REAL DEFAULT 0",
                "bonus_tokens REAL DEFAULT 0",
                "totp_secret TEXT DEFAULT ''", "totp_on INTEGER DEFAULT 0"):
        try:
            db.execute("ALTER TABLE users ADD COLUMN " + col)
            db.commit()
        except Exception:
            pass
    db.close()

def touch_user(uid):
    """Markiert einen User als aktiv (online)."""
    db = get_db()
    db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), uid))
    db.commit(); db.close()

# ═══════════════════════════════════════════════════════════
#  KI-PROVIDER
# ═══════════════════════════════════════════════════════════
def mask_key(k):
    """Key unsichtbar machen (fuer Statusanzeigen)."""
    if not k:
        return ""
    return "*" * 8 + k[-4:]

def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def maintenance_on(db=None):
    """Wartungsmodus (settings.maintenance='1'): 'Server abschaltet' – Login/Modelle/Chat
    sind fuer normale User gesperrt, Admins arbeiten normal weiter. Server bleibt laufen."""
    own = db is None
    if own:
        db = get_db()
    val = get_setting(db, "maintenance") == "1"
    if own:
        db.close()
    return val

def set_maintenance(on):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('maintenance',?)",
               ("1" if on else "0",))
    db.commit(); db.close()

def resolve_key(db, uid, provider):
    """Key-Auflösung: Admin-Settings (DB) > Env > fest im Code hinterlegter Default."""
    db_key = get_setting(db, provider + "_api_key")
    if db_key:
        return db_key
    return {"gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY, "cline": CLINE_API_KEY}.get(provider, "")

def web_search(query):
    # Kostenlose Websuche (DuckDuckGo + Wikipedia) fuer KI-Kontext. Kein Key noetig.
    out = []
    try:
        url = "https://api.duckduckgo.com/?q=" + urllib.parse.quote(query) + "&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        if d.get("AbstractText"):
            out.append("- " + d["AbstractText"][:400] + " (Quelle: " + d.get("AbstractURL", "") + ")")
        for t in (d.get("RelatedTopics") or [])[:5]:
            if isinstance(t, dict) and t.get("Text"):
                out.append("- " + t["Text"][:300])
    except Exception:
        pass
    if len(out) < 2:
        try:
            url = ("https://de.wikipedia.org/w/api.php?action=query&list=search&srsearch="
                   + urllib.parse.quote(query) + "&format=json&utf8=1&srlimit=5")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read())
            for r in (d.get("query", {}).get("search") or []):
                snip = re.sub("<[^>]+>", "", r.get("snippet", ""))
                out.append("- " + r.get("title", "") + ": " + snip[:300])
        except Exception:
            pass
    return out

def totp_code(secret, counter=None):
    """6-stelliger TOTP-Code (RFC 6238, SHA-1, 30s) – nur Stdlib."""
    try:
        key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
    except Exception:
        return ""
    if counter is None:
        counter = int(time.time()) // 30
    h = hmac.new(key, int(counter).to_bytes(8, "big"), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((int.from_bytes(h[o:o + 4], "big") & 0x7FFFFFFF) % 1000000)

def totp_ok(secret, code):
    code = (code or "").replace(" ", "")
    if not secret or len(code) != 6 or not code.isdigit():
        return False
    now = int(time.time()) // 30
    return any(hmac.compare_digest(totp_code(secret, now + off), code) for off in (-1, 0, 1))

def make_pdf(title, text):
    """Minimaler PDF-Writer (A4, Helvetica) – ohne externe Bibliotheken."""
    def esc(s):
        s = str(s).encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    t = re.sub(r"```[\s\S]*?```", " [Code] ", text or "")
    t = t.replace("**", "").replace("__", "").replace("`", "").replace("#", "")
    lines = []
    for para in ([title or "Sychos Export", ""] + t.split("\n")):
        para = (para or " ").rstrip() or " "
        while len(para) > 92:
            cut = para.rfind(" ", 0, 92)
            cut = cut if cut > 40 else 92
            lines.append(para[:cut])
            para = para[cut:].lstrip()
        lines.append(para)
    per_page = 46
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[""]]
    page_ids, content_ids, num = [], [], 4
    for _ in pages:
        page_ids.append(num); num += 1
        content_ids.append(num); num += 1
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    def add(n, body):
        offsets[n] = len(out)
        out.extend(("%d 0 obj\n" % n).encode())
        out.extend(body)
        out.extend(b"\nendobj\n")
    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, ("<< /Type /Pages /Kids [%s] /Count %d >>" %
            (" ".join("%d 0 R" % i for i in page_ids), len(pages))).encode())
    add(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, plines in enumerate(pages):
        body = ["BT /F1 11 Tf 50 792 Td 14 TL"]
        for ln in plines:
            body.append("(%s) Tj T*" % esc(ln))
        body.append("ET")
        stream = "\n".join(body).encode("latin-1", "replace")
        add(page_ids[i], ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                          "/Contents %d 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
                          % content_ids[i]).encode())
        add(content_ids[i], b"<< /Length " + str(len(stream)).encode() +
            b" >>\nstream\n" + stream + b"\nendstream")
    xref = len(out)
    out.extend(("xref\n0 %d\n0000000000 65535 f \n" % num).encode())
    for i in range(1, num):
        out.extend(("%010d 00000 n \n" % offsets[i]).encode())
    out.extend(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (num, xref)).encode())
    return bytes(out)

def gemini_parts(content):
    """History-Inhalt -> Gemini 'parts' (Text + inline Bilder)."""
    if isinstance(content, str):
        return [{"text": content}]
    out = []
    for p in content or []:
        if p.get("type") == "image":
            out.append({"inline_data": {"mime_type": p.get("mime", "image/png"),
                                        "data": p.get("data", "")}})
        else:
            out.append({"text": p.get("text", "")})
    return out or [{"text": ""}]

def oai_content(content):
    """History-Inhalt -> OpenAI-Style 'content' (Text + image_url)."""
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if p.get("type") == "image":
            out.append({"type": "image_url", "image_url": {
                "url": "data:%s;base64,%s" % (p.get("mime", "image/png"), p.get("data", ""))}})
        else:
            out.append({"type": "text", "text": p.get("text", "")})
    return out or ""

def call_gemini(messages, api_key, strength="medium", model="gemini-3.6-flash"):
    """Echter Gemini-Aufruf. Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Gemini-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": gemini_parts(m["content"])})
    payload = json.dumps({
        "contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]},
    }).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent?key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            cand = data.get("candidates") or [{}]
            parts = (cand[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Gemini hat eine leere Antwort geliefert."}
            tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Gemini-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Gemini-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Gemini-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Gemini nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_groq(messages, api_key, strength="medium", model="llama-3.3-70b-versatile"):
    """Echter Groq-Aufruf (OpenAI-kompatibel). Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Groq-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    effort = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps({
        "model": model, "messages": msgs,
        "max_tokens": s["max_tokens"] + 512, "temperature": s["temp"],
        "reasoning_effort": effort,
    }).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
        data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            msg = (data["choices"][0].get("message") or {})
            text = msg.get("content") or ""
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Groq hat eine leere Antwort geliefert. Bitte erneut versuchen."}
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Groq-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Groq-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Groq-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Groq nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_cline(messages, api_key, strength="medium", model="anthropic/claude-sonnet-4.6"):
    """Echter Cline-Aufruf (OpenAI-kompatibel, api.cline.bot)."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Cline-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    # Pro Modell nur senden, was es UNTERSTUETZT (Free-Modelle lehnen temperature ab)
    _body = {"model": model, "messages": msgs, "max_tokens": s["max_tokens"] + 512}
    if supports_temp(model):
        _body["temperature"] = s["temp"]
    payload = json.dumps(_body).encode()
    def _do_call():
        req2 = urllib.request.Request("https://api.cline.bot/api/v1/chat/completions",
            data=payload, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "HTTP-Referer": SYCHOS_HOME,
                "X-Title": "Sychos",
            })
        with urllib.request.urlopen(req2, timeout=90) as resp:
            data = json.loads(resp.read())
        ch = data.get("choices") or (data.get("data") or {}).get("choices") or []
        msg = (ch[0].get("message") or {}) if ch else {}
        text = msg.get("content") or ""
        return text, data.get("usage", {}).get("total_tokens", 0)

    try:
        text, tokens = _do_call()
        if not text.strip():
            return {"ok": False, "error_type": "api",
                    "error": "Cline hat eine leere Antwort geliefert."}
        return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code == 500:
            # Free-Tier-Flakiness: ein automatischer Wiederholungsversuch
            try:
                time.sleep(1.5)
                text, tokens = _do_call()
                if text.strip():
                    return {"ok": True, "text": text, "tokens": tokens}
            except Exception:
                pass
            return {"ok": False, "error_type": "api",
                    "error": "Free-Modell voruebergehend ueberlastet. Bitte erneut versuchen."}
        if e.code == 402:
            return {"ok": False, "error_type": "insufficient_credits",
                    "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Cline-API-Key ist ungueltig oder gesperrt."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Cline-Rate-Limit erreicht. Bitte kurz warten."}
        return {"ok": False, "error_type": "api",
                "error": "Cline-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api", "error": "Cline nicht erreichbar."}


# ═══════════════════════════════════════════════════════════
#  STREAMING — Token-fuer-Token (SSE) fuer echtes Live-Tippen
# ═══════════════════════════════════════════════════════════
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def _http_err(provider, e):
    if provider == "Cline" and e.code == 402:
        return {"error_type": "insufficient_credits",
                "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
    if e.code in (401, 403):
        return {"error_type": "invalid_key",
                "error": provider + "-API-Key ist ungueltig oder gesperrt."}
    if e.code == 429:
        return {"error_type": "rate_limit",
                "error": provider + "-Rate-Limit erreicht. Bitte kurz warten."}
    return {"error_type": "api",
            "error": provider + "-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}

def stream_gemini(messages, api_key, strength, model):
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein Gemini-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = [{"role": ("user" if m["role"] == "user" else "model"), "parts": gemini_parts(m["content"])}
                for m in messages]
    payload = json.dumps({"contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]}}).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":streamGenerateContent?alt=sse&key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    got = False
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob or blob == "[DONE]":
                    continue
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                cand = (d.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts)
                if txt:
                    got = True
                    yield {"type": "delta", "text": txt}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err("Gemini", e)}
        return
    except Exception:
        pass
    r = call_gemini(messages, api_key, strength, model)   # Fallback ohne Stream
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_openai_style(messages, api_key, strength, model, provider, base_url):
    name = "Cline" if provider == "cline" else "Groq"
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein %s-API-Key hinterlegt (Admin-Panel -> API-Keys)." % name}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    body = {"model": model, "messages": msgs, "max_tokens": s["max_tokens"] + 512, "stream": True}
    if supports_temp(model):        # nur was das Modell unterstuetzt
        body["temperature"] = s["temp"]
    if provider != "cline":
        body["temperature"] = s["temp"]
        body["reasoning_effort"] = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key,
               "User-Agent": UA, "Accept": "text/event-stream"}
    if provider == "cline":
        headers["HTTP-Referer"] = SYCHOS_HOME
        headers["X-Title"] = "Sychos"

    def once():
        req = urllib.request.Request(base_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if blob == "[DONE]":
                    break
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                ch = d.get("choices") or []
                dl = (ch[0].get("delta") or {}) if ch else {}
                rc = dl.get("reasoning_content") or ""
                if rc:
                    yield {"think": rc}          # Thinking-Modus: Denkprozess live
                delta = dl.get("content") or ""
                if delta:
                    yield {"text": delta}

    got = False
    try:
        for ev in once():
            if ev.get("think"):
                yield {"type": "think", "text": ev["think"]}
                continue
            got = True
            yield {"type": "delta", "text": ev.get("text", "")}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err(name, e)}
        return
    except Exception:
        pass
    r = call_cline(messages, api_key, strength, model) if provider == "cline" \
        else call_groq(messages, api_key, strength, model)
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        gen = stream_openai_style(messages, api_key, strength, api_model, "groq",
                                  "https://api.groq.com/openai/v1/chat/completions")
    elif prov == "cline":
        gen = stream_openai_style(messages, api_key, strength, api_model, "cline",
                                  "https://api.cline.bot/api/v1/chat/completions")
    else:
        gen = stream_gemini(messages, api_key, strength, model)
    for ev in gen:
        yield ev


def call_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        return call_groq(messages, api_key, strength, api_model)
    if prov == "cline":
        return call_cline(messages, api_key, strength, api_model)
    return call_gemini(messages, api_key, strength, model)

# ═══════════════════════════════════════════════════════════
#  EINGEBETTETES FRONTEND (Single-File-Modus)
# ═══════════════════════════════════════════════════════════
WEB_ASSETS = {
    "index.html": ("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" "/f8DyWdoAA==", "text/html; charset=utf-8"),
    "css/style.css": ("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" "Xacz", "text/css; charset=utf-8"),
    "js/app.js": ("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" "fwEqYjTF", "application/javascript; charset=utf-8"),
    "admin/index.html": ("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" "28Ub8NnV94OW/m3Agxb9vxv8HxFCx60=", "text/html; charset=utf-8"),
    "terms.html": ("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", "text/html; charset=utf-8"),
    "privacy.html": ("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", "text/html; charset=utf-8"),
    "refund.html": ("eNqNVs1u20YQvhfoO0xZILBRibLV/Bj6C+zGaQrHrRE7CZrbkhyRCy+Xwu5Ssl0U6Dv03lzyDOnFp+pN+iT9dilakpMAvWjJ4c43s998M6vRN89++eHi17Nj" "KlypJl9/NfIrKaHzcZRxFCwsMr+W7ASlhTCW3Th6ffG8exDd2bUoeRzNJS9mlXERpZV2rLFvITNXjDOey5S74aVDUksnheraVCge7wcUJ53iybGxTjhX69wa" "mRZOYSvTv3/8SefXaVHZUa/ZBwd8uiTDahxZd63YFsyIWxiejqNeam0vmGM8PZ2PH8V7cT/ECVY8EMWKc6HoNyrFVZPZgJ483ptdDWExudQD2iNRu2pIM5Fl" "UucDeojP1Pc/B2EjkuBuwTIv3ID24ydD+n0DutgH+hRMdKeilOp6QHNhdrrdYMp2h803K294QP3H24H3KBi24Pr/H25/C67fR8YrxLRSlWldHV+53e0os077" "pGQbb4X5ffzoswjd/j2MOBHpJZwzaWdKIFGpA1OJqtLLNq1uUjlXlcA92ED9NjuYpo/SIQXgjNPKCCcrHEJXmkOYUa8t4qjXijOpsmu/ZnJOqRLWjqOQShRK" "PRKt0Sd2p5Jo8kAJY4b0rjbLW2R8U5f0ok5GPdG4FfubiuzSA1HOhvRWZmxMPd2QKBLZb1xmFJIbR815VhUSUrvdaHLuhM4GdM4zx2XCBlLqPx71ZnBt4vUn" "+w/pQuSMlKZGcsYa6nI2F0ZoF+L0V3Emh0ox5Xwp6ilajc7U8oNm2rlg6zp0JKxMO3Rmqg6diqsOvVbOCNhri0JYu0uXy49aw2+UTCQeTCFUQvNK0yoDPeol" "IZQWaUEZl3SCSMQNH+xowQbpheasCkQ+4hws6gxUxXSMKPTKk8p3BMJ3WqncQQ0IhOwNvVh+LJiWt+DCh/J4tbEzj5MrsIv3d6JQnv0F5/E2VecVJQxajMit" "o6wm9kHXBVuTVauw4knJyXH3VEhFQkOeTLL06lgJQpYzg0pFk5/wAJbq0kuBCtjYQE+O6bye+QnXPcz8BhQEiGvsI3ZAmoKUv9Z5RPRdCJWwpDNMEpRtzmbB" "OmMANul8EU8YuAX2stqgEChXgxGnVUkvXRb7OtHOKeMryKBqSq9812S7tK4rgskcfL9lc+lQ3Lswo17gpqV0LW2GtDfV9oZNYkSNkhgqRILCZMJCfrgMbkKl" "aNt3S0oxnUoXNOTVUAqFZDQdJtgNa1N6mUuH60DTS5Y2HNg6WZbezdDPtbvB4hEgM5wrWO92itpmoYN9JuhiqnVGOnjf1AH+hLV2WtqOT9uG3O/l66vjVWkd" "+igLbOHMQVzQ7fJWqUbCSlpsH3oIDxw07ox0rsVRLJMWztygeAFrLYemoU7F8n0OHsNB3tXW93zlaa01mmF5Wxh3T+6HtdWiKH1jrstyhDAebwEqQhlKUG1t" "UyrauYmPYlr4OWKKSkFtJ8sP9ZQDP+uMWGMcCN00xNrsQ4gkZ8WFbts9Jh/xxPe5zIMrPvjfN76y1fI9JOFfPdK6r9A9pY0mvoq+lRP2t2nwbkdtOxou7/Xw" "PQ4uQLH2BWA/2kB1yU836XguUMBA6TPJGlNB+xFNGoETYQhyCUf3G55z4SdQs2HdKfjnA1B0ovZ7jPdIuPCV8SEW0mQdGKxjeDGs0xDxUGNoecb9iNxS0RfO" "snFPWekYd3gFkqLJJ5wd/ngUZtA/f2/wiSk1F+l1NHkmMPpBB8736S784rDR5N4Y/nTj58feqIckwyXbru0l21v9WfwPzh15NQ==", "text/html; charset=utf-8"),
    "imprint.html": ("eNqNVdtu3DYQfS/Qf5iqQGEDK+0l8QV7UeFkXduIb4jjAnXRB0oarQhTpEBSu14XBfoP/YC8GOgfpC9+yv5Jv6RD7srdTdwiL+JwNHPmPhx+M754/e6ny0Mo" "bCnir78auhMEk5NRkGHgOcgyd5ZoGaQF0wbtKLh+90O4HzzxJStxFEw5ziqlbQCpkhYlyc14ZotRhlOeYugvLeCSW85EaFImcNT1KJZbgfFJWWk0pi7h79//" "gKt5WigzbC//kZDg8hY0ilFg7FygKRDJVqExHwXt1Ji2Z0dEfT8d7USdqOexPZcIgEjghAn4FUp2t/SmD3u7nepuQBw94bIPHWC1VQOoWJZxOenDS/oNPffZ" "94LkBIYF8klh+9CN9gbw2xp00SX0nKIPc1ZyMe/DlOmtMPSsbHuw/Gf4Pfaht7tpuAOesQHX+3K47gZcr0cerxBTJZRuVC3e2e1NK1WroQRv7K0wX0Q7zyKE" "vU8wooSlt6SccVMJRo5y6TOVCJXeNm6FibJWlYS7v4b6bbafpzvpADxwhqnSzHJFQUglsTFjVaYIv9HJ82R/N1klYLYqx16n48WH7abmw3bTv4nK5u7M+BRS" "wYwZBd7zwHfGkDVMF8dTUwXxd4JpPYCbWi8eKcB76s3jOhm22VKt6P7btGSru+RW4O2PgqW3q5oxLu12EB/ICUtQwgTLxcPiPXz8E3ZgPD4ativSXqL24h9R" "M2lnNE2CpwVqaMOBTDha1GSnt7LjDyKS+IzdwZFmhRy2kxiGpmKyichlLoh/Tri1CFMlhLGLB5nxCTlxToMrAckzzej+C2WONONhohvoZ5CuSHjxHqGWGRyz" "2si6LFF/me7l6Q1caNsIt2CMtTVpQTsn8zFtZOENVZfd2vWID8MzxkX/ecfqyu2fcCnz32FdX9nwJDvX0fMwOaMcUaZ0QU6hbIFR0li4QS4QxOIDubuG+T9l" "g3zxqIEg4EQWTGzE8VQw2DqQhKh5bmHGERQ1x/Ym6jGX1OKGuk/DDStELSeGJTOe3jpyHXVMAJ9LAOpciYmFxWNCrUTtcknrza1KVcKpzSLXNC140YELkcEr" "yh3OW3CqZKYkHL5+eQZ7B9ctZ4Lio6zyiUUDbxYfiKI7Bbp1hpoeB2lB5fDWjXC2HTlpVwDfwdS+pnJDRCT1DGk/1DllJkFjsfDzQJXyDjpDSwcHlDwNFJTL" "v0GRGJcjnflE1fTGaDe5q2GlW2mC+Ly29y78BN0CJwqlG1dnyGUZ3eyhTjRLCxut5XltMRhukXasIsQg/szAwdErD/jxrzXjleZTls6DeMzo5SNL5MXnUvSl" "oQnit26ZoDaWWesr+Kkgp6VC3RusbxdGvUZO+q3WnM1Wa68e8H8AzDaQWA==", "text/html; charset=utf-8"),
    "404.html": ("eNqVVtuO2zYQfS/Qf5iqSLDBWrLstTde35qgaVH0oVl00wLpGy2NJNYUKZCULxsE6Ef0c/rWP+mXdEhJvm2KoLuQRHLODOdyOPT8qzdvv333/v47KGwpll9+" "MXdfEEzmiyDFwK8gS923RMsgKZg2aBfBL+++DyfBYV2yEhfBhuO2UtoGkChpURJuy1NbLFLc8ARDP+kBl9xyJkKTMIGLQRRf2kmUUJrEBZZ4Yitlen0JtQ4T" "eoUT5NfxJL6LUw+23ApcjuIR/PPHn/CwTwpl5v1mlcSCyzVoFIuAk3oAhcZsEWRs46aR2eQB2H1FO/GS5dinhetdKYJz1UojoSUmtjNQWFuZab+fkUsmypXK" "BbKKmyhR5f9VNpZZnnhNSLQyRmmec3mw8vkd+4kxw28yVnKxX7ytbcbtdJsX9tU4jme39LykZxLHz1vIvajN9Y9szbRl1w9MmgY9ItSJxvOUm0qw/cJsWRU0" "wRi7F2gKRHsR5Ymgddg51ffLEY083M9oABDJLNxqVsEHNwOolCHSKDl11igdG5zBY8hlirspDGZQICcPaRjHm2LW6LTuTSETuJsBEzyXIbdYmikkxBPUM/i9" "NpZn+7ClzlFQsTTlMp/CcFTtvMGPnV8J0yl8AIs7G3qjR62S7RqWT2F8G1duV0nEaT3nBiG6NZDUK56EK3zkqK+iYS+a9KKb3uDF7GQPlWIXu6to2FRmChum" "r8LQL6Wk4AfbNniq4OxExfBHJNcEK6urwZC86cFwuNm6N01ImaqDYZe56K7VFWgpltBULPEJCKN4hOUM/CGbgtXEh4ppirhVoP1Xa25DnxBjtVrTtsNq1/pK" "ZsjTBtpACpaq7RRi+ndJOsGFuVDbDnySukzwZI0aRoa6R+YaCB5r8mqN+0xTOzAHXJu5+FkP7m7c65ZexI1nVDflArN7T5uPDe5u5DDjM2k0HrfyQ1EsyzvL" "B25x6bO4EipZu/prOplkekJhUXSOAQcm0QwGHZsAVkqnSAkd0LJRgqdP89BiQs1SXhNrb+KD9mdJ0VR/MHAunLHk5YEll5WOblydfY18lTOlyynUVYU6YQYP" "FLis6/nJKAaUxicEHA4d/0aOfjcN+5pchStlrSrdyXWenpui43++Z1lbvIxwFI2d5oW54eTMHEsckQzZu+gKOavIxtCB/7MXXLLMH2SKUavylDHxDE7S5ofU" "qvC9j90dbrDqgn8neKkkOgxtNu93jXDe767flUr37pvyjcuoMYugbZGBb5gXApe+RvBUpNzNThfivE+CT2OI6cHygVobE7BBTRVAeQYvBssHpFMIkieFhRyz" "mnoxYUjQQqrlG46UqJ8VFQ1wxym7qG2rcVVioV8AL9srOfwJ7WM0X+lWnf5+5SgEevS2pnMAhiPU5Qolk9L665xC0bBlmowiFGQ+mverT4fUMiA42J+zTrqy" "EugJK00NR+8PF1Sw/K3Wf/+VrOGxLuGHejXvs0+rH1RYWnJJiq/dN7xnEsVR6ZjBbnT8dvXt+19i/wLI3AeU", "text/html; charset=utf-8"),
    "favicon.svg": ("eNp1Uk1vgzAMvU/af7DSy3ZICCEEOsEOu+y0H8FC+NAYQSEt9N/PoaXSJk0iefbzs7GtFPO5hfV7GOeSdN5PL1G0LAtbEmZdGwnOeYQKAufeLG92LQkHDkri" "R14fHwCK2jTzZqE99KOp3Lur6t6MHvq6JJi6xphF4HKFVZQkRm+DWyKmzt5OYJtmNn6TBZ9qO1hXkkMe57rJSfSPPP4j5+pT1fIuL6LffV37ju6NF85oD0tf" "+64kOBd0pm87f7UdjhwrAk0/DKH0J6/j6la6mCrfAQ75ISUIrmnCUjyC5kxSyXIaJ+gqqiDG+8gkZBjIGPqCHZFMQELKBCgM5YjhiBBmKUYEy/CWGOPIZBSL" "IHekcrsFy4HTFNmEKozif8m+Hri1O9rRhN04+2VKcnLD06F93gl6mzi9E2FNuppK4uxprPchde/0YEDjIiRq9QUxLAZhX8teOWQU4b0g/gAsK5qT", "image/svg+xml"),
}

def get_embedded(rel):
    """Eingebettete Frontend-Datei (gzip+base64), falls keine auf Disk liegt."""
    if not WEB_ASSETS:
        return None
    key = rel.replace("\\", "/").lstrip("/")
    if key in ("", "index.html"):
        key = "index.html"
    item = WEB_ASSETS.get(key)
    if item is None:
        return None
    return zlib.decompress(base64.b64decode(item[0])), item[1]


# ═══════════════════════════════════════════════════════════
#  HTTP HANDLER
# ═══════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            ts = time.strftime("%H:%M:%S")
            sys.stderr.write(f"  [{ts}] {fmt % args}\n")
        except Exception:
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, Accept")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")   # nie alte Frontends im Cache

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln) if ln else b""
        self._raw_body = raw                    # fuer Webhook-Signaturpruefung
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _get_user(self):
        """Auth per Session-Token (Bearer). Gibt User-Dict oder None zurueck."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        db = get_db()
        row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (token,)).fetchone()
        user = dict(row) if row else None
        if user:
            # Zeitlich begrenzte Sperre automatisch aufheben
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                db.commit()
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            # Zeitlich begrenztes Paid laeuft automatisch ab
            if user.get("is_paid") and user.get("paid_until", 0) and time.time() > user["paid_until"]:
                db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["is_paid"] = 0
                user["paid_until"] = 0
            # Abgelaufene Plaene laufen automatisch aus (zurueck auf Free)
            if user.get("plan", "free") != "free" and user.get("plan_until", 0) \
                    and time.time() > user["plan_until"]:
                db.execute("UPDATE users SET plan='free', plan_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["plan"] = "free"
                user["plan_until"] = 0
            db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), user["uid"]))
            db.commit()
        db.close()
        return user

    def _ban_info(self, user):
        """Sperr-Details fuer das Frontend-Modal."""
        until = user.get("ban_until", 0) or 0
        return {"banned": True,
                "ban_reason": user.get("ban_reason", ""),
                "ban_until": until,
                "ban_permanent": not bool(until)}

    def _require_user(self, allow_banned=False):
        """Auth + serverseitige Ban-Pruefung. Gibt (user, error) zurueck."""
        user = self._get_user()
        if not user:
            return None, (401, {"ok": False, "error": "Nicht angemeldet"})
        if user["is_banned"] and not allow_banned:
            d = {"ok": False, "error": "Account gesperrt"}
            d.update(self._ban_info(user))
            return None, (403, d)
        return user, None

    def _check_admin(self):
        """Nur angemeldete Admin-User (Session-Token). Kein statischer Admin-Key mehr."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:].strip()
        if not tok:
            return False
        db = get_db()
        row = db.execute("SELECT u.is_admin FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (tok,)).fetchone()
        db.close()
        return bool(row and row["is_admin"])

    def _serve_static(self, path):
        """Liefert Frontend-Dateien aus dem Projekt-Root aus."""
        if path in ("/", "/index.html"):
            path = "/index.html"
        elif path in ("/admin", "/admin/"):
            path = "/admin/index.html"
        elif path in ("/terms", "/privacy", "/refund", "/imprint"):
            path = path + ".html"   # Rechtsseiten: /terms -> terms.html usw.
        rel = unquote(path).lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.normpath(ROOT)):
            self._json(403, {"ok": False, "error": "Zugriff verweigert"}); return
        if os.path.isfile(full):
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
        else:
            emb = get_embedded(rel)
            if emb is None:
                # 404-Seite: echter 404-Status mit moderner "Page Not Found"
                nf = get_embedded("404.html")
                if nf and "." not in os.path.basename(rel):
                    data, ctype = nf
                    self.send_response(404)
                    self.send_header("Content-Type", ctype)
                    self._cors()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json(404, {"ok": False, "error": "Nicht gefunden"}); return
            data, ctype = emb
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────
    def do_GET(self):
        note_request()
        path = urlparse(self.path).path

        if path == "/status":
            self._json(200, {"ok": True, "server": "Sychos Oracle", "version": "5.0.0", "build": "24.09.2026-Final",
                "uptime": int(time.time() - START_TIME), "load": len(request_times),
                "maintenance": maintenance_on()}); return

        if path == "/models":
            u = self._get_user()
            if maintenance_on() and not (u and u.get("is_admin")):
                self._json(503, {"ok": False, "maintenance": True,
                                 "error": "Server derzeit nicht erreichbar"}); return
            plan = effective_plan(u)
            is_boss = bool(u and u.get("is_admin"))
            paid_ok = bool(is_boss or plan != "free")
            self._json(200, {"ok": True, "is_paid": paid_ok, "plan": plan, "load_factor": load_factor(),
                "models": [{"id": k, "name": v["name"], "provider": v["provider"],
                            "factor": v["factor"], "paid": bool(v.get("paid")),
                            "vision": bool(v.get("vision")), "reasoning": bool(v.get("reasoning")),
                            "plan": v.get("plan", "free"),
                            "locked": (not is_boss) and (not plan_ok(plan, v.get("plan", "free")))}
                           for k, v in MODELS.items()],
                "strengths": [{"id": k, "name": v["name"], "cost": v["cost"],
                               "max_tokens": v["max_tokens"]}
                              for k, v in STRENGTHS.items()]}); return

        # User holt seine offenen Warnungen (Admin-Warnsystem)
        if path == "/warnings":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            rows = db.execute("SELECT id,message,created_at FROM warnings WHERE uid=? AND read=0 ORDER BY id",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "warnings": [dict(r) for r in rows]}); return

        # Plaene & Token-Limits (public – wird im Settings-Planpicker angezeigt)
        if path == "/plans":
            self._json(200, {"ok": True, "days": PLAN_DAYS,
                "payments": bool(STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY),
                "mor": {
                    "paddle": {"enabled": bool(PADDLE_CLIENT_TOKEN and PADDLE_PRICES),
                               "token": PADDLE_CLIENT_TOKEN, "prices": PADDLE_PRICES,
                               "links": PADDLE_LINKS},
                    "lemonsqueezy": {"enabled": bool(LEMONSQUEEZY_STORE and LEMONSQUEEZY_VARIANTS),
                                     "store": LEMONSQUEEZY_STORE, "variants": LEMONSQUEEZY_VARIANTS}},
                "min_amount": 0.50,
                "plans": [{"id": pid, "name": PLANS[pid]["name"], "price": PLANS[pid]["price"],
                           "desc": PLANS[pid]["desc"],
                           "limits": {"5h": PLANS[pid]["t5"], "week": PLANS[pid]["tweek"],
                                      "month": PLANS[pid]["tmonth"]}}
                          for pid in PLAN_ORDER]}); return

        if path == "/me":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            plan = effective_plan(user)
            d = {"ok": True, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"], "is_paid": bool(user["is_paid"]),
                 "maintenance": maintenance_on(),
                 "created_at": user.get("created_at", 0),
                 "paid_until": user.get("paid_until", 0) or 0,
                 "plan": plan, "plan_name": PLANS[plan]["name"],
                 "plan_until": user.get("plan_until", 0) or 0,
                 "totp_on": bool(user.get("totp_on")),
                 "usage": usage_state(user["uid"], plan)}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        if path == "/chats":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            db = get_db()
            rows = db.execute("SELECT id,title,model,created_at FROM chats WHERE uid=? ORDER BY created_at DESC",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "chats": [dict(r) for r in rows]}); return

        if path.startswith("/messages/"):
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            cid = path.split("/")[2]
            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            rows = []
            if own:
                rows = db.execute("SELECT role,content,model,cost,created_at,images FROM messages WHERE chat_id=? ORDER BY id",
                                  (cid,)).fetchall()
            db.close()
            if not own:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            out = []
            for r in rows:
                d2 = dict(r)
                try:
                    d2["images"] = json.loads(d2.get("images") or "[]")
                except Exception:
                    d2["images"] = []
                out.append(d2)
            self._json(200, {"ok": True, "messages": out}); return

        # User-eigene Keys: Feature entfernt
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt."}); return

        if path == "/admin/users":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE is_paid=1 AND paid_until>0 AND paid_until<?",
                       (time.time(),))
            db.commit()
            rows = db.execute("SELECT uid,email,display_name,credits,bonus_tokens,is_banned,ban_reason,ban_until,is_admin,is_paid,paid_until,created_at,last_seen FROM users").fetchall()
            db.close()
            now = time.time()
            users = []
            for r in rows:
                u = dict(r)
                u["online"] = (now - u.get("last_seen", 0)) < ONLINE_WINDOW
                u["ban_permanent"] = bool(u.get("is_banned")) and not bool(u.get("ban_until", 0))
                users.append(u)
            self._json(200, {"ok": True, "users": users}); return

        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            res = {"ok": True}
            for prov in ("gemini", "groq", "cline"):
                res[prov + "_key_set"] = bool(resolve_key(db, None, prov))
            db.close()
            self._json(200, res); return

        self._serve_static(path)

    def do_POST(self):
        note_request()
        path = urlparse(self.path).path
        body = self._read_body()
        ip = self.client_address[0] if self.client_address else "?"

        # Register (Start-Tokens) -> Session-Token
        if path == "/register":
            if maintenance_on():
                self._json(403, {"ok": False, "maintenance": True, "error": "Login gesperrt"}); return
            if not rate_ok("reg:" + ip, REGISTER_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Registrierungen. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            name = (body.get("display_name", "").strip() or email.split("@")[0])[:40]
            if not email or len(pw) < 3 or "@" not in email:
                self._json(400, {"ok": False, "error": "Gueltige Email und Passwort (min. 3 Zeichen) benoetigt"}); return
            db = get_db()
            n_ip = db.execute("SELECT COUNT(*) AS n FROM users WHERE reg_ip=?", (ip,)).fetchone()["n"]
            if n_ip >= MAX_ACCOUNTS_PER_IP:
                db.close()
                self._json(429, {"ok": False,
                    "error": "Maximale Anzahl von Accounts (" + str(MAX_ACCOUNTS_PER_IP)
                             + ") pro IP erreicht."}); return
            if db.execute("SELECT uid FROM users WHERE email=?", (email,)).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "Email bereits registriert"}); return
            uid = str(uuid.uuid4())
            token = new_token()
            db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,bonus_tokens,created_at,reg_ip) VALUES (?,?,?,?,?,?,?,?)",
                       (uid, email, hash_pw(pw), name, START_CREDITS, START_TOKENS, time.time(), ip))
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, uid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "token": token, "uid": uid, "display_name": name,
                             "credits": START_CREDITS, "is_admin": 0}); return

        # Login -> Session-Token (auch bei Sperre: Sperr-Modal erscheint im App-Inneren)
        if path == "/login":
            if not rate_ok("login:" + ip, LOGIN_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Versuche. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            # Wartungsmodus ("Server abgeschaltet"): nur Admins duerfen rein,
            # alle anderen sehen im Login-Screen "Login gesperrt"
            if maintenance_on(db) and not (row and (row["is_admin"] or row["email"] == SUPER_ADMIN_EMAIL)):
                db.close()
                self._json(403, {"ok": False, "maintenance": True, "error": "Login gesperrt"}); return
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                # Brute-Force-Bremse pro Konto: zaehlt NUR echte Fehlversuche
                if not rate_ok("pwfail:" + email, PWFAIL_LIMIT, window=PWFAIL_WINDOW):
                    self._json(429, {"ok": False, "error": "Zu viele Fehlversuche fuer dieses Konto. Bitte 10 Minuten warten."}); return
                self._json(401, {"ok": False, "error": "Falsche Anmeldedaten"}); return
            user = dict(row)
            # 2FA: wenn aktiv, muss der Authenticator-Code stimmen
            if user.get("totp_on"):
                code = (body.get("code", "") or "").strip()
                if not totp_ok(user.get("totp_secret") or "", code):
                    db.close()
                    self._json(401, {"ok": False, "need_2fa": True,
                        "error": "2FA aktiv – bitte den 6-stelligen Authenticator-Code eingeben."}); return
            if not user["password_hash"].startswith("pbkdf2$"):
                db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(pw), user["uid"]))
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            token = new_token()
            now = time.time()
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, user["uid"], now))
            db.execute("UPDATE users SET last_login=?, last_seen=? WHERE uid=?", (now, now, user["uid"]))
            db.commit(); db.close()
            d = {"ok": True, "token": token, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"]}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        # Logout: Session invalidieren
        if path == "/logout":
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                db = get_db()
                db.execute("DELETE FROM sessions WHERE token=?", (auth[7:].strip(),))
                db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Eigener API-Key: Feature entfernt (Keys verwaltet ausschliesslich der Admin)
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt. Keys verwaltet der Admin."}); return

        # ── Profil & Einstellungen ─────────────────────────
        # PDF-Export: KI-Text als PDF herunterladen
        if path == "/export/pdf":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            title = (body.get("title", "") or "Sychos Export")[:120]
            content = body.get("content", "") or ""
            if not content.strip():
                self._json(400, {"ok": False, "error": "Kein Inhalt zum Exportieren."}); return
            data = make_pdf(title, content)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="sychos-export.pdf"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return

        # 2FA: Authenticator einrichten (Secret + otpauth-Link fuer die QR-Anzeige)
        if path == "/settings/2fa/setup":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            if user.get("totp_on"):
                self._json(400, {"ok": False, "error": "2FA ist bereits aktiv."}); return
            secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
            db = get_db()
            db.execute("UPDATE users SET totp_secret=? WHERE uid=?", (secret, user["uid"]))
            db.commit(); db.close()
            uri = ("otpauth://totp/Sychos:" + urllib.parse.quote(user["email"]) +
                   "?secret=" + secret + "&issuer=Sychos&digits=6&period=30")
            self._json(200, {"ok": True, "secret": secret, "otpauth": uri}); return

        # 2FA: mit bestaetigtem Code aktivieren
        if path == "/settings/2fa/enable":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            code = (body.get("code", "") or "").strip()
            db = get_db()
            row = db.execute("SELECT totp_secret FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not totp_ok(row["totp_secret"], code):
                db.close()
                self._json(400, {"ok": False, "error": "Code falsch – bitte erneut versuchen."}); return
            db.execute("UPDATE users SET totp_on=1 WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # 2FA: deaktivieren
        if path == "/settings/2fa/disable":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            db.execute("UPDATE users SET totp_on=0, totp_secret='' WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Anzeigename aendern
        if path == "/settings/profile":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            name = (body.get("display_name", "") or "").strip()[:40]
            if not name:
                self._json(400, {"ok": False, "error": "Name darf nicht leer sein."}); return
            db = get_db()
            db.execute("UPDATE users SET display_name=? WHERE uid=?", (name, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "display_name": name}); return

        # E-Mail aendern (Passwort bestaetigen)
        if path == "/settings/email":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            email = (body.get("email", "") or "").strip().lower()
            pw = body.get("password", "")
            if not email or "@" not in email:
                self._json(400, {"ok": False, "error": "Ungueltige E-Mail."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if db.execute("SELECT 1 FROM users WHERE email=? AND uid<>?", (email, user["uid"])).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "E-Mail bereits vergeben."}); return
            db.execute("UPDATE users SET email=? WHERE uid=?", (email, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "email": email}); return

        # Passwort aendern (aktuelles Passwort noetig)
        if path == "/settings/password":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            cur_pw = body.get("current_password", "")
            new_pw = body.get("new_password", "")
            if len(new_pw) < 4:
                self._json(400, {"ok": False, "error": "Neues Passwort: mind. 4 Zeichen."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(cur_pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Aktuelles Passwort falsch."}); return
            db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(new_pw), user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Account endgueltig loeschen (Passwort noetig; Admin-Account ist geschuetzt)
        if path == "/settings/delete":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT password_hash, is_admin FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if row["is_admin"]:
                db.close()
                self._json(403, {"ok": False, "error": "Der Admin-Account kann nicht geloescht werden."}); return
            for r in db.execute("SELECT id FROM chats WHERE uid=?", (user["uid"],)).fetchall():
                db.execute("DELETE FROM messages WHERE chat_id=?", (r["id"],))
            db.execute("DELETE FROM chats WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM sessions WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM user_keys WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM users WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # EIGENER Checkout + echte Abbuchung: PaymentIntent fuer die Kartenabfrage IM eigenen UI
        if path == "/pay/intent":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS or pid == "free":
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            if not (STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY):
                self._json(503, {"ok": False, "error": "Zahlung nicht konfiguriert (Stripe-Keys fehlen)."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                price = round(price * (1 - PROMO_CODES[code]), 2)
            amount = max(50, int(round(price * 100)))   # Stripe-Mindestbetrag 0,50 $
            data = urllib.parse.urlencode({
                "amount": str(amount),
                "currency": "usd",
                "automatic_payment_methods[enabled]": "true",
                "description": "Sychos Plan " + PLANS[pid]["name"] + " – 30 Tage",
                "metadata[uid]": user["uid"],
                "metadata[plan]": pid,
                "metadata[code]": code,
                "receipt_email": user["email"],
            }).encode()
            req = urllib.request.Request("https://api.stripe.com/v1/payment_intents",
                data=data, method="POST",
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                         "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    pi = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            self._json(200, {"ok": True, "client_secret": pi.get("client_secret", ""),
                             "publishable": STRIPE_PUBLISHABLE_KEY, "amount": amount / 100.0}); return

        # Abbuchung bestaetigen -> Plan aktivieren (prueft das ECHTE PaymentIntent bei Stripe)
        if path == "/pay/confirm":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pi_id = (body.get("intent_id", "") or "").strip()
            if not pi_id or not STRIPE_SECRET_KEY:
                self._json(400, {"ok": False, "error": "Keine Zahlung gefunden."}); return
            req = urllib.request.Request(
                "https://api.stripe.com/v1/payment_intents/" + urllib.parse.quote(pi_id),
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    pi = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            if pi.get("status") != "succeeded":
                self._json(402, {"ok": False, "error": "Zahlung wurde noch nicht abgebucht."}); return
            meta = pi.get("metadata") or {}
            if meta.get("uid") != user["uid"]:
                self._json(403, {"ok": False, "error": "Diese Zahlung gehoert zu einem anderen Konto."}); return
            pid = meta.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            price_paid = (pi.get("amount_received") or 0) / 100.0
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, meta.get("code", ""), price_paid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "price_paid": price_paid, "plan_until": until}); return

        # ECHTES Zahlen: Stripe Checkout Session anlegen -> User zahlt auf Stripes sicherer Seite
        if path == "/plan/checkout":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS or pid == "free":
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            if not STRIPE_SECRET_KEY:
                self._json(503, {"ok": False, "error": "Zahlung nicht konfiguriert (STRIPE_SECRET_KEY fehlt)."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                price = round(price * (1 - PROMO_CODES[code]), 2)
            # Stripe verlangt mindestens 0,50 $ – wird transparent an den Kunden ausgewiesen
            amount = max(50, int(round(price * 100)))
            host = self.headers.get("Host", "") or ("localhost:%d" % PORT)
            base = "http://" + host
            data = urllib.parse.urlencode({
                "mode": "payment",
                "client_reference_id": user["uid"],
                "customer_email": user["email"],
                "line_items[0][price_data][currency]": "usd",
                "line_items[0][price_data][unit_amount]": str(amount),
                "line_items[0][price_data][product_data][name]":
                    "Sychos Plan " + PLANS[pid]["name"] + " – 30 Tage",
                "line_items[0][quantity]": "1",
                "success_url": base + "/?paid={CHECKOUT_SESSION_ID}",
                "cancel_url": base + "/?pay=cancel",
                "metadata[uid]": user["uid"],
                "metadata[plan]": pid,
                "metadata[code]": code,
            }).encode()
            req = urllib.request.Request("https://api.stripe.com/v1/checkout/sessions",
                data=data, method="POST",
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                         "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    sess = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            self._json(200, {"ok": True, "url": sess.get("url", ""),
                             "session_id": sess.get("id", ""), "amount": amount / 100.0}); return

        # ECHTES Zahlen bestaetigen: Stripe pruefen, dann Plan aktivieren
        if path == "/plan/confirm":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            sid = (body.get("session_id", "") or "").strip()
            if not sid or not STRIPE_SECRET_KEY:
                self._json(400, {"ok": False, "error": "Keine Zahlungs-Sitzung."}); return
            req = urllib.request.Request(
                "https://api.stripe.com/v1/checkout/sessions/" + urllib.parse.quote(sid),
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    sess = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            if sess.get("payment_status") != "paid":
                self._json(402, {"ok": False, "error": "Zahlung wurde noch nicht abgebucht."}); return
            meta = sess.get("metadata") or {}
            if meta.get("uid") != user["uid"]:
                self._json(403, {"ok": False, "error": "Diese Zahlung gehoert zu einem anderen Konto."}); return
            pid = meta.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            price_paid = (sess.get("amount_total") or 0) / 100.0
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, meta.get("code", ""), price_paid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "price_paid": price_paid, "plan_until": until}); return

        # ── Merchant-of-Record Webhooks: Paddle / Lemon Squeezy buchen ab UND
        #    fuehren alle Steuern ab – hier wird der Plan nach Zahlung freigeschaltet.
        if path in ("/webhook/paddle", "/webhook/lemonsqueezy"):
            raw = getattr(self, "_raw_body", b"") or b""
            ok_sig = False
            if path == "/webhook/paddle" and PADDLE_WEBHOOK_SECRET:
                sig = self.headers.get("Paddle-Signature", "")
                parts = dict(p.split("=", 1) for p in sig.split(";") if "=" in p)
                ts = parts.get("ts", "")
                h1 = parts.get("h1") or parts.get("hmac", "")
                if ts and h1:
                    msg = (ts + ":" + raw.decode("utf-8", "replace")).encode()
                    # Secret mit/ohne abschliessenden Schrägstrich akzeptieren
                    for secret in {PADDLE_WEBHOOK_SECRET, PADDLE_WEBHOOK_SECRET.strip().rstrip("/"),
                                   PADDLE_WEBHOOK_SECRET.strip() + "/"}:
                        if secret and hmac.compare_digest(
                                hmac.new(secret.encode(), msg, hashlib.sha1).hexdigest(), h1):
                            ok_sig = True
                            break
            elif path == "/webhook/lemonsqueezy" and LEMONSQUEEZY_WEBHOOK_SECRET:
                sig = (self.headers.get("X-Signature", "") or "").replace("sha256=", "")
                calc = hmac.new(LEMONSQUEEZY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
                ok_sig = hmac.compare_digest(calc, sig)
            if not ok_sig:
                self._json(401, {"ok": False, "error": "Ungueltige Signatur."}); return
            try:
                payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                payload = {}
            custom, amt = {}, None
            if path == "/webhook/paddle":
                data = payload.get("data") or {}
                custom = data.get("custom_data") or payload.get("custom_data") or {}
                amt = (data.get("details", {}).get("totals", {}) or {}).get("total") \
                    or payload.get("amount_gross")
            else:
                attrs = (payload.get("data") or {}).get("attributes") or {}
                custom = (attrs.get("checkout_data", {}) or {}).get("custom") \
                    or (payload.get("meta", {}) or {}).get("custom_data") or {}
                amt = attrs.get("total")
            uid, pid = str(custom.get("uid", "")), custom.get("plan", "")
            code = str(custom.get("code", "") or "")
            if uid and pid in PLANS:
                try:
                    price_paid = round(float(amt or 0) / 100.0, 2)
                except Exception:
                    price_paid = PLANS[pid]["price"]
                until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
                db = get_db()
                if db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
                    db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                               (pid, until, 1 if pid != "free" else 0, until, uid))
                    db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                               (uid, pid, code, price_paid, time.time()))
                    db.commit()
                db.close()
            self._json(200, {"ok": True}); return

        # ── Persoenlicher API-Key (wie bei Free-Anbietern) mit Plan-Rate-Limit ──
        if path == "/settings/apikey":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            action = (body.get("action") or "list").lower()
            db = get_db()
            if action == "revoke":
                db.execute("UPDATE api_keys SET revoked=1 WHERE uid=?", (user["uid"],))
                db.commit(); db.close()
                self._json(200, {"ok": True, "key": ""}); return
            row = db.execute("SELECT key FROM api_keys WHERE uid=? AND revoked=0 ORDER BY id DESC",
                             (user["uid"],)).fetchone()
            if action == "create" and not row:
                key = "sk-sychos-" + uuid.uuid4().hex + uuid.uuid4().hex[:12]
                db.execute("INSERT INTO api_keys (uid,key,created_at) VALUES (?,?,?)",
                           (user["uid"], key, time.time()))
                db.commit()
                row = db.execute("SELECT key FROM api_keys WHERE uid=? AND revoked=0 ORDER BY id DESC",
                                 (user["uid"],)).fetchone()
            db.close()
            self._json(200, {"ok": True, "key": (row["key"] if row else "")}); return

        # ── Oeffentliche KI-API: POST /api/chat mit persoenlichem Key ──
        # { "prompt": "...", "model": "gemini-3.6-flash", "strength": "medium" }
        if path == "/api/chat":
            auth = self.headers.get("Authorization", "")
            key = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if not key.startswith("sk-sychos-"):
                self._json(401, {"ok": False, "error": "Persoenlichen API-Key senden: Authorization: Bearer sk-sychos-..."}); return
            db = get_db()
            krow = db.execute("SELECT uid FROM api_keys WHERE key=? AND revoked=0", (key,)).fetchone()
            urow = db.execute("SELECT * FROM users WHERE uid=?", (krow["uid"],)).fetchone() if krow else None
            akey = resolve_key(db, krow["uid"], MODELS.get(body.get("model", ""), {}).get("provider", "gemini")) if krow else ""
            db.close()
            if not urow:
                self._json(401, {"ok": False, "error": "API-Key ungueltig oder widerrufen."}); return
            user = dict(urow)
            plan = effective_plan(user)
            lim = API_RATE_PER_MIN.get(plan, 5)
            if not rate_ok("apikey:" + key, lim):
                self._json(429, {"ok": False, "error": "API-Rate-Limit erreicht: %d Anfragen/Minute (Plan %s)."
                                 % (lim, PLANS.get(plan, {}).get("name", plan))}); return
            model = body.get("model", "gemini-3.6-flash")
            if model not in MODELS:
                self._json(400, {"ok": False, "error": "Unbekanntes Modell."}); return
            need = MODELS[model].get("plan", "free")
            if not user.get("is_admin") and not plan_ok(plan, need):
                self._json(402, {"ok": False, "error": "Modell benoetigt den %s-Plan (oder hoeher)."
                                 % PLANS.get(need, {}).get("name", need)}); return
            prompt = (body.get("prompt") or body.get("message") or "").strip()
            if not prompt:
                self._json(400, {"ok": False, "error": "Prompt fehlt (Feld 'prompt')."}); return
            if len(prompt) > MAX_MSG_LEN:
                self._json(400, {"ok": False, "error": "Prompt zu lang (max. %d Zeichen)." % MAX_MSG_LEN}); return
            msgs = [{"role": "user", "content": prompt}]
            r = call_provider(msgs, akey, body.get("strength", "medium"), model)
            if not r.get("ok"):
                self._json(502, r); return
            used = max(1, (len(prompt) + len(r.get("text", ""))) // 4)
            add_usage(user["uid"], used)
            self._json(200, {"ok": True, "model": model, "plan": plan, "text": r.get("text", ""),
                             "tokens": r.get("tokens", used)}); return

        # Plan kaufen (Test-Checkout ohne echte Abbuchung) -> 30 Tage aktiv
        # Rabattcode: 'Release' = -20 % (in PROMO_CODES)
        if path == "/plan/buy":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            discount = 0.0
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                discount = PROMO_CODES[code]
                price = round(price * (1 - discount), 2)
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, code, price, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "plan_until": until, "price_paid": price,
                             "discount": discount, "code": code}); return

        # ── Admin-Warnsystem ──────────────────────────────
        # Admin sendet eine Warn-Nachricht an einen User (Popup im Hub)
        if path == "/admin/warn":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            msg = (body.get("message", "") or "").strip()[:500]
            if not msg:
                self._json(400, {"ok": False, "error": "Warnung ohne Text."}); return
            db = get_db()
            if not db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            db.execute("INSERT INTO warnings (uid,message,created_at) VALUES (?,?,?)",
                       (uid, msg, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # User holt seine offenen Warnungen
        if path == "/warnings":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            rows = db.execute("SELECT id,message,created_at FROM warnings WHERE uid=? AND read=0 ORDER BY id",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "warnings": [dict(r) for r in rows]}); return

        # Warnungen als gelesen markieren (ohne id = alle)
        if path == "/warnings/read":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            wid = body.get("id", 0)
            db = get_db()
            if wid:
                db.execute("UPDATE warnings SET read=1 WHERE id=? AND uid=?", (wid, user["uid"]))
            else:
                db.execute("UPDATE warnings SET read=1 WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Neuer Chat
        if path == "/chat/new":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = str(uuid.uuid4())
            title = (body.get("title", "Neuer Chat") or "Neuer Chat")[:MAX_TITLE_LEN]
            model = body.get("model", "")
            if model not in MODELS:
                model = "gemini-3.6-flash"
            db = get_db()
            db.execute("INSERT INTO chats (id,uid,title,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, user["uid"], title, model, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "chat_id": cid, "title": title, "model": model}); return

        # Chat umbenennen
        if path == "/chat/rename":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            title = (body.get("title", "") or "").strip()[:MAX_TITLE_LEN] or "Neuer Chat"
            db = get_db()
            cur = db.execute("UPDATE chats SET title=? WHERE id=? AND uid=?",
                             (title, cid, user["uid"]))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            self._json(200, {"ok": True, "chat_id": cid, "title": title}); return

        # Chat loeschen
        if path == "/chat/delete":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            db = get_db()
            db.execute("DELETE FROM messages WHERE chat_id=? AND chat_id IN (SELECT id FROM chats WHERE uid=?)",
                       (cid, user["uid"]))
            db.execute("DELETE FROM chats WHERE id=? AND uid=?", (cid, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # AI-Chat senden: Credits atomar reservieren (kein Doppelabzug bei Parallel-Requests)
        if path == "/chat/send":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            if maintenance_on() and not user.get("is_admin"):
                self._json(503, {"ok": False, "maintenance": True,
                                 "error": "Server derzeit nicht erreichbar"}); return
            if not rate_ok("send:" + user["uid"], SEND_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Nachrichten. Bitte kurz warten."}); return
            cid = body.get("chat_id", "")
            msg = (body.get("message", "") or "").strip()
            model = body.get("model", "")
            strength = body.get("strength", "medium")
            if model not in MODELS:
                self._json(400, {"ok": False, "error": "Unbekanntes Modell"}); return
            plan = effective_plan(user)
            need = MODELS[model].get("plan", "free")
            if not user.get("is_admin") and not plan_ok(plan, need):
                self._json(402, {"ok": False, "error_type": "premium_locked",
                    "error": "Das Modell '%s' benoetigt den %s-Plan (oder hoeher). Bitte unter Einstellungen -> Plan freischalten."
                             % (MODELS[model]["name"], PLANS.get(need, {}).get("name", need))}); return

            # Bilder (Vision): max. 3, nur an Modelle mit vision-Flag
            images = []
            for im in (body.get("images") or [])[:3]:
                try:
                    data = (im.get("data") or "").strip()
                    mime = (im.get("mime") or "image/png").strip().lower()
                except AttributeError:
                    continue
                if not data or len(data) > 4200000 or mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    self._json(400, {"ok": False, "error": "Bild ungueltig (PNG/JPEG/WEBP/GIF, max. 3 MB)."}); return
                images.append({"type": "image", "mime": mime, "data": data})
            if images and not MODELS[model].get("vision"):
                self._json(400, {"ok": False, "error_type": "no_vision",
                    "error": "Dieses Modell unterstuetzt keine Bilder. Bitte ein Modell mit dem Bild-Symbol \U0001F5BC waehlen (z. B. Gemini 3.6 Flash)."}); return
            if strength not in STRENGTHS:
                strength = "medium"
            if not msg:
                self._json(400, {"ok": False, "error": "Leere Nachricht"}); return
            if len(msg) > MAX_MSG_LEN:
                self._json(400, {"ok": False, "error": "Nachricht zu lang (max. %d Zeichen)" % MAX_MSG_LEN}); return

            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            if not own:
                db.close()
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            rows = db.execute("SELECT role,content,images FROM messages WHERE chat_id=? ORDER BY id",
                              (cid,)).fetchall()
            history = []
            for r in rows:
                content = r["content"]
                try:
                    rimgs = json.loads(r["images"] or "[]")
                except Exception:
                    rimgs = []
                if rimgs:
                    content = [{"type": "text", "text": r["content"]}] + rimgs
                history.append({"role": r["role"], "content": content})
            history.append({"role": "user", "content": ([{"type": "text", "text": msg}] + images) if images else msg})
            # Identitaet: bei Modellwechsel (z.B. GLM -> DeepSeek) antwortet die KI
            # ab jetzt IMMER mit dem AKTUELL gewaehlten Modell – nie mehr mit dem alten.
            ident = ("[System-Hinweis: Du bist 'Sychos'. Dein aktuelles Modell ist "
                     + MODELS[model]["name"] + ". Wenn gefragt, antworte genau so. Behaupte NIEMALS, "
                     "ein anderes Modell oder eine andere KI zu sein (z.B. GLM, DeepSeek, Gemini, ChatGPT), "
                     "auch wenn im Chatverlauf anderes steht.]")
            if isinstance(history[-1]["content"], str):
                history[-1]["content"] += "\n\n" + ident
            else:
                history[-1]["content"][0]["text"] += "\n\n" + ident

            # Token-Limits (5h / Woche / Monat) wie Cline: bei Erreichen bis zum Reset warten
            est = est_tokens(history)
            lim = limit_block(user["uid"], plan, est)
            if lim:
                win, wstate, _ = lim
                db.close()
                self._json(429, {"ok": False, "error_type": "limit_reached",
                    "error": "%s-Limit erreicht (Plan %s). Weiter in %s – oder Plan upgraden."
                             % (TOKEN_WINDOWS[win]["label"], PLANS[plan]["name"],
                                fmt_wait(wstate["resets_at"] - time.time())),
                    "limit_win": win, "resets_at": wstate["resets_at"]}); return

            db.execute("INSERT INTO messages (chat_id,role,content,model,images,created_at) VALUES (?,?,?,?,?,?)",
                       (cid, "user", msg, model, json.dumps(images), time.time()))
            web_used = False
            if body.get("web"):
                hits = web_search(msg)
                if hits:
                    ctx = "\n\n[Aktuelle Web-Recherche als Kontext]\n" + "\n".join(hits)
                    if isinstance(history[-1]["content"], str):
                        history[-1]["content"] += ctx
                    else:
                        history[-1]["content"][0]["text"] += ctx
                web_used = True
            api_key = resolve_key(db, user["uid"], MODELS[model]["provider"])
            db.commit(); db.close()

            # SSE-Stream: Tokens live an das Frontend senden
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self._cors()
            self.end_headers()

            def push(evt, data):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (evt, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()

            full, tokens, err = [], 0, None
            for ev in stream_provider(history, api_key, strength, model):
                if ev["type"] == "delta":
                    full.append(ev["text"])
                    push("delta", {"t": ev["text"]})
                elif ev["type"] == "think":
                    push("think", {"t": ev["text"]})
                elif ev["type"] == "end":
                    tokens = ev.get("tokens", 0)
                elif ev["type"] == "error":
                    err = ev
            text = "".join(full)
            if err or not text.strip():
                e2 = err or {"error": "Leere Antwort – bitte erneut versuchen.", "error_type": "api"}
                push("error", {"error": e2["error"], "error_type": e2.get("error_type", "api")})
                return
            # Token-Abrechnung (Input + Output) auf alle drei Limit-Fenster
            in_tok = int(sum((len(m.get("content")) if isinstance(m.get("content"), str)
                              else sum(len(p.get("text") or "") + (1800 if p.get("type") == "image" else 0)
                                       for p in (m.get("content") or [])))
                             for m in history) / 4)
            out_tok = tokens if tokens else max(1, len(text) // 4)
            used = max(1, in_tok + out_tok)
            add_usage(user["uid"], used)
            db = get_db()
            db.execute("INSERT INTO messages (chat_id,role,content,model,tokens,cost,created_at) VALUES (?,?,?,?,?,?,?)",
                       (cid, "assistant", text, model, used, 0, time.time()))
            db.commit(); db.close()
            push("done", {"text": text, "web": web_used, "tokens_used": used,
                          "usage": usage_state(user["uid"], plan)})
            return

        # ── Admin ─────────────────────────────────────────
        if path == "/admin/tokens":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            try:
                amount = float(body.get("tokens", 0))
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "Ungueltige Menge"}); return
            if amount == 0 or abs(amount) > 10 ** 9:
                self._json(400, {"ok": False, "error": "Menge ungueltig"}); return
            db = get_db()
            cur = db.execute("UPDATE users SET bonus_tokens = MAX(0, COALESCE(bonus_tokens,0) + ?) WHERE uid=?",
                             (amount, uid))
            row = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "uid": uid, "bonus_tokens": (row["bonus_tokens"] if row else 0)}); return

        # Admin: Account zuruecksetzen -> Free-Plan + Standard-Credits/-Tokens (Usage wird geleert)
        if path == "/admin/reset":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            db = get_db()
            cur = db.execute("UPDATE users SET plan='free', plan_until=0, is_paid=0, paid_until=0, "
                             "credits=?, bonus_tokens=? WHERE uid=?",
                             (START_CREDITS, START_TOKENS, uid))
            db.execute("DELETE FROM usage WHERE uid=?", (uid,))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "plan": "free", "credits": START_CREDITS,
                             "bonus_tokens": START_TOKENS}); return

        # Admin-Rechte vergeben/entziehen – AUSSCHLIESSLICH der Haupt-Admin (admin@sychos.net).
        # Delegierte Admins duerfen das NICHT; der Haupt-Admin ist selbst geschuetzt.
        if path == "/admin/setadmin":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            boss = self._get_user()
            if not boss or boss.get("email") != SUPER_ADMIN_EMAIL:
                self._json(403, {"ok": False, "error": "Nur der Haupt-Admin darf Admin-Rechte vergeben oder entziehen."}); return
            uid = body.get("uid", "")
            make = bool(body.get("admin", True))
            db = get_db()
            row = db.execute("SELECT is_admin, email FROM users WHERE uid=?", (uid,)).fetchone()
            if not row:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            if row["email"] == SUPER_ADMIN_EMAIL:
                db.close()
                self._json(400, {"ok": False, "error": "Der Haupt-Admin ist geschuetzt."}); return
            if not make and row["is_admin"]:
                n = db.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1").fetchone()["n"]
                if n <= 1:
                    db.close()
                    self._json(400, {"ok": False, "error": "Der letzte Admin kann nicht entzogen werden."}); return
            db.execute("UPDATE users SET is_admin=? WHERE uid=?", (1 if make else 0, uid))
            if not make:
                db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
            db.commit(); db.close()
            self._json(200, {"ok": True, "is_admin": bool(make)}); return

        # User sperren/entsperren: Grund + Dauer (Minuten, 0 = dauerhaft)
        if path == "/admin/ban":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            ban = 1 if body.get("ban", True) else 0
            reason = (body.get("reason", "") or "").strip()[:200]
            try:
                duration = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                duration = 0
            until = (time.time() + duration * 60) if duration else 0
            db = get_db()
            cur = db.execute(
                "UPDATE users SET is_banned=?, ban_reason=?, ban_until=? WHERE uid=?",
                (ban, reason if ban else "", until if ban else 0, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "banned": bool(ban), "ban_until": until if ban else 0}); return

        # Admin vergibt "Sychos Paid" -> entsperrt Premium-Modelle
        if path == "/admin/paid":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            # paid: true/1 -> geben, false/0/"nein" -> WEGNEHMEN
            raw = body.get("paid", True)
            raw = body.get("paid", True)
            # Plan-Auswahl: 'plan' = free/basic/pro/max/ultra/business (ohne 'plan' alt: paid -> pro)
            pid = (body.get("plan") or "").strip().lower()
            if pid not in PLANS:
                pid = "pro" if not (raw is False or raw == 0 or str(raw).strip().lower() in ("0", "false", "nein", "weg", "off")) else "free"
            # Dauer in Minuten: 0 = dauerhaft, sonst zeitlich begrenzt (laeuft automatisch ab)
            try:
                dur = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                dur = 0
            until = (time.time() + dur * 60) if (pid != "free" and dur) else 0
            paid = 0 if pid == "free" else 1
            db = get_db()
            cur = db.execute("UPDATE users SET is_paid=?, paid_until=?, plan=?, plan_until=? WHERE uid=?",
                             (paid, until, pid, until, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "paid": bool(paid), "paid_until": until, "plan": pid,
                             "plan_name": PLANS[pid]["name"], "plan_until": until}); return

        # Admin: globale Provider-Keys setzen (leerer Key = entfernen)
        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            for prov in ("gemini", "groq", "cline"):
                if prov + "_api_key" in body:
                    val = (body[prov + "_api_key"] or "").strip()[:200]
                    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                               (prov + "_api_key", val))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Server abschalten/an (Wartungsmodus) – AUSSCHLIESSLICH Haupt-Admin (admin@sychos.net).
        # Der Server bleibt technisch laufen: normale User sehen "Login gesperrt" bzw.
        # "Server derzeit nicht erreichbar" (Modelle + Chat aus), Admins arbeiten normal weiter.
        if path == "/admin/server":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            boss = self._get_user()
            if not boss or boss.get("email") != SUPER_ADMIN_EMAIL:
                self._json(403, {"ok": False, "error": "Nur admin@sychos.net darf den Server abschalten."}); return
            online = bool(body.get("online", True))
            set_maintenance(not online)
            self._json(200, {"ok": True, "online": online, "maintenance": not online}); return

        # Admin setzt das Passwort eines Users (alle Sessions des Users werden beendet).
        if path == "/admin/setpw":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            actor = self._get_user()
            uid = body.get("uid", "")
            new_pw = body.get("password", "")
            if len(new_pw) < 4:
                self._json(400, {"ok": False, "error": "Neues Passwort: mind. 4 Zeichen."}); return
            db = get_db()
            row = db.execute("SELECT email FROM users WHERE uid=?", (uid,)).fetchone()
            if not row:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            if row["email"] == SUPER_ADMIN_EMAIL and (not actor or actor.get("email") != SUPER_ADMIN_EMAIL):
                db.close()
                self._json(403, {"ok": False, "error": "Das Passwort des Haupt-Admins darf nur er selbst setzen."}); return
            db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(new_pw), uid))
            db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
            db.commit(); db.close()
            self._json(200, {"ok": True, "uid": uid}); return

def write_api_keys_to_db():
    """Traegt die Provider-API-Keys DIREKT in die Datenbank (settings) ein.

    Nutzung:
      python oracle_server.py --set-keys
          -> eingebaute bzw. per Env uebergebene Keys in die DB schreiben
      python oracle_server.py --set-keys GEMINI=xxx GROQ=yyy CLINE=zzz
          -> eigene Keys eintragen (leerer Wert = entfernen)
    Danach wird der Server automatisch gestartet (--no-start = nur in die DB schreiben).
    """
    custom = {}
    for a in sys.argv[1:]:
        if "=" in a and not a.startswith("-"):
            k, v = a.split("=", 1)
            custom[k.strip().upper()] = v
    keys = {
        "gemini": custom.get("GEMINI", GEMINI_API_KEY),
        "groq": custom.get("GROQ", GROQ_API_KEY),
        "cline": custom.get("CLINE", CLINE_API_KEY),
    }
    init_db()
    db = get_db()
    print("\n  API-Keys direkt in die DB schreiben (" + DB_PATH + "):")
    for prov, val in keys.items():
        val = (val or "").strip()
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                   (prov + "_api_key", val))
        print("    [OK] %-6s -> %s" % (prov.capitalize(), mask_key(val) if val else "(leer = entfernt)"))
    db.commit(); db.close()
    print("  Fertig. Keys sind sofort aktiv (Admin-Panel zeigt 'hinterlegt').")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    init_db()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("\n  ========================================")
    print("     S Y C H O S   O R A C L E  v5.0     ")
    print("  ========================================")
    print(f"  Hub   : http://localhost:{PORT}")
    print(f"  Admin : http://localhost:{PORT}/admin/")
    print("  Domains: " + " / ".join(SYCHOS_DOMAINS))
    print(f"  DB    : {DB_PATH}")
    print("  ----------------------------------------")
    print("  ========================================\n")
    print("  -> Ctrl+C zum Beenden\n")
    # Always-On: selbstheilend + Port-Konflikte loesen (alter Sychos-Server)
    attempts = 0
    while True:
        try:
            server = HTTPServerCls((HOST, PORT), Handler)
        except OSError as e:
            err = getattr(e, "errno", 0)
            if err in (98, 48, 10048) and attempts < 2:
                attempts += 1
                # 1) zuerst alten Sychos-Server raeumen (wichtigster Schritt!)
                if free_port():
                    print("  Alter Sychos-Server beendet -> Port %d ist wieder frei..." % PORT)
                    continue
                # 2) erst danach pruefen, ob schon eine laufende Instanz existiert
                if _service_active():
                    print("\n  Laeuft bereits als Always-On Service (sychos-hub).")
                    print("  -> http://localhost:%d  (systemctl status sychos-hub)" % PORT)
                    return
                print("\n  Port %d ist belegt! Anderen Port waehlen:" % PORT)
                print("    Windows: set ORACLE_PORT=7778 && python oracle_server.py")
                print("    Linux:   ORACLE_PORT=7778 python3 oracle_server.py")
                sys.exit(1)
            raise
        attempts = 0
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server gestoppt.")
            server.server_close()
            break
        except Exception as e:
            print("  Fehler -> Neustart in 5s:", e)
            time.sleep(5)

def install_autostart():
    """Always-On: Windows = Autostart-Link, Linux = systemd (mit sudo)."""
    script = os.path.abspath(__file__)
    if os.name == "nt":
        import subprocess
        py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(py):
            py = sys.executable
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
              "$s.TargetPath='%s';$s.Arguments='\"%s\"';$s.WindowStyle=7;$s.Save()"
              % (lnk.replace("'", "''"), py, script))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print("  [OK] Autostart installiert (startet bei jedem Windows-Login):")
        print("       " + lnk)
    else:
        if os.geteuid() != 0:
            print("  Linux: Bitte mit sudo starten fuer systemd-Autostart:")
            print("         sudo python3 oracle_server.py --install")
            return
        unit = ("[Unit]\nDescription=Sychos Hub (Always-On)\nAfter=network.target\n\n"
                "[Service]\nType=simple\nExecStart=%s %s\nRestart=always\nRestartSec=5\n"
                "Environment=ORACLE_HOST=0.0.0.0\nEnvironment=ORACLE_PORT=%d\n\n"
                "[Install]\nWantedBy=multi-user.target\n"
                % (sys.executable, script, PORT))
        with open("/etc/systemd/system/sychos-hub.service", "w") as f:
            f.write(unit)
        os.system("systemctl daemon-reload")
        os.system("systemctl enable sychos-hub >/dev/null 2>&1")
        os.system("systemctl restart sychos-hub")
        print("  [OK] systemd-Service installiert (Always-On): sychos-hub")
        print("       -> laeuft bereits im Hintergrund, kein extra Start noetig")
        return True
    return False

def _service_active():
    """True, wenn der eigene systemd-Service sychos-hub bereits laeuft."""
    if os.name == "nt":
        return False
    return os.system("systemctl is-active --quiet sychos-hub >/dev/null 2>&1") == 0

def free_port():
    """Beendet alte Sychos-Prozesse (v2 / Sychos Net) auf dem Port — nie sich selbst."""
    me = os.getpid()
    me_path = os.path.abspath(__file__)
    pat = ("sychos-oracle", "SychosOracle")
    killed = []
    try:
        if os.name == "nt":
            import subprocess
            res = subprocess.run(["powershell", "-NoProfile", "-Command",
                "(Get-NetTCPConnection -LocalPort %d -State Listen).OwningProcess" % PORT],
                capture_output=True, text=True).stdout
            pids = set(x.strip() for x in res.splitlines() if x.strip())
            for pid in pids:
                if pid == str(me):
                    continue
                info = subprocess.run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid],
                    capture_output=True, text=True).stdout
                if any(p in info for p in pat) or (
                        "oracle_server" in info and me_path not in info and "sychos-hub" not in info):
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    killed.append(pid)
        else:
            # WICHTIG: alten systemd-Service (v2) stoppen, sonst startet er durch
            # Restart=always sofort wieder und beide streiten um den Port
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.system("systemctl stop sychos-oracle >/dev/null 2>&1")
                os.system("systemctl disable sychos-oracle >/dev/null 2>&1")
            for pid in os.listdir("/proc"):
                if not pid.isdigit() or int(pid) == me:
                    continue
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except Exception:
                    continue
                if any(p in cmd for p in pat) or (
                        "oracle_server" in cmd and me_path not in cmd and "sychos-hub" not in cmd):
                    try:
                        os.kill(int(pid), 15)
                        killed.append(pid)
                    except Exception:
                        pass
    except Exception:
        pass
    if killed:
        time.sleep(1.5)
    # Erfolg = auf dem Port lauscht nichts mehr
    import socket as _sock
    try:
        c = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        c.settimeout(1)
        c.connect(("127.0.0.1", PORT))
        c.close()
        return False
    except Exception:
        return True

def uninstall_autostart():
    if os.name == "nt":
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        if os.path.isfile(lnk):
            os.remove(lnk)
        print("  [OK] Autostart entfernt.")
    else:
        os.system("systemctl disable --now sychos-hub >/dev/null 2>&1")
        if os.path.isfile("/etc/systemd/system/sychos-hub.service"):
            os.remove("/etc/systemd/system/sychos-hub.service")
        print("  [OK] systemd-Service entfernt.")

if __name__ == "__main__":
    if "--set-keys" in sys.argv:
        write_api_keys_to_db()
        if "--no-start" in sys.argv:
            sys.exit(0)
        print("  -> Server wird gestartet...\n")
    if "--install" in sys.argv:
        if install_autostart():
            sys.exit(0)      # Linux: Service laeuft bereits -> nicht doppelt starten
    elif "--uninstall" in sys.argv:
        uninstall_autostart()
        sys.exit(0)
    main()
