"""
Motor de IA — Claude API
=========================
Analiza el mercado, valida señales y aprende de operaciones pasadas.
Lee la API key directamente de config.py
"""

import httpx, json, logging
from datetime import datetime, timezone
import config_funded as config

log = logging.getLogger("aurum_bot")

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = """Eres el motor de análisis de AURUM BOT, un bot de trading especializado en XAUUSD (Gold).

Tu estrategia es Break & Retest (BNR):
1. Niveles clave: máx/mín de las últimas 12 velas M5 antes de las 9AM NYC
2. BOS (Break of Structure): precio cierra fuera del nivel con cuerpo sólido
3. Retest: precio regresa a testear el nivel roto (±5 USD)
4. Confirmación: vela M5 con cuerpo ≥38% y mecha ≤50% en dirección del BOS
5. STOP LOSS: extremo de la vela que generó el BOS + 1.5 USD buffer
6. TAKE PROFIT: RR 1:1 (misma distancia que el SL)
7. Ventana: 9AM–12PM hora New York (se ajusta automático verano/invierno)
8. Riesgo: 1% dinámico del capital por trade

Backtesting validado (Mayo–Junio 2026 datos reales):
- 22 trades · WR 90.9% · PF 11.14 · Retorno +19.1% · MaxDD 0.9%
- Longs WR 86% · Shorts WR 93%

IMPORTANTE sobre el historial: el bot opera COMO MÁXIMO 1 vez por día, así
que "2 pérdidas el mismo día" es imposible — nunca lo uses como criterio.
El mensaje te da un campo explícito "RACHA DE PÉRDIDAS CONSECUTIVAS" ya
calculado (cuenta las últimas operaciones consecutivas que fueron LOSS,
sin importar en qué día ocurrieron, cortando en el primer WIN). Usá ESE
número tal cual — no lo recalcules ni lo infieras del historial de texto,
y no trates una sola pérdida aislada (racha=1) como una racha real.

REGLAS DE DECISIÓN:
EJECUTAR (confidence > 0.65):
- SMA9 y SMA50 alineadas con la dirección del trade
- Vela BOS con cuerpo sólido y sin mechas largas
- El retest no cerró al otro lado del nivel
- RACHA DE PÉRDIDAS CONSECUTIVAS ≤ 1

RECHAZAR:
- SMAs fuertemente en contra de la dirección — EXCEPCIÓN: si la vela del BOS
  tiene cuerpo ≥60%, las SMA9/50 son indicadores rezagados y es normal que
  todavía reflejen la tendencia anterior justo después de una reversión
  fuerte. En ese caso no rechaces únicamente por la SMA — priorizá la
  calidad de la vela BOS y del retest.
- SL mayor a 25 USD (movimiento sobreextendido)
- RACHA DE PÉRDIDAS CONSECUTIVAS ≥ 2 (no una pérdida aislada de hace días)
- Múltiples BOS fallidos en el mismo día

ESPERAR:
- Señal marginal — mejor esperar confirmación adicional
- SMAs en zona neutral o cruzando

Responde SIEMPRE con JSON puro, sin markdown ni texto adicional:
{"decision": "EJECUTAR|ESPERAR|RECHAZAR", "confidence": 0.0-1.0, "reasoning": "1-2 frases en español", "market_analysis": "contexto en 2-3 frases"}"""

async def _call_claude(messages: list, max_tokens: int = 400) -> dict:
    """Llamada directa a la API de Anthropic."""
    api_key = config.ANTHROPIC_API_KEY

    if not api_key:
        log.warning("[IA] ANTHROPIC_API_KEY no configurada en config.py — IA desactivada")
        return {
            "decision": "EJECUTAR",
            "confidence": 0.60,
            "reasoning": "IA no disponible — coloca tu API key en config.py",
            "market_analysis": "Sin análisis (API key no configurada)"
        }

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": config.AI_MODEL,
                    "max_tokens": max_tokens,
                    "system": SYSTEM_PROMPT,
                    "messages": messages
                }
            )
            resp.raise_for_status()
            data = resp.json()
            raw  = data["content"][0]["text"].strip()
            raw  = raw.replace("```json", "").replace("```", "").strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # Claude a veces agrega texto antes/después del JSON pese a la instrucción —
                # extraemos el objeto {...} más externo antes de rendirnos.
                start, end = raw.find("{"), raw.rfind("}")
                if start != -1 and end > start:
                    return json.loads(raw[start:end + 1])
                raise

    except json.JSONDecodeError as e:
        log.error(f"[IA] Error parseando JSON: {e}")
        return {"decision": "ESPERAR", "confidence": 0.40,
                "reasoning": f"Error en respuesta IA: {e}",
                "market_analysis": "Error de parsing"}
    except Exception as e:
        log.error(f"[IA] Error llamada API: {e}")
        return {"decision": "EJECUTAR", "confidence": 0.55,
                "reasoning": f"Error API: {str(e)[:60]} — usando lógica base",
                "market_analysis": "Sin análisis disponible"}

