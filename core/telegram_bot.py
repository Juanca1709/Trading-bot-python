"""
Telegram — Notificaciones del Bot
====================================
Envía mensajes detallados en cada evento:
- Análisis de mercado al abrir ventana
- BOS detectado (esperando retest)
- Entrada colocada (solo SL primero)
- TP colocado (5 segundos después)
- Cierre WIN o LOSS con resumen
- Resumen diario al cerrar ventana
"""

import httpx, asyncio, logging
from datetime import datetime, timezone
import config_funded as config

log = logging.getLogger("aurum_bot")

BASE_URL = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"

async def _send(text: str):
    """Envía un mensaje Telegram. Silencia errores para no interrumpir el bot."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado — skipping mensaje")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{BASE_URL}/sendMessage", json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML"
            })
    except Exception as e:
        log.error(f"Telegram error: {e}")

async def msg_startup(balance: float):
    now = datetime.now(tz=timezone.utc)
    ventana = f"{config.NYC_WINDOW_START_HOUR}AM–{config.NYC_WINDOW_END_HOUR}PM NYC"
    text = (
        f"🤖 <b>AURUM BOT — INICIANDO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Fecha: <code>{now.strftime('%d/%m/%Y')}</code>\n"
        f"⏰ Hora UTC: <code>{now.strftime('%H:%M')}</code>\n"
        f"💰 Balance: <code>${balance:,.2f}</code>\n"
        f"📊 Par: <code>XAUUSD · M5</code>\n"
        f"🎯 Estrategia: <code>Break & Retest · SL en vela BOS</code>\n"
        f"🕐 Ventana: <code>{ventana}</code>\n"
        f"⚙️ Riesgo: <code>1% dinámico · RR 1:1</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Bot activo y monitoreando..."
    )
    await _send(text)

async def msg_market_analysis(analysis: str, key_high: float, key_low: float,
                               sma9: float, sma50: float, bias: str):
    now = datetime.now(tz=timezone.utc)
    bias_icon = "📈" if bias == "bullish" else ("📉" if bias == "bearish" else "➡️")
    text = (
        f"🔍 <b>ANÁLISIS DE MERCADO — {now.strftime('%d/%m %H:%M')} UTC</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 Nivel resistencia: <code>${key_high:.2f}</code>\n"
        f"📐 Nivel soporte:     <code>${key_low:.2f}</code>\n"
        f"📊 SMA9:  <code>${sma9:.2f}</code>\n"
        f"📊 SMA50: <code>${sma50:.2f}</code>\n"
        f"{bias_icon} Sesgo: <b>{bias.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <i>{analysis}</i>"
    )
    await _send(text)

async def msg_bos_detected(direction: str, level: float, bos_candle_price: float,
                            current_price: float):
    icon = "⬆️" if direction == "long" else "⬇️"
    dir_text = "ALCISTA (LONG)" if direction == "long" else "BAJISTA (SHORT)"
    now = datetime.now(tz=timezone.utc)
    text = (
        f"{icon} <b>BOS DETECTADO — {now.strftime('%H:%M')} UTC</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Tipo: <b>{dir_text}</b>\n"
        f"🔑 Nivel roto: <code>${level:.2f}</code>\n"
        f"🕯️ Precio vela BOS: <code>${bos_candle_price:.2f}</code>\n"
        f"💹 Precio actual: <code>${current_price:.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <i>Esperando retest al nivel para confirmar entrada...</i>"
    )
    await _send(text)

async def msg_entry(ticket: int, direction: str, entry_price: float,
                     sl: float, sl_dist: float, lot: float, risk_money: float,
                     bos_ext: float):
    """
    Primer mensaje al entrar: SOLO entrada + SL.
    El TP se enviará en un mensaje separado 5 segundos después.
    """
    icon = "🟢" if direction == "long" else "🔴"
    type_text = "COMPRA (LONG)" if direction == "long" else "VENTA (SHORT)"
    now = datetime.now(tz=timezone.utc)
    text = (
        f"{icon} <b>ENTRADA EJECUTADA — {now.strftime('%H:%M')} UTC</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket: <code>#{ticket}</code>\n"
        f"📌 Tipo: <b>{type_text}</b>\n"
        f"💵 Precio entrada: <code>${entry_price:.2f}</code>\n"
        f"🛑 Stop Loss: <code>${sl:.2f}</code>\n"
        f"   Distancia SL: <code>{sl_dist:.2f} USD</code>\n"
        f"   (sobre extremo vela BOS: <code>${bos_ext:.2f}</code>)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Lote: <code>{lot:.2f}</code>\n"
        f"💸 Riesgo: <code>${risk_money:.2f}</code> (1% dinámico)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <i>Calculando Take Profit RR 1:1...</i>"
    )
    await _send(text)

async def msg_tp_set(ticket: int, tp: float, sl_dist: float):
    """Segundo mensaje: TP colocado (se envía ~5 segundos después de la entrada)."""
    text = (
        f"🎯 <b>TAKE PROFIT COLOCADO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket: <code>#{ticket}</code>\n"
        f"✅ Take Profit: <code>${tp:.2f}</code>\n"
        f"📐 RR: <code>1:1</code> ({sl_dist:.2f} USD)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <i>Operación activa — monitoreando cada 30s...</i>"
    )
    await _send(text)

async def msg_close_win(ticket: int, direction: str, entry: float,
                         close_price: float, profit: float,
                         duration_min: int, balance: float):
    now = datetime.now(tz=timezone.utc)
    type_text = "LONG ⬆️" if direction == "long" else "SHORT ⬇️"
    text = (
        f"✅ <b>OPERACIÓN CERRADA — WIN 🏆</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket: <code>#{ticket}</code>\n"
        f"📌 Tipo: <b>{type_text}</b>\n"
        f"💵 Entrada: <code>${entry:.2f}</code>\n"
        f"💵 Cierre:  <code>${close_price:.2f}</code>\n"
        f"💰 Ganancia: <b>+${profit:.2f}</b>\n"
        f"⏱️ Duración: <code>{duration_min} min</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance: <code>${balance:,.2f}</code>\n"
        f"🕐 Cierre: <code>{now.strftime('%H:%M')} UTC</code>"
    )
    await _send(text)

async def msg_close_loss(ticket: int, direction: str, entry: float,
                          close_price: float, profit: float,
                          duration_min: int, balance: float):
    now = datetime.now(tz=timezone.utc)
    type_text = "LONG ⬆️" if direction == "long" else "SHORT ⬇️"
    text = (
        f"❌ <b>OPERACIÓN CERRADA — LOSS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket: <code>#{ticket}</code>\n"
        f"📌 Tipo: <b>{type_text}</b>\n"
        f"💵 Entrada: <code>${entry:.2f}</code>\n"
        f"💵 Cierre:  <code>${close_price:.2f}</code>\n"
        f"📉 Pérdida: <b>-${abs(profit):.2f}</b>\n"
        f"⏱️ Duración: <code>{duration_min} min</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance: <code>${balance:,.2f}</code>\n"
        f"🕐 Cierre: <code>{now.strftime('%H:%M')} UTC</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <i>Analizando el trade para aprender...</i>"
    )
    await _send(text)

async def msg_daily_summary(stats: dict, balance: float):
    now = datetime.now(tz=timezone.utc)
    wr_text = f"{stats['win_rate']*100:.1f}%"
    pf_text = f"{stats['profit_factor']:.2f}"
    text = (
        f"📊 <b>RESUMEN DIARIO — {now.strftime('%d/%m/%Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 Total operaciones: <code>{stats['total']}</code>\n"
        f"✅ Ganadoras: <code>{stats['wins']}</code>\n"
        f"❌ Perdedoras: <code>{stats['losses']}</code>\n"
        f"📈 Win Rate: <b>{wr_text}</b>\n"
        f"💹 Profit Factor: <b>{pf_text}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ganancia bruta: <code>+${stats['gross_profit']:.2f}</code>\n"
        f"📉 Pérdida bruta:  <code>-${stats['gross_loss']:.2f}</code>\n"
        f"💼 Neto: <b>${stats['net_profit']:+.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance actual: <code>${balance:,.2f}</code>\n"
        f"🔴 Ventana NY cerrada — hasta mañana a las 13:00 UTC"
    )
    await _send(text)

async def msg_no_trade_summary(motivo: str, balance: float):
    """Resumen al cierre de la ventana NY cuando el bot NO operó ese día."""
    now = datetime.now(tz=timezone.utc)
    text = (
        f"🔕 <b>SIN OPERACIONES HOY — {now.strftime('%d/%m/%Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{motivo}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance: <code>${balance:,.2f}</code>\n"
        f"🔴 Ventana NY cerrada — hasta mañana a las 13:00 UTC"
    )
    await _send(text)

async def msg_ai_analysis(analysis: str, decision: str, confidence: float):
    icon = "✅" if decision == "EJECUTAR" else "⛔"
    text = (
        f"🤖 <b>ANÁLISIS IA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} Decisión: <b>{decision}</b>\n"
        f"🎯 Confianza: <code>{confidence*100:.0f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{analysis}</i>"
    )
    await _send(text)

async def msg_partial_close(ticket: int, direction: str, entry: float,
                             close_price: float, lot_closed: float,
                             lot_remaining: float, profit_partial: float,
                             new_sl: float, tp: float):
    """Mensaje al ejecutar el cierre parcial + movimiento de SL a BE."""
    now = datetime.now(tz=timezone.utc)
    type_text = "LONG ⬆️" if direction == "long" else "SHORT ⬇️"
    pips = abs(close_price - entry)
    text = (
        f"🔒 <b>CIERRE PARCIAL — 75% asegurado</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket: <code>#{ticket}</code>\n"
        f"📌 Tipo: <b>{type_text}</b>\n"
        f"💵 Entrada: <code>${entry:.2f}</code>\n"
        f"💵 Cierre parcial: <code>${close_price:.2f}</code>\n"
        f"📏 Movimiento: <code>{pips:.2f} USD</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ganancia parcial: <b>+${profit_partial:.2f}</b>\n"
        f"📦 Lote cerrado: <code>{lot_closed:.2f}</code> (75%)\n"
        f"📦 Lote restante: <code>{lot_remaining:.2f}</code> (25%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ SL movido a: <code>${new_sl:.2f}</code> (breakeven)\n"
        f"🎯 TP sigue en: <code>${tp:.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <i>Ganancia asegurada. 25% sigue corriendo hacia el TP.</i>\n"
        f"🕐 {now.strftime('%H:%M')} UTC"
    )
    await _send(text)


async def msg_retest_waiting(direction: str, level: float, current_price: float,
                             dist_to_zone: float, touches: int, elapsed_min: int):
    """Heartbeat cada 10 min mientras el bot espera el retest."""
    icon    = "⬆️" if direction == "long" else "⬇️"
    dir_txt = "LONG" if direction == "long" else "SHORT"
    now     = datetime.now(tz=timezone.utc)
    in_zone = dist_to_zone <= 0
    zone_st = "🟡 <b>EN ZONA DE RETEST</b>" if in_zone else f"📏 {dist_to_zone:.2f} USD lejos de la zona"
    text = (
        f"⏳ <b>ESPERANDO RETEST — {now.strftime('%H:%M')} UTC</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} Setup activo: <b>{dir_txt}</b>\n"
        f"🔑 Nivel: <code>${level:.2f}</code> ({touches} toque{'s' if touches != 1 else ''})\n"
        f"💹 Precio actual: <code>${current_price:.2f}</code>\n"
        f"{zone_st}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ Esperando hace <b>{elapsed_min} min</b>\n"
        f"🤖 <i>Bot activo — revisando cada 30s</i>"
    )
    await _send(text)


async def msg_retest_touch_failed(direction: str, level: float, reason: str,
                                   candle_close: float, current_price: float):
    """Notificación cuando el precio toca la zona pero la vela no confirma."""
    icon    = "⬆️" if direction == "long" else "⬇️"
    dir_txt = "LONG" if direction == "long" else "SHORT"
    now     = datetime.now(tz=timezone.utc)
    text = (
        f"⚠️ <b>ZONA TOCADA — SIN CONFIRMAR ({now.strftime('%H:%M')} UTC)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} Setup: <b>{dir_txt}</b>\n"
        f"🔑 Nivel: <code>${level:.2f}</code>\n"
        f"🕯️ Cierre vela: <code>${candle_close:.2f}</code>\n"
        f"💹 Precio actual: <code>${current_price:.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❌ <i>{reason}</i>\n"
        f"⏳ <i>Esperando siguiente vela...</i>"
    )
    await _send(text)


async def msg_error(error_text: str):
    text = (
        f"⚠️ <b>ERROR DEL BOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{error_text}</code>"
    )
    await _send(text)

_SOURCE_LABELS = {
    "M5-swing":      "M5 estructural (2+ toques) ✅",
    "12-velas":      "12 velas base (1 toque) ⚠️",
    "M15":           "M15 estructural (2+ toques) ✅",
    "M30":           "M30 estructural (2+ toques) ✅",
    "fallback-6velas": "Fallback 6 velas (sin filtro) ⛔",
}

async def msg_levels_ready(key_high: float, key_low: float,
                            ref_price: float, label: str,
                            src_high: str = "", src_low: str = ""):
    now = datetime.now(tz=timezone.utc)
    src_h_txt = _SOURCE_LABELS.get(src_high, src_high)
    src_l_txt = _SOURCE_LABELS.get(src_low,  src_low)
    text = (
        f"📐 <b>NIVELES CALCULADOS — {now.strftime('%H:%M')} UTC</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔺 Resistencia: <code>${key_high:.2f}</code>\n"
        f"   └ Fuente: <i>{src_h_txt}</i>\n"
        f"🔻 Soporte:     <code>${key_low:.2f}</code>\n"
        f"   └ Fuente: <i>{src_l_txt}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📏 Rango: <code>${key_high - key_low:.2f} USD</code>\n"
        f"💹 Ref. precio: <code>${ref_price:.2f}</code>\n"
        f"ℹ️ <i>{label}</i>"
    )
    await _send(text)

async def msg_level_updated(direction: str, old_level: float, new_level: float,
                             current_price: float, source: str):
    """Notifica cuando un nivel se reemplaza en vivo por uno más cercano y válido."""
    icon    = "🔺" if direction == "high" else "🔻"
    tipo    = "Resistencia" if direction == "high" else "Soporte"
    src_txt = _SOURCE_LABELS.get(source, source)
    text = (
        f"🔄 <b>NIVEL ACTUALIZADO EN VIVO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} {tipo}: <code>${old_level:.2f}</code> → <code>${new_level:.2f}</code>\n"
        f"   └ Fuente: <i>{src_txt}</i>\n"
        f"💹 Precio actual: <code>${current_price:.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"ℹ️ <i>Se detectó un nivel más cercano y válido — reemplaza al anterior.</i>"
    )
    await _send(text)

async def msg_ai_daily_summary(summary: str, net_today: float, balance: float):
    now = datetime.now(tz=timezone.utc)
    icon = "📈" if net_today >= 0 else "📉"
    text = (
        f"🧠 <b>RESUMEN IA DEL DÍA — {now.strftime('%d/%m/%Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} P&L del día: <b>${net_today:+.2f}</b>\n"
        f"💼 Balance: <code>${balance:,.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{summary}</i>"
    )
    await _send(text)
