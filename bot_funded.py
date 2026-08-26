"""
AURUM FUNDED — Motor Principal
================================
Bot dedicado para operar la cuenta fondeada de FundedHero.

Cambios vs Aurum original:
  ✅ 1 trade por día (MAX_TRADES_DAY = 1)
  ✅ Sin cierre parcial — solo SL a breakeven al 75% (lote completo corre)
  ✅ Lote fijo calculado sobre balance de referencia inicial (no escala con ganancias)
  ✅ Lot Size Consistency Rule: ajuste automático ±0.5 por trade
  ✅ Sistema de protección en 3 niveles (RiskGuard)
  ✅ Extracción real de balance/equity/info de MT5 al arrancar
  ✅ Registro completo: operaciones, drawdown, alertas
  ✅ Todas las notificaciones Telegram de Aurum original
"""

import asyncio
import json
import logging
import os
import sys
import csv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_funded as config
import MetaTrader5 as mt5

from core.logger        import save_open, save_close, get_open_trades, get_recent_trades, get_stats, get_performance_breakdown
from core.telegram_bot  import (msg_startup, msg_market_analysis, msg_bos_detected,
                                msg_entry, msg_tp_set, msg_close_win, msg_close_loss,
                                msg_daily_summary, msg_ai_analysis, msg_error,
                                msg_levels_ready, msg_ai_daily_summary,
                                msg_retest_waiting, msg_retest_touch_failed,
                                msg_no_trade_summary, msg_level_updated)
from core.ai_engine     import validate_signal, analyze_market_open, post_trade_lesson, daily_learning_summary
from core.mt5_connector import (connect, disconnect, get_balance, get_candles,
                                get_current_price, calc_lot, calc_lot_dynamic,
                                get_symbol_specs, log_symbol_specs,
                                send_order, set_tp,
                                set_sl, close_position_market, get_position_info,
                                check_position_closed, get_market_context)
