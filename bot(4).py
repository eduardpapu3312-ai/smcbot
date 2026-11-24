# -*- coding: utf-8 -*-
"""
BOT DE TRADING SMC PARA TELEGRAM — Termux
=========================================
Motor analítico estilo Smart Money Concepts (SMC) para 4 activos:
BTCUSD, XAUUSD, EURUSD, NAS100/US100.

REGLA DE ORO (anti-alucinación): el bot NUNCA inventa precios.
Solo calcula con los datos que TÚ le pegues en un bloque clave:valor.
Si faltan datos clave, te pide exactamente cuáles faltan.

Uso:
  /analizar XAUUSD          → te pide el bloque de datos
  /scalping XAUUSD          → mismo wizard, sesgo M1-M15
  /daytrading XAUUSD        → sesgo D1-H4-H1-M15
  /smc XAUUSD               → radiografía OB/FVG/OTE
  /ejemplo                  → bloque de datos de muestra
  /cancelar                 → aborta el wizard
"""

import os
import re
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters,
)

# ------------------------------------------------------------------ #
# CONFIG
# ------------------------------------------------------------------ #
TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    raise SystemExit("Falta la variable de entorno BOT_TOKEN")

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

# ------------------------------------------------------------------ #
# ESTADOS DEL WIZARD
# ------------------------------------------------------------------ #
WAITING_DATA = 1

# alias de claves que el usuario puede escribir (normalización)
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
    "liquidez_barri" : ["liquidez_barri", "barrido", "sweep", "stop_hunt", "liquidez_tomada"],
    "zona":      ["zona", "ubicacion", "ubicación", "pd_zone"],
    "ob":        ["ob", "order_block"],
    "fvg":       ["fvg", "gap", "fair_value_gap"],
}

REQUIRED_KEYS = ["precio", "pdh", "pdl"]
RECOMMENDED_KEYS = ["asia_alto", "asia_bajo", "sesgo_h1", "rvol", "hora_ny", "atr"]

# ------------------------------------------------------------------ #
# UTILIDADES
# ------------------------------------------------------------------ #
def norm(s: str) -> str:
    return (s.strip().lower()
             .replace("á", "a").replace("é", "e").replace("í", "i")
             .replace("ó", "o").replace("ú", "u").replace("ñ", "n"))

def parse_block(text: str) -> dict:
    """Parsea un bloque clave:valor multilínea, tolerante a formatos."""
    data, current_asset = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper().replace("/", "").replace(" ", "")
        for a in ASSETS:
            if upper in (a, a.replace("USD", "")) or upper.startswith(a):
                current_asset = "US100" if a in ("US100",) and "US100" in upper else a
                current_asset = a
                break
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = norm(k), v.strip().rstrip(",;")
            canon = None
            for field, aliases in KEY_ALIASES.items():
                if k in aliases:
                    canon = field
                    break
            if canon is None:
                canon = k
            data[canon] = v
        elif "=" in line:
            k, v = line.split("=", 1)
            k, v = norm(k), v.strip().rstrip(",;")
            canon = None
            for field, aliases in KEY_ALIASES.items():
                if k in aliases:
                    canon = field
                    break
            data[canon or k] = v
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

# ------------------------------------------------------------------ #
# KILLZONES (hora NY en decimal, ej. 09:45 → 9.75)
# ------------------------------------------------------------------ #
def killzone(h: float):
    if h is None:
        return None, "Hora NY no informada — timing no verificable."
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
    if best:
        return best[0], best[2]
    return "Fuera de killzone principal", 0

