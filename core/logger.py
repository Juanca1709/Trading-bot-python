"""
Logger — Registro de Operaciones
==================================
Guarda cada trade en Operaciones_Gold/registro.csv con el formato exacto:
Timestamp,Ticket,Symbol,Type,Entry Price,Close Price,Profit,SL,TP,Volume,Risk Money,Status
"""

import csv, os, logging
from datetime import datetime, timezone
from typing import Optional
import config_funded as config

# Logger de sistema
logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("aurum_bot")

# ── CSV HEADERS ───────────────────────────────────────────────────────────────
CSV_HEADERS = [
    "Timestamp", "Ticket", "Symbol", "Type",
    "Entry Price", "Close Price", "Profit",
    "SL", "TP", "Volume", "Risk Money", "Status"
]

def _ensure_csv():
    """Crea el CSV con headers si no existe."""
    os.makedirs(config.OPERATIONS_DIR, exist_ok=True)
    if not os.path.exists(config.OPERATIONS_CSV):
        with open(config.OPERATIONS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
        log.info("Archivo registro.csv creado")

def save_open(ticket: int, direction: str, entry_price: float,
              sl: float, tp: float, volume: float, risk_money: float):
    """
    Guarda la apertura de una operación.
    Status = OPEN mientras está activa.
    """
    _ensure_csv()
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now,
        ticket,
        config.SYMBOL,
        "BUY" if direction == "long" else "SELL",
        round(entry_price, 2),
        "",           # Close Price — se llena al cerrar
        "",           # Profit — se llena al cerrar
        round(sl, 2),
        round(tp, 2),
        volume,
        round(risk_money, 2),
        "OPEN"
    ]
    with open(config.OPERATIONS_CSV, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)
    log.info(f"OPEN  ticket={ticket} {direction.upper()} entry={entry_price:.2f} sl={sl:.2f} tp={tp:.2f} lot={volume} risk=${risk_money:.2f}")

def save_close(ticket: int, close_price: float, profit: float, status: str):
    """
    Actualiza la fila del ticket con el precio de cierre y resultado.
    status: 'WIN', 'LOSS', 'BREAKEVEN'
    """
    _ensure_csv()
    rows = []
    updated = False
    with open(config.OPERATIONS_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[1] == str(ticket) and row[11] == "OPEN":
                row[5]  = round(close_price, 2)   # Close Price
                row[6]  = round(profit, 2)         # Profit
                row[11] = status                   # Status
                updated = True
            rows.append(row)

    with open(config.OPERATIONS_CSV, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    if updated:
        log.info(f"CLOSE ticket={ticket} close={close_price:.2f} profit=${profit:.2f} status={status}")
    else:
        log.warning(f"Ticket {ticket} no encontrado en registro para cerrar")

def get_open_trades() -> list:
    """Retorna todas las operaciones con Status=OPEN."""
    _ensure_csv()
    open_trades = []
    with open(config.OPERATIONS_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Status") == "OPEN":
                open_trades.append(row)
    return open_trades

def get_recent_trades(n: int = 20) -> list:
    """Retorna las últimas N operaciones cerradas para que la IA aprenda."""
    _ensure_csv()
    closed = []
    with open(config.OPERATIONS_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Status") in ("WIN", "LOSS", "BREAKEVEN"):
                closed.append(row)
    return closed[-n:]

def get_performance_breakdown() -> dict:
    """Desglose de rendimiento por dirección (BUY/SELL)."""
    _ensure_csv()
    result = {}
    for trade_type in ("BUY", "SELL"):
        wins = losses = bes = 0
        gross_profit = gross_loss = 0.0
        with open(config.OPERATIONS_CSV, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Type") != trade_type: continue
                st = row.get("Status", "")
                if st == "OPEN": continue
                try:
                    p = float(row.get("Profit", 0) or 0)
                except:
                    p = 0.0
                if st == "WIN":         wins += 1;   gross_profit += p
                elif st == "LOSS":      losses += 1; gross_loss   += abs(p)
                elif st == "BREAKEVEN": bes += 1
        total = wins + losses + bes
        wr = wins / (wins + losses) if (wins + losses) > 0 else 0
        pf = gross_profit / gross_loss if gross_loss > 0 else 0
        result[trade_type] = {
            "total": total, "wins": wins, "losses": losses, "bes": bes,
            "win_rate": round(wr, 4), "profit_factor": round(pf, 4),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_profit": round(gross_profit - gross_loss, 2),
        }
    return result

def get_stats() -> dict:
    """Estadísticas globales del registro."""
    _ensure_csv()
    total = wins = losses = bes = 0
    gross_profit = gross_loss = 0.0
    with open(config.OPERATIONS_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            st = row.get("Status", "")
            if st == "OPEN": continue
            total += 1
            try:
                p = float(row.get("Profit", 0) or 0)
            except:
                p = 0.0
            if st == "WIN":       wins += 1;   gross_profit += p
            elif st == "LOSS":    losses += 1; gross_loss   += abs(p)
            elif st == "BREAKEVEN": bes += 1
    wr = wins / (wins + losses) if (wins + losses) > 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else 0
    return {
        "total": total, "wins": wins, "losses": losses, "bes": bes,
        "win_rate": round(wr, 4), "profit_factor": round(pf, 4),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_profit": round(gross_profit - gross_loss, 2),
    }
