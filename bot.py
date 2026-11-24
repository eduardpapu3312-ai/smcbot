# -*- coding: utf-8 -*-
"""
BOT CUANTITATIVO SMC & SCALPING/DAYTRADING 24/7 (RENDER + UPTIMEROBOT)
Datos en tiempo real automáticos para:
- BTCUSD (Binance / Kraken)
- XAUUSD / Oro (Kraken PAXG + Yahoo Finance GC=F respaldo)
- EURUSD (Kraken + Yahoo Finance EURUSD=X respaldo)
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

# ------------------------------------------------------------------ #
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ------------------------------------------------------------------ #
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("Falta la variable de entorno BOT_TOKEN")

ALLOWED_USER = str(os.environ.get("ALLOWED_USER_ID", "")).strip()
URL = f"https://api.telegram.org/bot{TOKEN}"
ALERTAS_FILE = "alertas_smc.json"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("smc-live")

sess = requests.Session()
sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

ACTIVOS = {
    "btc":    dict(code="BTCUSD", name="Bitcoin (BTC/USD)",   dec=2, kraken="XBTUSD",  yahoo="BTC-USD"),
    "btcusd": dict(code="BTCUSD", name="Bitcoin (BTC/USD)",   dec=2, kraken="XBTUSD",  yahoo="BTC-USD"),
    "oro":    dict(code="XAUUSD", name="Oro (XAU/USD)",       dec=2, kraken="PAXGUSD", yahoo="GC=F"),
    "xau":    dict(code="XAUUSD", name="Oro (XAU/USD)",       dec=2, kraken="PAXGUSD", yahoo="GC=F"),
    "xauusd": dict(code="XAUUSD", name="Oro (XAU/USD)",       dec=2, kraken="PAXGUSD", yahoo="GC=F"),
    "eur":    dict(code="EURUSD", name="Euro/Dólar (EUR/USD)",dec=5, kraken="EURUSD",  yahoo="EURUSD=X"),
    "eurusd": dict(code="EURUSD", name="Euro/Dólar (EUR/USD)",dec=5, kraken="EURUSD",  yahoo="EURUSD=X"),
    "nas":    dict(code="NAS100", name="NASDAQ 100 (US100)",  dec=2, kraken=None,      yahoo="NQ=F"),
    "nas100": dict(code="NAS100", name="NASDAQ 100 (US100)",  dec=2, kraken=None,      yahoo="NQ=F"),
    "us100":  dict(code="NAS100", name="NASDAQ 100 (US100)",  dec=2, kraken=None,      yahoo="NQ=F"),
}

TFS = {
    "1m":  dict(k_iv=1,   y_iv="1m",  y_rng="1d"),
    "5m":  dict(k_iv=5,   y_iv="5m",  y_rng="5d"),
    "15m": dict(k_iv=15,  y_iv="15m", y_rng="5d"),
    "1h":  dict(k_iv=60,  y_iv="1h",  y_rng="1mo"),
    "4h":  dict(k_iv=240, y_iv="1h",  y_rng="3mo", agg=4),
    "1d":  dict(k_iv=1440,y_iv="1d",  y_rng="6mo"),
}

# ------------------------------------------------------------------ #
# DESCARGA DE VELAS OHLCV + VOLUMEN EN TIEMPO REAL
# ------------------------------------------------------------------ #
def _velas_kraken(pair: str, interval: int):
    r = sess.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": interval}, timeout=8).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"]))
    raw = [v for k, v in r["result"].items() if k != "last"][0]
    velas = []
    for x in raw:
        velas.append({
            "t": int(x[0]),
            "o": float(x[1]),
            "h": float(x[2]),
            "l": float(x[3]),
            "c": float(x[4]),
            "v": float(x[6]),
        })
    return velas[-120:]

def _velas_yahoo(symbol: str, interval: str, rng: str, agg: int = 1):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = sess.get(url, params={"interval": interval, "range": rng}, timeout=8).json()
    res = r["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    velas = []
    for i in range(len(ts)):
        if q["close"][i] is None or q["high"][i] is None or q["low"][i] is None:
            continue
        velas.append({
            "t": int(ts[i]),
            "o": float(q["open"][i] or q["close"][i]),
            "h": float(q["high"][i]),
            "l": float(q["low"][i]),
            "c": float(q["close"][i]),
            "v": float(q["volume"][i] or 1.0),
        })
    if agg > 1 and len(velas) >= agg:
        velas = velas[len(velas) % agg:]
        agrupadas = []
        for i in range(0, len(velas), agg):
            g = velas[i:i+agg]
            agrupadas.append({
                "t": g[0]["t"],
                "o": g[0]["o"],
                "h": max(x["h"] for x in g),
                "l": min(x["l"] for x in g),
                "c": g[-1]["c"],
                "v": sum(x["v"] for x in g),
            })
        velas = agrupadas
    return velas[-120:]

def obtener_velas(act_key: str, tf_key: str):
    info = ACTIVOS[act_key]
    tf = TFS[tf_key]
    # Para NASDAQ usamos Yahoo directamente; para el resto intentamos Kraken y respaldo Yahoo
    if info["kraken"]:
        try:
            v = _velas_kraken(info["kraken"], tf["k_iv"])
            if len(v) >= 30:
                return v
        except Exception as e:
            log.warning(f"Kraken fallback para {act_key}: {e}")
    return _velas_yahoo(info["yahoo"], tf["y_iv"], tf["y_rng"], tf.get("agg", 1))

def precio_actual(act_key: str) -> float:
    velas = obtener_velas(act_key, "5m")
    return velas[-1]["c"]

# ------------------------------------------------------------------ #
# INDICADORES TÉCNICOS, VOLUMEN VSA/RVOL Y SMC
# ------------------------------------------------------------------ #
def calc_ema(p, n):
    k = 2 / (n + 1)
    v = p[0]
    for x in p[1:]:
        v = x * k + v * (1 - k)
    return v

def calc_rsi(p, n=14):
    g = [max(p[i] - p[i-1], 0) for i in range(1, len(p))]
    l = [max(p[i-1] - p[i], 0) for i in range(1, len(p))]
    ag = sum(g[:n]) / n
    al = sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))

def calc_atr(velas, n=14):
    trs = []
    for i in range(1, len(velas)):
        tr = max(
            velas[i]["h"] - velas[i]["l"],
            abs(velas[i]["h"] - velas[i-1]["c"]),
            abs(velas[i]["l"] - velas[i-1]["c"]),
        )
        trs.append(tr)
    return sum(trs[-n:]) / max(1, min(n, len(trs)))

def killzone_ny():
    # Hora actual en Nueva York (UTC-5 invierno / UTC-4 verano)
    utc_now = datetime.now(timezone.utc)
    ny_now = utc_now - timedelta(hours=5)
    h = ny_now.hour + ny_now.minute / 60.0
    hora_str = ny_now.strftime("%H:%M EST")
    if 8.0 <= h < 11.0:
        return f"NY Open Killzone ({hora_str})", 15
    if 2.0 <= h < 5.0:
        return f"London Open Killzone ({hora_str})", 12
    if 14.0 <= h < 15.0:
        return f"Silver Bullet PM ({hora_str})", 10
    if 16.0 <= h < 19.5:
        return f"Zona Muerta Cierre NY ({hora_str})", 3
    return f"Sesión Asia / Transición ({hora_str})", 8

def detectar_smc(velas):
    """Detecta PDH/PDL proxy, FVG (Fair Value Gaps), Order Blocks y Liquidez (BSL/SSL)."""
    h = [v["h"] for v in velas]
    l = [v["l"] for v in velas]
    bsl = max(h[-30:-1])  # Buy-Side Liquidity (máximo relevante)
    ssl = min(l[-30:-1])  # Sell-Side Liquidity (mínimo relevante)
    eq = (bsl + ssl) / 2.0

    # Buscar último Fair Value Gap (FVG) en últimas 15 velas
    fvg_txt = "Sin FVG limpio cercano"
    for i in range(len(velas) - 2, max(2, len(velas) - 15), -1):
        # FVG Alcista: mínimo vela i+1 > máximo vela i-1
        if velas[i+1]["l"] > velas[i-1]["h"]:
            fvg_txt = f"FVG Alcista: {velas[i-1]['h']:.2f} - {velas[i+1]['l']:.2f}"
            break
        # FVG Bajista: máximo vela i+1 < mínimo vela i-1
        if velas[i+1]["h"] < velas[i-1]["l"]:
            fvg_txt = f"FVG Bajista: {velas[i+1]['h']:.2f} - {velas[i-1]['l']:.2f}"
            break

    # Barrido de liquidez (Sweep) en últimas 3 velas
    sweep = "Ninguno reciente"
    for v in velas[-3:]:
        if v["l"] < ssl and v["c"] > ssl:
            sweep = "Barrido SSL (Stop Hunt alcista detectado)"
        elif v["h"] > bsl and v["c"] < bsl:
            sweep = "Barrido BSL (Stop Hunt bajista detectado)"

    return bsl, ssl, eq, fvg_txt, sweep

def analizar_volumen(velas):
    vols = [v["v"] for v in velas[-21:-1] if v["v"] > 0]
    v_act = velas[-1]["v"]
    if not vols or v_act <= 0:
        return 1.0, "Volumen tick estándar (sin anomalía)"
    rvol = v_act / (sum(vols) / len(vols))
    v_last = velas[-1]
    rango = max(1e-9, v_last["h"] - v_last["l"])
    cuerpo = abs(v_last["c"] - v_last["o"])
    mecha_inf = min(v_last["o"], v_last["c"]) - v_last["l"]
    mecha_sup = v_last["h"] - max(v_last["o"], v_last["c"])

    if rvol >= 1.4 and (mecha_inf > cuerpo * 1.3 or mecha_sup > cuerpo * 1.3):
        vsa = "Absorción institucional (mecha + volumen climático)"
    elif rvol >= 1.3 and cuerpo / rango > 0.65:
        vsa = "Expansión con fuerza real (RVOL alto)"
    elif rvol < 0.85:
        vsa = "Volumen bajo / precaución ante falsa ruptura"
    else:
        vsa = "Flujo normal equilibrado"
    return rvol, vsa

# ------------------------------------------------------------------ #
# MOTOR PRINCIPAL DE ANÁLISIS (SCALPING, DAY TRADING, SMC, COMPLETO)
# ------------------------------------------------------------------ #
def generar_reporte(act_key: str, tf_key: str, modo: str = "COMPLETO") -> str:
    info = ACTIVOS[act_key]
    dec = info["dec"]
    velas = obtener_velas(act_key, tf_key)
    c = [v["c"] for v in velas]
    pr = c[-1]

    e20 = calc_ema(c[-50:], 20)
    e50 = calc_ema(c, 50)
    macd = calc_ema(c[-40:], 12) - calc_ema(c[-60:], 26)
    rsi = calc_rsi(c, 14)
    atr = calc_atr(velas, 14)
    bsl, ssl, eq, fvg_txt, sweep = detectar_smc(velas)
    rvol, vsa_txt = analizar_volumen(velas)
    kz_txt, kz_pts = killzone_ny()

    # VWAP aproximado últimas 24 velas
    vwap_num = sum(((v["h"] + v["l"] + v["c"]) / 3.0) * v["v"] for v in velas[-24:])
    vwap_den = sum(v["v"] for v in velas[-24:]) or 1.0
    vwap = vwap_num / vwap_den

    # Matriz de puntuación cuantitativa (0 - 100 pts)
    pts_est = 25 if (pr > e20 > e50 or pr < e20 < e50) else (15 if (pr > e50 or pr < e50) else 8)
    alcista = pr >= e50
    zona_pd = "Discount (Descuento)" if pr < eq else "Premium (Caro)"
    pts_smc = 22 if ((alcista and pr <= eq) or (not alcista and pr >= eq)) else 12
    if "Barrido" in sweep:
        pts_smc = min(25, pts_smc + 5)

    pts_vol = 20 if rvol >= 1.3 else (14 if rvol >= 0.95 else 7)
    pts_mom = 15 if ((alcista and macd > 0 and 40 <= rsi <= 68) or (not alcista and macd < 0 and 32 <= rsi <= 60)) else 8
    total_pts = min(100, pts_est + pts_smc + pts_vol + pts_mom + kz_pts)

    # Decisión y niveles exactos con colchón ATR
    if alcista and rsi >= 72:
        veredicto = "🟡 ESPERAR RETROCESO (Sobrecompra)"
        plan = (
            f"• No perseguir arriba; esperar pullback a EMA20 / VWAP\n"
            f"• Zona compra ideal: `{min(e20, vwap):,.{dec}f}`\n"
            f"• SL técnico: `{min(e20, vwap) - atr * 1.4:,.{dec}f}`"
        )
    elif not alcista and rsi <= 28:
        veredicto = "🟡 ESPERAR REBOTE (Sobreventa)"
        plan = (
            f"• Evitar vender en piso; esperar rebote a EMA20 / VWAP\n"
            f"• Zona venta ideal: `{max(e20, vwap):,.{dec}f}`\n"
            f"• SL técnico: `{max(e20, vwap) + atr * 1.4:,.{dec}f}`"
        )
    elif total_pts >= 75 and alcista:
        ent = max(e20, pr - atr * 0.25)
        sl = ent - atr * 1.5
        tp1 = ent + atr * 2.2
        tp2 = max(bsl, ent + atr * 3.5)
        veredicto = "🟢 COMPRAR (BUY — Setup Válido)"
        plan = (
            f"• Entrada: `{ent:,.{dec}f}` (retesteo vela actual)\n"
            f"• Stop Loss: `{sl:,.{dec}f}` (1.5x ATR)\n"
            f"• TP1 (Break Even): `{tp1:,.{dec}f}` | TP2: `{tp2:,.{dec}f}`"
        )
    elif total_pts >= 75 and not alcista:
        ent = min(e20, pr + atr * 0.25)
        sl = ent + atr * 1.5
        tp1 = ent - atr * 2.2
        tp2 = min(ssl, ent - atr * 3.5)
        veredicto = "🔴 VENDER (SELL — Setup Válido)"
        plan = (
            f"• Entrada: `{ent:,.{dec}f}` (retesteo vela actual)\n"
            f"• Stop Loss: `{sl:,.{dec}f}` (1.5x ATR)\n"
            f"• TP1 (Break Even): `{tp1:,.{dec}f}` | TP2: `{tp2:,.{dec}f}`"
        )
    else:
        veredicto = "🟡 NO OPERAR (Confluencia < 75 pts)"
        plan = (
            f"• Mercado sin alineación completa. Esperar ruptura confirmada:\n"
            f"• Ruptura alcista: > `{bsl:,.{dec}f}` | Bajista: < `{ssl:,.{dec}f}`"
        )

    tend_txt = "🟢 Alcista" if pr > e20 > e50 else ("🔴 Bajista" if pr < e20 < e50 else "🟡 Rango / Transición")

    return (
        f"📊 *{info['name']} ({tf_key.upper()}) | {modo}*\n"
        f"🕒 {kz_txt}\n\n"
        f"💰 *Precio:* `{pr:,.{dec}f}` | *VWAP:* `{vwap:,.{dec}f}`\n"
        f"🧭 *Tendencia:* {tend_txt} | *Zona:* {zona_pd}\n"
        f"〰️ *EMA20:* `{e20:,.{dec}f}` | *EMA50:* `{e50:,.{dec}f}`\n"
        f"⚡ *RSI(14):* `{rsi:.1f}` | *ATR(14):* `{atr:,.{dec}f}`\n\n"
        f"🎯 *LIQUIDEZ Y SMC*\n"
        f"• *Techo / BSL:* `{bsl:,.{dec}f}`\n"
        f"• *Piso / SSL:* `{ssl:,.{dec}f}`\n"
        f"• *Desequilibrio:* {fvg_txt}\n"
        f"• *Trampas:* {sweep}\n\n"
        f"📶 *VOLUMEN (RVOL: {rvol:.2f}x)*\n"
        f"• {vsa_txt}\n\n"
        f"🧮 *CONFLUENCIA: {total_pts}/100 pts*\n"
        f"🚦 *VEREDICTO:* *{veredicto}*\n\n"
        f"⚡ *PLAN DE EJECUCIÓN*\n{plan}"
    )

def plan_multitf(act_key: str) -> str:
    info = ACTIVOS[act_key]
    dec = info["dec"]
    v1h = obtener_velas(act_key, "1h")
    v15 = obtener_velas(act_key, "15m")
    c1h = [x["c"] for x in v1h]
    c15 = [x["c"] for x in v15]
    alc_1h = c1h[-1] > calc_ema(c1h, 50)
    alc_15 = c15[-1] > calc_ema(c15[-50:], 20)
    alineado = alc_1h == alc_15
    estado = "✅ *ALTA PROBABILIDAD (1H y 15M alineados)*" if alineado else "⚠️ *PRECAUCIÓN (1H y 15M en contra)*"
    rep_15 = generar_reporte(act_key, "15m", "MULTI-TF")
    return (
        f"🎯 *CRUCE MULTI-TEMPORALIDAD — {info['name']}*\n"
        f"• *Sesgo 1H:* {'🟢 Alcista' if alc_1h else '🔴 Bajista'}\n"
        f"• *Gatillo 15M:* {'🟢 Alcista' if alc_15 else '🔴 Bajista'}\n"
        f"{estado}\n\n" + rep_15
    )

# ------------------------------------------------------------------ #
# GESTIÓN DE ALERTAS Y TELEGRAM
# ------------------------------------------------------------------ #
def cargar_alertas():
    if os.path.exists(ALERTAS_FILE):
        try:
            with open(ALERTAS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def guardar_alertas(data):
    with open(ALERTAS_FILE, "w") as f:
        json.dump(data, f)

alertas = cargar_alertas()

def enviar(chat_id: str, txt: str):
    try:
        r = sess.post(f"{URL}/sendMessage", data={"chat_id": chat_id, "text": txt, "parse_mode": "Markdown"}, timeout=8).json()
        if not r.get("ok"):
            sess.post(f"{URL}/sendMessage", data={"chat_id": chat_id, "text": txt}, timeout=8)
    except Exception as e:
        log.error(f"Error enviando mensaje: {e}")

def procesar_mensaje(chat_id: str, user_id: str, txt: str):
    if ALLOWED_USER and user_id != ALLOWED_USER:
        enviar(chat_id, "⛔ Bot privado.")
        return

    try:
        sess.post(f"{URL}/sendChatAction", data={"chat_id": chat_id, "action": "typing"}, timeout=3)
    except Exception:
        pass

    p = txt.replace("\xa0", " ").strip().lower().split()
    cmd = p[0] if p else ""

    if cmd in ("/start", "/help", "/ayuda"):
        enviar(
            chat_id,
            "🤖 *BOT CUANTITATIVO SMC — EN VIVO*\n\n"
            "📊 *Análisis Directo (Datos Reales Automáticos):*\n"
            "• `/btc 1h` (soporta `1m, 5m, 15m, 1h, 4h, 1d`)\n"
            "• `/oro 15m` o `/xau 1h`\n"
            "• `/eurusd 1h` o `/eur 15m`\n"
            "• `/nas100 15m` o `/nas 1h`\n\n"
            "⚡ *Modos Especializados:*\n"
            "• `/scalping oro` (análisis rápido M5)\n"
            "• `/daytrading nas100` (análisis H1 + H4)\n"
            "• `/plan btc` (cruce 1H + 15M)\n\n"
            "🔔 *Alertas de Precio:*\n"
            "• `/comprar oro 2650` | `/vender btc 95000`\n"
            "• `/alertas` | `/borrar`"
        )
        return

    if cmd in ("/btc", "/btcusd", "/oro", "/xau", "/xauusd", "/eur", "/eurusd", "/nas", "/nas100", "/us100"):
        act = cmd[1:]
        tf = p[1] if len(p) > 1 and p[1] in TFS else "1h"
        try:
            enviar(chat_id, generar_reporte(act, tf, f"TF {tf.upper()}"))
        except Exception as e:
            enviar(chat_id, f"⚠️ Error obteniendo datos de mercado: {e}")
        return

    if cmd == "/scalping":
        act = p[1] if len(p) > 1 and p[1] in ACTIVOS else "oro"
        try:
            enviar(chat_id, generar_reporte(act, "5m", "SCALPING M5"))
        except Exception as e:
            enviar(chat_id, f"⚠️ Error scalping {act}: {e}")
        return

    if cmd == "/daytrading":
        act = p[1] if len(p) > 1 and p[1] in ACTIVOS else "oro"
        try:
            enviar(chat_id, generar_reporte(act, "1h", "DAY TRADING H1"))
        except Exception as e:
            enviar(chat_id, f"⚠️ Error daytrading {act}: {e}")
        return

    if cmd == "/plan":
        act = p[1] if len(p) > 1 and p[1] in ACTIVOS else "btc"
        try:
            enviar(chat_id, plan_multitf(act))
        except Exception as e:
            enviar(chat_id, f"⚠️ Error plan {act}: {e}")
        return

    if cmd in ("/comprar", "/vender"):
        tipo = "COMPRA" if cmd == "/comprar" else "VENTA"
        try:
            act = p[1] if len(p) >= 3 and p[1] in ACTIVOS else "btc"
            val = p[2] if len(p) >= 3 and p[1] in ACTIVOS else p[1]
            obj = float(val.replace(",", ""))
            info = ACTIVOS[act]
            act_p = precio_actual(act)
            dir_a = "abajo" if obj < act_p else "arriba"
            alertas.append({"chat_id": chat_id, "activo": act, "tipo": tipo, "precio": obj, "direccion": dir_a})
            guardar_alertas(alertas)
            enviar(chat_id, f"🔔 *Alerta {tipo} ({info['name']})* en `{obj:,.{info['dec']}f}` (Actual: `{act_p:,.{info['dec']}f}`)")
        except Exception:
            enviar(chat_id, "⚠️ Uso correcto: `/comprar oro 2650` o `/vender nas100 21500`")
        return

    if cmd == "/alertas":
        if not alertas:
            enviar(chat_id, "📭 No tienes alertas activas.")
        else:
            msg = "\n".join(f"{i}. {a['activo'].upper()} {a['tipo']} @ {a['precio']}" for i, a in enumerate(alertas, 1))
            enviar(chat_id, f"🔔 *Alertas activas:*\n{msg}")
        return

    if cmd == "/borrar":
        alertas.clear()
        guardar_alertas(alertas)
        enviar(chat_id, "🗑️ Todas las alertas fueron eliminadas.")
        return

# ------------------------------------------------------------------ #
# SERVIDOR WEB DE SALUD (RENDER + UPTIMEROBOT)
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
            self.send_response(200)
            self.end_headers()
        def log_message(self, *a):
            pass
    threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever(), daemon=True).start()

def registrar_comandos():
    try:
        sess.post(f"{URL}/setMyCommands", json={"commands": [
            {"command": "btc",        "description": "Análisis en vivo BTC/USD (ej. /btc 1h)"},
            {"command": "oro",        "description": "Análisis en vivo Oro XAU/USD (ej. /oro 15m)"},
            {"command": "eurusd",     "description": "Análisis en vivo EUR/USD (ej. /eurusd 1h)"},
  