# ------------------------------------------------------------------ #
# MOTOR DE PUNTUACIÓN 0–100
# ------------------------------------------------------------------ #
def score_setup(d: dict) -> dict:
    price  = fnum(d.get("precio"))
    pdh    = fnum(d.get("pdh"))
    pdl    = fnum(d.get("pdl"))
    ah     = fnum(d.get("asia_alto"))
    al     = fnum(d.get("asia_bajo"))
    vwap   = fnum(d.get("vwap"))
    atr    = fnum(d.get("atr"))
    rvol   = fnum(d.get("rvol"))
    spread = fnum(d.get("spread"))
    hora   = fnum(d.get("hora_ny"))

    t_d1, t_h4, t_h1, t_m15 = (trend(d.get(k)) for k in
                               ("sesgo_d1", "sesgo_h4", "sesgo_h1", "sesgo_m15"))
    smt_ok   = yesno(d.get("smt"))
    news     = yesno(d.get("noticias"))
    sweep    = yesno(d.get("liquidez_barri"))
    velag_ok = yesno(d.get("vela_gatillo"))
    ob_ok    = yesno(d.get("ob"))
    fvg_ok   = yesno(d.get("fvg"))

    det = []   # detalle de cada pilar
    p1 = p2 = p3 = p4 = p5 = 0

    # ---- PILAR 1: Alineación estructural MTF (0-25) ----
    trends = [t for t in (t_d1, t_h4, t_h1, t_m15) if t is not None]
    if trends:
        dominant = 1 if sum(trends) > 0 else (-1 if sum(trends) < 0 else 0)
        aligned = sum(1 for t in trends if t == dominant and t != 0)
        p1 = min(25, aligned * 8 + (3 if len(trends) >= 3 else 0))
        det.append(f"P1 Estructura MTF: {p1}/25 ({aligned}/{len(trends)} temporalidades alineadas "
                   f"{'alcistas' if dominant == 1 else 'bajistas' if dominant == -1 else 'en rango'})")
    else:
        det.append("P1 Estructura MTF: 0/25 (sin sesgos informados)")

    # ---- PILAR 2: Zona de valor + liquidez (0-25) ----
    if price is not None and pdh is not None and pdl is not None:
        rng = pdh - pdl
        if rng > 0:
            pos = (price - pdl) / rng          # 0 = en PDL, 1 = en PDH
            dominant = 1 if (t_h1 or t_h4 or t_d1 or 1) == 1 else 1
            if trends:
                dominant = 1 if sum(trends) > 0 else -1
            # ubicación correcta según sesgo
            if dominant == 1:
                if pos <= 0.5:  p2 += 12; det.append(f"P2 Discount: {p2}/25 — precio en zona de descuento ({pos:.0%} del rango PDH/PDL)")
                elif pos >= 0.62 and pos <= 0.79: p2 += 15; det.append(f"P2 Zona OTE: {p2}/25 — retroceso {pos:.0%} dentro de 61.8–78.6%")
                else: p2 += 4; det.append(f"P2 Precio en premium ({pos:.0%} del rango) — esperar pullback")
            else:
                if pos >= 0.5:  p2 += 12; det.append(f"P2 Premium: {p2}/25 — precio en zona de premium para ventas")
                elif 0.21 <= pos <= 0.38: p2 += 15; det.append(f"P2 Zona OTE venta: {p2}/25 — retroceso {pos:.0%}")
                else: p2 += 4; det.append(f"P2 Precio en discount para venta ({pos:.0%}) — esperar")
            # barrido de liquidez
            if sweep is True:
                p2 += 8; det[-1] += " + barrido previo de liquidez ✔"
            elif sweep is None:
                det[-1] += " (sin confirmar barrido de liquidez)"
            # OB / FVG
            if ob_ok: p2 += 3
            if fvg_ok: p2 += 2
            p2 = min(25, p2)
    else:
        det.append("P2 Zona de valor: 0/25 (faltan precio/PDH/PDL)")

    # ---- PILAR 3: Volumen y vela gatillo (0-20) ----
    if rvol is not None:
        if rvol >= 1.5: p3 += 10
        elif rvol >= 1.2: p3 += 7
        elif rvol < 1.0: p3 += 0; det.append(f"P3 ⚠ RVOL {rvol:.2f} < 1.0 → rupturas sospechosas de trampa de liquidez")
        else: p3 += 3
        if not any("RVOL" in x for x in det[-1:]):
            det.append(f"P3 Volumen: RVOL {rvol:.2f}")
    if velag_ok is True: p3 += 8; det.append("P3 Vela gatillo confirmada (envolvente/rechazo)")
    elif velag_ok is False: p3 += 0; det.append("P3 Sin vela gatillo válida aún")
    else: det.append("P3 Vela gatillo: sin confirmar (esperar cierre)")
    if rvol is not None and rvol < 1.2:
        det.append("P3 Regla: ruptura con RVOL < 1.2 = falsa ruptura probable → NO perseguir")
    p3 = min(20, p3)

    # ---- PILAR 4: Correlación / SMT (0-15) ----
    if smt_ok is True: p4 += 10; det.append("P4 Divergencia SMT confirmada a favor ✔")
    if yesno(d.get("dxy")) is not None:
        p4 += 5; det.append("P4 DXY informado como confluencia ✔")
    if p4 == 0: det.append("P4 Sin confirmación inter-mercado (SMT/DXY) informada")
    p4 = min(15, p4)

    # ---- PILAR 5: Timing + R:R (0-15) ----
    kz_name, kz_pts = killzone(hora)
    p5 += max(0, kz_pts * 3)
    det.append(f"P5 Timing: {kz_name} ({max(0,kz_pts*3)}/9 pts)")
    if news is True:
        p5 -= 15
        det.append("P5 ⚠ ALERTA MACRO: noticia de alto impacto → NO OPERAR hasta nueva estructura")
    elif news is None:
        det.append("P5 Noticias: no verificadas — asume riesgo de Carpeta Roja")
    # R:R estimado con niveles disponibles
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
            if rr >= 2: p5 += 6; det.append(f"P5 R:R {rr:.1f}:1 ≥ 1:2 ✔")
            elif rr >= 1.5: p5 += 3; det.append(f"P5 R:R {rr:.1f}:1 (mejorable, mínimo 1:2 exigido)")
            else: det.append(f"P5 R:R {rr:.1f}:1 < 1:2 ✘")
    p5 = max(0, min(15, p5))

    total = p1 + p2 + p3 + p4 + p5
    return dict(total=total, detalle=det, price=price, pdh=pdh, pdl=pdl, atr=atr,
                vwap=vwap, rvol=rvol, spread=spread, hora=hora, kz=kz_name,
                p1=p1, p2=p2, p3=p3, p4=p4, p5=p5, news=news, trends=trends)

