<p align="center">
  <img src="banner.png" alt="AURUM — Bot de Trading con Validación por IA" width="220">
</p>

Sistema de trading algorítmico en Python que opera sobre MetaTrader 5,
integra un modelo de lenguaje (Claude, de Anthropic) como capa de validación
de señales, y aplica un motor de gestión de riesgo con reglas de cuenta
fondeada (prop firm) en tres niveles de severidad.

Desarrollado como proyecto personal para profundizar en automatización de
procesos en tiempo real, integración de APIs externas, programación
asíncrona y aplicación práctica de IA en la toma de decisiones.

> ⚠️ **Aviso**: este proyecto es de carácter educativo y demuestra
> arquitectura de software, no una recomendación de inversión. Operar
> instrumentos financieros conlleva riesgo real de pérdida de capital.

---

## Qué resuelve

Un bot de trading "que solo ejecuta órdenes" es fácil de escribir y difícil
de confiar. Este proyecto está diseñado alrededor de tres problemas reales
de operar una cuenta con capital de un tercero (prop firm):

1. **La señal técnica no siempre es suficiente** — se necesita una segunda
   opinión contextual (historial reciente, rachas de pérdidas, estructura
   del mercado) antes de arriesgar capital.
2. **El drawdown hay que vigilarlo en tiempo real, no al final del día** —
   un solo trade fuera de control puede violar las reglas de la cuenta
   fondeada y perderla.
3. **El bot puede reiniciarse en cualquier momento** (caída del proceso,
   reinicio de Windows, corte de conexión) y debe recuperar su estado
   exacto sin duplicar operaciones ni romper las reglas de lotaje.

## Arquitectura

```
bot_funded.py        # Bucle principal: detección de estructura de mercado,
                      # ventana operativa NY, orquestación de todo lo demás
risk_guard.py         # RiskGuard: monitoreo de drawdown en 3 niveles
                      # (WARNING / DANGER / BREACH) con acciones automáticas
config_funded.py      # Parámetros de estrategia y carga de credenciales
                      # desde variables de entorno (nunca hardcodeadas)
core/
  mt5_connector.py     # Conexión y operaciones sobre MetaTrader 5
  telegram_bot.py       # Notificaciones y alertas en tiempo real (async)
  ai_engine.py           # Integración con la API de Claude para validar
                          # señales, analizar apertura de mercado y generar
                          # aprendizaje post-operación
  logger.py               # Registro estructurado de operaciones y métricas
```

### Decisiones técnicas destacadas

- **Async I/O** (`asyncio` + `httpx.AsyncClient`) para las llamadas a
  Telegram y a la API de Claude, evitando bloquear el bucle principal
  mientras se espera una respuesta de red.
- **Máquina de estados de riesgo** con `Enum` (`RiskLevel.SAFE / WARNING /
  DANGER / BREACH`) en lugar de banderas booleanas sueltas — cada nivel
  dispara una acción concreta (alerta, cierre de posición, bloqueo del día).
- **Validación de señales asistida por IA**: antes de ejecutar una entrada,
  el bot arma un contexto (nivel roto, estructura de la vela, medias
  móviles, racha de resultados recientes) y se lo envía a la API de Claude,
  que responde en JSON estructurado con decisión, nivel de confianza y
  razonamiento. La racha de pérdidas se calcula en código —no se deja a
  interpretación del modelo— para evitar errores de conteo.
- **Persistencia de estado en disco** (balance de referencia, último
  lotaje operado, estado del día) para que el bot sea tolerante a
  reinicios sin violar la regla de consistencia de lotaje del prop firm.
- **Separación estricta de secretos y configuración**: todas las
  credenciales se cargan por variables de entorno (`python-dotenv`); el
  código fuente no contiene ningún valor sensible.

## Stack

Python 3 · MetaTrader5 API · Telegram Bot API · Anthropic API (Claude) ·
pandas · NumPy · httpx (async) · asyncio · logging estructurado

## Puesta en marcha

```bash
git clone <este-repo>
cd aurum-trading-bot
pip install -r requirements.txt
cp .env.example .env      # completa tus propias credenciales
python bot_funded.py
```

Requiere una terminal de MetaTrader 5 instalada y una cuenta activa
(demo o real) en el equipo donde se ejecuta el bot.

## Autor

Juan Camilo Losada Pedraza — Desarrollador Python Jr., enfocado en
automatización de sistemas e integración práctica de IA.
[LinkedIn](#) · [GitHub](#)