from risk_guard import RiskGuard, RiskLevel

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
os.makedirs(os.path.join(config.BASE_DIR, "logs"), exist_ok=True)
os.makedirs(config.OPERATIONS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("aurum_funded")
NYC_TZ = ZoneInfo(config.NYC_TIMEZONE)


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS DE TIEMPO
# ─────────────────────────────────────────────────────────────────────────────
def now_nyc() -> datetime:
    return datetime.now(tz=timezone.utc).astimezone(NYC_TZ)

def is_in_window() -> bool:
    nyc = now_nyc()
    h = nyc.hour + nyc.minute / 60
    return config.NYC_WINDOW_START_HOUR <= h < config.NYC_WINDOW_END_HOUR

def is_pre_window() -> bool:
    nyc = now_nyc()
    h = nyc.hour + nyc.minute / 60
    return 1.0 <= h < config.NYC_WINDOW_START_HOUR

def window_label() -> str:
    nyc = now_nyc()
    start = nyc.replace(hour=config.NYC_WINDOW_START_HOUR, minute=0, second=0)
    end   = nyc.replace(hour=config.NYC_WINDOW_END_HOUR,   minute=0, second=0)
    utc_s = start.astimezone(timezone.utc)
    utc_e = end.astimezone(timezone.utc)
    return (f"{config.NYC_WINDOW_START_HOUR}AM–{config.NYC_WINDOW_END_HOUR}PM NYC "
            f"({utc_s.strftime('%H:%M')}–{utc_e.strftime('%H:%M')} UTC)")


# ─────────────────────────────────────────────────────────────────────────────
#  EXTRACCIÓN DE INFO REAL DE MT5
# ─────────────────────────────────────────────────────────────────────────────
def get_account_info_full() -> dict:
    """
    Extrae información completa de la cuenta MT5:
    balance, equity, margin, profit flotante, apalancamiento, servidor.
    Usado al arrancar y en el resumen diario.
    """
    info = mt5.account_info()
    if info is None:
        log.error("No se pudo obtener info de cuenta MT5")
        return {}
    return {
        "balance":    info.balance,
        "equity":     info.equity,
        "margin":     info.margin,
        "free_margin": info.margin_free,
        "profit":     info.profit,
        "leverage":   info.leverage,
        "server":     info.server,
        "currency":   info.currency,
        "login":      info.login,
    }

def get_symbol_info_full() -> dict:
    """
    Extrae información del símbolo XAUUSD: spread actual, digits, tick value.
    Usado para verificar condiciones antes de operar.
    """
    sym = mt5.symbol_info(config.SYMBOL)
    if sym is None:
        log.error(f"No se pudo obtener info del símbolo {config.SYMBOL}")
        return {}
    return {
        "spread":      sym.spread,
        "digits":      sym.digits,
        "tick_value":  sym.trade_tick_value,
        "tick_size":   sym.trade_tick_size,
        "volume_min":  sym.volume_min,
        "volume_max":  sym.volume_max,
        "volume_step": sym.volume_step,
        "bid":         sym.bid,
        "ask":         sym.ask,
    }

def calc_lot_funded(balance: float, reference_balance: float,
                    sl_dist: float, last_lot: float | None,
                    risk_guard: RiskGuard, specs: dict) -> float:
    """
    Calcula el lote para la cuenta fondeada usando specs REALES del broker:
    1. Base: calc_lot_dynamic con tick_value real (no asume $100 fijo)
    2. Tope duro: MAX_RISK_PCT sobre balance actual (dentro de calc_lot_dynamic)
    3. Ajuste Lot Size Consistency Rule: ±0.5 respecto al trade anterior
    4. Redondeo al step real del símbolo
    """
    lot = calc_lot_dynamic(
        balance=balance,
        sl_dist_usd=sl_dist,
        specs=specs,
        risk_pct=config.RISK_PCT,
        reference_balance=reference_balance
    )

    if lot <= 0:
        return 0.0

    # Lot Size Consistency Rule
    _, lot = risk_guard.validate_lot(lot, last_lot)

    return lot


# ─────────────────────────────────────────────────────────────────────────────
#  LÓGICA DE NIVELES (idéntica a Aurum original)
# ─────────────────────────────────────────────────────────────────────────────
def _resample(df, minutes):
    if df is None or df.empty:
        return df
    n = minutes // 5
    if n < 2:
        return df
    rows = []
    for i in range(0, len(df) - n + 1, n):
        chunk = df.iloc[i:i + n]
        rows.append({
            "time":  chunk["time"].iloc[0],
            "open":  chunk["open"].iloc[0],
            "high":  chunk["high"].max(),
            "low":   chunk["low"].min(),
            "close": chunk["close"].iloc[-1],
        })
    import pandas as _pd
    return _pd.DataFrame(rows)

def _find_swings(df, mode, left=2, right=2):
    if df is None or len(df) < left + right + 1:
        return []
    arr = df["high"].values if mode == "high" else df["low"].values
    out = []
    for i in range(left, len(arr) - right):
        if mode == "high":
            if arr[i] > arr[i - left:i].max() and arr[i] > arr[i + 1:i + 1 + right].max():
                out.append(float(arr[i]))
        else:
            if arr[i] < arr[i - left:i].min() and arr[i] < arr[i + 1:i + 1 + right].min():
                out.append(float(arr[i]))
    return out

def find_structural_sl(df, bos_c, direction, entry):
    """
    Ubica el ancla del SL en el swing estructural (2 velas a cada lado) más
    cercano que quede más allá del wick de la vela de BOS, en vez de usar
    siempre solo ese wick — evita dejar el SL justo donde el precio ya
    demostró que puede barrer con ruido, cuando hay un soporte/resistencia
    real un poco más atrás. Si ese swing dejaría el SL fuera de MAX_SL,
    se descarta y se usa el wick de la vela de BOS (comportamiento original).
    """
    if direction == "long":
        swings = [s for s in _find_swings(df, "low", 2, 2) if s <= bos_c["low"]]
        anchor = max(swings) if swings else bos_c["low"]
        if entry - (anchor - config.BOS_SL_BUFFER) > config.MAX_SL:
            anchor = bos_c["low"]
    else:
        swings = [s for s in _find_swings(df, "high", 2, 2) if s >= bos_c["high"]]
        anchor = min(swings) if swings else bos_c["high"]
        if (anchor + config.BOS_SL_BUFFER) - entry > config.MAX_SL:
            anchor = bos_c["high"]
    return anchor

def _nearest_structural_with_count(df, ref_price, mode, min_t=1):
    swings = _find_swings(df, mode, 2, 2)
    zones = []
    for p in sorted(swings):
        placed = False
        for z in zones:
            if abs(p - z["c"]) <= config.SWING_CLUSTER_TOL:
                z["pts"].append(p)
                z["c"] = sum(z["pts"]) / len(z["pts"])
                placed = True
                break
        if not placed:
            zones.append({"c": p, "pts": [p]})
    if mode == "high":
        cands = [(round(z["c"], 2), len(z["pts"])) for z in zones
                 if z["c"] > ref_price and len(z["pts"]) >= min_t]
    else:
        cands = [(round(z["c"], 2), len(z["pts"])) for z in zones
                 if z["c"] < ref_price and len(z["pts"]) >= min_t]
    if not cands:
        return None, 0
    return min(cands, key=lambda x: abs(x[0] - ref_price))

def compute_levels(df, ref_price):
    if df is None or len(df) < config.PRE_WINDOW_BARS:
        return None, None, "sin-datos", "sin-datos", 0, 0
    ref12 = df.tail(config.PRE_WINDOW_BARS)
    kh12  = round(ref12["high"].max(), 2)
    kl12  = round(ref12["low"].min(),  2)
    try:
        london = df[df["time"].dt.hour >= config.SWING_SEARCH_START_HOUR]
        if len(london) < 6:
            london = df
    except Exception:
        london = df
    if ref_price is None:
        ref_price = float(df["close"].iloc[-1])

    def pick(mode, base_level):
        lvl, touches = _nearest_structural_with_count(london, ref_price, mode, min_t=2)
        if lvl is not None and abs(lvl - ref_price) <= config.MAX_LEVEL_DIST:
            return lvl, "M5-swing", touches
        valid_base = (base_level > ref_price) if mode == "high" else (base_level < ref_price)
        if valid_base and abs(base_level - ref_price) <= config.MAX_LEVEL_DIST:
            return base_level, "12-velas", 1
        m15 = _resample(df, 15)
        lvl, touches = _nearest_structural_with_count(m15, ref_price, mode, min_t=2)
        if lvl is not None and abs(lvl - ref_price) <= config.MAX_LEVEL_DIST:
            return lvl, "M15", touches
        m30 = _resample(df, 30)
        lvl, touches = _nearest_structural_with_count(m30, ref_price, mode, min_t=2)
        if lvl is not None and abs(lvl - ref_price) <= config.MAX_LEVEL_DIST:
            return lvl, "M30", touches
        return None, "sin-nivel", 0

    kh, src_h, kh_t = pick("high", kh12)
    kl, src_l, kl_t = pick("low",  kl12)
    if kh is None or kl is None:
        return None, None, "sin-nivel", "sin-nivel", 0, 0
    return round(kh, 2), round(kl, 2), src_h, src_l, kh_t, kl_t


# ─────────────────────────────────────────────────────────────────────────────
#  FALLBACK — NIVELES H1 NO TOCADOS (hasta 1 mes atrás)
#  SOLO se usa cuando compute_levels() no encontró ningún nivel válido en el
#  día. No modifica ni interviene en la lógica original de niveles/BOS/
#  retest/entrada — una vez asignado, el nivel se trata exactamente igual
#  que uno calculado normalmente.
# ─────────────────────────────────────────────────────────────────────────────
def _find_swings_with_index(df, mode, left=2, right=2):
    if df is None or len(df) < left + right + 1:
        return []
    arr = df["high"].values if mode == "high" else df["low"].values
    out = []
    for i in range(left, len(arr) - right):
        if mode == "high":
            if arr[i] > arr[i - left:i].max() and arr[i] > arr[i + 1:i + 1 + right].max():
                out.append((float(arr[i]), i))
        else:
            if arr[i] < arr[i - left:i].min() and arr[i] < arr[i + 1:i + 1 + right].min():
                out.append((float(arr[i]), i))
    return out

def _swing_is_untouched(df, mode, price, formed_idx):
    after = df.iloc[formed_idx + 1:]
    if after.empty:
        return True
    if mode == "high":
        return bool((after["high"] < price).all())
    return bool((after["low"] > price).all())

def find_fallback_level_h1(ref_price: float, mode: str):
    """
    Busca el nivel H1 no tocado más cercano a ref_price, sin tope de distancia,
    dentro de ~1 mes de velas (720 H1). No se llama nunca si compute_levels()
    ya encontró un nivel válido para el día.
    """
    df = get_candles(mt5.TIMEFRAME_H1, 720)
    if df is None or df.empty or len(df) < 10:
        return None, None

    swings = _find_swings_with_index(df, mode, 2, 2)
    candidates = []
    for price, idx in swings:
        if mode == "high" and price <= ref_price:
            continue
        if mode == "low" and price >= ref_price:
            continue
        if _swing_is_untouched(df, mode, price, idx):
            candidates.append(round(price, 2))

    if not candidates:
        return None, None

    nearest = min(candidates, key=lambda p: abs(p - ref_price))
    return nearest, "H1-no-tocado (1 mes)"


def candle_quality(c, direction: str) -> bool:
    rng = c["high"] - c["low"]
    if rng < 0.5:
        return False
    body = abs(c["close"] - c["open"]) / rng
    wick = ((c["high"] - max(c["open"], c["close"])) / rng if direction == "long"
            else (min(c["open"], c["close"]) - c["low"]) / rng)
    return body >= config.MIN_BODY_PCT and wick <= config.MAX_WICK_PCT


# ─────────────────────────────────────────────────────────────────────────────
#  ESTADO DEL BOT
# ─────────────────────────────────────────────────────────────────────────────
class FundedBotState:
    def __init__(self):
        self.start_balance      = 0.0
        self.reference_balance  = 0.0   # Balance inicial fondeado (fijo)
        self.last_lot           = None  # Último lote ejecutado (para consistencia)
        self.symbol_specs       = {}    # Specs reales del símbolo extraídas de MT5
        self.reset_day()

    def reset_day(self):
        self.key_high           = None
        self.key_low            = None
        self.key_high_touches   = 0
        self.key_low_touches    = 0
        self.key_high_updates   = 0   # veces que se actualizó la resistencia hoy
        self.key_low_updates    = 0   # veces que se actualizó el soporte hoy
        self.bos_up             = False
        self.bos_dn             = False
        self.retest_up          = False
        self.retest_dn          = False
        self.bos_candle_up      = None
        self.bos_candle_dn      = None
        self.level_up           = None
        self.level_dn           = None
        self.level_up_touches   = 0
        self.level_dn_touches   = 0
        self.bos_time_up        = None
        self.bos_time_dn        = None
        self.last_retest_notify = None
        self.last_retest_fail_notify = None
        self.trades_today       = 0
        self.open_ticket        = None
        self.open_entry         = None
        self.open_direction     = None
        self.open_time          = None
        self.open_sl            = None
        self.open_tp            = None
        self.open_lot           = None
        self.open_peak_price    = None    # mejor precio alcanzado desde que se abrió el trade
        self.be_done            = False   # SL ya movido a breakeven
        self.window_analyzed    = False
        self.last_status_log    = None
        self.window_close_notified = False  # resumen "no operó" ya enviado hoy
        self.retest_fails       = 0         # retests que tocaron pero no confirmaron
        self.ai_rejections      = 0         # señales rechazadas por la IA
        self.risk_blocked       = False     # RiskGuard bloqueó entradas en algún momento
        self.last_level_check_time = None   # última vela ya revisada para actualizar niveles
        self.entry_attempts_up  = 0         # intentos de entrada rechazados por la IA (lado alcista)
        self.entry_attempts_dn  = 0         # intentos de entrada rechazados por la IA (lado bajista)
        self.last_entry_try_up  = None      # última vela ya evaluada para entrada (lado alcista)
        self.last_entry_try_dn  = None      # última vela ya evaluada para entrada (lado bajista)
        log.info("─" * 60)
        log.info("Estado diario reseteado")


def _build_no_trade_reason(state: "FundedBotState") -> str:
    """Arma el texto explicando por qué el bot no operó en la ventana de hoy."""
    partes = []

    if state.key_high is None:
        partes.append("📐 No se encontraron niveles estructurales válidos hoy (ni con el fallback H1).")
    else:
        partes.append(f"📐 Niveles del día — H:${state.key_high}  L:${state.key_low}")
        if not state.bos_up and not state.bos_dn:
            partes.append("⚡ El precio no rompió ninguno de los dos niveles (sin BOS) durante la ventana.")
        else:
            dirs = []
            if state.bos_up: dirs.append("alcista")
            if state.bos_dn: dirs.append("bajista")
            partes.append(f"⚡ Hubo BOS {' y '.join(dirs)}, pero no se concretó una entrada.")
            if state.retest_fails > 0:
                partes.append(f"🕯️ {state.retest_fails} intento(s) de retest no confirmaron "
                               f"(vela débil o no cerró a favor del nivel).")
            if state.ai_rejections > 0:
                partes.append(f"🤖 La IA rechazó {state.ai_rejections} señal(es) por baja confianza "
                               f"o condiciones desfavorables.")

    if state.risk_blocked:
        partes.append("🛡️ RiskGuard bloqueó nuevas entradas por drawdown en algún momento del día.")

    return "\n".join(partes)


# ─────────────────────────────────────────────────────────────────────────────
#  PERSISTENCIA DEL ESTADO DEL DÍA (niveles/BOS/retest/posición)
#  Evita que un reinicio del bot a mitad de ventana pierda un setup en curso.
# ─────────────────────────────────────────────────────────────────────────────
def _serialize_candle(c):
    if c is None:
        return None
    out = {}
    for k, v in c.items():
        if k == "time":
            out[k] = v.isoformat() if hasattr(v, "isoformat") else str(v)
        else:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = v
    return out

def _deserialize_candle(d):
    if d is None:
        return None
    import pandas as _pd
    out = dict(d)
    out["time"] = _pd.Timestamp(d["time"])
    return out

_DAY_STATE_FIELDS = [
    "key_high", "key_low", "key_high_touches", "key_low_touches",
    "key_high_updates", "key_low_updates",
    "bos_up", "bos_dn", "retest_up", "retest_dn",
    "level_up", "level_dn", "level_up_touches", "level_dn_touches",
    "trades_today", "open_ticket", "open_entry", "open_direction",
    "open_sl", "open_tp", "open_lot", "open_peak_price", "be_done",
    "window_analyzed", "window_close_notified",
    "retest_fails", "ai_rejections", "risk_blocked",
    "entry_attempts_up", "entry_attempts_dn",
]

def _save_day_state(state: "FundedBotState", day) -> None:
    """Guarda en disco el estado del día en curso (niveles, BOS, retest,
    posición abierta) para poder recuperarlo si el bot se reinicia a mitad
    de ventana en vez de perder el setup y empezar de cero."""
    try:
        data = {f: getattr(state, f) for f in _DAY_STATE_FIELDS}
        data["date"]          = day.isoformat()
        data["bos_candle_up"] = _serialize_candle(state.bos_candle_up)
        data["bos_candle_dn"] = _serialize_candle(state.bos_candle_dn)
        data["bos_time_up"]   = state.bos_time_up.isoformat() if state.bos_time_up else None
        data["bos_time_dn"]   = state.bos_time_dn.isoformat() if state.bos_time_dn else None
        data["open_time"]     = state.open_time.isoformat() if state.open_time else None
        with open(config.DAY_STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"Error guardando estado del día: {e}")

def _load_day_state(state: "FundedBotState", day) -> bool:
    """Restaura el estado del día si el archivo persistido corresponde al
    mismo día NYC. Retorna True si se recuperó algo."""
    if not os.path.exists(config.DAY_STATE_FILE):
        return False
    try:
        with open(config.DAY_STATE_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") != day.isoformat():
            return False

        for f in _DAY_STATE_FIELDS:
            if f in data:
                setattr(state, f, data[f])
        state.bos_candle_up = _deserialize_candle(data.get("bos_candle_up"))
        state.bos_candle_dn = _deserialize_candle(data.get("bos_candle_dn"))
        state.bos_time_up   = datetime.fromisoformat(data["bos_time_up"]) if data.get("bos_time_up") else None
        state.bos_time_dn   = datetime.fromisoformat(data["bos_time_dn"]) if data.get("bos_time_dn") else None
        state.open_time     = datetime.fromisoformat(data["open_time"]) if data.get("open_time") else None
        return True
    except Exception as e:
        log.error(f"Error cargando estado del día: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  MONITOREO DE POSICIÓN ABIERTA
# ─────────────────────────────────────────────────────────────────────────────
async def _monitor_open_position(state: FundedBotState, risk_guard: RiskGuard):
    if not state.open_ticket:
        return

    # ── Chequeo de breakeven al 75% (sin cierre parcial) ─────────────────
    # Se usa el MEJOR precio alcanzado desde la apertura, no solo el precio
    # del instante de este chequeo — así, si el mercado tocó el 75% entre
    # dos chequeos (cada MONITOR_INTERVAL) y ya retrocedió, el BE se activa
    # igual. Se combina el polling en vivo (mid de cada ciclo) con el
    # high/low real de las velas M5 cerradas desde la apertura, que capturan
    # el movimiento intra-vela aunque el bot no haya "visto" el pico exacto.
    if not state.be_done:
        bid, ask = get_current_price()
        mid = (bid + ask) / 2
        entry     = state.open_entry
        tp        = state.open_tp
        direction = state.open_direction

        if direction == "long":
            state.open_peak_price = max(state.open_peak_price or mid, mid)
        elif direction == "short":
            state.open_peak_price = min(state.open_peak_price or mid, mid)

        if state.open_time:
            df_since = get_candles(mt5.TIMEFRAME_M5, 60)
            if not df_since.empty:
                df_since = df_since[df_since["time"] >= state.open_time]
                if not df_since.empty:
                    if direction == "long":
                        state.open_peak_price = max(state.open_peak_price, df_since["high"].max())
                    elif direction == "short":
                        state.open_peak_price = min(state.open_peak_price, df_since["low"].min())

        peak = state.open_peak_price
        total_dist = abs(tp - entry) if tp and entry else 0

        if total_dist > 0 and peak is not None:
            if direction == "long":
                progress = (peak - entry) / total_dist
            else:
                progress = (entry - peak) / total_dist

            if progress >= config.BE_TRIGGER:
                new_sl = round(entry + config.BE_BUFFER, 2) if direction == "long" \
                    else round(entry - config.BE_BUFFER, 2)
                if set_sl(state.open_ticket, new_sl):
                    log.info(f"🔒  BREAKEVEN activado — SL movido a ${new_sl:.2f}  "
                             f"(progreso máximo alcanzado: {progress:.1%}  "
                             f"pico:${peak:.2f}  ticket: {state.open_ticket})")
                    state.open_sl = new_sl
                    state.be_done = True
                else:
                    log.warning(f"   No se pudo mover SL a BE para ticket {state.open_ticket}")

    # ── Verificar si la posición cerró ───────────────────────────────────
    result = check_position_closed(state.open_ticket)
    if not result["closed"]:
        bid, ask   = get_current_price()
        mid        = round((bid + ask) / 2, 2)
        entry      = state.open_entry or 0
        direction  = state.open_direction or "?"
        tp         = state.open_tp or 0
        sl         = state.open_sl or 0

        mov = round(mid - entry, 2) if direction == "long" else round(entry - mid, 2)
        progress = 0
        if tp != entry and tp:
            progress = round((mid - entry) / abs(tp - entry) * 100, 1) if direction == "long" \
                else round((entry - mid) / abs(entry - tp) * 100, 1)

        be_tag = " [BE ACTIVO]" if state.be_done else ""
        log.info(f"👁️  ticket:{state.open_ticket}  {direction.upper()}  "
                 f"entry:${entry}  precio:${mid}  mov:${mov:+.2f}  "
                 f"progreso:{progress:.0f}%  SL:${sl}  TP:${tp}{be_tag}")

        # Registrar drawdown cada ciclo de monitoreo
        balance = get_balance()
        risk_guard.log_drawdown(balance)
        return

    # ── Posición cerrada ─────────────────────────────────────────────────
    balance     = get_balance()
    profit      = result["profit"]
    close_price = result["close_price"]
    status      = result["status"]
    duration_m  = 0
    if state.open_time:
        duration_m = int((datetime.now(tz=timezone.utc) - state.open_time).total_seconds() / 60)

    save_close(state.open_ticket, close_price, profit, status)

    icon = "✅ WIN" if profit > 0 else "❌ LOSS" if profit < 0 else "= BE"
    log.info(f"{icon} — ticket:{state.open_ticket}  {status}  "
             f"entry:${state.open_entry}  close:${close_price}  "
             f"profit:${profit:+.2f}  duración:{duration_m}min  "
             f"balance:${balance:,.2f}"
             + (" [BE ejecutado]" if state.be_done else ""))

    if profit >= 0:
        await msg_close_win(state.open_ticket, state.open_direction or "?",
                            state.open_entry or 0, close_price,
                            profit, duration_m, balance)
    else:
        await msg_close_loss(state.open_ticket, state.open_direction or "?",
                             state.open_entry or 0, close_price,
                             profit, duration_m, balance)
        if config.AI_ENABLED and config.ANTHROPIC_API_KEY:
            trade_data = {
                "Ticket":      state.open_ticket,
                "Type":        "BUY" if state.open_direction == "long" else "SELL",
                "Entry Price": state.open_entry,
                "Close Price": close_price,
                "Profit":      profit,
            }
            lesson = await post_trade_lesson(trade_data, status)
            if lesson:
                log.info(f"🤖  Lección IA: {lesson[:100]}")

    # Reset estado del trade
    state.open_ticket    = None
    state.open_entry     = None
    state.open_direction = None
    state.open_time      = None
    state.open_sl        = None
    state.open_tp        = None
    state.open_lot       = None
    state.open_peak_price = None
    state.be_done        = False


# ─────────────────────────────────────────────────────────────────────────────
#  BUCLE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────
async def run():
    state    = FundedBotState()
    last_day = None

    # Recuperar estado del día si el bot se reinició a mitad de ventana
    _today_nyc = now_nyc().date()
    if _load_day_state(state, _today_nyc):
        last_day = _today_nyc  # evita que el "reset diario" borre lo recuperado
        log.info(f"♻️  Estado del día recuperado tras reinicio — "
                 f"H:${state.key_high}  L:${state.key_low}  "
                 f"BOS_up:{state.bos_up}  BOS_dn:{state.bos_dn}  "
                 f"Trades:{state.trades_today}  Posición abierta:{state.open_ticket}")

    # ── Inicializar logging ──────────────────────────────────────────────
    log.info("═" * 60)
    log.info("  AURUM FUNDED v1.0 — XAUUSD BREAK & RETEST")
    log.info("  Cuenta FundedHero — 2 Step | 1 trade/día | BE al 75%")
    log.info(f"  Ventana: {window_label()}")
    log.info("═" * 60)

    # ── Verificar configuración ───────────────────────────────────────────
    if not config.MT5_LOGIN:
        log.error("❌  MT5_LOGIN no configurado en config_funded.py")
        return

    # ── Conectar MT5 ─────────────────────────────────────────────────────
    log.info("Conectando a MT5...")
    if not connect():
        log.error("❌  No se pudo conectar a MT5")
        return

    # ── Extraer info real de cuenta y símbolo ────────────────────────────
    account_info = get_account_info_full()
    symbol_info  = get_symbol_info_full()

    if not account_info:
        log.error("❌  No se pudo obtener info de cuenta — abortando")
        disconnect()
        return

    balance = account_info["balance"]
    state.start_balance = balance
    log.info(f"✅  MT5 conectado — Cuenta: {account_info['login']}  "
             f"Servidor: {account_info['server']}")
    log.info(f"    Balance: ${balance:,.2f}  Equity: ${account_info['equity']:,.2f}  "
             f"Apalancamiento: 1:{account_info['leverage']}")

    # ── Extraer y validar specs reales del símbolo ────────────────────────
    specs = get_symbol_specs(config.SYMBOL)
    if not specs:
        log.error(f"❌  No se encontró el símbolo '{config.SYMBOL}' en el broker.")
        log.error(f"    Verificá el nombre exacto en tu MT5 (puede ser XAUUSD#, XAUUSDm, etc.)")
        log.error(f"    Actualizá config.SYMBOL en config_funded.py con el nombre correcto.")
        disconnect()
        return

    state.symbol_specs = specs
    log_symbol_specs(specs)

    # Validar que el símbolo tiene valores mínimos coherentes
    if specs["tick_value"] <= 0 or specs["volume_min"] <= 0:
        log.error(f"❌  Specs del símbolo inválidas — tick_value={specs['tick_value']}  "
                  f"volume_min={specs['volume_min']}  Bot detenido.")
        disconnect()
        return

    log.info(f"    {config.SYMBOL} — Spread: {specs['spread_pts']} pts  "
             f"Bid: ${specs['bid']}  Ask: ${specs['ask']}")

    # ── Inicializar RiskGuard ─────────────────────────────────────────────
    async def _telegram_send(msg):
        try:
            from core.telegram_bot import _send
            await _send(msg)
        except Exception as e:
            log.error(f"Error enviando Telegram: {e}")

    async def _close_pos(ticket, direction):
        try:
            close_position_market(ticket, direction)
        except Exception as e:
            log.error(f"Error cerrando posición de emergencia: {e}")

    risk_guard = RiskGuard(config, _telegram_send, _close_pos)
    risk_guard.set_reference_balance(balance)
    risk_guard.reset_day(balance, _today_nyc)  # recupera bloqueo/alertas si es reinicio mismo día
    state.reference_balance = risk_guard.reference_balance
    state.last_lot          = risk_guard.last_lot  # recupera el lote persistido (Lot Size Consistency)

    log.info(f"🛡️  RiskGuard inicializado — Balance referencia: ${risk_guard.reference_balance:,.2f}")
    if state.last_lot is not None:
        log.info(f"📦  Último lote recuperado: {state.last_lot} "
                 f"(próximo debe estar entre {max(config.MIN_LOT, round(state.last_lot - config.LOT_CONSISTENCY_MAX_CHANGE, 2))} "
                 f"y {round(state.last_lot + config.LOT_CONSISTENCY_MAX_CHANGE, 2)})")
    log.info(f"    Alerta diaria: {config.DAILY_DD_WARNING*100:.0f}%  |  "
             f"Límite diario: {config.DAILY_DD_LIMIT*100:.0f}%")
    log.info(f"    Alerta máxima: {config.MAX_DD_WARNING*100:.0f}%  |  "
             f"Límite máximo: {config.MAX_DD_LIMIT*100:.0f}%")

    await msg_startup(balance)

    # ── Bucle principal ───────────────────────────────────────────────────
    while True:
        try:
            now_utc = datetime.now(tz=timezone.utc)
            nyc     = now_nyc()

            _save_day_state(state, nyc.date())

            # ── Reset diario ─────────────────────────────────────────────
            if last_day != nyc.date():
                if last_day is not None:
                    bal       = get_balance()
                    stats     = get_stats()
                    net_today = round(bal - state.start_balance, 2)
                    await msg_daily_summary(stats, bal)

                    # Resumen de drawdown del día
                    await _telegram_send(risk_guard.status_summary(bal))

                    if config.AI_ENABLED and config.ANTHROPIC_API_KEY:
                        recent    = get_recent_trades(config.AI_LEARN_FROM_LAST)
                        breakdown = get_performance_breakdown()
                        summary   = await daily_learning_summary(stats, recent, bal, net_today, breakdown)
                        await msg_ai_daily_summary(summary, net_today, bal)

                balance_new = get_balance()
                state.reset_day()
                state.start_balance = balance_new
                risk_guard.reset_day(balance_new, nyc.date())
                last_day = nyc.date()

                # Re-extraer info de cuenta y specs al inicio de cada día
                account_info = get_account_info_full()
                specs_new    = get_symbol_specs(config.SYMBOL)
                if specs_new:
                    state.symbol_specs = specs_new
                    log_symbol_specs(specs_new)
                symbol_info  = get_symbol_info_full()
                log.info(f"📅  Nuevo día NYC: {nyc.strftime('%A %d/%m/%Y')}")
                log.info(f"    Balance: ${balance_new:,.2f}  "
                         f"Equity: ${account_info.get('equity', '?'):,.2f}  "
                         f"Spread actual: {state.symbol_specs.get('spread_pts', '?')} pts")
                log.info(f"🕐  Ventana hoy: {window_label()}")

            # ── Evaluar riesgo en cada ciclo ─────────────────────────────
            current_balance = get_balance()
            risk_level = await risk_guard.evaluate(
                current_balance,
                open_ticket=state.open_ticket,
                open_direction=state.open_direction
            )

            if risk_level in (RiskLevel.DANGER, RiskLevel.BREACH):
                state.risk_blocked = True
                log.warning(f"🛑  Bot detenido por RiskGuard ({risk_level.name}) — "
                            f"esperando próximo día")
                await asyncio.sleep(config.NIGHT_INTERVAL)
                continue

            in_win  = is_in_window()
            pre_win = is_pre_window()

            # ── Pre-ventana: solo espera, NO calcula niveles todavía ──────
            # Los niveles se calculan recién al abrir la ventana operativa,
            # con datos frescos — así se evita operar sobre un nivel que el
            # mercado ya rompió antes de que arranque la ventana.
            if pre_win:
                now_min = now_utc.minute
                if now_min % 10 == 0 and state.last_status_log != now_utc.strftime("%H:%M"):
                    state.last_status_log = now_utc.strftime("%H:%M")
                    mins = int(((nyc.replace(hour=config.NYC_WINDOW_START_HOUR,
                                             minute=0, second=0) - nyc)
                                .total_seconds() / 60))
                    log.info(f"💤  Pre-ventana — NYC: {nyc.strftime('%H:%M')} — "
                             f"faltan {mins} min para la apertura")
                await asyncio.sleep(config.OUTSIDE_INTERVAL)
                continue

            # ── Fuera de ventana ──────────────────────────────────────────
            if not in_win and not pre_win:
                # La ventana ya se abrió y se acaba de cerrar sin operar hoy
                if state.window_analyzed and not state.window_close_notified:
                    state.window_close_notified = True
                    if state.trades_today == 0:
                        motivo = _build_no_trade_reason(state)
                        await msg_no_trade_summary(motivo, get_balance())
                        log.info("🔕  Resumen enviado — sin operaciones en la ventana de hoy")

                now_min = now_utc.minute
                if now_min % 10 == 0 and state.last_status_log != now_utc.strftime("%H:%M"):
                    state.last_status_log = now_utc.strftime("%H:%M")
                    mins = int(((nyc.replace(hour=config.NYC_WINDOW_START_HOUR,
                                             minute=0, second=0) - nyc)
                                .total_seconds() / 60))
                    log.info(f"💤  Fuera de ventana — NYC: {nyc.strftime('%H:%M')} — "
                             f"faltan {mins} min")
                if state.open_ticket:
                    await _monitor_open_position(state, risk_guard)
                await asyncio.sleep(config.NIGHT_INTERVAL)
                continue

            # ── Análisis al abrir ventana ─────────────────────────────────
            if in_win and not state.window_analyzed:
                log.info("🔔  VENTANA NY ABIERTA")
                df  = get_candles(mt5.TIMEFRAME_M5, 200)
                ctx = get_market_context(df)

                if state.key_high is None and not df.empty:
                    ref_price = float(df["close"].iloc[-1])
                    kh, kl, src_h, src_l, kh_t, kl_t = compute_levels(df, ref_price)
                    if kh is None:
                        # Fallback — SOLO porque la lógica original no encontró
                        # nivel válido hoy. No afecta días con nivel normal.
                        fb_kh, _ = find_fallback_level_h1(ref_price, "high")
                        fb_kl, _ = find_fallback_level_h1(ref_price, "low")
                        if fb_kh is not None and fb_kl is not None:
                            kh, kl, kh_t, kl_t = fb_kh, fb_kl, 0, 0
                            log.info("🔎  Fallback H1 activado al abrir ventana — "
                                     "sin nivel del día, usando nivel no tocado (hasta 1 mes atrás)")
                    if kh is not None:
                        state.key_high         = kh
                        state.key_low          = kl
                        state.key_high_touches = kh_t
                        state.key_low_touches  = kl_t

                recent   = get_recent_trades(config.AI_LEARN_FROM_LAST)
                analysis = await analyze_market_open(
                    state.key_high or 0, state.key_low or 0,
                    ctx.get("sma9", 0), ctx.get("sma50", 0),
                    ctx.get("candles_summary", ""), recent
                )
                await msg_market_analysis(analysis, state.key_high or 0,
                                          state.key_low or 0, ctx.get("sma9", 0),
                                          ctx.get("sma50", 0), ctx.get("trend", "neutral"))

                # Enviar estado de RiskGuard al abrir ventana
                await _telegram_send(risk_guard.status_summary(current_balance))
                state.window_analyzed = True

            # ── Posición abierta → monitorear ─────────────────────────────
            if state.open_ticket:
                await _monitor_open_position(state, risk_guard)
                await asyncio.sleep(config.MONITOR_INTERVAL)
                continue

            # ── Límite diario ─────────────────────────────────────────────
            if state.trades_today >= config.MAX_TRADES_DAY:
                log.info(f"🚫  Máximo diario alcanzado ({state.trades_today}/{config.MAX_TRADES_DAY})")
                await asyncio.sleep(config.OUTSIDE_INTERVAL)
                continue

            # ── RiskGuard bloqueó el día ──────────────────────────────────
            if risk_guard.day_blocked:
                state.risk_blocked = True
                log.info("🛑  Día bloqueado por RiskGuard — sin nuevas entradas")
                await asyncio.sleep(config.OUTSIDE_INTERVAL)
                continue

            # ── Obtener velas ─────────────────────────────────────────────
            df = get_candles(mt5.TIMEFRAME_M5, 100)
            if df.empty:
                await asyncio.sleep(config.LOOP_INTERVAL)
                continue

            if len(df) < 2:
                await asyncio.sleep(config.LOOP_INTERVAL)
                continue

            last      = df.iloc[-2]   # vela cerrada
            bid, ask  = get_current_price()
            mid_price = round((bid + ask) / 2, 2)
            last_time = last["time"].strftime("%H:%M")
            sma9_val  = float(last.get("sma9",  0) or 0)
            sma50_val = float(last.get("sma50", 0) or 0)

            if now_utc.minute % 5 == 0 and state.last_status_log != now_utc.strftime("%H:%M"):
                state.last_status_log = now_utc.strftime("%H:%M")
                dd = risk_guard.get_drawdown(current_balance)
                log.info(f"📡  [{last_time}] ${mid_price}  "
                         f"H:${state.key_high or '?'}  L:${state.key_low or '?'}  "
                         f"Trades:{state.trades_today}  "
                         f"DD_d:{dd['day_dd_pct']:.2f}%  DD_m:{dd['max_dd_pct']:.2f}%")

            if state.key_high is None:
                await asyncio.sleep(config.LOOP_INTERVAL)
                continue

            # ── FASE 1: DETECTAR BOS ──────────────────────────────────────
            # Se evalúa ANTES de la reevaluación de niveles, usando el nivel
            # vigente sin tocar. Así, si esta misma vela rompe el nivel, el
            # BOS queda registrado antes de que la lógica de niveles pueda
            # "correr" el nivel hacia un swing nuevo que la propia ruptura
            # acaba de formar y borrar la ruptura sin que se detecte.
            if not np.isnan(sma9_val) and sma9_val > 0:
                bos_range = last["high"] - last["low"]

                if bos_range > config.BOS_MAX_RANGE:
                    await asyncio.sleep(config.LOOP_INTERVAL)
                    continue

                if not state.bos_up and last["close"] > state.key_high + config.BNR_CONFIRM:
                    body_pct = (abs(last["close"] - last["open"]) / bos_range
                                if bos_range > 0 else 0)
                    if bos_range >= config.BOS_MIN_RANGE and body_pct >= config.BOS_MIN_BODY_PCT:
                        state.bos_up        = True
                        state.retest_up     = True
                        state.bos_candle_up = last.to_dict()
                        state.level_up      = state.key_high
                        state.level_up_touches = state.key_high_touches
                        state.bos_time_up   = datetime.now(tz=timezone.utc)
                        log.info(f"⚡  BOS ALCISTA — nivel:${state.key_high}  "
                                 f"cierre:${last['close']:.2f}  rango:${bos_range:.2f}  "
                                 f"cuerpo:{body_pct:.0%}  hora:{last_time}")
                        await msg_bos_detected("long", state.key_high, last["close"], mid_price)
                        await asyncio.sleep(config.LOOP_INTERVAL)
                        continue

                if not state.bos_dn and last["close"] < state.key_low - config.BNR_CONFIRM:
                    body_pct = (abs(last["close"] - last["open"]) / bos_range
                                if bos_range > 0 else 0)
                    if bos_range >= config.BOS_MIN_RANGE and body_pct >= config.BOS_MIN_BODY_PCT:
                        state.bos_dn        = True
                        state.retest_dn     = True
                        state.bos_candle_dn = last.to_dict()
                        state.level_dn      = state.key_low
                        state.level_dn_touches = state.key_low_touches
                        state.bos_time_dn   = datetime.now(tz=timezone.utc)
                        log.info(f"⚡  BOS BAJISTA — nivel:${state.key_low}  "
                                 f"cierre:${last['close']:.2f}  rango:${bos_range:.2f}  "
                                 f"cuerpo:{body_pct:.0%}  hora:{last_time}")
                        await msg_bos_detected("short", state.key_low, last["close"], mid_price)
                        await asyncio.sleep(config.LOOP_INTERVAL)
                        continue

            # ── REEVALUACIÓN DE NIVELES EN VIVO ────────────────────────────
            # Una vez por vela cerrada, busca si el mercado formó un nivel
            # más cercano y válido (misma lógica/reglas de compute_levels).
            # Exige 2+ toques — un nivel de un solo toque (fuente "12-velas")
            # no es suficiente para reemplazar el nivel vigente en vivo.
            # Si un lado ya tiene un BOS en curso, no se toca ese lado — la
            # entrada de ese setup usa level_up/level_dn, ya capturados
            # aparte, así que esto no afecta ninguna entrada en progreso.
            # Dos frenos adicionales para no perder niveles importantes:
            #   1) Tope de actualizaciones por lado por día (MAX_LEVEL_UPDATES_PER_SIDE).
            #   2) Si el nivel vigente ya está cerca del precio (LEVEL_FREEZE_DIST),
            #      se congela — no se reemplaza aunque aparezca uno "más cercano".
            if last_time != state.last_level_check_time:
                state.last_level_check_time = last_time
                new_kh, new_kl, new_src_h, new_src_l, new_kh_t, new_kl_t = compute_levels(df, mid_price)

                if (new_kh is not None and not state.bos_up
                        and new_kh_t >= 2
                        and new_kh != state.key_high
                        and state.key_high_updates < config.MAX_LEVEL_UPDATES_PER_SIDE
                        and abs(state.key_high - mid_price) > config.LEVEL_FREEZE_DIST
                        and abs(new_kh - mid_price) < abs(state.key_high - mid_price)):
                    log.info(f"🔄  Resistencia actualizada: ${state.key_high} → ${new_kh} "
                             f"({new_kh_t} toques, fuente: {new_src_h}, "
                             f"update {state.key_high_updates + 1}/{config.MAX_LEVEL_UPDATES_PER_SIDE})")
                    old_kh = state.key_high
                    state.key_high         = new_kh
                    state.key_high_touches = new_kh_t
                    state.key_high_updates += 1
                    await msg_level_updated("high", old_kh, new_kh, mid_price, new_src_h)

                if (new_kl is not None and not state.bos_dn
                        and new_kl_t >= 2
                        and new_kl != state.key_low
                        and state.key_low_updates < config.MAX_LEVEL_UPDATES_PER_SIDE
                        and abs(state.key_low - mid_price) > config.LEVEL_FREEZE_DIST
                        and abs(new_kl - mid_price) < abs(state.key_low - mid_price)):
                    log.info(f"🔄  Soporte actualizado: ${state.key_low} → ${new_kl} "
                             f"({new_kl_t} toques, fuente: {new_src_l}, "
                             f"update {state.key_low_updates + 1}/{config.MAX_LEVEL_UPDATES_PER_SIDE})")
                    old_kl = state.key_low
                    state.key_low          = new_kl
                    state.key_low_touches  = new_kl_t
                    state.key_low_updates += 1
                    await msg_level_updated("low", old_kl, new_kl, mid_price, new_src_l)

            # ── FASE 2: RETEST + CONFIRMACIÓN ────────────────────────────
            for direction, retest_flag, level, bos_c, level_touches in [
                ("long",  state.retest_up, state.level_up, state.bos_candle_up, state.level_up_touches),
                ("short", state.retest_dn, state.level_dn, state.bos_candle_dn, state.level_dn_touches),
            ]:
                if not retest_flag or level is None or bos_c is None:
                    continue

                if direction == "long":
                    touched   = last["low"]   <= level + config.BNR_RETEST_TOL
                    confirmed = last["close"]  > level + 0.3
                else:
                    touched   = last["high"]  >= level - config.BNR_RETEST_TOL
                    confirmed = last["close"]  < level - 0.3

                qual = candle_quality(last, direction)

                # Heartbeat cada 10 min esperando retest
                now_check = datetime.now(tz=timezone.utc)
                if (state.last_retest_notify is None or
                        (now_check - state.last_retest_notify).total_seconds() >= 600):
                    state.last_retest_notify = now_check
                    bos_t = state.bos_time_up if direction == "long" else state.bos_time_dn
                    elapsed = int((now_check - bos_t).total_seconds() / 60) if bos_t else 0
                    dist = (max(0.0, round(mid_price - (level + config.BNR_RETEST_TOL), 2))
                            if direction == "long" else
                            max(0.0, round((level - config.BNR_RETEST_TOL) - mid_price, 2)))
                    await msg_retest_waiting(direction, level, mid_price,
                                            dist, level_touches, elapsed)

                if touched and not (confirmed and qual):
                    state.retest_fails += 1
                    now_fail = datetime.now(tz=timezone.utc)
                    if (state.last_retest_fail_notify is None or
                            (now_fail - state.last_retest_fail_notify).total_seconds() >= 300):
                        state.last_retest_fail_notify = now_fail
                        rng  = last["high"] - last["low"]
                        body = abs(last["close"] - last["open"]) / rng if rng > 0 else 0
                        reasons = []
                        if not confirmed:
                            reasons.append(f"no cerró sobre el nivel (${last['close']:.2f})")
                        if not qual:
                            reasons.append(f"vela débil (cuerpo {body:.0%})")
                        await msg_retest_touch_failed(direction, level,
                                                      " / ".join(reasons),
                                                      last["close"], mid_price)

                if not (touched and confirmed and qual):
                    continue

                # No reevaluar la misma vela más de una vez (evita llamar a
                # la IA repetidamente cada 5s mientras el candle sigue vigente)
                last_try_attr = "last_entry_try_up" if direction == "long" else "last_entry_try_dn"
                if getattr(state, last_try_attr) == last_time:
                    continue
                setattr(state, last_try_attr, last_time)

                # ── Calcular SL y entry ───────────────────────────────────
                # El SL se ancla al swing estructural más cercano más allá
                # del wick de la vela de BOS (si existe uno útil dentro de
                # MAX_SL) en vez de usar siempre ese wick a secas.
                if direction == "long":
                    entry = round(level + config.SPREAD_EST, 2)
                    sl_anchor = find_structural_sl(df, bos_c, "long", entry)
                    sl_p  = round(sl_anchor - config.BOS_SL_BUFFER, 2)
                    if sl_anchor != bos_c["low"]:
                        log.info(f"   SL reubicado en swing estructural ${sl_anchor:.2f} "
                                 f"(wick de la vela BOS: ${bos_c['low']:.2f})")
                else:
                    entry = round(level - config.SPREAD_EST, 2)
                    sl_anchor = find_structural_sl(df, bos_c, "short", entry)
                    sl_p  = round(sl_anchor + config.BOS_SL_BUFFER, 2)
                    if sl_anchor != bos_c["high"]:
                        log.info(f"   SL reubicado en swing estructural ${sl_anchor:.2f} "
                                 f"(wick de la vela BOS: ${bos_c['high']:.2f})")

                sl_dist = round(abs(entry - sl_p), 2)

                if direction == "long" and sl_p >= entry:
                    if direction == "long": state.retest_up = False
                    continue
                if direction == "short" and sl_p <= entry:
                    state.retest_dn = False
                    continue
                if not (config.MIN_SL <= sl_dist <= config.MAX_SL):
                    if direction == "long": state.retest_up = False
                    else:                  state.retest_dn = False
                    continue

                # ── Calcular lote (fijo sobre referencia, con specs reales) ──
                balance = get_balance()
                lot = calc_lot_funded(balance, risk_guard.reference_balance,
                                      sl_dist, state.last_lot, risk_guard,
                                      state.symbol_specs)
                if lot <= 0:
                    log.warning("   Lote inválido — trade rechazado")
                    if direction == "long": state.retest_up = False
                    else:                  state.retest_dn = False
                    continue

                if sl_dist >= config.LARGE_SL_THRESHOLD:
                    tp_rr = 1.0
                    log.info(f"   SL grande (${sl_dist:.2f} >= ${config.LARGE_SL_THRESHOLD}) — "
                             f"TP forzado a 1:1 aunque el nivel tenga {level_touches} toques")
                else:
                    tp_rr = 2.0 if level_touches >= 2 else 1.0
                tp_p  = round(entry + sl_dist * tp_rr if direction == "long"
                              else entry - sl_dist * tp_rr, 2)
                vpl_plan = state.symbol_specs.get("value_per_point_per_lot", 100.0)
                risk_m   = round(sl_dist * lot * vpl_plan, 2)
                risk_pct = round(risk_m / balance * 100, 2)

                log.info(f"   Balance:${balance:,.2f}  Ref:${risk_guard.reference_balance:,.2f}  "
                         f"Lote:{lot}  Riesgo:${risk_m} ({risk_pct}%)  "
                         f"TP:${tp_p}  RR:1:{tp_rr:.0f}  ({level_touches} toques)")

                # ── Validación IA ─────────────────────────────────────────
                recent    = get_recent_trades(config.AI_LEARN_FROM_LAST)
                ai_result = {"decision": "EJECUTAR", "confidence": 0.60, "reasoning": "IA desactivada"}

                if config.AI_ENABLED and config.ANTHROPIC_API_KEY:
                    breakdown = get_performance_breakdown()
                    ai_result = await validate_signal(
                        direction, entry, sl_p, tp_p, sl_dist,
                        bos_c, level, sma9_val, sma50_val, recent, mid_price, breakdown
                    )
                    log.info(f"🤖  IA → {ai_result.get('decision')}  "
                             f"conf:{ai_result.get('confidence',0):.0%}  "
                             f"| {ai_result.get('reasoning','')[:60]}")
                    await msg_ai_analysis(
                        ai_result.get("market_analysis", ai_result.get("reasoning", "")),
                        ai_result.get("decision", "ESPERAR"),
                        ai_result.get("confidence", 0.0)
                    )

                decision   = ai_result.get("decision", "EJECUTAR")
                confidence = ai_result.get("confidence", 0.0)

                if decision != "EJECUTAR" or confidence < config.AI_MIN_CONFIDENCE:
                    state.ai_rejections += 1
                    attempts_attr = "entry_attempts_up" if direction == "long" else "entry_attempts_dn"
                    attempts = getattr(state, attempts_attr) + 1
                    setattr(state, attempts_attr, attempts)
                    if attempts >= config.MAX_ENTRY_ATTEMPTS_PER_BOS:
                        log.info(f"   IA rechazó — máximo de intentos alcanzado "
                                 f"({attempts}/{config.MAX_ENTRY_ATTEMPTS_PER_BOS}) — abandonando este BOS")
                        if direction == "long": state.retest_up = False
                        else:                  state.retest_dn = False
                    else:
                        log.info(f"   IA rechazó — se sigue esperando otro retest "
                                 f"(intento {attempts}/{config.MAX_ENTRY_ATTEMPTS_PER_BOS})")
                    continue

                # ── Verificar drawdown una vez más antes de entrar ────────
                final_balance = get_balance()
                final_risk = await risk_guard.evaluate(final_balance)
                if final_risk in (RiskLevel.DANGER, RiskLevel.BREACH) or risk_guard.day_blocked:
                    state.risk_blocked = True
                    log.warning("   RiskGuard bloqueó la entrada en último chequeo")
                    break

                # ── Resimulación con precio en vivo justo antes de operar ──
                # Entre la señal y este punto pasaron la validación IA y los
                # chequeos de riesgo — el precio pudo moverse. La orden se
                # manda a MERCADO, así que puede llenarse lejos del "entry"
                # planeado; si el lote quedó calculado sobre el sl_dist
                # planeado y el SL real termina más lejos, el riesgo real
                # supera el % configurado (esto fue justo lo que pasó el
                # 07/07: entry planeado $4164.91, SL fijo $4156.62 →
                # sl_dist=8.29 → lote 0.24 para ~2% de riesgo; la orden se
                # llenó en $4170.06 con el mismo SL, sl_dist real=13.44 →
                # riesgo real ~3.2%, pérdida real $326 en vez de los ~$199
                # previstos). Por eso se vuelve a leer precio y specs en
                # vivo y se resimula el lote antes de disparar la orden.
                bid_live, ask_live = get_current_price()
                live_entry = ask_live if direction == "long" else bid_live
                if live_entry <= 0:
                    log.warning("   Sin precio en vivo disponible — trade rechazado")
                    if direction == "long": state.retest_up = False
                    else:                  state.retest_dn = False
                    continue

                if abs(live_entry - entry) > config.ENTRY_PRICE_DRIFT_TOL:
                    log.warning(f"   Precio se movió demasiado desde la señal "
                                f"(${entry} → ${live_entry}, drift ${abs(live_entry-entry):.2f} "
                                f"> ${config.ENTRY_PRICE_DRIFT_TOL}) — retest inválido, "
                                f"trade rechazado")
                    if direction == "long": state.retest_up = False
                    else:                  state.retest_dn = False
                    continue

                live_sl_dist = round(abs(live_entry - sl_p), 2)
                if not (config.MIN_SL <= live_sl_dist <= config.MAX_SL):
                    log.warning(f"   SL en vivo (${live_sl_dist}) fuera de rango "
                                f"[{config.MIN_SL},{config.MAX_SL}] — trade rechazado")
                    if direction == "long": state.retest_up = False
                    else:                  state.retest_dn = False
                    continue

                live_specs = get_symbol_specs(config.SYMBOL) or state.symbol_specs
                live_lot = calc_lot_funded(balance, risk_guard.reference_balance,
                                           live_sl_dist, state.last_lot, risk_guard,
                                           live_specs)
                if live_lot <= 0:
                    log.warning("   Lote en vivo inválido — trade rechazado")
                    if direction == "long": state.retest_up = False
                    else:                  state.retest_dn = False
                    continue

                if live_lot != lot or live_sl_dist != sl_dist:
                    log.info(f"   Resimulado con precio en vivo — entry:${entry}→${live_entry}  "
                             f"SL_dist:${sl_dist}→${live_sl_dist}  lote:{lot}→{live_lot}")
                vpl_live = live_specs.get("value_per_point_per_lot", vpl_plan)
                entry    = live_entry
                sl_dist  = live_sl_dist
                lot      = live_lot
                state.symbol_specs = live_specs
                tp_p     = round(entry + sl_dist * tp_rr if direction == "long"
                                  else entry - sl_dist * tp_rr, 2)
                risk_m   = round(sl_dist * lot * vpl_live, 2)
                risk_pct = round(risk_m / balance * 100, 2)

                # ── Ejecutar orden ────────────────────────────────────────
                log.info(f"✅  Ejecutando {direction.upper()}  entry:{entry}  sl:{sl_p}  lot:{lot}")
                order = send_order(direction, lot, sl_p)

                if not order["success"]:
                    log.error(f"❌  Orden falló: {order.get('error')}")
                    await msg_error(f"Orden falló: {order.get('error')}")
                    continue

                ticket     = order["ticket"]
                real_entry = order["entry_price"]

                save_open(ticket, direction, real_entry, sl_p, tp_p, lot, risk_m)
                state.last_lot = lot   # Guardar para Lot Size Consistency
                risk_guard.save_last_lot(lot)  # Persistir en disco — sobrevive a reinicios

                # Actualizar estado ANTES de la espera de 5s para el TP —
                # así, si el bot se reinicia en esos segundos, ya sabe que
                # hay una posición abierta y no intenta operar de nuevo.
                state.open_ticket    = ticket
                state.open_entry     = real_entry
                state.open_direction = direction
                state.open_time      = datetime.now(tz=timezone.utc)
                state.open_sl        = sl_p
                state.open_tp        = tp_p
                state.open_lot       = lot
                state.open_peak_price = real_entry
                state.be_done        = False
                state.trades_today  += 1

                if direction == "long": state.retest_up = False
                else:                  state.retest_dn = False

                _save_day_state(state, nyc.date())

                bos_ext = bos_c["low"] if direction == "long" else bos_c["high"]
                await msg_entry(ticket, direction, real_entry, sl_p, sl_dist,
                                lot, risk_m, bos_ext)

                await asyncio.sleep(5)
                recalc_dist = abs(real_entry - sl_p)
                real_tp = round(real_entry + recalc_dist * tp_rr if direction == "long"
                                else real_entry - recalc_dist * tp_rr, 2)
                if set_tp(ticket, real_tp):
                    log.info(f"🎯  TP colocado (RR 1:{tp_rr:.0f}): ${real_tp}")
                    state.open_tp = real_tp
                    _save_day_state(state, nyc.date())
                    await msg_tp_set(ticket, real_tp, recalc_dist)

                log.info(f"📊  Trade #{state.trades_today} abierto — "
                         f"ticket:{ticket}  {direction.upper()}  "
                         f"entry:${real_entry}  sl:${sl_p}  tp:${real_tp}  lote:{lot}")
                break

            await asyncio.sleep(config.LOOP_INTERVAL)

        except KeyboardInterrupt:
            log.info("\n🛑  Bot detenido por usuario (Ctrl+C)")
            break
        except Exception as e:
            log.error(f"❌  Error en bucle principal: {e}", exc_info=True)
            await msg_error(str(e)[:200])
            await asyncio.sleep(config.LOOP_INTERVAL)

    disconnect()
    log.info("Bot desconectado")


if __name__ == "__main__":
    asyncio.run(run())
