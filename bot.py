"""
Tennis Injury Bot — Pre-match injury alerts
ATP / WTA / Challenger
"""

import asyncio
import sqlite3
import logging
import hashlib
import time
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── CONFIGURACIÓN — CAMBIA ESTOS DOS VALORES ────────────────────────────────
TELEGRAM_TOKEN = "8883681354:AAEssfeuVrW5hPOpTKrMWW4HTwJJoVXnY_A"       # token de BotFather
CHAT_ID        = "7895672167"     # tu número de ID
CHECK_INTERVAL = 30                         # revisar cada 45 minutos
DB_PATH        = "lesiones.db"
FECHA_INICIO   = datetime(2026, 5, 28, tzinfo=timezone.utc)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── KEYWORDS ────────────────────────────────────────────────────────────────
KEYWORDS = [
    "lesión","lesionado","lesionada","baja","retira","abandona",
    "no jugará","descartado","descartada","se baja","fuera de",
    "dolor","molestias","se pierde","no participará",
    "injury","injured","withdrawal","withdraws","pulls out",
    "retires","out of","won't play","unable to play","scratch",
    "scratches","muscle","ankle","wrist","back injury","hip",
    "knee","illness","sick","doubtful","late withdrawal","MTO",
]

# ─── PERIODISTAS EN X (via nitter RSS) ───────────────────────────────────────
NITTER_BASE = "https://nitter.privacydev.net"

JOURNALISTS = [
    {"user": "josemorgado",    "name": "José Morgado (ATP insider)"},
    {"user": "BenRothenberg",  "name": "Ben Rothenberg (NYT Tennis)"},
    {"user": "Tumaini_C",      "name": "Tumaini Carayol (The Guardian)"},
    {"user": "BastienFachan",  "name": "Bastien Fachan (L'Equipe)"},
    {"user": "scambers73",     "name": "Simon Cambers (Tennis journalist)"},
    {"user": "atptour",        "name": "ATP Tour (oficial)"},
    {"user": "WTA",            "name": "WTA (oficial)"},
]

