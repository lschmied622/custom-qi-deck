#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import logging
import csv
import uuid
from pathlib import Path
from datetime import datetime

import cflib.crtp
from cflib.crazyflie import Crazyflie
from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
from cflib.crazyflie.log import LogConfig
from cflib.utils import uri_helper

# ------------------------------------------------------------
# Verbindung
# ------------------------------------------------------------
URI = uri_helper.uri_from_env(default='radio://0/80/2M/E7E7E7E7E5')

# ------------------------------------------------------------
# Motor-Setup (Direkt-PWM)
# ------------------------------------------------------------
PWM_PERCENT = 0.04                 # ≈ 5-10 %
PWM_MAX = 65535
PWM_VAL = max(0, min(PWM_MAX, int(round(PWM_PERCENT * PWM_MAX))))  # ≈ 3277
vbat = None

def set_all_motors(cf, val: int):
    cf.param.set_value('motorPowerSet.m1', str(val))
    cf.param.set_value('motorPowerSet.m2', str(val))
    cf.param.set_value('motorPowerSet.m3', str(val))
    cf.param.set_value('motorPowerSet.m4', str(val))

# ------------------------------------------------------------
# Logging-Konfiguration
# ------------------------------------------------------------
LOG_PERIOD_MS = 200  # Abtastrate der Telemetrie
LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
CSV_PATH = LOG_DIR / f"cf_powerlog_{RUN_ID}.csv"
CSV_HEADER = ["t_host_s", "baro.temp_C", "pm.batteryLevel_pct",
              "pm.chargeCurrent_mA", "pm.state", "pm.vbat_V"]

csv_file = None
csv_writer = None
t0 = None

def _fmt(val, ndigits=3):
    try:
        return f"{float(val):.{ndigits}f}"
    except Exception:
        return "nan"

def init_csv():
    global csv_file, csv_writer
    csv_file = CSV_PATH.open("w", newline="", encoding="utf-8")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(CSV_HEADER)

def close_csv():
    global csv_file
    if csv_file:
        csv_file.flush()
        csv_file.close()
        csv_file = None

def on_log_data(timestamp, data, logconf):
    """Callback je Stichprobe: Konsole + CSV."""
    if csv_writer is None:
        return
    t = (time.time() - t0) if t0 is not None else 0.0
    global vbat, state

    batt = data.get("pm.batteryLevel")
    temp = data.get("baro.temp")
    ichg = data.get("pm.chargeCurrent")
    state = data.get("pm.state")
    vbat = data.get("pm.vbat")

    # Konsole
    print(f"[{t:7.2f}s] T={_fmt(temp,2)} °C | Vbat={_fmt(vbat,3)} V | "
          f"Batt={_fmt(batt,1)} % | Ichg={_fmt(ichg,1)} mA | pm.state={int(state) if state is not None else 'nan'}")

    # CSV (Werte in definierten Einheiten)
    csv_writer.writerow([
        f"{t:.3f}",
        _fmt(temp,3),
        _fmt(batt,3),
        _fmt(ichg,3),
        int(state) if state is not None else "",
        _fmt(vbat,3),
    ])

def on_log_error(logconf, msg):
    print(f"[LOG][ERROR] {msg}")

def safe_stop(cf, retries: int = 5, delay_s: float = 0.05):
    """
    Robuste Abschaltung für motorPowerSet:
    - Mehrfaches Setzen aller Motoren auf 0
    - Mehrfaches Deaktivieren von enable
    - Kurze Delays zwischen den Writes, damit ACKs sicher durchlaufen
    """
    for _ in range(retries):
        try:
            cf.param.set_value('motorPowerSet.m1', '0')
            time.sleep(delay_s)
            cf.param.set_value('motorPowerSet.m2', '0')
            time.sleep(delay_s)
            cf.param.set_value('motorPowerSet.m3', '0')
            time.sleep(delay_s)
            cf.param.set_value('motorPowerSet.m4', '0')
            time.sleep(delay_s)
            cf.param.set_value('motorPowerSet.enable', '0')
            time.sleep(delay_s)
        except Exception:
            # Nächster Versuch
            pass
    # Zusätzlich
    try:
        cf.commander.send_stop_setpoint()
    except Exception:
        pass
    # Kurze Wartezeit, damit alle Übertragungen abgeschlossen sind
    time.sleep(0.3)


# ------------------------------------------------------------
# Hauptprogramm
# ------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cflib.crtp.init_drivers()

    init_csv()
    print(f"[INFO] CSV-Logging nach: {CSV_PATH.resolve()}  (Periode: {LOG_PERIOD_MS} ms)")

    cf = Crazyflie(rw_cache='./cache')
    lg = None  # Referenz auf LogConfig für sauberes Stoppen

    with SyncCrazyflie(URI, cf=cf) as scf:
        # 1) Arm
        scf.cf.platform.send_arming_request(True)  # benötigt aktuelle Firmware
        time.sleep(1)

        try:
            # --- Telemetrie-Logging starten (asynchron, nicht blockierend) ---
            lg = LogConfig(name='PowerLog', period_in_ms=LOG_PERIOD_MS)
            lg.add_variable('baro.temp', 'float')
            lg.add_variable('pm.batteryLevel', 'float')
            lg.add_variable('pm.chargeCurrent', 'float')
            lg.add_variable('pm.state', 'uint8_t')
            lg.add_variable('pm.vbat', 'float')

            lg.data_received_cb.add_callback(on_log_data)
            lg.error_cb.add_callback(on_log_error)
            scf.cf.log.add_config(lg)

            t0 = time.time()
            lg.start()

            # 2) Direkt-PWM aktivieren
            scf.cf.param.set_value('motorPowerSet.enable', '1')  # 1 = PWM direkt an Motoren
            time.sleep(0.05)

            #Kickstart: einmal kurz auf 20% setzen, damit die Motoren sicher starten
            set_all_motors(scf.cf, int(0.2 * PWM_MAX))
            time.sleep(0.15)

            # 3) Alle Motoren auf ~5 % setzen
            set_all_motors(scf.cf, PWM_VAL)
            print(f"[INFO] Motors at ~{PWM_PERCENT*100:.1f}% PWM ({PWM_VAL}/{PWM_MAX})")

            # Testdauer: währenddessen läuft das Logging im Hintergrund
            while (vbat < 4.2) if vbat is not None else True:
                time.sleep(0.2)

        finally:
            # 4) Sicher abschalten (zuerst Motoren!)
            safe_stop(scf.cf, retries=6, delay_s=0.06)
            try:
                scf.cf.platform.send_arming_request(False)
            except Exception:
                pass

            # Logging stoppen & Datei schließen
            try:
                if lg is not None:
                    lg.stop()
            except Exception:
                pass
            close_csv()
            print("[INFO] Motors stopped and disarmed.")
            print(f"[INFO] Log-Datei geschrieben: {CSV_PATH.resolve()}")
