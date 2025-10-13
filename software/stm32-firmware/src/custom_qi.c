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

#define DEBUG_MODULE "CUSTOM_QI"

// ---------- API-Kompatibilität für ID-Validierung ----------
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

// ---------------- Konfiguration (per Param änderbar) ----------------
static volatile uint16_t cfgKickPct  = 15;     // %
static volatile uint16_t cfgHoldPct  = 5;      // %
static volatile uint16_t cfgKickMs   = 200;    // ms
static volatile uint8_t  cfgEnable   = 1;      // 1=aktiv
static volatile uint8_t  cfgForceRun = 0;      // 1=erzwinge Rotorlauf (Test)

/* pm.state-Wert, der "Charging" bedeutet
   0 Battery
   1 Charging
   2 Charged
   3 Low power
   4 Shutdown */
static volatile int      cfgPmChargingVal = 1;

// ---------------- Mock-Override mit TTL (per Param) ----------------
static volatile uint8_t  cfgMockEnable   = 0;     // 1: Mock aktiv
static volatile uint8_t  cfgMockCharging = 0;     // 1: "laden"-Ersatz
static volatile uint16_t cfgMockTtlMs    = 3000;  // Gültigkeit in ms
static TickType_t        mockTouchedTick = 0;
static int               prevMockCharging = -1;

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
  // --- Mock mit TTL vorschalten ---
  if (cfgMockEnable) {
    if ((int)cfgMockCharging != prevMockCharging) {
      prevMockCharging = (int)cfgMockCharging;
      mockTouchedTick  = xTaskGetTickCount();
    }
    const TickType_t now = xTaskGetTickCount();
    const TickType_t ttl = M2T(cfgMockTtlMs);
    if (ttl > 0 && (now - mockTouchedTick) < ttl) {
      lgCharging = cfgMockCharging ? 1 : 0;
      return cfgMockCharging != 0;
    }
    // TTL abgelaufen → auf echten PM-Pfad zurückfallen
  }

#ifdef PM_H_EXISTS
  // Primärweg, falls pm.h vorhanden: echte Ladeerkennung
  bool ch = pmIsCharging();
  lgCharging = ch ? 1 : 0;
  return ch;
#else
  // Fallback über Log-Variable pm.state
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
PARAM_GROUP_START(custom_qi)
PARAM_ADD(PARAM_UINT8,  enable,          &cfgEnable)
PARAM_ADD(PARAM_UINT8,  forceRun,        &cfgForceRun)
PARAM_ADD(PARAM_UINT16, kickPct,         &cfgKickPct)
PARAM_ADD(PARAM_UINT16, holdPct,         &cfgHoldPct)
PARAM_ADD(PARAM_UINT16, kickMs,          &cfgKickMs)
PARAM_ADD(PARAM_INT32,  pmChargingValue, &cfgPmChargingVal)
// Mock-Steuerung
PARAM_ADD(PARAM_UINT8,  mockEnable,      &cfgMockEnable)
PARAM_ADD(PARAM_UINT8,  mockCharging,    &cfgMockCharging)
PARAM_ADD(PARAM_UINT16, mockTtlMs,       &cfgMockTtlMs)
PARAM_GROUP_STOP(custom_qi)

LOG_GROUP_START(custom_qi)
LOG_ADD(LOG_UINT8,  state,     &lgState)
LOG_ADD(LOG_UINT8,  charging,  &lgCharging)
LOG_ADD(LOG_UINT32, pmState,   &lgPmStateRaw)
LOG_GROUP_STOP(custom_qi)

// --------------- Worker-Task ----------------------
static void customQiTask(void *arg) {
  (void)arg;
  QiState st = QI_IDLE;
  lgState = st;

  DEBUG_PRINT("[CUSTOM_QI] task started\n");

  for (;;) {
    vTaskDelay(M2T(20));

    if (!cfgEnable) {
      if (st != QI_IDLE) {
        motorsStopAll(); paramSetInt(idMpEnable, 0); st = QI_IDLE; lgState = st;
        DEBUG_PRINT("[CUSTOM_QI] disabled -> OFF\n");
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
          DEBUG_PRINT("[CUSTOM_QI] -> RUN (chg=%d, force=%d)\n", chg, cfgForceRun);
        }
        break;

      case QI_RUNNING:
        if (!shouldRun) {
          motorsStopAll();
          paramSetInt(idMpEnable, 0);                         // Bypass aus
          st = QI_IDLE; lgState = st;
          DEBUG_PRINT("[CUSTOM_QI] -> IDLE\n");
        } else {
          // Halte-PWM regelmäßig setzen (robust ggü. Überschreiben)
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
    DEBUG_PRINT("[CUSTOM_QI] motorPowerSet param IDs invalid\n");
    return;
  }

  // pm.state (Fallback) erst nach Param-Init holen
  idPmState = logGetVarId("pm", "state");

  // Sicherheit: Bypass aus
  paramSetInt(idMpEnable, 0);

  // Task starten
  xTaskCreate(customQiTask, "custom_qi", configMINIMAL_STACK_SIZE*2,
              NULL, tskIDLE_PRIORITY + 1, NULL);

  DEBUG_PRINT("[CUSTOM_QI] driver initialized\n");
}

static bool __attribute__((used)) customQiTest(void) { return true; }

// ---------------- Deck-Definition ------------------
static const DeckDriver customQiDriver = {
  .vid  = 0,
  .pid  = 0,
  .name = "custom_qi",
  .init = customQiInit,
  .test = customQiTest,
};

DECK_DRIVER(customQiDriver);