# ─── FUENTES RSS ─────────────────────────────────────────────────────────────
RSS_SOURCES = [
    {
        "name": "Google News EN — Tennis Injury",
        "url": "https://news.google.com/rss/search?q=tennis+injury+withdrawal+ATP+WTA+2026&hl=en&gl=US&ceid=US:en",
        "tipo": "noticia",
    },
    {
        "name": "Google News ES — Tenis Lesión",
        "url": "https://news.google.com/rss/search?q=tenis+lesion+baja+retiro+ATP+WTA&hl=es&gl=ES&ceid=ES:es",
        "tipo": "noticia",
    },
    {
        "name": "Tennis Majors",
        "url": "https://www.tennismajors.com/feed",
        "tipo": "noticia",
    },
    {
        "name": "Reddit r/tennis",
        "url": "https://www.reddit.com/r/tennis/search.rss?q=injury+withdrawal&sort=new&restrict_sr=1",
        "tipo": "comunidad",
    },
]

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enviadas (
            hash TEXT PRIMARY KEY,
            titulo TEXT,
            enviado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def ya_enviada(h):
    conn = sqlite3.connect(DB_PATH)
    r = conn.execute("SELECT 1 FROM enviadas WHERE hash=?", (h,)).fetchone()
    conn.close()
    return r is not None

def marcar_enviada(h, titulo):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO enviadas (hash, titulo) VALUES (?,?)", (h, titulo))
    conn.commit()
    conn.close()

def get_ultimas_enviadas():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT titulo FROM enviadas ORDER BY enviado_en DESC LIMIT 10").fetchall()
    conn.close()
    return [r[0] for r in rows]

# ─── UTILIDADES ───────────────────────────────────────────────────────────────
def hash_noticia(titulo, link):
    return hashlib.md5(f"{titulo}{link}".encode()).hexdigest()

def contiene_keyword(texto):
    t = texto.lower()
    return any(kw.lower() in t for kw in KEYWORDS)

def limpiar_html(html):
    return BeautifulSoup(html, "html.parser").get_text(separator=" ").strip()[:300]

def es_reciente(entry):
    pp = entry.get("published_parsed")
    if not pp:
        return False
    try:
        dt = datetime.fromtimestamp(time.mktime(pp), tz=timezone.utc)
        return dt >= FECHA_INICIO
    except Exception:
        return False

def credibilidad(tipo, fuente):
    if "oficial" in fuente.lower() or "ATP Tour" in fuente or fuente == "WTA (oficial)":
        return "🔴 OFICIAL"
    if tipo == "periodista":
        return "🟠 PERIODISTA"
    if tipo == "comunidad":
        return "🟡 COMUNIDAD"
    return "🔵 NOTICIA"

def formato_mensaje(titulo, resumen, fuente, link, fecha, nivel):
    return (
        f"{nivel}\n\n"
        f"📰 *{titulo}*\n\n"
        f"📝 {resumen}\n\n"
        f"📅 {fecha}\n"
        f"🔗 [Ver fuente]({link})\n"
        f"📡 _{fuente}_"
    )

# ─── SCRAPING ─────────────────────────────────────────────────────────────────
def parse_journalists():
    noticias = []
    for j in JOURNALISTS:
        url = f"{NITTER_BASE}/{j['user']}/rss"
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                titulo  = entry.get("title", "")
                resumen = limpiar_html(entry.get("summary", titulo))
                link    = entry.get("link", "")
                fecha   = entry.get("published", "")
                if not es_reciente(entry):
                    continue
                if not contiene_keyword(f"{titulo} {resumen}"):
                    continue
                noticias.append({
                    "titulo": titulo[:200], "resumen": resumen,
                    "link": link, "fecha": fecha,
                    "fuente": j["name"], "tipo": "periodista",
                })
            log.info(f"[X] @{j['user']} ✓")
        except Exception as e:
            log.error(f"[X] Error @{j['user']}: {e}")
    return noticias

def parse_rss():
    noticias = []
    for s in RSS_SOURCES:
        try:
            feed = feedparser.parse(s["url"])
            for entry in feed.entries:
                titulo  = entry.get("title", "")
                resumen = limpiar_html(entry.get("summary", ""))
                link    = entry.get("link", "")
                fecha   = entry.get("published", "")
                if not es_reciente(entry):
                    continue
                if not contiene_keyword(f"{titulo} {resumen}"):
                    continue
                noticias.append({
                    "titulo": titulo[:200], "resumen": resumen,
                    "link": link, "fecha": fecha,
                    "fuente": s["name"], "tipo": s["tipo"],
                })
            log.info(f"[RSS] {s['name']} ✓")
        except Exception as e:
            log.error(f"[RSS] Error {s['name']}: {e}")
    return noticias

# ─── RESUMEN DIARIO ───────────────────────────────────────────────────────────
async def resumen_diario(bot):
    enviadas = get_ultimas_enviadas()
    if not enviadas:
        msg = "📋 *Resumen diario*\n\nSin bajas registradas hoy."
    else:
        lista = "\n".join([f"• {t[:80]}" for t in enviadas])
        msg = f"📋 *Resumen diario — {datetime.now().strftime('%d/%m/%Y')}*\n\n{lista}"
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ─── JOB PRINCIPAL ────────────────────────────────────────────────────────────
async def revisar(bot):
    log.info("🔍 Revisando fuentes...")
    todas = parse_journalists() + parse_rss()

    orden = {"🔴 OFICIAL": 0, "🟠 PERIODISTA": 1, "🔵 NOTICIA": 2, "🟡 COMUNIDAD": 3}
    todas.sort(key=lambda x: orden.get(credibilidad(x["tipo"], x["fuente"]), 2))

    nuevas = 0
    for n in todas:
        h = hash_noticia(n["titulo"], n["link"])
        if ya_enviada(h):
            continue
        nivel = credibilidad(n["tipo"], n["fuente"])
        msg = formato_mensaje(n["titulo"], n["resumen"], n["fuente"], n["link"], n["fecha"], nivel)
        try:
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", disable_web_page_preview=False)
            marcar_enviada(h, n["titulo"])
            nuevas += 1
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Error enviando: {e}")

    log.info(f"{'📬 ' + str(nuevas) + ' alertas nuevas.' if nuevas else 'Sin novedades.'}")

# ─── INICIO ───────────────────────────────────────────────────────────────────
async def main():
    init_db()
    bot = Bot(token=TELEGRAM_TOKEN)

    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "🤖 *Tennis Injury Bot iniciado* ✅\n\n"
            "Monitoreando lesiones ATP/WTA/Challenger\n\n"
            "📡 Fuentes activas:\n"
            "• X/Twitter — periodistas clave\n"
            "• Google News EN + ES\n"
            "• Reddit r/tennis\n"
            "• Tennis Majors\n\n"
            f"⏱ Revisando cada *{CHECK_INTERVAL} minutos*\n"
            "🟥 Oficial  🟧 Periodista  🟦 Noticia  🟨 Comunidad"
        ),
        parse_mode="Markdown",
    )

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(revisar, "interval", minutes=CHECK_INTERVAL, args=[bot], next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(resumen_diario, "cron", hour=8, minute=0, args=[bot])
    scheduler.start()

    log.info(f"✅ Bot corriendo — revisando cada {CHECK_INTERVAL} min.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
