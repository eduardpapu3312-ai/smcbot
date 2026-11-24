# -*- coding: utf-8 -*-
"""
BOT CUANTITATIVO SMC & SCALPING/DAYTRADING 24/7
Datos en tiempo real automáticos para:
- BTCUSD (Kraken / Binance)
- XAUUSD / Oro (Kraken PAXG + Yahoo Finance GC=F)
- EURUSD (Kraken + Yahoo Finance EURUSD=X)
- NAS100 / NASDAQ (Yahoo Finance NQ=F)
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

# ==========================================================
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ==========================================================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_RAW = os.environ.get("ALLOWED_USER_ID", "").strip()
ALLOWED_ID = int(ALLOWED_RAW) if ALLOWED_RAW.isdigit() else None

URL = f"https://api.telegram.org/bot{TOKEN}"
ALERTAS_FILE = "alertas.json"
LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("smcbot")

sess = requests.Session()
sess.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SMCQuantBot/2.0)"})


# ==========================================================
# UTILIDADES Y PERSISTENCIA DE ALERTAS
# ==========================================================
def autorizado(uid):
    if ALLOWED_ID is None:
        return True
    return uid == ALLOWED_ID


def enviar_mensaje(chat_id, texto):
    try:
        sess.post(
            f"{URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Error enviando mensaje: {e}")


def cargar_alertas():
    with LOCK:
        if not os.path.exists(ALERTAS_FILE):
            return []
        try:
            with open(ALERTAS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []


def guardar_alertas(lista):
    with LOCK:
        try:
            with open(ALERTAS_FILE, "w", encoding="utf-8") as f:
                json.dump(lista, f, indent=2)
        except Exception as e:
            log.warning(f"Error guardando alertas: {e}")


# ==========================================================
# DESCARGA DE DATOS EN TIEMPO REAL (OHLCV)
# ==========================================================
def _kraken_ohlc(pair, interval_min):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval_min}"
    r = sess.get(url, timeout=12).json()
    if r.get("error"):
        raise ValueError(str(r["error"]))
    res = r["result"]
    key = [k for k in res.keys() if k != "last"][0]
    velas = []
    for row in res[key][-120:]:
        velas.append({
            "t": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[6]),
        })
    return velas


def _yahoo_ohlc(ticker, interval, range_str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={range_str}"
    r = sess.get(url, timeout=12).json()
    chart = r["chart"]["result"][0]
    ts = chart["timestamp"]
    q = chart["indicators"]["quote"][0]
    velas = []
    for i in range(len(ts)):
        if q["open"][i] is None or q["close"][i] is None:
            continue
        velas.append({
            "t": int(ts[i]),
            "o": float(q["open"][i]),
            "h": float(q["high"][i]),
            "l": float(q["low"][i]),
            "c": float(q["close"][i]),
            "v": float(q["volume"][i] or 0.0),
        })
    return velas[-120:]


def obtener_velas(activo, tf="15m"):
    tf_map_kraken = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
    tf_map_yahoo = {
        "5m": ("5m", "5d"),
        "15m": ("15m", "5d"),
        "1h": ("60m", "1mo"),
        "4h": ("60m", "3mo"),
    }
    k_int = tf_map_kraken.get(tf, 15)
    y_int, y_rng = tf_map_yahoo.get(tf, ("15m", "5d"))

    if activo == "BTCUSD":
        try:
            return _kraken_ohlc("XXBTZUSD", k_int), "Kraken Spot"
        except Exception:
            return _yahoo_ohlc("BTC-USD", y_int, y_rng), "Yahoo Finance"

    if activo == "XAUUSD":
        try:
            return _yahoo_ohlc("GC=F", y_int, y_rng), "COMEX Gold (GC=F)"
        except Exception:
            return _kraken_ohlc("PAXGUSD", k_int), "Kraken PAXG"

    if activo == "EURUSD":
        try:
            return _kraken_ohlc("ZEURZUSD", k_int), "Kraken FX"
        except Exception:
            return _yahoo_ohlc("EURUSD=X", y_int, y_rng), "Yahoo FX"

    if activo == "NAS100":
        return _yahoo_ohlc("NQ=F", y_int, y_rng), "CME E-Mini Nasdaq (NQ=F)"

    raise ValueError("Activo no soportado")


# ==========================================================
# MOTOR CUANTITATIVO SMC, KILLZONES, ATR, RVOL Y SCORING
# ==========================================================
def detectar_killzone():
    ahora_utc = datetime.now(timezone.utc)
    h = ahora_utc.hour + ahora_utc.minute / 60.0
    if 7.0 <= h < 10.0:
        return "London Killzone (Alta liquidez)", 15
    if 12.0 <= h < 15.0:
        return "NY AM Killzone (Máxima liquidez)", 20
    if 15.0 <= h < 17.0:
        return "London Close / NY Midday", 10
    if 0.0 <= h < 4.0:
        return "Asian Range (Acumulación)", 5
    return "Fuera de Killzone (Baja liquidez)", 0


def calcular_atr(velas, periodo=14):
    if len(velas) < periodo + 1:
        return 0.0
    trs = []
    for i in range(1, len(velas)):
        h = velas[i]["h"]
        l = velas[i]["l"]
        pc = velas[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-periodo:]) / periodo


def analizar_mercado(activo, tf="15m", modo="ESTÁNDAR"):
    velas, fuente = obtener_velas(activo, tf)
    if len(velas) < 30:
        return "No hay suficientes velas disponibles en este momento."

    actual = velas[-1]
    precio = actual["c"]
    dec = 5 if activo == "EURUSD" else 2

    # 1. Rango de dealing (últimas 50 velas) y Premium/Discount
    ventana = velas[-50:]
    swing_high = max(v["h"] for v in ventana)
    swing_low = min(v["l"] for v in ventana)
    eq = (swing_high + swing_low) / 2.0
    rango_total = max(swing_high - swing_low, 1e-9)
    pos_pct = ((precio - swing_low) / rango_total) * 100.0

    if pos_pct >= 62.0:
        zona_pd = f"PREMIUM ({pos_pct:.1f}%)"
    elif pos_pct <= 38.0:
        zona_pd = f"DISCOUNT ({pos_pct:.1f}%)"
    else:
        zona_pd = f"EQUILIBRIO ({pos_pct:.1f}%)"

    # 2. Estructura (MSS / BOS) comparando últimos 20 vs previos 20
    prev_high = max(v["h"] for v in velas[-35:-10])
    prev_low = min(v["l"] for v in velas[-35:-10])
    rec_high = max(v["h"] for v in velas[-10:])
    rec_low = min(v["l"] for v in velas[-10:])

    if rec_high > prev_high and precio > eq:
        estructura = "ALCISTA (BOS/MSS al alza)"
        sesgo = "COMPRA"
    elif rec_low < prev_low and precio < eq:
        estructura = "BAJISTA (BOS/MSS a la baja)"
        sesgo = "VENTA"
    elif pos_pct <= 35.0:
        estructura = "REVERSIÓN EN DISCOUNT"
        sesgo = "COMPRA"
    elif pos_pct >= 65.0:
        estructura = "REVERSIÓN EN PREMIUM"
        sesgo = "VENTA"
    else:
        estructura = "CONSOLIDACIÓN / RANGO"
        sesgo = "NEUTRAL"

    # 3. Detección de FVG (Fair Value Gap) reciente
    fvg_txt = "Sin FVG limpio cercano"
    fvg_val = None
    for i in range(len(velas) - 3, len(velas) - 18, -1):
        v1, v3 = velas[i - 2], velas[i]
        if v1["h"] < v3["l"]:
            fvg_val = (v1["h"], v3["l"])
            fvg_txt = f"FVG Alcista: {v1['h']:.{dec}f} - {v3['l']:.{dec}f}"
            break
        if v1["l"] > v3["h"]:
            fvg_val = (v3["h"], v1["l"])
            fvg_txt = f"FVG Bajista: {v3['h']:.{dec}f} - {v1['l']:.{dec}f}"
            break

    # 4. Order Block (última vela contraria antes de impulso)
    ob_txt = "No confirmado"
    for i in range(len(velas) - 3, len(velas) - 20, -1):
        v = velas[i]
        impulso = abs(velas[i + 1]["c"] - velas[i + 1]["o"])
        cuerpo = abs(v["c"] - v["o"])
        if impulso > cuerpo * 1.8 and cuerpo > 0:
            if sesgo == "COMPRA" and v["c"] < v["o"]:
                ob_txt = f"OB Alcista: {v['l']:.{dec}f} - {v['h']:.{dec}f}"
                break
            if sesgo == "VENTA" and v["c"] > v["o"]:
                ob_txt = f"OB Bajista: {v['l']:.{dec}f} - {v['h']:.{dec}f}"
                break

    # 5. Volumen Relativo (RVOL) y ATR
    vols = [v["v"] for v in velas[-21:-1] if v["v"] > 0]
    vol_prom = (sum(vols) / len(vols)) if vols else 0.0
    rvol = (actual["v"] / vol_prom) if vol_prom > 0 else 1.0
    atr = calcular_atr(velas, 14)
    kz_nombre, kz_pts = detectar_killzone()

    # 6. Sistema de Puntuación Cuantitativa (0 - 100 pts)
    score = 30
    score += kz_pts
    if (sesgo == "COMPRA" and pos_pct <= 42.0) or (sesgo == "VENTA" and pos_pct >= 58.0):
        score += 20
    if fvg_val is not None:
        score += 15
    if ob_txt != "No confirmado":
        score += 10
    if rvol >= 1.2:
        score += 10
    if sesgo == "NEUTRAL":
        score = min(score, 48)
    score = min(score, 100)

    # 7. Niveles exactos de Ejecución, SL y TP (Riesgo/Beneficio)
    mult_sl = 1.2 if modo == "SCALPING" else 1.8
    if sesgo == "COMPRA":
        entrada = precio
        sl = min(rec_low, precio - atr * mult_sl)
        riesgo = max(entrada - sl, atr * 0.5)
        tp1 = entrada + riesgo * 2.0
        tp2 = max(swing_high, entrada + riesgo * 3.0)
        accion = "🟢 BUSCAR COMPRAS (LONG)"
    elif sesgo == "VENTA":
        entrada = precio
        sl = max(rec_high, precio + atr * mult_sl)
        riesgo = max(sl - entrada, atr * 0.5)
        tp1 = entrada - riesgo * 2.0
        tp2 = min(swing_low, entrada - riesgo * 3.0)
        accion = "🔴 BUSCAR VENTAS (SHORT)"
    else:
        entrada = precio
        sl = precio - atr * mult_sl
        tp1 = swing_high
        tp2 = swing_low
        accion = "🟡 ESPERAR CONFIRMACIÓN (RANGO)"

    semaforo = "🔥 ALTA PROBABILIDAD" if score >= 75 else ("⚠️ MODERADA" if score >= 55 else "🛑 NO OPERAR / ESPERAR")

    return (
        f"📊 <b>ANÁLISIS SMC — {activo} ({tf.upper()})</b>\n"
        f"<i>Modo: {modo} | Fuente: {fuente}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Precio Actual:</b> <code>{precio:.{dec}f}</code>\n"
        f"🎯 <b>Score Cuantitativo:</b> <b>{score}/100</b> ({semaforo})\n"
        f"📌 <b>Sesgo:</b> {accion}\n\n"
        f"🏛 <b>Estructura & Liquidez:</b>\n"
        f"• Estructura: {estructura}\n"
        f"• Zona P/D: {zona_pd}\n"
        f"• Swing High (BSL): <code>{swing_high:.{dec}f}</code>\n"
        f"• Equilibrium (50%): <code>{eq:.{dec}f}</code>\n"
        f"• Swing Low (SSL): <code>{swing_low:.{dec}f}</code>\n\n"
        f"🧲 <b>Confluencias Institucionales:</b>\n"
        f"• {fvg_txt}\n"
        f"• {ob_txt}\n"
        f"• Sesión: {kz_nombre}\n"
        f"• RVOL: {rvol:.2f}x | ATR(14): {atr:.{dec}f}\n\n"
        f"📐 <b>Plan de Ejecución:</b>\n"
        f"• Entrada: <code>{entrada:.{dec}f}</code>\n"
        f"• Stop Loss: <code>{sl:.{dec}f}</code>\n"
        f"• TP1 (1:2): <code>{tp1:.{dec}f}</code>\n"
        f"• TP2 (Liquidez): <code>{tp2:.{dec}f}</code>"
    )


def analizar_plan_multitf(activo):
    r1h = analizar_mercado(activo, "1h", "DAY TRADING")
    r15m = analizar_mercado(activo, "15m", "CONFIRMACIÓN M15")
    return f"🧭 <b>PLAN MULTI-TEMPORAL (1H + 15M)</b>\n\n{r1h}\n\n━━━━━━━━━━━━━━━━━━━━\n\n{r15m}"


# ==========================================================
# VIGILANTE DE ALERTAS EN SEGUNDO PLANO
# ==========================================================
def normalizar_activo(txt):
    t = txt.upper().strip()
    if t in ("BTC", "BTCUSD", "BITCOIN"):
        return "BTCUSD"
    if t in ("ORO", "XAU", "XAUUSD", "GOLD"):
        return "XAUUSD"
    if t in ("EUR", "EURUSD"):
        return "EURUSD"
    if t in ("NAS", "NAS100", "NASDAQ", "NQ"):
        return "NAS100"
    return None


def vigilar_alertas():
    while True:
        time.sleep(30)
        alertas = cargar_alertas()
        if not alertas:
            continue
        restantes = []
        cambio = False
        for a in alertas:
            try:
                velas, _ = obtener_velas(a["activo"], "5m")
                precio = velas[-1]["c"]
                disparada = False
                if a["tipo"] == "COMPRA" and precio <= a["objetivo"]:
                    disparada = True
                elif a["tipo"] == "VENTA" and precio >= a["objetivo"]:
                    disparada = True

                if disparada:
                    cambio = True
                    enviar_mensaje(
                        a["chat_id"],
                        f"🚨 <b>ALERTA SMC EJECUTADA</b> 🚨\n"
                        f"• Activo: <b>{a['activo']}</b>\n"
                        f"• Dirección: <b>{a['tipo']}</b>\n"
                        f"• Nivel Objetivo: <code>{a['objetivo']}</code>\n"
                        f"• Precio Actual: <code>{precio}</code>",
                    )
                else:
                    restantes.append(a)
            except Exception:
                restantes.append(a)
        if cambio:
            guardar_alertas(restantes)


# ==========================================================
# PROCESAMIENTO DE COMANDOS TELEGRAM
# ==========================================================
def procesar_mensaje(upd):
    msg = upd.get("message") or upd.get("edited_message")
    if not msg or "text" not in msg:
        return

    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    texto = msg["text"].strip()

    if not autorizado(uid):
        enviar_mensaje(chat_id, "⛔ Acceso no autorizado.")
        return

    partes = texto.split()
    cmd = partes[0].lower().split("@")[0]

    if cmd == "/start":
        panel = (
            "🤖 <b>BOT CUANTITATIVO SMC — EN VIVO</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Análisis institucional automático con liquidez, FVG, Order Blocks, Killzones, RVOL y Score (0-100).\n\n"
            "📈 <b>Análisis por Activo:</b>\n"
            "• <code>/btc 1h</code> — Bitcoin (5m, 15m, 1h, 4h)\n"
            "• <code>/oro 15m</code> — Oro XAU/USD\n"
            "• <code>/eurusd 1h</code> — Euro / Dólar\n"
            "• <code>/nas100 15m</code> — NASDAQ 100\n\n"
            "⚡ <b>Modos Operativos:</b>\n"
            "• <code>/scalping oro</code> — Gatillo rápido M5\n"
            "• <code>/daytrading nas100</code> — Estructura H1\n"
            "• <code>/plan btc</code> — Cruce Multi-TF (1H + 15M)\n\n"
            "🔔 <b>Alertas de Precio 24/7:</b>\n"
            "• <code>/comprar oro 2650</code>\n"
            "• <code>/vender btc 95000</code>\n"
            "• <code>/alertas</code> | <code>/borrar</code>"
        )
        enviar_mensaje(chat_id, panel)
        return

    mapa_directo = {
        "/btc": "BTCUSD",
        "/oro": "XAUUSD",
        "/eurusd": "EURUSD",
        "/nas100": "NAS100",
    }
    if cmd in mapa_directo:
        tf = partes[1].lower() if len(partes) > 1 and partes[1].lower() in ("5m", "15m", "1h", "4h") else "15m"
        enviar_mensaje(chat_id, f"⏳ Analizando <b>{mapa_directo[cmd]}</b> en {tf.upper()}...")
        try:
            res = analizar_mercado(mapa_directo[cmd], tf, "ESTÁNDAR")
            enviar_mensaje(chat_id, res)
        except Exception as e:
            enviar_mensaje(chat_id, f"❌ Error obteniendo datos: {e}")
        return

    if cmd in ("/scalping", "/daytrading", "/plan"):
        if len(partes) < 2:
            enviar_mensaje(chat_id, f"⚠️ Indica el activo. Ejemplo: <code>{cmd} oro</code>")
            return
        activo = normalizar_activo(partes[1])
        if not activo:
            enviar_mensaje(chat_id, "⚠️ Activo no reconocido. Usa: btc, oro, eurusd o nas100.")
            return
        enviar_mensaje(chat_id, f"⏳ Calculando {cmd.upper()} para <b>{activo}</b>...")
        try:
            if cmd == "/scalping":
                res = analizar_mercado(activo, "5m", "SCALPING")
            elif cmd == "/daytrading":
                res = analizar_mercado(activo, "1h", "DAY TRADING")
            else:
                res = analizar_plan_multitf(activo)
            enviar_mensaje(chat_id, res)
        except Exception as e:
            enviar_mensaje(chat_id, f"❌ Error en análisis: {e}")
        return

    if cmd in ("/comprar", "/vender"):
        if len(partes) < 3:
            enviar_mensaje(chat_id, f"⚠️ Formato: <code>{cmd} oro 2650</code>")
            return
        activo = normalizar_activo(partes[1])
        if not activo:
            enviar_mensaje(chat_id, "⚠️ Activo inválido. Usa: btc, oro, eurusd o nas100.")
            return
        try:
            objetivo = float(partes[2].replace(",", "."))
        except ValueError:
            enviar_mensaje(chat_id, "⚠️ El precio debe ser un número válido.")
            return
        tipo = "COMPRA" if cmd == "/comprar" else "VENTA"
        alertas = cargar_alertas()
        alertas.append({
            "chat_id": chat_id,
            "activo": activo,
            "tipo": tipo,
            "objetivo": objetivo,
        })
        guardar_alertas(alertas)
        enviar_mensaje(chat_id, f"✅ Alerta guardada: <b>{tipo}</b> en <b>{activo}</b> a <code>{objetivo}</code>.")
        return

    if cmd == "/alertas":
        alertas = cargar_alertas()
        if not alertas:
            enviar_mensaje(chat_id, "📭 No tienes alertas activas.")
            return
        lineas = ["🔔 <b>Alertas Activas:</b>"]
        for idx, a in enumerate(alertas, 1):
            lineas.append(f"{idx}. <b>{a['activo']}</b> — {a['tipo']} en <code>{a['objetivo']}</code>")
        enviar_mensaje(chat_id, "\n".join(lineas))
        return

    if cmd == "/borrar":
        guardar_alertas([])
        enviar_mensaje(chat_id, "🗑 Todas las alertas han sido eliminadas.")
        return


# ==========================================================
# SERVIDOR DE SALUD HTTP (RENDER / UPTIMEROBOT)
# ==========================================================
def iniciar_servidor_salud():
    port = int(os.environ.get("PORT", "10000"))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK - SMC Bot Online")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=srv.serve_forever
            