# ------------------------------------------------------------------ #
# PLAN CONDICIONAL POR NIVELES (nunca inventa precios)
# ------------------------------------------------------------------ #
def build_plan(asset: str, mode: str, d: dict, sc: dict) -> str:
    dec = ASSETS[asset]["dec"]
    atr = sc["atr"]
    price, pdh, pdl = sc["price"], sc["pdh"], sc["pdl"]
    dominant = 1
    if sc["trends"]:
        dominant = 1 if sum(sc["trends"]) > 0 else (-1 if sum(sc["trends"]) < 0 else 1)

    lines = [f"\n📋 *PLAN CONDICIONAL — {asset} ({mode.upper()})*"]
    if price is None or pdh is None or pdl is None:
        lines.append("⚠️ Datos insuficientes para plan por niveles.")
        lines.append("Necesito como mínimo: `precio`, `pdh`, `pdl` y preferiblemente `atr`.")
        return "\n".join(lines)

    cushion = (atr or 0) * ASSETS[asset]["atr_mult"]
    if dominant == 1:
        entry_zone = f"{fmt_price(min(pdl, price) + 0.0, dec)} (retesteo zona discount/OB)"
        sl = pdl - cushion
        tp1, tp2 = pdh, pdh + (pdh - pdl) * 0.5
        lines.append(f"🟢 Sesgo: ALCISTA — buscar COMPRAS en discount")
    else:
        entry_zone = f"{fmt_price(max(pdh, price), dec)} (retesteo zona premium/OB)"
        sl = pdh + cushion
        tp1, tp2 = pdl, pdl - (pdh - pdl) * 0.5
        lines.append(f"🔴 Sesgo: BAJISTA — buscar VENTAS en premium")

    risk = abs(price - sl)
    if risk > 0:
        rr1 = abs(tp1 - price) / risk
        lines.append(
            f"• Entrada (condicional): esperar retesteo / FVG, *no perseguir*\n"
            f"• SL: `{fmt_price(sl, dec)}` (estructura {fmt_price(pdl if dominant==1 else pdh, dec)} "
            f"+ {ASSETS[asset]['atr_mult']}x ATR colchón)\n"
            f"• TP1: `{fmt_price(tp1, dec)}` (PD{'H' if dominant==1 else 'L'}) | R:R ≈ *{rr1:.1f}:1*\n"
            f"• TP2: `{fmt_price(tp2, dec)}` (extensión 50% del rango)\n"
            f"• Gestión: al tocar TP1 mover SL a Break Even y asegurar parcial (50%)."
        )
    # Escudo anti-FOMO
    if atr and sc["vwap"] is not None and price is not None:
        if abs(price - sc["vwap"]) > 1.5 * atr:
            lines.append(f"🚫 ANTI-FOMO: precio a {abs(price-sc['vwap'])/atr:.1f}x ATR del VWAP "
                         f"({'>1.5x' if abs(price-sc['vwap'])>1.5*atr else 'ok'}) — *prohibido entrar por mercado, esperar pullback*")
    # Filtro de spread
    if sc["spread"] is not None and risk > 0:
        if sc["spread"] > 0.15 * abs(tp1 - price):
            lines.append(f"⚠️ SPREAD ALTO: {sc['spread']:.{dec}f} supera 15% de la distancia al TP1 → desaconsejado scalping")
    return "\n".join(lines)

