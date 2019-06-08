#!/usr/bin/env python3
"""
Scripts to drive a donkey 2 car

Usage:
    path_follower.py (drive) [--log=INFO]
 

Options:
    -h --help          Show this screen.
    --js               Use physical joystick.
    -f --file=<file>   A text file containing paths to tub files, one per line. Option may be used more than once.
    --meta=<key:value> Key/Value strings describing describing a piece of meta data about this drive. Option may be used more than once.
"""
import os
import time
import logging
import json
from subprocess import Popen
import shlex

from docopt import docopt
import numpy as np
import zmq


import donkeycar as dk
from donkeycar.parts.controller import LocalWebController, JoystickController
from donkeycar.parts.controller import PS3JoystickController, PS4JoystickController, NimbusController, XboxOneJoystickController
from donkeycar.parts.actuator import PCA9685, PWMSteering, PWMThrottle
from donkeycar.parts.path import Path, PathPlot, CTE, PID_Pilot, PlotCircle, PImage, OriginOffset
from donkeycar.parts.transform import PIDController
from donkeycar.parts.encoder import RotaryEncoder


ODOM_PLUS_RS_T265 = False
USE_KINEMATIC_MODEL = True

class T265Server():
    def __init__(self, path, config, port=5555):
        command = '%s --config %s --port=%d' % (path, config, port)
        args =  shlex.split(command)
        self.proc = Popen(args)        

    def shutdown(self):
        if self.proc is not None:
            print("stopping tflite server")
            self.proc.terminate()
            self.proc = None

    def run(self):
        pass

    def __del__(self):
        self.shutdown()

class OdomRemoteAdapter:
    '''
    send odom to t265 and get back pos stream
    '''

    def __init__(self, ip='localhost', port=5555):
        self.ctx = zmq.Context()
        self.socket = self.ctx.socket(zmq.REQ)
        self.socket.connect("tcp://%s:%d" % (ip, port))
        self.frame = 0
        self.running = True
        self.odom_vel_ms = 0.0
        self.pos = (0, 0)

    def update(self):
        while self.running:
            self.frame += 1
            packet = '{"wheel_id" : 0 , "frame" : %d , "vel_x" : 0.0 , "vel_y" : 0.0 , "vel_z" : %f }' % ( self.frame, self.odom_vel_ms)
            self.socket.send_string(packet)
            message = self.socket.recv()
            telem_obj = json.loads(message.decode('UTF-8'))
            self.pos = (telem_obj['x'], telem_obj['z'])

    def run_threaded(self, odom_vel_ms):
        self.odom_vel_ms = odom_vel_ms
        return self.pos[0], self.pos[1]

    def shutdown(self):
        self.running = False
        time.sleep(1.0)

import math

class CarKinematics(object):
    #reference http://correll.cs.colorado.edu/?p=1869
    '''
    Let the robot coordinate system (x_r, y_r, theta_r) be centered on the car’s rear axis.
    '''

    def __init__(self, x, y, theta, front_rear_wheel_dist_m):
        self.x = x
        self.y = y
        self.L = front_rear_wheel_dist_m
        self.theta = theta

    def run(self, dist_m, steering_angle_radians):
        print('dist', dist_m, "steer", steering_angle_radians)
        B = dist_m / self.L * math.tan(steering_angle_radians)
        thresh = 0.001
        if abs(B) > thresh:
            R = self.L / math.tan(steering_angle_radians)
            B = dist_m / R
            Xc = self.x - R * math.sin(self.theta) 
            Yc = self.y + R * math.cos(self.theta) 
            self.x = Xc + R * math.sin(self.theta + B)
            self.y = Yc - R * math.cos(self.theta + B)
            self.theta = (self.theta + B) % (2.0 * math.pi)
        else:
            self.x = self.x + dist_m * math.cos(self.theta)
            self.y = self.y + dist_m * math.sin(self.theta)
            self.theta = (self.theta + B) % (2.0 * math.pi)
        return self.x, self.y
        

