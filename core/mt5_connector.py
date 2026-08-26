"""
MT5 Connector — Conexión y operaciones con MetaTrader 5
=========================================================
FIX: modo de llenado ahora detecta automáticamente el que acepta el broker.
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timezone
import config_funded as config

log = logging.getLogger("aurum_bot")

def connect() -> bool:
    if not mt5.initialize():
        log.error(f"MT5 initialize() falló — {mt5.last_error()}")
        return False
    if config.MT5_LOGIN and config.MT5_PASSWORD and config.MT5_SERVER:
        ok = mt5.login(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER)
        if not ok:
            log.error(f"MT5 login falló — {mt5.last_error()}")
            mt5.shutdown(); return False
    info = mt5.account_info()
    if info:
        log.info(f"MT5 conectado — cuenta={info.login} balance=${info.balance:.2f} server={info.server}")
    return True

def disconnect():
    mt5.shutdown()
    log.info("MT5 desconectado")

def get_balance() -> float:
    info = mt5.account_info()
    return info.balance if info else 0.0

def get_candles(timeframe, count: int = 150) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(config.SYMBOL, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    if "tick_volume" in df.columns:
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["time","open","high","low","close","volume"]].copy()
    df.sort_values("time", inplace=True); df.reset_index(drop=True, inplace=True)
    df["sma9"]  = df["close"].rolling(9).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    return df

def get_current_price() -> tuple:
    tick = mt5.symbol_info_tick(config.SYMBOL)
    if tick is None: return 0.0, 0.0
    return tick.bid, tick.ask

def get_symbol_specs(symbol: str = None) -> dict:
    """
    Extrae toda la información técnica del símbolo directamente de MT5.
    Usado al arrancar el bot para conocer los parámetros exactos del broker
    antes de calcular cualquier lote.

    Retorna un dict con todos los valores necesarios para calc_lot_dynamic.
    Si el símbolo no existe en el broker, retorna dict vacío y loguea el error.
    """
    sym = symbol or config.SYMBOL
    info = mt5.symbol_info(sym)

    if info is None:
        log.error(f"❌  Símbolo '{sym}' no encontrado en el broker — "
                  f"verificá que el nombre es exacto (ej: XAUUSD#, XAUUSD, XAUUSDm)")
        return {}

    # Precio actual para referencia
    tick = mt5.symbol_info_tick(sym)
    bid  = round(tick.bid, info.digits) if tick else 0.0
    ask  = round(tick.ask, info.digits) if tick else 0.0

    specs = {
        "symbol":          info.name,
        "digits":          info.digits,
        "bid":             bid,
        "ask":             ask,
        "spread_pts":      round(ask - bid, info.digits) if bid and ask else 0,
        "spread_usd":      round((ask - bid) * info.trade_tick_value / info.trade_tick_size, 4)
                           if info.trade_tick_size > 0 else 0,
        "tick_size":       info.trade_tick_size,
        "tick_value":      info.trade_tick_value,     # USD por tick por lote
        "contract_size":   info.trade_contract_size,
        "volume_min":      info.volume_min,
        "volume_max":      info.volume_max,
        "volume_step":     info.volume_step,
        "stops_level_pts": info.trade_stops_level,    # Distancia mínima de stops del broker
        "stops_level_usd": round(info.trade_stops_level * info.trade_tick_size *
                                 info.trade_tick_value / info.trade_tick_size, 4)
                           if info.trade_tick_size > 0 else 0,
        "currency_profit": info.currency_profit,
        # Valor de 1 USD de movimiento por lote (derivado)
        # Para XAUUSD: tick_size=0.01, tick_value=~1.0 → value_per_usd_per_lot = 100
        # Para Nasdaq: tick_size=0.01, tick_value=~0.01 → value_per_usd_per_lot = 1
        "value_per_point_per_lot": round(info.trade_tick_value / info.trade_tick_size, 4)
                                   if info.trade_tick_size > 0 else 100.0,
    }

    return specs


def log_symbol_specs(specs: dict):
    """
    Imprime en log el resumen completo de especificaciones del símbolo.
    Idéntico al formato de BotFenix para facilitar revisión antes de operar.
    """
    if not specs:
        log.error("❌  No se pudieron obtener specs del símbolo — bot NO debería operar")
        return

    sym  = specs["symbol"]
    vpl  = specs["value_per_point_per_lot"]
    bid  = specs["bid"]
    ask  = specs["ask"]

    log.info("─" * 60)
    log.info(f"📊  ESPECIFICACIONES DEL SÍMBOLO: {sym}")
    log.info(f"    Bid/Ask: {bid} / {ask}  |  Spread={specs['spread_pts']} pts "
             f"(~${specs['spread_usd']:.4f} USD/lote)")
    log.info(f"    Tick: size={specs['tick_size']} | value={specs['tick_value']:.4f} USD/lote")
    log.info(f"    Volumen: min={specs['volume_min']} | max={specs['volume_max']} "
             f"| step={specs['volume_step']}")
    log.info(f"    Contrato: {specs['contract_size']} | Moneda P&L: {specs['currency_profit']}")
    log.info(f"    SL mínimo broker: {specs['stops_level_pts']} pts "
             f"(~${specs['stops_level_usd']:.2f} USD)")
    log.info(f"    Valor por punto por lote: ${vpl:.4f} USD")
    log.info("─" * 60)

    # Simulación de lotajes para distintos SL — igual que BotFenix
    if bid > 0:
        account = mt5.account_info()
        bal = account.balance if account else 10000
        log.info(f"    Simulación riesgo obj=${bal * 0.02:.2f} USD (2% de ${bal:,.0f})")
        for sl_pct in [0.10, 0.30, 0.60]:
            sl_usd  = round(bid * sl_pct / 100, specs["digits"])
            if sl_usd <= 0 or vpl <= 0:
                continue
            vol = (bal * 0.02) / (sl_usd * vpl)
            vol = max(specs["volume_min"],
                      round(round(vol / specs["volume_step"]) * specs["volume_step"], 2))
            vol = min(vol, specs["volume_max"])
            riesgo_real = round(sl_usd * vol * vpl, 2)
            log.info(f"    SL {sl_pct:.2f}%={sl_usd:.2f}pts → vol={vol} lotes "
                     f"| riesgo real~${riesgo_real:.2f} USD")
    log.info("─" * 60)


def calc_lot(balance: float, sl_dist: float) -> float:
    """Versión simple — mantenida por compatibilidad. Usar calc_lot_dynamic en el bot fondeado."""
    if sl_dist <= 0: return config.MIN_LOT
    lot = (balance * config.RISK_PCT) / (sl_dist * 100)
    return round(max(config.MIN_LOT, min(lot, config.MAX_LOT)), 2)


def calc_lot_dynamic(balance: float, sl_dist_usd: float,
                     specs: dict, risk_pct: float = 0.02,
                     reference_balance: float = None) -> float:
    """
    Calcula el lote usando los valores REALES del símbolo obtenidos de MT5.
    NO asume $100/punto — usa tick_value y tick_size reales del broker.

    Fórmula:
        riesgo_objetivo = reference_balance × risk_pct
        lote = riesgo_objetivo / (sl_dist_usd × value_per_point_per_lot)

    Después aplica:
        - Mínimo/máximo/step del broker (del specs)
        - Tope duro MAX_RISK_PCT sobre el balance actual
        - Redondeo al step del símbolo

    Args:
        balance:           Balance actual de la cuenta (para tope duro)
        sl_dist_usd:       Distancia del SL en USD (ya calculada en la estrategia)
        specs:             Dict retornado por get_symbol_specs()
        risk_pct:          % de riesgo objetivo (default 2%)
        reference_balance: Balance fijo de referencia (para lote constante).
                           Si es None, usa el balance actual.

    Returns:
        Lote calculado y ajustado. Retorna 0.0 si el cálculo no es seguro.
    """
    if not specs:
        log.error("calc_lot_dynamic: specs vacío — no se puede calcular lote")
        return 0.0

    if sl_dist_usd <= 0:
        log.warning("calc_lot_dynamic: sl_dist_usd <= 0 — retornando lote mínimo")
        return specs.get("volume_min", config.MIN_LOT)

    ref_bal  = reference_balance if reference_balance and reference_balance > 0 else balance
    vpl      = specs.get("value_per_point_per_lot", 100.0)
    vol_min  = specs.get("volume_min",  config.MIN_LOT)
    vol_max  = specs.get("volume_max",  config.MAX_LOT)
    vol_step = specs.get("volume_step", 0.01)

    if vpl <= 0:
        log.error(f"calc_lot_dynamic: value_per_point_per_lot={vpl} inválido")
        return 0.0

    # ── Lote base sobre balance de referencia ────────────────────────────
    riesgo_obj = ref_bal * risk_pct
    lot_raw    = riesgo_obj / (sl_dist_usd * vpl)

    # ── Redondear al step del broker ─────────────────────────────────────
    lot = round(round(lot_raw / vol_step) * vol_step, 10)
    lot = round(lot, 2)

    # ── Aplicar límites del broker ────────────────────────────────────────
    lot = max(vol_min, min(lot, vol_max))

    # ── Tope duro MAX_RISK_PCT sobre balance actual ───────────────────────
    riesgo_real = sl_dist_usd * lot * vpl
    riesgo_cap  = balance * config.MAX_RISK_PCT
    if riesgo_real > riesgo_cap:
        lot_capped = riesgo_cap / (sl_dist_usd * vpl)
        lot_capped = round(round(lot_capped / vol_step) * vol_step, 2)
        lot_capped = max(vol_min, min(lot_capped, vol_max))
        if lot_capped < vol_min:
            log.warning(f"   calc_lot_dynamic: lote mínimo {vol_min} excede tope de riesgo — "
                        f"trade rechazado")
            return 0.0
        log.info(f"   Lote reducido por tope MAX_RISK_PCT: {lot} → {lot_capped} "
                 f"(riesgo real ${riesgo_real:.2f} > cap ${riesgo_cap:.2f})")
        lot = lot_capped

    riesgo_final = round(sl_dist_usd * lot * vpl, 2)
    log.info(f"   calc_lot_dynamic: ref_bal=${ref_bal:,.2f}  riesgo_obj=${riesgo_obj:.2f}  "
             f"SL=${sl_dist_usd:.2f}  vpl=${vpl:.4f}  "
             f"→ lote={lot}  riesgo_real=${riesgo_final:.2f} "
             f"({riesgo_final/balance*100:.2f}% del balance actual)")

    return lot

def _get_filling_mode() -> int:
    """
    Detecta automáticamente el modo de llenado que acepta el broker.
    Orden de preferencia: RETURN → IOC → FOK
    Evita el error 'Unsupported filling mode'.
    """
    info = mt5.symbol_info(config.SYMBOL)
    if info is None:
        log.warning("No se pudo obtener info del símbolo — usando RETURN por defecto")
        return mt5.ORDER_FILLING_RETURN

    filling = info.filling_mode
    log.debug(f"Broker filling_mode bits: {filling}")

    # filling_mode es un bitmask: bit0=FOK, bit1=IOC, bit2=RETURN (según MT5)
    # Probamos en orden de compatibilidad más amplia
    if filling & 4:   # RETURN — más compatible
        log.debug("Usando ORDER_FILLING_RETURN")
        return mt5.ORDER_FILLING_RETURN
    elif filling & 2: # IOC
        log.debug("Usando ORDER_FILLING_IOC")
        return mt5.ORDER_FILLING_IOC
    elif filling & 1: # FOK
        log.debug("Usando ORDER_FILLING_FOK")
        return mt5.ORDER_FILLING_FOK
    else:
        log.warning(f"Filling mode desconocido ({filling}) — intentando RETURN")
        return mt5.ORDER_FILLING_RETURN

def send_order(direction: str, lot: float, sl: float, comment: str = "") -> dict:
    """
    Envía orden de mercado SIN TP (se añade después con set_tp).
    Detecta automáticamente el filling mode del broker.
    """
    bid, ask = get_current_price()
    if bid == 0:
        return {"success": False, "error": "Sin precio disponible"}

    order_type = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
    price      = ask if direction == "long" else bid
    filling    = _get_filling_mode()

    # Validar distancia mínima de stops exigida por el broker
    sym_info = mt5.symbol_info(config.SYMBOL)
    if sym_info and sym_info.trade_stops_level > 0:
        min_stop_dist = sym_info.trade_stops_level * sym_info.point
        sl_dist_actual = abs(price - sl)
        if sl_dist_actual < min_stop_dist:
            msg = (f"SL ${sl:.2f} demasiado cerca del precio ${price:.2f} "
                   f"(distancia {sl_dist_actual:.2f} USD < mín. del broker {min_stop_dist:.2f} USD)")
            log.warning(f"⚠️  ORDEN RECHAZADA — {msg}")
            return {"success": False, "error": msg}

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       config.SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "sl":           round(sl, 2),
        "deviation":    config.DEVIATION,
        "magic":        config.MAGIC,
        "comment":      comment or config.COMMENT,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    log.info(f"Enviando orden — {direction.upper()} {lot} lotes a ${price:.2f} SL=${sl:.2f} filling={filling}")
    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = getattr(result, 'retcode', None)
        err     = getattr(result, 'comment', str(mt5.last_error()))
        log.error(f"Orden falló — retcode={retcode} error={err}")

        # Si falla por filling mode, intentar con los otros dos automáticamente
        if retcode in (10006, 10014) or "filling" in str(err).lower():
            for fallback in [mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK]:
                if fallback == filling:
                    continue
                log.info(f"Reintentando con filling mode {fallback}...")
                request["type_filling"] = fallback
                result2 = mt5.order_send(request)
                if result2 and result2.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f"Orden exitosa con filling {fallback} — ticket={result2.order}")
                    return {"success": True, "ticket": result2.order,
                            "entry_price": result2.price, "volume": result2.volume}
        return {"success": False, "error": err}

    log.info(f"Orden enviada — ticket={result.order} entry=${result.price:.2f}")
    return {"success": True, "ticket": result.order,
            "entry_price": result.price, "volume": result.volume}

def set_sl(ticket: int, sl: float) -> bool:
    """Modifica solo el SL de una posición abierta (para mover a breakeven)."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.warning(f"set_sl: ticket {ticket} no encontrado")
        return False
    pos = positions[0]
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   config.SYMBOL,
        "position": ticket,
        "sl":       round(sl, 2),
        "tp":       pos.tp,   # mantener el TP actual
    }
    result = mt5.order_send(request)
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok: log.info(f"SL modificado — ticket={ticket} nuevo_sl=${sl:.2f}")
    else:  log.error(f"set_sl falló — {getattr(result,'retcode',None)} {getattr(result,'comment','')}")
    return ok

