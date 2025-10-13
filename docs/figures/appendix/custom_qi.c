#include <stdbool.h>
#include <stdint.h>
#include "deck.h"
#include "param.h"
#include "log.h"
#include "debug.h"
#include "motors.h"
#include "FreeRTOS.h"
#include "task.h"
#ifdef PM_H_EXISTS
#include "pm.h"
#endif

#define DEBUG_MODULE "QI_FAN_DECK"

// ---------- API-Kompatibilitaet fuer ID-Validierung ----------
#ifndef LOG_ID_VALID
#  ifdef LOG_VARID_IS_VALID
#    define LOG_ID_VALID(id)        (LOG_VARID_IS_VALID(id))
#  else
#    ifdef LOG_VAR_ID_INVALID
#      define LOG_ID_VALID(id)      ((id) != LOG_VAR_ID_INVALID)
#    elif defined(LOG_VAR_ID_UNDEFINED)
#      define LOG_ID_VALID(id)      ((id) != LOG_VAR_ID_UNDEFINED)
#    else
#      define LOG_ID_VALID(id)      ((id) != (logVarId_t)-1)
#    endif
#  endif
#endif
#ifndef PARAM_ID_VALID
#  ifdef PARAM_VARID_IS_VALID
#    define PARAM_ID_VALID(id)      (PARAM_VARID_IS_VALID(id))
#  else
#    ifdef PARAM_VAR_ID_INVALID
#      define PARAM_ID_VALID(id)    ((id) != PARAM_VAR_ID_INVALID)
#    elif defined(PARAM_VAR_ID_UNDEFINED)
#      define PARAM_ID_VALID(id)    ((id) != PARAM_VAR_ID_UNDEFINED)
#    else
#      define PARAM_ID_VALID(id)    ((id) != (paramVarId_t)-1)
#    endif
#  endif
#endif
// ----------------------------------------------------------

// ---------------- Konfiguration (per Param aenderbar) ----------------
static volatile uint16_t cfgKickPct  = 15;      // %
static volatile uint16_t cfgHoldPct  = 5;      // %
static volatile uint16_t cfgKickMs   = 200;     // ms
static volatile uint8_t  cfgEnable   = 1;       // 1=aktiv
static volatile uint8_t  cfgForceRun = 0;       // 1=erzwinge Rotorlauf (Test)

/* pm.state-Wert, der "Charging" bedeutet 
0	Battery
1	Charging
2	Charged
3	Low power
4	Shutdown */
static volatile int      cfgPmChargingVal = 1;

// ---------------- Laufzeit-/Log-Status ----------------
typedef enum { QI_IDLE=0, QI_RUNNING } QiState;
static volatile uint8_t  lgState      = QI_IDLE;
static volatile uint8_t  lgCharging   = 0;
static volatile uint32_t lgPmStateRaw = 0;

// -------------- IDs für motorPowerSet & pm.state --------------
static paramVarId_t idMpEnable, idM1, idM2, idM3, idM4;
static logVarId_t   idPmState = (logVarId_t)-1;

// ----------------- Hilfsfunktionen -----------------
static inline uint16_t pctToRaw(uint16_t pct) {
  return (uint16_t)((UINT16_MAX / 100U) * pct);
}
static inline void setMotorRawAll(uint16_t raw) {
  paramSetInt(idM1, (int32_t)raw);
  paramSetInt(idM2, (int32_t)raw);
  paramSetInt(idM3, (int32_t)raw);
  paramSetInt(idM4, (int32_t)raw);
}
static inline void motorsStopAll(void) { setMotorRawAll(0); }

static bool detectCharging(void) {
#ifdef pmIsCharging
  bool ch = pmIsCharging();
  lgCharging = ch ? 1 : 0;
  return ch;
#else
  if (LOG_ID_VALID(idPmState)) {
    uint32_t s = logGetUint(idPmState);
    lgPmStateRaw = s;
    bool ch = ((int)s == cfgPmChargingVal);
    lgCharging = ch ? 1 : 0;
    return ch;
  }
  lgCharging = 0;
  return false;
#endif
}

// ---------------- Param/Log-Gruppen ----------------
PARAM_GROUP_START(qiFan)
PARAM_ADD(PARAM_UINT8,  enable,          &cfgEnable)
PARAM_ADD(PARAM_UINT8,  forceRun,        &cfgForceRun)
PARAM_ADD(PARAM_UINT16, kickPct,         &cfgKickPct)
PARAM_ADD(PARAM_UINT16, holdPct,         &cfgHoldPct)
PARAM_ADD(PARAM_UINT16, kickMs,          &cfgKickMs)
PARAM_ADD(PARAM_INT32,  pmChargingValue, &cfgPmChargingVal)
PARAM_GROUP_STOP(qiFan)