# ------------------------------------------------------------------ #
# VEREDICTO Y MENSAJE FINAL
# ------------------------------------------------------------------ #
def verdict(total: int, news) -> str:
    if news is True:
        return "⚠️ *ALERTA MACRO: NO OPERAR* — noticia roja inminente/reciente. Esperar expansión y nueva estructura."
    if total >= 85:
        return "🟢/🔴 *SETUP A+ (ALTA PRECISIÓN)* — Ejecución válida con riesgo estándar (1%)."
    if total >= 75:
        return "🟢/🔴 *SETUP B (VÁLIDO CON CONFIRMACIÓN)* — Ejecutar solo tras cierre de vela gatillo, riesgo 0.5–0.75%."
    return "🟡 *NO OPERAR (ESPERAR)* — el setup no supera 75/100. Abajo indico qué falta para subir el puntaje."

def build_report(asset: str, mode: str, d: dict) -> str:
    sc = score_setup(d)
    asset_info = ASSETS[asset]
    missing = [k for k in REQUIRED_KEYS if d.get(k) is None]
    out = []
    out.append(f"📊 *ANÁLISIS {asset} — {asset_info['name']}* | Modo: {mode.upper()}")
    out.append(f"🔗 Cruces: {asset_info['cross']}")
    if sc["price"] is not None:
        out.append(f"💰 Precio informado: `{fmt_price(sc['price'], asset_info['dec'])}`")
    out.append("")
    out.append(f"🎯 *PUNTAJE DE CONFLUENCIA: {sc['total']}/100*")
    out.append(verdict(sc["total"], sc["news"]))
    out.append("")
    out.append("*Desglose de pilares:*")
    for line in sc["detalle"]:
        out.append(f"• {line}")
    if sc["total"] < 75:
        out.append("")
        out.append("*Qué falta para superar 75 pts:*")
        gaps = []
        if sc["p1"] < 18: gaps.append("→ Alinear sesgos MTF (D1/H4/H1) a favor o reducir tamaño.")
        if sc["p2"] < 15: gaps.append("→ Esperar pullback a Discount/Premium-OTE o a OB+FVG sin mitigar.")
        if sc["p3"] < 12: gaps.append("→ Confirmar RVOL ≥ 1.2 y vela gatillo de rechazo/envolvente al cierre.")
        if sc["p4"] < 8:  gaps.append("→ Buscar divergencia SMT o confirmación de DXY/US10Y/USDT.D.")
        if sc["p5"] < 9:  gaps.append("→ Operar dentro de Killzone (Londres 02–05 / NY 08–11 EST) y R:R ≥ 1:2.")
        if sc["news"] is True: gaps.append("→ NO operar: Carpeta Roja activa.")
        out.extend(gaps or ["→ Faltan datos para evaluar; completa el bloque."])
    out.append(build_plan(asset, mode, d, sc))
    if missing:
        out.append(f"\n⚠️ Faltan datos obligatorios: {', '.join(missing)}. Sin ellos el plan es solo condicional.")
    out.append("\n_⚠️ Análisis educativo basado en los datos que proporcionaste. No es asesoría financiera._")
    return "\n".join(out)