def set_tp(ticket: int, tp: float) -> bool:
    """Modifica solo el TP de una posición abierta."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.warning(f"set_tp: ticket {ticket} no encontrado")
        return False
    pos = positions[0]
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   config.SYMBOL,
        "position": ticket,
        "sl":       pos.sl,   # mantener el SL actual
        "tp":       round(tp, 2),
    }
    result = mt5.order_send(request)
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok: log.info(f"TP modificado — ticket={ticket} nuevo_tp=${tp:.2f}")
    else:  log.error(f"set_tp falló — {getattr(result,'retcode',None)} {getattr(result,'comment','')}")
    return ok

def close_partial(ticket: int, lot_to_close: float, direction: str) -> dict:
    """
    Cierra parcialmente una posición abierta.
    lot_to_close: volumen a cerrar (debe ser múltiplo de 0.01 y >= MIN_LOT)
    Retorna dict con success, profit parcial y precio de cierre.
    """
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"success": False, "error": f"Ticket {ticket} no encontrado"}

    pos = positions[0]
    lot_to_close = round(max(config.MIN_LOT, min(lot_to_close, pos.volume - config.MIN_LOT)), 2)

    if lot_to_close <= 0:
        return {"success": False, "error": "Lote a cerrar inválido"}

    bid, ask = get_current_price()
    if bid == 0:
        return {"success": False, "error": "Sin precio disponible"}

    # Para cerrar parcial: tipo opuesto al de la posición
    if direction == "long":
        close_type = mt5.ORDER_TYPE_SELL
        price      = bid
    else:
        close_type = mt5.ORDER_TYPE_BUY
        price      = ask

    filling = _get_filling_mode()
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       config.SYMBOL,
        "volume":       lot_to_close,
        "type":         close_type,
        "position":     ticket,
        "price":        price,
        "deviation":    config.DEVIATION,
        "magic":        config.MAGIC,
        "comment":      "AURUM_PARTIAL",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    log.info(f"Cierre parcial — ticket={ticket} lote={lot_to_close} precio=${price:.2f}")
    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = getattr(result, 'comment', str(mt5.last_error()))
        log.error(f"Cierre parcial falló — {getattr(result,'retcode',None)} {err}")
        return {"success": False, "error": err}

    log.info(f"Cierre parcial exitoso — lote={lot_to_close} precio=${result.price:.2f}")
    return {"success": True, "close_price": result.price, "lot_closed": lot_to_close}

def get_position_info(ticket: int) -> dict:
    """Retorna info actualizada de una posición abierta."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {}
    pos = positions[0]
    return {
        "ticket":  pos.ticket,
        "volume":  pos.volume,
        "profit":  round(pos.profit, 2),
        "sl":      pos.sl,
        "tp":      pos.tp,
        "price_open": pos.price_open,
        "price_current": pos.price_current,
    }


    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.warning(f"set_tp: ticket {ticket} no encontrado")
        return False
    pos = positions[0]
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   config.SYMBOL,
        "position": ticket,
        "sl":       pos.sl,
        "tp":       round(tp, 2),
    }
    result = mt5.order_send(request)
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok: log.info(f"TP set — ticket={ticket} tp=${tp:.2f}")
    else:  log.error(f"set_tp falló — {getattr(result,'retcode',None)} {getattr(result,'comment','')}")
    return ok