LOG_GROUP_START(qiFan)
LOG_ADD(LOG_UINT8,  state,     &lgState)
LOG_ADD(LOG_UINT8,  charging,  &lgCharging)
LOG_ADD(LOG_UINT32, pmState,   &lgPmStateRaw)
LOG_GROUP_STOP(qiFan)

// --------------- Worker-Task ----------------------
static void qiFanTask(void *arg) {
  (void)arg;
  QiState st = QI_IDLE;
  lgState = st;

  DEBUG_PRINT("[QI_FAN_DECK] task started\n");

  for (;;) {
    vTaskDelay(M2T(20));

    if (!cfgEnable) {
      if (st != QI_IDLE) {
        motorsStopAll(); paramSetInt(idMpEnable, 0); st = QI_IDLE; lgState = st;
        DEBUG_PRINT("[QI_FAN_DECK] disabled -> OFF\n");
      }
      continue;
    }

    const bool chg = detectCharging();
    const bool shouldRun = (cfgForceRun != 0) || chg;

    switch (st) {
      case QI_IDLE:
        if (shouldRun) {
          paramSetInt(idMpEnable, 1);                         // Bypass an
          setMotorRawAll(pctToRaw(cfgKickPct));               // Kick
          vTaskDelay(M2T(cfgKickMs));
          setMotorRawAll(pctToRaw(cfgHoldPct));               // Halte-PWM
          st = QI_RUNNING; lgState = st;
          DEBUG_PRINT("[QI_FAN_DECK] -> RUN (chg=%d, force=%d)\n", chg, cfgForceRun);
        }
        break;

      case QI_RUNNING:
        if (!shouldRun) {
          motorsStopAll();
          paramSetInt(idMpEnable, 0);                         // Bypass aus
          st = QI_IDLE; lgState = st;
          DEBUG_PRINT("[QI_FAN_DECK] -> IDLE\n");
        } else {
          // Halte-PWM regelmaessig setzen
          setMotorRawAll(pctToRaw(cfgHoldPct));
        }
        break;
    }
  }
}

// ---------------- Deck-Callbacks ------------------
static void __attribute__((used)) customQiInit(DeckInfo *info) {
  (void)info;

  // IDs holen
  idMpEnable = paramGetVarId("motorPowerSet", "enable");
  idM1 = paramGetVarId("motorPowerSet", "m1");
  idM2 = paramGetVarId("motorPowerSet", "m2");
  idM3 = paramGetVarId("motorPowerSet", "m3");
  idM4 = paramGetVarId("motorPowerSet", "m4");

  if (!(PARAM_ID_VALID(idMpEnable) && PARAM_ID_VALID(idM1) &&
        PARAM_ID_VALID(idM2) && PARAM_ID_VALID(idM3) && PARAM_ID_VALID(idM4))) {
    DEBUG_PRINT("[QI_FAN_DECK] motorPowerSet param IDs invalid\n");
    return;
  }

  // pm.state (Fallback) erst nach Param-Init holen
  idPmState = logGetVarId("pm", "state");

  // Sicherheit: Bypass aus
  paramSetInt(idMpEnable, 0);

  // Task starten
  xTaskCreate(qiFanTask, "qiFan", configMINIMAL_STACK_SIZE*2,
              NULL, tskIDLE_PRIORITY + 1, NULL);

  //DEBUG_PRINT("[QI_FAN_DECK] driver initialized (mp en=%ld)\n", (long)idMpEnable);
  DEBUG_PRINT("[QI_FAN_DECK] driver initialized\n");
}

static bool __attribute__((used)) customQiTest(void) { return true; }

static void __attribute__((used)) customQiDeinit(void) {
  motorsStopAll();
  if (PARAM_ID_VALID(idMpEnable)) { paramSetInt(idMpEnable, 0); }
  DEBUG_PRINT("[QI_FAN_DECK] driver deinit\n");
}

// ---------------- Deck-Definition ------------------
// Registrierung: automatische Bindung an OW-EEPROM-Namen
static const DeckDriver qiDriver = {
  .vid  = 0,
  .pid  = 0,
  .name = "custom_qi",
  .init = customQiInit,
  .test = customQiTest,
};

DECK_DRIVER(qiDriver);
