"""
AURUM FUNDED — Configuración Central
======================================
Bot dedicado a operar una cuenta fondeada bajo las reglas de un prop firm.

Las credenciales y claves NUNCA se escriben aquí. Se cargan desde variables
de entorno (ver `.env.example`) usando python-dotenv, para que este archivo
pueda vivir en un repositorio público sin exponer nada sensible.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── CREDENCIALES MT5 (cuenta fondeada) ────────────────────────────────────────
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", "0")) or None   # Número de cuenta fondeada
MT5_PASSWORD = os.getenv("MT5_PASSWORD")                   # Contraseña de la cuenta fondeada
MT5_SERVER   = os.getenv("MT5_SERVER")                     # Servidor MT5 del broker/prop firm

# ── CREDENCIALES TELEGRAM ─────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── CLAUDE AI ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# ── SÍMBOLO ───────────────────────────────────────────────────────────────────
SYMBOL    = "XAUUSD#"
MAGIC     = 20260201
DEVIATION = 20
COMMENT   = "AURUM_FUNDED"

# ── GESTIÓN DE RIESGO — LOTE FIJO SOBRE BALANCE DE REFERENCIA ────────────────
# El lote se calcula como RISK_PCT × REFERENCE_BALANCE_FOR_LOT / (SL × 100).
# REFERENCE_BALANCE_FOR_LOT es el balance INICIAL de la cuenta fondeada,
# obtenido automáticamente de MT5 al arrancar el bot y guardado en disco.
# Esto evita que el lote escale con las ganancias (cumplimiento de la
# Lot Size Consistency Rule del prop firm).
RISK_PCT                  = 0.02     # 2% de riesgo por operación
MAX_RISK_PCT              = 0.05     # Tope duro: nunca más del 5% en un trade
MIN_LOT                   = 0.01
MAX_LOT                   = 10.0
SPREAD_EST                = 0.13
COMMISSION                = 7.0      # USD por lote, round trip

# ── REGLAS DEL PROP FIRM — LÍMITES DE DRAWDOWN ────────────────────────────────
# Drawdown se mide sobre el balance al inicio de cada día (diario) y sobre
# el balance al inicio de la cuenta fondeada (máximo acumulado).
DAILY_DD_LIMIT            = 0.06     # 6%  — límite real del prop firm
DAILY_DD_WARNING          = 0.03     # 3%  — alerta preventiva (50% del límite)
MAX_DD_LIMIT              = 0.12     # 12% — límite real del prop firm
MAX_DD_WARNING            = 0.084    # 8.4% — alerta preventiva (70% del límite)

BREACH_ACTION             = "close_and_stop"

# ── REGLAS DEL PROP FIRM — LOT SIZE CONSISTENCY ──────────────────────────────
LOT_CONSISTENCY_MAX_CHANGE = 0.50

# ── OPERATIVA ─────────────────────────────────────────────────────────────────
MAX_TRADES_DAY            = 1        # Una sola operación por día (regla propia)

# ── ESTRATEGIA BNR (Break & Retest) ──────────────────────────────────────────
BNR_CONFIRM               = 1.5
BOS_MAX_RANGE             = 20.0
BOS_MIN_RANGE             = 3.0
BOS_MIN_BODY_PCT          = 0.45
BNR_RETEST_TOL            = 5.0
BOS_SL_BUFFER             = 1.5
MIN_SL                    = 2.0
MAX_SL                    = 45.0
TP_RR                     = 2.0
LARGE_SL_THRESHOLD        = 20.0
ENTRY_PRICE_DRIFT_TOL     = 3.0
MIN_BODY_PCT              = 0.38
MAX_WICK_PCT              = 0.50
PRE_WINDOW_BARS           = 12
MAX_ENTRY_ATTEMPTS_PER_BOS = 3

# ── DETECCIÓN DE NIVELES ──────────────────────────────────────────────────────
SWING_SEARCH_START_HOUR   = 8
SWING_CLUSTER_TOL         = 2.0
SWING_MIN_TOUCHES         = 2
MAX_LEVEL_DIST            = 25.0

MAX_LEVEL_UPDATES_PER_SIDE = 3
LEVEL_FREEZE_DIST          = 5.0

# ── VENTANA OPERATIVA — 9AM-12PM HORA NEW YORK ───────────────────────────────
NYC_WINDOW_START_HOUR     = 9
NYC_WINDOW_END_HOUR       = 12
NYC_TIMEZONE              = "America/New_York"

# ── GESTIÓN DE TRADE — SOLO BREAKEVEN (sin cierre parcial) ───────────────────
BE_TRIGGER                = 0.75
BE_BUFFER                 = 0.5

# ── IA ────────────────────────────────────────────────────────────────────────
AI_ENABLED                = True
AI_MIN_CONFIDENCE         = 0.55
AI_MODEL                  = "claude-sonnet-4-6"
AI_LEARN_FROM_LAST        = 20

# ── INTERVALOS (segundos) ─────────────────────────────────────────────────────
LOOP_INTERVAL             = 5
MONITOR_INTERVAL          = 30
OUTSIDE_INTERVAL          = 60
NIGHT_INTERVAL            = 300

# ── RUTAS ─────────────────────────────────────────────────────────────────────
BASE_DIR              = os.path.dirname(os.path.abspath(__file__))
OPERATIONS_DIR        = os.path.join(BASE_DIR, "Operaciones_Funded")
LOG_FILE              = os.path.join(BASE_DIR, "logs", "aurum_funded.log")
OPERATIONS_CSV        = os.path.join(OPERATIONS_DIR, "registro_funded.csv")
DRAWDOWN_LOG_CSV      = os.path.join(OPERATIONS_DIR, "drawdown_log.csv")
ALERTS_LOG_CSV        = os.path.join(OPERATIONS_DIR, "alerts_log.csv")
REFERENCE_BALANCE_FILE = os.path.join(BASE_DIR, "funded_reference_balance.txt")
LAST_LOT_FILE          = os.path.join(BASE_DIR, "funded_last_lot.txt")
DAY_STATE_FILE         = os.path.join(BASE_DIR, "funded_day_state.json")
RISK_DAY_STATE_FILE    = os.path.join(BASE_DIR, "funded_risk_day_state.json")