def check_position_closed(ticket: int) -> dict:
    open_pos = mt5.positions_get(ticket=ticket)
    if open_pos:
        return {"closed": False, "profit": 0.0, "close_price": 0.0, "status": "OPEN"}
    history = mt5.history_deals_get(0, datetime.now(tz=timezone.utc).timestamp())
    if history:
        for deal in reversed(history):
            if deal.position_id == ticket and deal.entry == mt5.DEAL_ENTRY_OUT:
                profit = round(deal.profit + deal.commission + deal.swap, 2)
                status = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BREAKEVEN")
                return {"closed": True, "profit": profit,
                        "close_price": round(deal.price, 2), "status": status}
    return {"closed": False, "profit": 0.0, "close_price": 0.0, "status": "OPEN"}

def get_market_context(df_m5: pd.DataFrame) -> dict:
    if df_m5.empty or len(df_m5) < 20:
        return {}
    last  = df_m5.iloc[-1]
    prev5 = df_m5.tail(5)
    trend = "alcista" if last["sma9"] > last["sma50"] else "bajista"
    candles_summary = " | ".join([
        f"{r['time'].strftime('%H:%M')} {'▲' if r['close']>r['open'] else '▼'}{abs(r['close']-r['open']):.1f}"
        for _, r in prev5.iterrows()
    ])
    return {
        "current_price":   round(last["close"], 2),
        "sma9":            round(last["sma9"],  2) if not np.isnan(last["sma9"])  else 0,
        "sma50":           round(last["sma50"], 2) if not np.isnan(last["sma50"]) else 0,
        "trend":           trend,
        "candles_summary": candles_summary,
    }

