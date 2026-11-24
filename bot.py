# -*- coding: utf-8 -*-
"""
BOT DE TRADING SMC PARA TELEGRAM
Motor analítico Smart Money Concepts (SMC) para BTCUSD, XAUUSD, EURUSD y NAS100.
"""

import os
import re
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters,
)

# ------------------------------------------------------------------ #
# CONFIGURACIÓN
# ------------------------------------------------------------------ #
TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    raise SystemExit("Falta la variable de entorno BOT_TOKEN")

ALLOWED_USER = int(os.environ.get("ALLOWED_USER_ID", "0") or 0)

ASSETS = {
    "BTCUSD":  dict(name="Bitcoin",     dec=2, cross="USDT.D / ETHUSD / NAS100", atr_mult=0.5),
    "XAUUSD":  dict(name="Oro",         dec=2, cross="DXY inverso / US10Y",      atr_mult=0.4),
    "EURUSD":  dict(name="Euro/Dólar",  dec=5, cross="DXY inverso / GBPUSD SMT", atr_mult=0.4),
    "NAS100":  dict(name="NASDAQ 100",  dec=2, cross="US10Y / VIX / US500 SMT",  atr_mult=0.5),
    "US100":   dict(name="NASDAQ 100",  dec=2, cross="US10Y / VIX / US500 SMT",  atr_mult=0.5),
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("smc-bot")

WAITING_DATA = 1

KEY_ALIASES = {
    "precio": ["precio", "price", "actual", "spot", "ultimo", "último"],
    "pdh":    ["pdh", "max_dia_ant", "high_anterior", "max_anterior"],
    "pdl":    ["pdl", "min_dia_ant", "low_anterior", "min_anterior"],
    "pwh":    ["pwh", "max_semana", "high_semana"],
    "pwl":    ["pwl", "min_semana", "low_semana"],
    "asia_alto": ["asia_alto", "asia_high", "max_asia", "high_asia", "ah"],
    "asia_bajo": ["asia_bajo", "asia_low", "min_asia", "low_asia", "al"],
    "apertura":  ["apertura", "open", "daily_open", "apertura_dia", "apertura_ny"],
    "sesgo_d1":  ["sesgo_d1", "d1", "tendencia_d1"],
    "sesgo_h4":  ["sesgo_h4", "h4", "tendencia_h4"],
    "sesgo_h1":  ["sesgo_h1", "h1", "tendencia_h1"],
    "sesgo_m15": ["sesgo_m15", "m15", "tendencia_m15"],
    "rvol":      ["rvol", "volumen_relativo", "rel_vol"],
    "vwap":      ["vwap"],
    "atr":       ["atr", "atr14", "atr_m15", "atr_m5"],
    "spread":    ["spread", "sp"],
    "hora_ny":   ["hora_ny", "hora", "hora_est", "hora_nueva_york", "ny_time", "hora_broker_ny"],
    "noticias":  ["noticias", "news", "calendario", "red_folder"],
    "smt":       ["smt", "divergencia", "divergencia_smt"],
    "dxy":       ["dxy"],
    "estructura": ["estructura", "bos_choch", "bos", "choch"],
    "vela_gatillo": ["vela_gatillo", "gatillo", "trigger", "vela"],
    "liquidez_barri": ["liquidez_barri", "barrido", "sweep", "stop_hunt", "liquidez_tomada"],
    "zona":      ["zona", "ubicacion", "ubicación", "pd_zone"],
    "ob":        ["ob", "order_block"],
    "fvg":       ["fvg", "gap", "fair_value_gap"],
}

REQUIRED_KEYS = ["precio", "pdh", "pdl"]

# ------------------------------------------------------------------ #
# UTILIDADES Y CÁLCULO
# ------------------------------------------------------------------ #
def norm(s: str) -> str:
    return (s.strip().lower()
             .replace("á", "a").replace("é", "e").replace("í", "i")
             .replace("ó", "o").replace("ú", "u").replace("ñ", "n"))

def parse_block(text: str) -> dict:
    data, current_asset = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper().replace("/", "").replace(" ", "")
        for a in ASSETS:
            if upper in (a, a.replace("USD", "")) or upper.startswith(a):
                current_asset = a
                break
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = norm(k), v.strip().rstrip(",;")
            canon = next((f for f, al in KEY_ALIASES.items() if k in al), k)
            data[canon] = v
        elif "=" in line:
            k, v = line.split("=", 1)
            k, v = norm(k), v.strip().rstrip(",;")
            canon = next((f for f, al in KEY_ALIASES.items() if k in al), k)
            data[canon] = v
        elif current_asset:
            data["_asset"] = current_asset
        elif len(line.split()) <= 2 and not current_asset:
            data["_asset_guess"] = line.upper().replace(" ", "")
    return data

def fnum(x, default=None):
    if x is None:
        return default
    try:
        return float(str(x).replace(",", "."))
    except ValueError:
        m = re.search(r"-?\d+(?:[.,]\d+)?", str(x))
        return float(m.group(0).replace(",", ".")) if m else default

def yesno(x, default=None):
    if x is None:
        return default
    x = norm(str(x))
    if x in ("si", "sí", "yes", "y", "1", "true", "confirmado", "confirmada"):
        return True
    if x in ("no", "n", "0", "false", "negativo"):
        return False
    return default

def trend(x, default=None):
    if x is None:
        return default
    x = norm(str(x))
    if x in ("alcista", "bullish", "alta", "long", "compras", "compra", "up"):
        return 1
    if x in ("bajista", "bearish", "baja", "short", "ventas", "venta", "down"):
        return -1
    if x in ("rango", "neutral", "lateral", "0"):
        return 0
    return default

def fmt_price(v, dec=2):
    return f"{v:.{dec}f}" if v is not None else "N/D"

def killzone(h: float):
    if h is None:
        return "Hora NY no informada", 0
    zones = [
        ((2.0, 5.0),   "London Open Killzone", 2),
        ((8.0, 11.0),  "NY Open Killzone", 3),
        ((3.0, 4.0),   "Silver Bullet (AM)", 3),
        ((10.0, 11.0), "Silver Bullet (NY)", 3),
        ((14.0, 15.0), "Silver Bullet (PM)", 2),
        ((16.0, 19.5), "Zona Muerta", -2),
        ((20.0, 24.0), "Sesión Asiática (acumulación)", 1),
        ((0.0, 2.0),   "Sesión Asiática (acumulación)", 1),
    ]
    best = None
    for (a, b), name, pts in zones:
        if a <= h < b:
            if best is None or pts > best[2]:
                best = (name, True, pts)
    return (best[0], best[2]) if best else ("Fuera de killzone principal", 0)

def score_setup(d: dict) -> dict:
    price  = fnum(d.get("precio"))
    pdh    = fnum(d.get("pdh"))
    pdl    = fnum(d.get("pdl"))
    vwap   = fnum(d.get("vwap"))
    atr    = fnum(d.get("atr"))
    rvol   = fnum(d.get("rvol"))
    spread = fnum(d.get("spread"))
    hora   = fnum(d.get("hora_ny"))

    t_d1, t_h4, t_h1, t_m15 = (trend(d.get(k)) for k in ("sesgo_d1", "sesgo_h4", "sesgo_h1", "sesgo_m15"))
    smt_ok   = yesno(d.get("smt"))
    news     = yesno(d.get("noticias"))
    sweep    = yesno(d.get("liquidez_barri"))
    velag_ok = yesno(d.get("vela_gatillo"))
    ob_ok    = yesno(d.get("ob"))
    fvg_ok   = yesno(d.get("fvg"))

    det = []
    p1 = p2 = p3 = p4 = p5 = 0

    trends = [t for t in (t_d1, t_h4, t_h1, t_m15) if t is not None]
    if trends:
        dominant = 1 if sum(trends) > 0 else (-1 if sum(trends) < 0 else 0)
        aligned = sum(1 for t in trends if t == dominant and t != 0)
        p1 = min(25, aligned * 8 + (3 if len(trends) >= 3 else 0))
        det.append(f"P1 Estructura MTF: {p1}/25 ({aligned}/{len(trends)} temporalidades alineadas)")
    else:
        det.append("P1 Estructura MTF: 0/25 (sin sesgos informados)")

    if price is not None and pdh is not None and pdl is not None:
        rng = pdh - pdl
        if rng > 0:
            pos = (price - pdl) / rng
            dominant = (1 if sum(trends) > 0 else -1) if trends else 1
            if dominant == 1:
                if pos <= 0.5:
                    p2 += 12
                    det.append(f"P2 Discount: precio en zona de descuento ({pos:.0%})")
                elif 0.62 <= pos <= 0.79:
                    p2 += 15
                    det.append(f"P2 Zona OTE: retroceso {pos:.0%} (61.8-78.6%)")
                else:
                    p2 += 4
                    det.append(f"P2 Precio en premium ({pos:.0%}) - esperar pullback")
            else:
                if pos >= 0.5:
                    p2 += 12
                    det.append("P2 Premium: precio en zona premium para ventas")
                elif 0.21 <= pos <= 0.38:
                    p2 += 15
                    det.append(f"P2 Zona OTE venta: retroceso {pos:.0%}")
                else:
                    p2 += 4
                    det.append(f"P2 Precio en discount ({pos:.0%}) - esperar pullback")
            if sweep is True:
                p2 += 8
                det[-1] += " + barrido de liquidez OK"
            if ob_ok:
                p2 += 3
            if fvg_ok:
                p2 += 2
            p2 = min(25, p2)
    else:
        det.append("P2 Zona de valor: 0/25 (faltan precio/PDH/PDL)")

    if rvol is not None:
        if rvol >= 1.5:
            p3 += 10
        elif rvol >= 1.2:
            p3 += 7
        elif rvol < 1.0:
            det.append(f"P3 ALERTA RVOL {rvol:.2f} menor a 1.0 - posible trampa de liquidez")
        else:
            p3 += 3
        det.append(f"P3 Volumen relativo: RVOL {rvol:.2f}")
    if velag_ok is True:
        p3 += 8
        det.append("P3 Vela gatillo confirmada")
    elif velag_ok is False:
        det.append("P3 Sin vela gatillo válida aún")
    else:
        det.append("P3 Vela gatillo sin confirmar (esperar cierre)")
    p3 = min(20, p3)

    if smt_ok is True:
        p4 += 10
        det.append("P4 Divergencia SMT confirmada")
    if yesno(d.get("dxy")) is not None:
        p4 += 5
        det.append("P4 Confluencia DXY confirmada")
    if p4 == 0:
        det.append("P4 Sin confirmación inter-mercado (SMT/DXY)")
    p4 = min(15, p4)

    kz_name, kz_pts = killzone(hora)
    p5 += max(0, kz_pts * 3)
    det.append(f"P5 Timing: {kz_name} ({max(0, kz_pts * 3)}/9 pts)")
    if news is True:
        p5 -= 15
        det.append("P5 ALERTA MACRO: noticia roja activa -> NO OPERAR")
    elif news is None:
        det.append("P5 Noticias: no verificadas")

    if price is not None and pdh is not None and pdl is not None and atr is not None:
        cushion = atr * 0.4
        dominant = 1 if (not trends or sum(trends) >= 0) else -1
        if dominant == 1:
            sl = pdl - cushion
            risk = price - sl
            tp = pdh
        else:
            sl = pdh + cushion
            risk = sl - price
            tp = pdl
        if risk and risk > 0:
            rr = abs(tp - price) / risk
            if rr >= 2:
                p5 += 6
                det.append(f"P5 R:R {rr:.1f}:1 (Cumple >= 1:2)")
            elif rr >= 1.5:
                p5 += 3
                det.append(f"P5 R:R {rr:.1f}:1 (Aceptable, ideal >= 1:2)")
            else:
                det.append(f"P5 R:R {rr:.1f}:1 (Menor a 1:2)")
    p5 = max(0, min(15, p5))

    total = p1 + p2 + p3 + p4 + p5
    return dict(total=total, detalle=det, price=price, pdh=pdh, pdl=pdl, atr=atr,
                vwap=vwap, rvol=rvol, spread=spread, hora=hora, kz=kz_name,
                p1=p1, p2=p2, p3=p3, p4=p4, p5=p5, news=news, trends=trends)

def build_plan(asset: str, mode: str, sc: dict) -> str:
    dec = ASSETS[asset]["dec"]
    atr = sc["atr"]
    price, pdh, pdl = sc["price"], sc["pdh"], sc["pdl"]
    dominant = (1 if sum(sc["trends"]) > 0 else (-1 if sum(sc["trends"]) < 0 else 1)) if sc["trends"] else 1

    lines = [f"\n📋 PLAN CONDICIONAL — {asset} ({mode.upper()})"]
    if price is None or pdh is None or pdl is None:
        lines.append("⚠️ Datos insuficientes. Necesito mínimo: precio, pdh y pdl.")
        return "\n".join(lines)

    cushion = (atr or 0) * ASSETS[asset]["atr_mult"]
    if dominant == 1:
        sl = pdl - cushion
        tp1, tp2 = pdh, pdh + (pdh - pdl) * 0.5
        lines.append("🟢 Sesgo: ALCISTA — buscar COMPRAS en zona discount")
    else:
        sl = pdh + cushion
        tp1, tp2 = pdl, pdl - (pdh - pdl) * 0.5
        lines.append("🔴 Sesgo: BAJISTA — buscar VENTAS en zona premium")

    risk = abs(price - sl)
    if risk > 0:
        rr1 = abs(tp1 - price) / risk
        lines.append(
            f"• Entrada: esperar retesteo / FVG (no perseguir)\n"
            f"• SL: {fmt_price(sl, dec)} (estructura + colchón ATR)\n"
            f"• TP1: {fmt_price(tp1, dec)} | R:R aprox {rr1:.1f}:1\n"
            f"• TP2: {fmt_price(tp2, dec)}\n"
            f"• Gestión: al tocar TP1 cerrar 50% y mover SL a Break Even."
        )
    if atr and sc["vwap"] is not None and abs(price - sc["vwap"]) > 1.5 * atr:
        lines.append(f"🚫 ANTI-FOMO: precio alejado {abs(price - sc['vwap']) / atr:.1f}x ATR del VWAP — esperar pullback.")
    if sc["spread"] is not None and risk > 0 and sc["spread"] > 0.15 * abs(tp1 - price):
        lines.append(f"⚠️ SPREAD ALTO ({sc['spread']:.{dec}f}): supera el 15% del recorrido al TP1.")
    return "\n".join(lines)

def verdict(total: int, news) -> str:
    if news is True:
        return "⚠️ ALERTA MACRO: NO OPERAR — noticia roja activa."
    if total >= 85:
        return "🟢 SETUP A+ (ALTA PRECISIÓN) — Ejecución válida (riesgo 1%)."
    if total >= 75:
        return "🟢 SETUP B (VÁLIDO CON CONFIRMACIÓN) — Ejecutar tras cierre de gatillo (riesgo 0.5-0.75%)."
    return "🟡 NO OPERAR (ESPERAR) — El setup no supera 75/100 puntos."

def build_report(asset: str, mode: str, d: dict) -> str:
    sc = score_setup(d)
    info = ASSETS[asset]
    missing = [k for k in REQUIRED_KEYS if d.get(k) is None]
    out = [
        f"📊 ANÁLISIS {asset} ({info['name']}) | Modo: {mode.upper()}",
        f"🔗 Cruces: {info['cross']}"
    ]
    if sc["price"] is not None:
        out.append(f"💰 Precio informado: {fmt_price(sc['price'], info['dec'])}")
    out.append(f"\n🎯 PUNTAJE DE CONFLUENCIA: {sc['total']}/100")
    out.append(verdict(sc["total"], sc["news"]))
    out.append("\nDesglose de pilares:")
    for line in sc["detalle"]:
        out.append(f"• {line}")
    if sc["total"] < 75:
        out.append("\nQué falta para superar 75 pts:")
        if sc["p1"] < 18: out.append("→ Alinear sesgos MTF (D1/H4/H1).")
        if sc["p2"] < 15: out.append("→ Esperar pullback a Discount/Premium-OTE u OB+FVG.")
        if sc["p3"] < 12: out.append("→ Confirmar RVOL >= 1.2 y vela gatillo al cierre.")
        if sc["p4"] < 8:  out.append("→ Confirmar divergencia SMT o DXY/US10Y.")
        if sc["p5"] < 9:  out.append("→ Operar en Killzone (Londres/NY) con R:R >= 1:2.")
    out.append(build_plan(asset, mode, sc))
    if missing:
        out.append(f"\n⚠️ Faltan datos obligatorios: {', '.join(missing)}.")
    return "\n".join(out)

# ------------------------------------------------------------------ #
# HANDLERS Y BOTONES TÁCTILES
# ------------------------------------------------------------------ #
EXAMPLE = """XAUUSD
precio: 2650.50
pdh: 2660.00
pdl: 2638.00
asia_alto: 2655.00
asia_bajo: 2641.00
vwap: 2648.00
atr: 3.50
rvol: 1.60
spread: 0.35
hora_ny: 09.45
sesgo_d1: alcista
sesgo_h4: alcista
sesgo_h1: alcista
estructura: BOS
liquidez_barri: si
smt: si
noticias: no
vela_gatillo: si"""

HELP = (
    "🤖 Bot SMC — Analista Cuantitativo\n\n"
    "Comandos disponibles:\n"
    "/analizar — Análisis completo\n"
    "/scalping — Sesgo intradía M1-M15\n"
    "/daytrading — Sesgo macro D1-H4-H1\n"
    "/smc — Radiografía OB / FVG / OTE\n"
    "/ejemplo — Plantilla de datos lista para copiar\n"
    "/cancelar — Cancelar operación actual"
)

def _authorized(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return not (ALLOWED_USER and uid != ALLOWED_USER)

def asset_keyboard(mode: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿ BTCUSD", callback_data=f"sel|{mode}|BTCUSD"),
            InlineKeyboardButton("🥇 XAUUSD", callback_data=f"sel|{mode}|XAUUSD"),
        ],
        [
            InlineKeyboardButton("💶 EURUSD", callback_data=f"sel|{mode}|EURUSD"),
            InlineKeyboardButton("📈 NAS100", callback_data=f"sel|{mode}|NAS100"),
        ]
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    await update.message.reply_text(HELP)

async def cmd_ejemplo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    await update.message.reply_text(EXAMPLE)

async def start_analysis(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mode: str):
    if not _authorized(update): return ConversationHandler.END
    args = ctx.args
    asset = args[0].upper().replace("/", "") if args else None
    if asset in ("US100", "USTEC"):
        asset = "NAS100"

    if not asset or asset not in ASSETS:
        await update.message.reply_text(
            f"Selecciona el activo para modo {mode.upper()}:",
            reply_markup=asset_keyboard(mode)
        )
        return WAITING_DATA

    ctx.user_data["asset"] = asset
    ctx.user_data["mode"] = mode
    await update.message.reply_text(
        f"✅ Activo: {asset} | Modo: {mode.upper()}\n\n"
        "Pega ahora tu bloque de datos (precio, pdh, pdl, asia_alto, asia_bajo, "
        "sesgos, rvol, vwap, atr, hora_ny, noticias...).\n"
        "Usa /ejemplo si quieres ver una plantilla."
    )
    return WAITING_DATA

async def button_asset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _authorized(update): return ConversationHandler.END
    _, mode, asset = query.data.split("|")
    ctx.user_data["asset"] = asset
    ctx.user_data["mode"] = mode
    await query.edit_message_text(
        f"✅ Activo: {asset} | Modo: {mode.upper()}\n\n"
        "Pega ahora tu bloque de datos (precio, pdh, pdl, asia_alto, asia_bajo, "
        "sesgos, rvol, vwap, atr, hora_ny, noticias...).\n"
        "Usa /ejemplo si quieres ver una plantilla."
    )
    return WAITING_DATA

async def cmd_analizar(update, ctx):   return await start_analysis(update, ctx, "completo")
async def cmd_scalping(update, ctx):   return await start_analysis(update, ctx, "scalping")
async def cmd_daytrading(update, ctx): return await start_analysis(update, ctx, "daytrading")
async def cmd_smc(update, ctx):        return await start_analysis(update, ctx, "smc")

async def receive_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return ConversationHandler.END
    text = update.message.text
    d = parse_block(text)
    asset = d.pop("_asset", None) or d.pop("_asset_guess", None) or ctx.user_data.get("asset")
    asset = (asset or "").upper().replace("/", "")
    if asset in ("US100", "USTEC"):
        asset = "NAS100"
    if asset not in ASSETS:
        await update.message.reply_text("No detecté el activo. Toca uno arriba o escríbelo en la primera línea (BTCUSD, XAUUSD, EURUSD o NAS100).")
        return WAITING_DATA
    mode = ctx.user_data.get("mode", "completo")
    report = build_report(asset, mode, d)
    for i in range(0, len(report), 4000):
        await update.message.reply_text(report[i:i + 4000])
    ctx.user_data.clear()
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Operación cancelada. Usa /start para ver comandos.")
    return ConversationHandler.END

# ------------------------------------------------------------------ #
# SERVIDOR DE SALUD (UptimeRobot / Render)
# ------------------------------------------------------------------ #
def _health_server():
    port = int(os.environ.get("PORT", 10000))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"smcbot: ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_HEAD(self):
            self.send_