async def validate_signal(direction: str, entry: float, sl: float, tp: float,
                            sl_dist: float, bos_candle: dict, level: float,
                            sma9: float, sma50: float,
                            recent_trades: list, current_price: float,
                            breakdown: dict = None) -> dict:
    """Valida si ejecutar la señal BNR con contexto completo."""
    wins_r   = sum(1 for t in recent_trades if t.get("Status") == "WIN")
    losses_r = sum(1 for t in recent_trades if t.get("Status") == "LOSS")
    last5    = recent_trades[-5:] if recent_trades else []
    last5_str = " | ".join([
        f"{t.get('Timestamp','?')[:10]} {t.get('Type','?')} {t.get('Status','?')} ${t.get('Profit','0')}"
        for t in last5
    ]) if last5 else "Sin historial"

    # Racha de pérdidas consecutivas — calculada en código, no dejada a
    # interpretación de la IA. Con MAX_TRADES_DAY=1 no puede haber "2
    # pérdidas el mismo día"; lo relevante es la racha en las operaciones
    # más recientes, sin importar en qué día calendario ocurrieron.
    losing_streak = 0
    for t in reversed(recent_trades):
        if t.get("Status") == "LOSS":
            losing_streak += 1
        else:
            break

    sma_bias    = "ALCISTA" if sma9 > sma50 else "BAJISTA"
    sma_aligned = (direction == "long" and sma9 > sma50) or \
                  (direction == "short" and sma9 < sma50)

    bos_open  = bos_candle.get("open", 0)
    bos_close = bos_candle.get("close", 0)
    bos_high  = bos_candle.get("high", 0)
    bos_low   = bos_candle.get("low", 0)
    bos_range = bos_high - bos_low
    bos_body_pct = abs(bos_close - bos_open) / bos_range if bos_range > 0 else 0

    msg = (
        f"Señal BNR detectada — {datetime.now(tz=timezone.utc).strftime('%H:%M UTC')}\n\n"
        f"DIRECCIÓN: {direction.upper()}\n"
        f"NIVEL ROTO: ${level:.2f}\n"
        f"ENTRADA: ${entry:.2f}\n"
        f"STOP LOSS: ${sl:.2f} (dist {sl_dist:.2f} USD — vela BOS extremo)\n"
        f"TAKE PROFIT: ${tp:.2f} (RR 1:1)\n"
        f"PRECIO ACTUAL: ${current_price:.2f}\n\n"
        f"SMA9: ${sma9:.2f} | SMA50: ${sma50:.2f}\n"
        f"Sesgo SMAs: {sma_bias} | Alineada: {'SÍ' if sma_aligned else 'NO'}\n\n"
        f"Vela BOS — High: ${bos_high:.2f} Low: ${bos_low:.2f} "
        f"Rango: ${bos_range:.2f} | Cuerpo: {bos_body_pct:.0%}\n\n"
        f"Historial del bot ({wins_r+losses_r} ops): {wins_r}W/{losses_r}L\n"
        f"Últimas 5 (con fecha): {last5_str}\n"
        f"RACHA DE PÉRDIDAS CONSECUTIVAS (últimas operaciones, no por día): {losing_streak}\n\n"
        f"¿Ejecuto esta operación?"
    )

    result = await _call_claude([{"role": "user", "content": msg}])
    log.info(f"[IA] validate → {result.get('decision')} conf={result.get('confidence',0):.0%} | {result.get('reasoning','')[:70]}")
    return result

