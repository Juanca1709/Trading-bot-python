"""
AURUM FUNDED — Risk Guard
==========================
Sistema de protección de la cuenta fondeada de FundedHero.
Monitorea el drawdown en tiempo real con 3 niveles de respuesta:

  Nivel 1 — WARNING  (3% diario / 8.4% total): alerta Telegram, sigue operando
  Nivel 2 — DANGER   (toque del límite real):  cierra posición + bloquea el día
  Nivel 3 — BREACH   (ya superó el límite):    registro de incidente + log
"""

import csv
import json
import logging
import os
from datetime import datetime, timezone, date
from enum import Enum, auto

log = logging.getLogger("aurum_funded")


class RiskLevel(Enum):
    SAFE    = auto()   # Todo en orden
    WARNING = auto()   # Acercándose al límite (alerta preventiva)
    DANGER  = auto()   # Tocó el límite real — acción inmediata
    BREACH  = auto()   # Ya superó el límite — cuenta en riesgo crítico


class RiskGuard:
    """
    Monitorea drawdown diario y máximo en tiempo real.
    Calcula el estado de riesgo y dispara las acciones correspondientes.
    """

    def __init__(self, config, telegram_send_fn, close_position_fn):
        self.cfg                   = config
        self._send                 = telegram_send_fn    # async fn(msg)
        self._close_position       = close_position_fn  # async fn(ticket, direction)

        # Balance de referencia para drawdown máximo (balance inicial fondeado)
        self.reference_balance     = self._load_reference_balance()

        # Último lote ejecutado (Lot Size Consistency Rule) — persistido en
        # disco para que sobreviva a reinicios del bot.
        self.last_lot               = self._load_last_lot()

        # Balance al inicio del día (se actualiza cada reset diario)
        self.day_start_balance     = None

        # Estado interno
        self.daily_warning_sent    = False
        self.max_warning_sent      = False
        self.day_blocked           = False         # True = no operar más hoy
        self.breach_logged         = False
        self._current_day          = None          # fecha NYC del día en curso

        os.makedirs(config.OPERATIONS_DIR, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────────────
    #  BALANCE DE REFERENCIA (guardado en disco)
    # ─────────────────────────────────────────────────────────────────────────
    def _load_reference_balance(self) -> float:
        """
        Lee el balance de referencia guardado en disco.
        Si no existe (primera ejecución), devuelve 0 — debe inicializarse
        con set_reference_balance() al arrancar el bot.
        """
        path = self.cfg.REFERENCE_BALANCE_FILE
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    val = float(f.read().strip())
                    log.info(f"📋  Balance de referencia cargado: ${val:,.2f}")
                    return val
            except Exception as e:
                log.error(f"Error leyendo balance de referencia: {e}")
        return 0.0

    def set_reference_balance(self, balance: float):
        """
        Guarda el balance de referencia en disco.
        Llamar UNA SOLA VEZ al arrancar el bot por primera vez con la cuenta fondeada.
        En ejecuciones posteriores, se carga del archivo.
        """
        if self.reference_balance > 0:
            log.info(f"📋  Balance de referencia ya existe: ${self.reference_balance:,.2f} — no se sobreescribe")
            return
        self.reference_balance = balance
        try:
            with open(self.cfg.REFERENCE_BALANCE_FILE, "w") as f:
                f.write(str(balance))
            log.info(f"📋  Balance de referencia guardado: ${balance:,.2f}")
        except Exception as e:
            log.error(f"Error guardando balance de referencia: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    #  ÚLTIMO LOTE (Lot Size Consistency Rule — guardado en disco)
    # ─────────────────────────────────────────────────────────────────────────
    def _load_last_lot(self) -> float | None:
        """
        Lee el último lote ejecutado, guardado en disco. Necesario para que
        la Lot Size Consistency Rule (±0.5) siga aplicándose correctamente
        aunque el bot se reinicie entre operaciones.
        """
        path = self.cfg.LAST_LOT_FILE
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    val = float(f.read().strip())
                    log.info(f"📋  Último lote cargado: {val}")
                    return val
            except Exception as e:
                log.error(f"Error leyendo último lote: {e}")
        return None

    def save_last_lot(self, lot: float):
        """Guarda el último lote ejecutado en disco — llamar tras cada entrada exitosa."""
        self.last_lot = lot
        try:
            with open(self.cfg.LAST_LOT_FILE, "w") as f:
                f.write(str(lot))
            log.info(f"📋  Último lote guardado: {lot}")
        except Exception as e:
            log.error(f"Error guardando último lote: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    #  ESTADO DEL DÍA (day_start_balance, bloqueos, alertas — guardado en disco)
    #  Evita que un reinicio a mitad de sesión "olvide" un bloqueo por
    #  drawdown, o reinicie day_start_balance al balance actual (lo que
    #  subestimaría el drawdown diario real).
    # ─────────────────────────────────────────────────────────────────────────
    def _load_day_tracking(self, day) -> bool:
        path = self.cfg.RISK_DAY_STATE_FILE
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if data.get("date") != day.isoformat():
                return False
            self.day_start_balance  = data["day_start_balance"]
            self.daily_warning_sent = data["daily_warning_sent"]
            self.max_warning_sent   = data["max_warning_sent"]
            self.day_blocked        = data["day_blocked"]
            self.breach_logged      = data.get("breach_logged", False)
            log.info(f"📋  Estado de riesgo diario recuperado — "
                     f"day_start=${self.day_start_balance:,.2f}  bloqueado={self.day_blocked}")
            return True
        except Exception as e:
            log.error(f"Error cargando estado de riesgo diario: {e}")
            return False

    def _save_day_tracking(self, day):
        if day is None:
            return
        try:
            data = {
                "date":               day.isoformat(),
                "day_start_balance":  self.day_start_balance,
                "daily_warning_sent": self.daily_warning_sent,
                "max_warning_sent":   self.max_warning_sent,
                "day_blocked":        self.day_blocked,
                "breach_logged":      self.breach_logged,
            }
            with open(self.cfg.RISK_DAY_STATE_FILE, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.error(f"Error guardando estado de riesgo diario: {e}")

    def reset_day(self, current_balance: float, day=None):
        """
        Llamar al inicio de cada nuevo día NYC. Si `day` corresponde al mismo
        día ya persistido (reinicio del bot a mitad de sesión), RECUPERA el
        estado en vez de resetearlo — así no se pierde un bloqueo por
        drawdown ni se subestima el drawdown diario ya acumulado.
        """
        self._current_day = day
        if day is not None and self._load_day_tracking(day):
            return
        self.day_start_balance  = current_balance
        self.daily_warning_sent = False
        self.max_warning_sent   = False
        self.day_blocked        = False
        self.breach_logged      = False
        log.info(f"🛡️  RiskGuard — nuevo día — balance inicio: ${current_balance:,.2f}  "
                 f"ref: ${self.reference_balance:,.2f}")
        self._save_day_tracking(day)

    # ─────────────────────────────────────────────────────────────────────────
    #  CÁLCULO DE DRAWDOWN
    # ─────────────────────────────────────────────────────────────────────────
    def get_drawdown(self, current_balance: float) -> dict:
        """
        Calcula drawdown diario y máximo acumulado.
        FundedHero usa drawdown ESTÁTICO (ancla en balance inicial, no en equity peak).
        """
        day_dd = max(0.0, (self.day_start_balance - current_balance) / self.day_start_balance) \
            if self.day_start_balance and self.day_start_balance > 0 else 0.0

        max_dd = max(0.0, (self.reference_balance - current_balance) / self.reference_balance) \
            if self.reference_balance > 0 else 0.0

        return {
            "day_dd_pct":    round(day_dd * 100, 3),
            "max_dd_pct":    round(max_dd * 100, 3),
            "day_dd_usd":    round(self.day_start_balance - current_balance, 2) if self.day_start_balance else 0,
            "max_dd_usd":    round(self.reference_balance - current_balance, 2),
            "day_remaining": round((self.cfg.DAILY_DD_LIMIT - day_dd) * 100, 3),
            "max_remaining": round((self.cfg.MAX_DD_LIMIT - max_dd) * 100, 3),
        }

    # ─────────────────────────────────────────────────────────────────────────
    #  EVALUACIÓN DE RIESGO (llamar en cada ciclo del bot)
    # ─────────────────────────────────────────────────────────────────────────
    async def evaluate(self, current_balance: float, open_ticket=None,
                       open_direction=None) -> RiskLevel:
        """
        Evalúa el estado de riesgo actual y ejecuta la acción correspondiente.
        Devuelve el RiskLevel actual para que el bot decida si opera o no.
        """
        if self.day_start_balance is None:
            return RiskLevel.SAFE

        dd = self.get_drawdown(current_balance)
        day_pct = dd["day_dd_pct"] / 100
        max_pct = dd["max_dd_pct"] / 100

        # ── NIVEL 3: BREACH — ya superó el límite real ────────────────────
        if day_pct > self.cfg.DAILY_DD_LIMIT or max_pct > self.cfg.MAX_DD_LIMIT:
            if not self.breach_logged:
                self.breach_logged = True
                self._save_day_tracking(self._current_day)
                msg = (f"🚨 BREACH DETECTADO\n"
                       f"Drawdown diario: {dd['day_dd_pct']:.2f}% (límite: {self.cfg.DAILY_DD_LIMIT*100:.0f}%)\n"
                       f"Drawdown máximo: {dd['max_dd_pct']:.2f}% (límite: {self.cfg.MAX_DD_LIMIT*100:.0f}%)\n"
                       f"Balance actual: ${current_balance:,.2f}\n"
                       f"⚠️ El bot ha sido detenido para proteger la cuenta.")
                await self._send(msg)
                self._log_alert("BREACH", dd, current_balance)
            return RiskLevel.BREACH

        # ── NIVEL 2: DANGER — tocó el límite real → cerrar y bloquear ─────
        if day_pct >= self.cfg.DAILY_DD_LIMIT or max_pct >= self.cfg.MAX_DD_LIMIT:
            tipo = "DIARIO" if day_pct >= self.cfg.DAILY_DD_LIMIT else "MÁXIMO"
            pct  = dd["day_dd_pct"] if tipo == "DIARIO" else dd["max_dd_pct"]
            lim  = self.cfg.DAILY_DD_LIMIT * 100 if tipo == "DIARIO" else self.cfg.MAX_DD_LIMIT * 100

            msg = (f"🛑 LÍMITE DE DRAWDOWN {tipo} ALCANZADO\n"
                   f"Drawdown {tipo.lower()}: {pct:.2f}% (límite: {lim:.0f}%)\n"
                   f"Balance actual: ${current_balance:,.2f}\n"
                   f"Acción: Cerrando posición abierta y bloqueando operaciones por hoy.")
            await self._send(msg)
            self._log_alert(f"DANGER_{tipo}", dd, current_balance)

            # Cerrar posición abierta si existe
            if open_ticket and open_direction:
                try:
                    await self._close_position(open_ticket, open_direction)
                    log.warning(f"🛑  Posición {open_ticket} cerrada por protección de drawdown")
                except Exception as e:
                    log.error(f"Error cerrando posición de emergencia: {e}")

            self.day_blocked = True
            self._save_day_tracking(self._current_day)
            return RiskLevel.DANGER

        # ── NIVEL 1: WARNING — acercándose al límite ──────────────────────
        if day_pct >= self.cfg.DAILY_DD_WARNING and not self.daily_warning_sent:
            self.daily_warning_sent = True
            self._save_day_tracking(self._current_day)
            msg = (f"⚠️ ALERTA DRAWDOWN DIARIO\n"
                   f"Drawdown diario: {dd['day_dd_pct']:.2f}% "
                   f"(límite: {self.cfg.DAILY_DD_LIMIT*100:.0f}%)\n"
                   f"Margen restante: {dd['day_remaining']:.2f}%\n"
                   f"Balance actual: ${current_balance:,.2f}\n"
                   f"El bot sigue operando — monitoreando de cerca.")
            await self._send(msg)
            self._log_alert("WARNING_DIARIO", dd, current_balance)
            log.warning(f"⚠️  ALERTA DIARIA — DD diario: {dd['day_dd_pct']:.2f}%")

        if max_pct >= self.cfg.MAX_DD_WARNING and not self.max_warning_sent:
            self.max_warning_sent = True
            self._save_day_tracking(self._current_day)
            msg = (f"⚠️ ALERTA DRAWDOWN MÁXIMO\n"
                   f"Drawdown acumulado: {dd['max_dd_pct']:.2f}% "
                   f"(límite: {self.cfg.MAX_DD_LIMIT*100:.0f}%)\n"
                   f"Margen restante: {dd['max_remaining']:.2f}%\n"
                   f"Balance actual: ${current_balance:,.2f}\n"
                   f"El bot sigue operando — monitoreando de cerca.")
            await self._send(msg)
            self._log_alert("WARNING_MAXIMO", dd, current_balance)
            log.warning(f"⚠️  ALERTA MÁXIMA — DD total: {dd['max_dd_pct']:.2f}%")

        return RiskLevel.WARNING if (day_pct >= self.cfg.DAILY_DD_WARNING or
                                      max_pct >= self.cfg.MAX_DD_WARNING) else RiskLevel.SAFE

    # ─────────────────────────────────────────────────────────────────────────
    #  REGISTRO DE DRAWDOWN Y ALERTAS
    # ─────────────────────────────────────────────────────────────────────────
    def log_drawdown(self, current_balance: float):
        """Registra el estado de drawdown actual en CSV."""
        dd = self.get_drawdown(current_balance)
        row = {
            "timestamp":    datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "balance":      current_balance,
            "day_start":    self.day_start_balance,
            "ref_balance":  self.reference_balance,
            "dd_diario_pct": dd["day_dd_pct"],
            "dd_maximo_pct": dd["max_dd_pct"],
            "dd_diario_usd": dd["day_dd_usd"],
            "dd_maximo_usd": dd["max_dd_usd"],
        }
        self._write_csv(self.cfg.DRAWDOWN_LOG_CSV, row)

    def _log_alert(self, alert_type: str, dd: dict, balance: float):
        """Registra una alerta disparada en CSV."""
        row = {
            "timestamp":     datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "tipo":          alert_type,
            "balance":       balance,
            "dd_diario_pct": dd["day_dd_pct"],
            "dd_maximo_pct": dd["max_dd_pct"],
            "ref_balance":   self.reference_balance,
        }
        self._write_csv(self.cfg.ALERTS_LOG_CSV, row)
        log.warning(f"🚨  ALERTA REGISTRADA: {alert_type}  DD_d:{dd['day_dd_pct']:.2f}%  "
                    f"DD_m:{dd['max_dd_pct']:.2f}%  bal:${balance:,.2f}")

    def _write_csv(self, path: str, row: dict):
        """Escribe una fila en un CSV, creando el header si el archivo no existe."""
        try:
            file_exists = os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as e:
            log.error(f"Error escribiendo CSV {path}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    #  VERIFICACIÓN DE LOT SIZE CONSISTENCY
    # ─────────────────────────────────────────────────────────────────────────
    def validate_lot(self, proposed_lot: float, last_lot: float | None) -> tuple[bool, float]:
        """
        Valida y ajusta el lote propuesto según la Lot Size Consistency Rule.
        Retorna (es_valido, lote_ajustado).
        Si last_lot es None (primer trade), el lote pasa sin restricción.
        """
        if last_lot is None:
            return True, proposed_lot

        max_allowed = round(last_lot + self.cfg.LOT_CONSISTENCY_MAX_CHANGE, 2)
        min_allowed = max(self.cfg.MIN_LOT,
                          round(last_lot - self.cfg.LOT_CONSISTENCY_MAX_CHANGE, 2))

        if proposed_lot > max_allowed:
            log.info(f"   Lote ajustado por consistencia: {proposed_lot} → {max_allowed} "
                     f"(anterior: {last_lot}, máx cambio: ±{self.cfg.LOT_CONSISTENCY_MAX_CHANGE})")
            return False, max_allowed
        elif proposed_lot < min_allowed:
            log.info(f"   Lote ajustado por consistencia: {proposed_lot} → {min_allowed} "
                     f"(anterior: {last_lot}, máx cambio: ±{self.cfg.LOT_CONSISTENCY_MAX_CHANGE})")
            return False, min_allowed

        return True, proposed_lot

    # ─────────────────────────────────────────────────────────────────────────
    #  STATUS SUMMARY
    # ─────────────────────────────────────────────────────────────────────────
    def status_summary(self, current_balance: float) -> str:
        """Genera un resumen de estado para logs y Telegram."""
        dd = self.get_drawdown(current_balance)
        blocked = "🔴 BLOQUEADO" if self.day_blocked else "🟢 OPERATIVO"
        return (f"🛡️ Estado RiskGuard: {blocked}\n"
                f"Balance: ${current_balance:,.2f}  |  Ref: ${self.reference_balance:,.2f}\n"
                f"DD Diario: {dd['day_dd_pct']:.2f}% (límite: {self.cfg.DAILY_DD_LIMIT*100:.0f}%, "
                f"alerta: {self.cfg.DAILY_DD_WARNING*100:.0f}%)\n"
                f"DD Máximo: {dd['max_dd_pct']:.2f}% (límite: {self.cfg.MAX_DD_LIMIT*100:.0f}%, "
                f"alerta: {self.cfg.MAX_DD_WARNING*100:.0f}%)\n"
                f"Margen diario restante: {dd['day_remaining']:.2f}%  |  "
                f"Margen total restante: {dd['max_remaining']:.2f}%")