# ------------------------------------------------------------------ #
# HANDLERS
# ------------------------------------------------------------------ #
EXAMPLE = """Ejemplo de bloque (pégalo adaptado a tu gráfico):

XAUUSD
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
    "🤖 *Bot SMC — Analista Cuantitativo (BTCUSD / XAUUSD / EURUSD / NAS100)*\n\n"
    "NUNCA invento precios: analizo únicamente los datos que tú pegues.\n\n"
    "*Comandos:*\n"
    "/analizar ACTIVO — análisis completo (day trading + scalping)\n"
    "/scalping ACTIVO — sesgo M1–M15, killzones, VWAP\n"
    "/daytrading ACTIVO — sesgo D1–H4–H1, PDH/PDL, OTE\n"
    "/smc ACTIVO — radiografía OB / FVG / Breaker / OTE\n"
    "/ejemplo — bloque de datos de muestra\n"
    "/cancelar — abortar\n\n"
    "*Bloque de datos (claves aceptadas):*\n"
    "`precio, pdh, pdl, pwh, pwl, asia_alto, asia_bajo, vwap, atr, rvol, spread, "
    "hora_ny, sesgo_d1/h4/h1/m15 (alcista|bajista|rango), estructura (BOS|CHoCH), "
    "liquidez_barri (si|no), smt (si|no), dxy, ob, fvg, vela_gatillo, noticias (si|no)`"
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update, ctx): return
    await update.message.reply_text(HELP, parse_mode="Markdown")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update, ctx): return
    await update.message.reply_text(HELP, parse_mode="Markdown")

async def cmd_ejemplo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update, ctx): return
    await update.message.reply_text(EXAMPLE, parse_mode="Markdown")

async def start_analysis(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mode: str):
    if not await _gate(update, ctx): return ConversationHandler.END
    args = ctx.args
    asset = args[0].upper().replace("/", "") if args else None
    if asset in ("NAS100", "US100", "USTEC"):
        asset = "US100" if asset != "NAS100" else "NAS100"
    if asset not in ASSETS:
        await update.message.reply_text(
            "Indica un activo válido: BTCUSD, XAUUSD, EURUSD o NAS100.\n"
            "Ejemplo: /scalping XAUUSD", parse_mode="Markdown")
        return ConversationHandler.END
    ctx.user_data["asset"] = "NAS100" if asset == "US100" else asset
    ctx.user_data["mode"] = mode
    await update.message.reply_text(
        f"✅ Activo: *{asset}* | Modo: *{mode.upper()}*\n\n"
        "Pega ahora tu bloque de datos (precio, pdh, pdl, asia_alto, asia_bajo, "
        "sesgos, rvol, vwap, atr, hora_ny, noticias...). Usa /ejemplo como guía.\n"
        "Solo analizo los datos que des — *no invento cifras*.",
        parse_mode="Markdown")
    return WAITING_DATA

async def cmd_analizar(update, ctx): return await start_analysis(update, ctx, "completo")
async def cmd_scalping(update, ctx): return await start_analysis(update, ctx, "scalping")
async def cmd_daytrading(update, ctx): return await start_analysis(update, ctx, "daytrading")
async def cmd_smc(update, ctx):      return await start_analysis(update, ctx, "smc")

async def receive_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return ConversationHandler.END
    text = update.message.text
    if text.startswith("/"):
        if text.startswith("/cancelar"):
            await update.message.reply_text("Wizard cancelado. /start para reiniciar.")
            return ConversationHandler.END
        return WAITING_DATA
    d = parse_block(text)
    asset = d.pop("_asset", None) or d.pop("_asset_guess", None) or ctx.user_data.get("asset")
    asset = (asset or "").upper().replace("/", "")
    if asset in ("NAS100", "US100", "USTEC"):
        asset = "NAS100"
    if asset not in ASSETS:
        await update.message.reply_text(
            "No detecté el activo. Incluye la primera línea: BTCUSD, XAUUSD, EURUSD o NAS100.",
            parse_mode="Markdown")
        return WAITING_DATA
    mode = ctx.user_data.get("mode", "completo")
    report = build_report(asset, mode, d)
    # Telegram límite 4096 chars → partir si hace falta
    for i in range(0, len(report), 4000):
        await update.message.reply_text(report[i:i + 4000], parse_mode="Markdown")
    ctx.user_data.clear()
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Wizard cancelado. /start para ver ayuda.")
    return ConversationHandler.END



# ------------------------------------------------------------------ #
# SERVIDOR DE SALUD (para UptimeRobot / Render) — NO toca el análisis
# ------------------------------------------------------------------ #
def _health_server():
    """Mini servidor HTTP que responde 200 a todo. UptimeRobot lo pinguea
    cada 5 min para que Render no duerma el servicio."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, os
    port = int(os.environ.get("PORT", 10000))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"smcbot: ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):  # silenciar logs
            pass
    def run():
        srv = ThreadingHTTPServer(("0.0.0.0", port), H)
        srv.serve_forever()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    log.info("Health server escuchando en puerto %s", port)

ALLOWED_USER = int(os.environ.get("ALLOWED_USER_ID", "0") or 0)

def _authorized(update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if ALLOWED_USER and uid != ALLOWED_USER:
        return False
    return True

async def _gate(update, ctx):
    if not _authorized(update):
        await update.message.reply_text("⛔ Bot privado.")
        return False
    return True

def main():
    _health_server()
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("analizar", cmd_analizar),
            CommandHandler("scalping", cmd_scalping),
            CommandHandler("daytrading", cmd_daytrading),
            CommandHandler("smc", cmd_smc),
        ],
        states={WAITING_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_data)]},
        fallbacks=[CommandHandler("cancelar", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ejemplo", cmd_ejemplo))
    app.add_handler(conv)
    log.info("Bot iniciado. Escuchando en Telegram…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