async def analyze_market_open(key_high: float, key_low: float,
                               sma9: float, sma50: float,
                               candles_summary: str,
                               recent_trades: list) -> str:
    """Análisis del mercado al abrir la ventana NY. Retorna texto para Telegram."""
    wins_r   = sum(1 for t in recent_trades if t.get("Status") == "WIN")
    losses_r = sum(1 for t in recent_trades if t.get("Status") == "LOSS")
    wr       = wins_r / max(1, wins_r + losses_r)

    msg = (
        f"Apertura ventana NY. Analiza el mercado para esta sesión:\n\n"
        f"NIVELES HOY:\n"
        f"  Resistencia: ${key_high:.2f}\n"
        f"  Soporte: ${key_low:.2f}\n"
        f"  Rango: ${key_high - key_low:.2f} USD\n\n"
        f"SMAs M5: SMA9=${sma9:.2f} | SMA50=${sma50:.2f}\n"
        f"Sesgo: {'ALCISTA' if sma9>sma50 else 'BAJISTA'}\n\n"
        f"Historial del bot: {wins_r+losses_r} ops | WR {wr:.0%}\n"
        f"  Ganadoras: {wins_r} | Perdedoras: {losses_r}\n\n"
        f"Velas recientes M5:\n{candles_summary}\n\n"
        f"¿Es un día favorable para BNR? ¿Qué dirección tiene mayor probabilidad?"
    )

    result = await _call_claude([{"role": "user", "content": msg}], max_tokens=500)
    return result.get("market_analysis", result.get("reasoning", "Sin análisis disponible"))

async def post_trade_lesson(trade: dict, status: str) -> str:
    """La IA aprende de cada operación cerrada."""
    msg = (
        f"Operación {'GANADA' if status=='WIN' else 'PERDIDA'} del AURUM BOT:\n\n"
        f"Tipo: {trade.get('Type')} | Entrada: ${trade.get('Entry Price',0)}\n"
        f"Cierre: ${trade.get('Close Price',0)} | Resultado: ${trade.get('Profit',0)}\n"
        f"SL: ${trade.get('SL',0)} | TP: ${trade.get('TP',0)}\n\n"
        f"¿Qué lección extraes para mejorar la selección de señales BNR?\n"
        f"Responde con JSON: {{\"decision\":\"APRENDER\",\"confidence\":0.8,\"reasoning\":\"lección\",\"market_analysis\":\"qué mejorar\"}}"
    )
    result = await _call_claude([{"role": "user", "content": msg}], max_tokens=150)
    return result.get("market_analysis", result.get("reasoning", ""))

async def daily_learning_summary(stats: dict, recent_trades: list,
                                  balance: float, net_today: float,
                                  breakdown: dict = None) -> str:
    """Resumen de aprendizaje diario. Retorna texto para Telegram."""
    buy_info  = breakdown.get("BUY",  {}) if breakdown else {}
    sell_info = breakdown.get("SELL", {}) if breakdown else {}

    breakdown_str = (
        f"BUY  → {buy_info.get('wins',0)}W/{buy_info.get('losses',0)}L "
        f"WR {buy_info.get('win_rate',0):.0%}\n"
        f"SELL → {sell_info.get('wins',0)}W/{sell_info.get('losses',0)}L "
        f"WR {sell_info.get('win_rate',0):.0%}"
    ) if breakdown else "Sin desglose disponible"

    last5_str = " | ".join([
        f"{t.get('Type','?')} {t.get('Status','?')} ${t.get('Profit','0')}"
        for t in recent_trades[-5:]
    ]) if recent_trades else "Sin operaciones"

    msg = (
        f"Fin de sesión AURUM BOT — resumen de aprendizaje:\n\n"
        f"RESULTADO DEL DÍA: ${net_today:+.2f}\n"
        f"Balance: ${balance:,.2f}\n\n"
        f"ESTADÍSTICAS GLOBALES:\n"
        f"Total: {stats.get('total',0)} ops | WR {stats.get('win_rate',0):.0%} | "
        f"PF {stats.get('profit_factor',0):.2f}\n"
        f"Neto acumulado: ${stats.get('net_profit',0):+.2f}\n\n"
        f"DESGLOSE POR DIRECCIÓN:\n{breakdown_str}\n\n"
        f"Últimas 5 operaciones:\n{last5_str}\n\n"
        f"¿Qué patrones observas? ¿Qué ajustar para mañana?\n"
        f"Responde en 3-4 frases en español, directo y accionable."
    )

    result = await _call_claude([{"role": "user", "content": msg}], max_tokens=300)
    return result.get("market_analysis", result.get("reasoning", "Sin análisis disponible"))