def drive(cfg):
    '''
    Construct a working robotic vehicle from many parts.
    Each part runs as a job in the Vehicle loop, calling either
    it's run or run_threaded method depending on the constructor flag `threaded`.
    All parts are updated one after another at the framerate given in
    cfg.DRIVE_LOOP_HZ assuming each part finishes processing in a timely manner.
    Parts may have named outputs and inputs. The framework handles passing named outputs
    to parts requesting the same named input.
    '''
    
    #Initialize car
    V = dk.vehicle.Vehicle()

    if cfg.HAVE_SOMBRERO:
        from donkeycar.utils import Sombrero
        s = Sombrero()

    PIN = 36 # doug 7
    enc = RotaryEncoder(mm_per_tick=22.16, pin=PIN, poll_delay=0.05, debug=False)
    V.add(enc, outputs=['enc/dist_m', 'enc/vel_m_s', 'enc/delta_dist_m'], threaded=True)
    
    cont_class = PS3JoystickController

    if cfg.CONTROLLER_TYPE == "nimbus":
        cont_class = NimbusController
    
    ctr = cont_class(throttle_scale=cfg.JOYSTICK_MAX_THROTTLE,
                                steering_scale=cfg.JOYSTICK_STEERING_SCALE,
                                auto_record_on_throttle=cfg.AUTO_RECORD_ON_THROTTLE)
    
    ctr.set_deadzone(cfg.JOYSTICK_DEADZONE)

    V.add(ctr, 
          inputs=['null'],
          outputs=['user/angle', 'user/throttle', 'user/mode', 'recording'],
          threaded=True)

    class Steer_to_rad:
        def __init__(self, max_steer_rad):
            self.max_steer_rad = max_steer_rad

        def run(self, steer):
            return steer * self.max_steer_rad

   

    if cfg.DONKEY_GYM:

        from donkeycar.parts.dgym import DonkeyGymEnv 
        gym_env = DonkeyGymEnv(cfg.DONKEY_SIM_PATH, env_name=cfg.DONKEY_GYM_ENV_NAME)
        threaded = True
        inputs = ['angle', 'throttle']
        V.add(gym_env, inputs=inputs, outputs=['cam/image_array', 'rs/pos'], threaded=threaded)


        class PosStream:
            def run(self, pos):
                #y is up, x is right, z is backwards/forwards
                logging.debug("pos %s" % str(pos))
                return pos[0], pos[2]

        V.add(PosStream(), inputs=['rs/pos'], outputs=['pos/x', 'pos/y'])

    elif USE_KINEMATIC_MODEL:

        MAX_STEER_DEG = 15.0
        MAX_STEER_RAD = MAX_STEER_DEG * math.pi / 180.0
        V.add(Steer_to_rad(MAX_STEER_RAD), inputs=['user/angle'], outputs=['steering/radian'])

        FRONT_REAR_WHEELDIST_M = 0.017 #stadard donkey magnet chassis
        carKine = CarKinematics(0.0, 0.0, theta=0.0, front_rear_wheel_dist_m=FRONT_REAR_WHEELDIST_M)
        V.add(carKine, inputs=['enc/delta_dist_m' , 'steering/radian'], outputs=['pos/x', 'pos/y'])

    elif ODOM_PLUS_RS_T265:
        port = 5555

        s = T265Server(path="/home/tkramer/projects/t265wOdom/build/t265odom",
            config="/home/tkramer/projects/t265wOdom/wheel_config.json",
            port=port)
        V.add(s)

        o = OdomRemoteAdapter(port=port)
        V.add(o, inputs=['enc/vel_m_s'], outputs=['pos/x', 'pos/y'], threaded=True)

    else:
        from donkeycar.parts.realsense import RS_T265
        rs = RS_T265(image_output=False)
        V.add(rs, inputs=['enc/vel_m_s'], outputs=['rs/pos', 'rs/vel', 'rs/acc' , 'rs/camera/left/img_array'], threaded=True)

        class PosStream:
            def run(self, pos):
                #y is up, x is right, z is backwards/forwards
                return pos.x, pos.z

        V.add(PosStream(), inputs=['rs/pos'], outputs=['pos/x', 'pos/y'])

    origin_reset = OriginOffset()
    V.add(origin_reset, inputs=['pos/x', 'pos/y'], outputs=['pos/x', 'pos/y'] )

    ctr.set_button_down_trigger(cfg.RESET_ORIGIN_BTN, origin_reset.init_to_last)

    class UserCondition:
        def run(self, mode):
            if mode == 'user':
                return True
            else:
                return False

    V.add(UserCondition(), inputs=['user/mode'], outputs=['run_user'])

    #See if we should even run the pilot module. 
    #This is only needed because the part run_condition only accepts boolean
    class PilotCondition:
        def run(self, mode):
            if mode == 'user':
                return False
            else:
                return True

    V.add(PilotCondition(), inputs=['user/mode'], outputs=['run_pilot'])


    path = Path(min_dist=cfg.PATH_MIN_DIST)
    V.add(path, inputs=['pos/x', 'pos/y'], outputs=['path'], run_condition='run_user')

    if os.path.exists(cfg.PATH_FILENAME):
        path.load(cfg.PATH_FILENAME)
        print("loaded path:", cfg.PATH_FILENAME)

    def save_path():
        path.save(cfg.PATH_FILENAME)
        print("saved path:", cfg.PATH_FILENAME)

    ctr.set_button_down_trigger(cfg.SAVE_PATH_BTN, save_path)

    img = PImage(clear_each_frame=True)
    V.add(img, outputs=['map/image'])


    plot = PathPlot(scale=cfg.PATH_SCALE, offset=cfg.PATH_OFFSET)
    V.add(plot, inputs=['map/image', 'path'], outputs=['map/image'])

    cte = CTE()
    V.add(cte, inputs=['path', 'pos/x', 'pos/y'], outputs=['cte/error'], run_condition='run_pilot')

    pid = PIDController(p=cfg.PID_P, i=cfg.PID_I, d=cfg.PID_D)
    pilot = PID_Pilot(pid, cfg.PID_THROTTLE)
    V.add(pilot, inputs=['cte/error'], outputs=['pilot/angle', 'pilot/throttle'], run_condition="run_pilot")

    def dec_pid_d():
        pid.Kd -= 0.5
        logging.info("pid: d- %f" % pid.Kd)

    def inc_pid_d():
        pid.Kd += 0.5
        logging.info("pid: d+ %f" % pid.Kd)

    ctr.set_button_down_trigger("L2", dec_pid_d)
    ctr.set_button_down_trigger("R2", inc_pid_d)


    loc_plot = PlotCircle(scale=cfg.PATH_SCALE, offset=cfg.PATH_OFFSET)
    V.add(loc_plot, inputs=['map/image', 'pos/x', 'pos/y'], outputs=['map/image'])


    #This web controller will create a web server
    web_ctr = LocalWebController()
    V.add(web_ctr,
          inputs=['map/image'],
          outputs=['web/angle', 'web/throttle', 'web/mode', 'web/recording'],
          threaded=True)
    

    #Choose what inputs should change the car.
    class DriveMode:
        def run(self, mode, 
                    user_angle, user_throttle,
                    pilot_angle, pilot_throttle):
            if mode == 'user':
                #print(user_angle, user_throttle)
                return user_angle, user_throttle
            
            elif mode == 'local_angle':
                return pilot_angle, user_throttle
            
            else: 
                return pilot_angle, pilot_throttle
        
    V.add(DriveMode(), 
          inputs=['user/mode', 'user/angle', 'user/throttle',
                  'pilot/angle', 'pilot/throttle'], 
          outputs=['angle', 'throttle'])
    

    if not cfg.DONKEY_GYM:
        steering_controller = PCA9685(cfg.STEERING_CHANNEL, cfg.PCA9685_I2C_ADDR, busnum=cfg.PCA9685_I2C_BUSNUM)
        steering = PWMSteering(controller=steering_controller,
                                        left_pulse=cfg.STEERING_LEFT_PWM, 
                                        right_pulse=cfg.STEERING_RIGHT_PWM)
        
        throttle_controller = PCA9685(cfg.THROTTLE_CHANNEL, cfg.PCA9685_I2C_ADDR, busnum=cfg.PCA9685_I2C_BUSNUM)
        throttle = PWMThrottle(controller=throttle_controller,
                                        max_pulse=cfg.THROTTLE_FORWARD_PWM,
                                        zero_pulse=cfg.THROTTLE_STOPPED_PWM, 
                                        min_pulse=cfg.THROTTLE_REVERSE_PWM)

        V.add(steering, inputs=['angle'])
        V.add(throttle, inputs=['throttle'])

    V.start(rate_hz=cfg.DRIVE_LOOP_HZ, 
        max_loop_count=cfg.MAX_LOOPS)


if __name__ == '__main__':
    args = docopt(__doc__)
    cfg = dk.load_config()

    log_level = args['--log'] or "INFO"
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % log_level)
    logging.basicConfig(level=numeric_level)

    
    if args['drive']:
        drive(cfg)
