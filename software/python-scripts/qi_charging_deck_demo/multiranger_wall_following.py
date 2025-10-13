#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#     ||          ____  _ __
#  +------+      / __ )(_) /_______________ _____  ___
#  | 0xBC |     / __  / / __/ ___/ ___/ __ `/_  / / _ \
#  +------+    / /_/ / / /_/ /__/ /  / /_/ / / /_/  __/
#   ||  ||    /_____/_/\__/\___/_/   \__,_/ /___/\___/
#
#  Copyright (C) 2023 Bitcraze AB
#
#  Crazyflie Python Library
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
Example script that makes the Crazyflie follow a wall and land on a charging pad

This examples uses the Flow and Multi-ranger decks to measure distances
in all directions and do wall following. Straight walls with corners
are advised to have in the test environment.
This is a python port of c-based app layer example from the Crazyflie-firmware
found here https://github.com/bitcraze/crazyflie-firmware/tree/master/examples/
demos/app_wall_following_demo

For the example to run the following hardware is needed:
 * Crazyflie 2.0
 * Crazyradio PA
 * Flow deck
 * Multiranger deck
"""
import logging
import time
from wf_logging import start_new_session, log_status, log_event, instrument_wall_following, LogConfig, get_logger
from math import degrees
from math import radians

from wall_following import WallFollowing

import cflib.crtp
from cflib.crazyflie import Crazyflie
from cflib.crazyflie.log import LogConfig
from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
from cflib.crazyflie.syncLogger import SyncLogger
from cflib.positioning.motion_commander import MotionCommander
from cflib.utils import uri_helper
from cflib.utils.multiranger import Multiranger
import keyboard

URI = uri_helper.uri_from_env(default='radio://0/80/2M/E7E7E7E7E7')


def handle_range_measurement(range):
    if range is None:
        range = 999
    return range


if __name__ == '__main__':
    # Initialize the low-level drivers
    cflib.crtp.init_drivers()

    # Only output errors from the logging framework
    logging.basicConfig(level=logging.ERROR)
    # Start a new logging session
    start_new_session()



    # Tastatur-Listener registrieren

    keep_flying = True

    wall_following = WallFollowing(
        angle_value_buffer=0.1, reference_distance_from_wall=0.15,
        max_forward_speed=0.1, init_state=WallFollowing.StateWallFollowing.FORWARD)

    instrument_wall_following(wall_following)


    def on_key_press(e):
        if e.name == 'c':
            # green console print, charging initiated
            print("\033[92mTaste 'c' gedrückt - Suche nach Ladegerät in einer Ecke wird gestartet!\033[0m")
            log_event("TRIGGER", "Battery low -> PREPARE_TO_LAND")
            wall_following.is_battery_low = True

    def charging_take_off(mc):
        mc.take_off(height=0.05, velocity=0.2)
        mc.start_linear_motion(0.0, 0.0, 0.5)
        time.sleep(0.5)
        mc.start_linear_motion(0.0, 0.0, 0)
        time.sleep(0.5)
        mc.stop()


    def check_battery_level(data):
        pm_state = data.get('pm.state', None)
        wall_following.pm_state = pm_state
        if pm_state == 3 and not wall_following.is_battery_low:
            wall_following.is_battery_low = True
            log_event("TRIGGER", "Battery low -> PREPARE_TO_LAND")
            get_logger().info("Battery low -> PREPARE_TO_LAND")

    keyboard.on_press(on_key_press)

    # Setup logging to get the yaw data
    lg_stab = LogConfig(name='Stabilizer', period_in_ms=100)
    lg_stab.add_variable('stabilizer.yaw', 'float')
    lg_stab.add_variable('pm.state', 'uint8_t')

    cf = Crazyflie(rw_cache='./cache')
    with SyncCrazyflie(URI, cf=cf) as scf:
        # Arm the Crazyflie
        scf.cf.platform.send_arming_request(True)
        time.sleep(1.0)

        with MotionCommander(scf) as motion_commander:
            with Multiranger(scf) as multiranger:
                with SyncLogger(scf, lg_stab) as logger:
                    while keep_flying:

                        # initialize variables
                        velocity_x = 0.0
                        velocity_y = 0.0
                        yaw_rate = 0.0
                        state_wf = WallFollowing.StateWallFollowing.HOVER

                        # Get Yaw
                        log_entry = logger.next()
                        data = log_entry[1]
                        actual_yaw = data['stabilizer.yaw']
                        actual_yaw_rad = radians(actual_yaw)

                        # check battery level
                        check_battery_level(data)

                        # get front range in meters
                        front_range = handle_range_measurement(multiranger.front)
                        top_range = handle_range_measurement(multiranger.up)
                        left_range = handle_range_measurement(multiranger.left)
                        right_range = handle_range_measurement(multiranger.right)

                        # choose here the direction that you want the wall following to turn to
                        wall_following_direction = WallFollowing.WallFollowingDirection.RIGHT
                        side_range = left_range

                        # get velocity commands and current state from wall following state machine
                        velocity_x, velocity_y, yaw_rate, state_wf = wall_following.wall_follower(
                            front_range, side_range, actual_yaw_rad, wall_following_direction, time.time())

                        #--- Logging: zyklischer Status ---
                        try:
                            dt_state = (time.time() - getattr(wall_following, 'state_change_time', time.time()))
                            log_status(state_wf if 'state_wf' in locals() else WallFollowing.StateWallFollowing.HOVER,
                                       front_range, side_range, getattr(wall_following, 'is_battery_low', False),
                                       dt_in_state_s=dt_state)
                        except Exception:
                            pass
                        #----------------------------------
                        pm_state = data.get('pm.state', None)
                        get_logger().info(
                            f"CMD: vx={velocity_x:.2f} vy={velocity_y:.2f} yaw_rate={yaw_rate:.3f} rad/s | state={state_wf} | battery_level={pm_state}")

                        # If battery is low and we are in a corner, land and take off again
                        # here handling of the LANDING state is done
                        if state_wf == WallFollowing.StateWallFollowing.LANDING:
                            get_logger().info("IM HERE LANDING")
                            motion_commander.land(velocity=0.3)
                            for countdown in range(60, 0, -1):
                                log_event("COUNTDOWN", f"Restart in {countdown} Sekunden")
                                time.sleep(1)
                            log_event("COUNTDOWN", "Restart jetzt!")
                            #ensure pwm mode of motors is disabled, so that we can take off again
                            scf.cf.param.set_value('motorPowerSet.enable', '0')
                            time.sleep(0.5)
                            charging_take_off(motion_commander)
                            log_event("CHARGE", "Ladezyklus beendet, Neustart vom Pad")

                            # FSM & Flags sauber resetten
                            wall_following.is_battery_low = False
                            wall_following.align_ok_since = None
                            wall_following.first_run = True  # Heading-Baseline sauber neu setzen
                            wall_following.state = wall_following.state_transition(
                                WallFollowing.StateWallFollowing.TURN_TO_FIND_WALL
                            )

                            motion_commander.stop()
                            continue

                        # convert yaw_rate from rad to deg
                        yaw_rate_deg = degrees(yaw_rate)

                        motion_commander.start_linear_motion(
                            velocity_x, velocity_y, 0, rate_yaw=yaw_rate_deg)

                        # if top_range is activated, stop the demo
                        if top_range < 0.2:
                            keep_flying = False