def close_position_market(ticket: int, direction: str) -> dict:
    """
    Cierra una posición abierta a mercado de forma inmediata.
    Usado por el RiskGuard para cerrar posiciones de emergencia
    cuando se toca el límite de drawdown diario o máximo.
    """
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.warning(f"close_position_market: ticket {ticket} no encontrado")
        return {"success": False, "error": f"Ticket {ticket} no encontrado"}

    pos = positions[0]
    bid, ask = get_current_price()
    if bid == 0:
        return {"success": False, "error": "Sin precio disponible"}

    # Para cerrar: tipo opuesto a la dirección de la posición
    if direction == "long":
        close_type = mt5.ORDER_TYPE_SELL
        price      = bid
    else:
        close_type = mt5.ORDER_TYPE_BUY
        price      = ask

    filling = _get_filling_mode()
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       config.SYMBOL,
        "volume":       pos.volume,
        "type":         close_type,
        "position":     ticket,
        "price":        price,
        "deviation":    config.DEVIATION,
        "magic":        config.MAGIC,
        "comment":      "RISKGUARD_EMERGENCY_CLOSE",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    log.warning(f"🛑  CIERRE DE EMERGENCIA — ticket:{ticket}  {direction.upper()}  "
                f"lote:{pos.volume}  precio:${price:.2f}")
    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = getattr(result, 'comment', str(mt5.last_error()))
        log.error(f"Cierre de emergencia falló — retcode:{getattr(result,'retcode',None)}  {err}")
        return {"success": False, "error": err}

    log.warning(f"✅  Cierre de emergencia exitoso — ticket:{ticket}  precio:${result.price:.2f}")
    return {"success": True, "close_price": result.price, "volume": pos.volume}
