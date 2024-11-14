#!/usr/bin/python

from __future__ import absolute_import
from __future__ import print_function

import re
import sys
import time
import threading
import webbrowser

from datetime import datetime

try:
    from Queue import *
except ImportError:
    from queue import *

from .CNC import CNC
from .USBStream import USBStream
from .WIFIStream import WIFIStream
from .XMODEM import EOT, CAN

STREAM_POLL = 0.2 # s
DIAGNOSE_POLL = 0.5  # s
RX_BUFFER_SIZE = 128

GPAT = re.compile(r"[A-Za-z]\s*[-+]?\d+.*")
FEEDPAT = re.compile(r"^(.*)[fF](\d+\.?\d+)(.*)$")

STATUSPAT = re.compile(r"^<(\w*?),MPos:([+\-]?\d*\.\d*),([+\-]?\d*\.\d*),([+\-]?\d*\.\d*),WPos:([+\-]?\d*\.\d*),([+\-]?\d*\.\d*),([+\-]?\d*\.\d*),?(.*)>$")
POSPAT	  = re.compile(r"^\[(...):([+\-]?\d*\.\d*),([+\-]?\d*\.\d*),([+\-]?\d*\.\d*):?(\d*)\]$")
TLOPAT	  = re.compile(r"^\[(...):([+\-]?\d*\.\d*)\]$")
DOLLARPAT = re.compile(r"^\[G\d* .*\]$")
SPLITPAT  = re.compile(r"[:,]")
VARPAT    = re.compile(r"^\$(\d+)=(\d*\.?\d*) *\(?.*")


WIKI = "https://github.com/vlachoudis/bCNC/wiki"

CONNECTED = "Wait"
NOT_CONNECTED = "N/A"

STATECOLORDEF = (155/255, 155/255, 155/255, 1)  # Default color for unknown types or not connected
STATECOLOR = {
    "Idle":         (52/255, 152/255, 219/255, 1),
    "Run":          (34/255, 153/255, 84/255, 1),
    "Tool":        (34/255, 153/255, 84/255, 1),
    "Alarm":        (231/255, 76/255, 60/255, 1),
    "Home":         (247/255, 220/255, 111/255, 1),
    "Hold":         (34/255, 153/255, 84/255, 1),
    'Wait':         (247/255, 220/255, 111/255, 1),
    'Disable':      (100/255, 100/255, 100/255, 1),
    'Sleep':        (220/255, 220/255, 220/255, 1),
    'Pause':        (52/255, 152/255, 219/255, 1),
    NOT_CONNECTED:  (155/255, 155/255, 155/255, 1)
}

LOAD_DIR   = 1
LOAD_RM    = 2
LOAD_MV    = 3
LOAD_MKDIR = 4
LOAD_WIFI  = 7
LOAD_CONN_WIFI = 8

SEND_FILE = 1

CONN_USB = 0
CONN_WIFI = 1

# ==============================================================================
# Controller class
# ==============================================================================
class Controller:
    MSG_NORMAL = 0
    MSG_ERROR = 1
    MSG_INTERIOR = 2

    stop = threading.Event()
    usb_stream = None
    wifi_stream = None
    stream = None
    modem = None
    connection_type = CONN_WIFI

    def __init__(self, cnc, callback):
        self.usb_stream = USBStream()
        self.wifi_stream = WIFIStream()

        # Global variables
        self.history = []
        self._historyPos = None

        # CNC.loadConfig(Utils.config)
        self.cnc = cnc

        self.execCallback = callback

        self.log = Queue()  # Log queue returned from GRBL
        self.queue = Queue()  # Command queue to be send to GRBL
        self.load_buffer = Queue()
        self.load_buffer_size = 0
        self.total_buffer_size = 0

        self.loadNUM = 0
        self.loadEOF = False
        self.loadERR = False
        self.loadCANCEL = False
        self.loadCANCELSENT = False

        self.sendNUM = 0
        self.sendEOF = False
        self.sendCANCEL = False

        self.thread = None

        self.posUpdate = False  # Update position
        self.diagnoseUpdate = False
        self._probeUpdate = False  # Update probe
        self._gUpdate = False  # Update $G
        self._update = None  # Generic update

        self.cleanAfter = False
        self._runLines = 0
        self._quit = 0  # Quit counter to exit program
        self._stop = False  # Raise to stop current run
        self._pause = False  # machine is on Hold
        self._alarm = True  # Display alarm message if true
        self._msg = None
        self._sumcline = 0
        self._lastFeed = 0
        self._newFeed = 0

        self._onStart = ""
        self._onStop = ""

        self.paused = False
        self.pausing = False

        self.diagnosing = False

    # ----------------------------------------------------------------------
    def quit(self, event=None):
        pass

    # ----------------------------------------------------------------------
    def loadConfig(self):
        pass

    # ----------------------------------------------------------------------
    def saveConfig(self):
        pass

    # ----------------------------------------------------------------------
    # Execute a line as gcode if pattern matches
    # @return True on success
    #	  False otherwise
    # ----------------------------------------------------------------------
    def executeGcode(self, line):
        if isinstance(line, tuple) or \
                line[0] in ("$", "!", "~", "?", "(", "@") or GPAT.match(line):
            self.sendGCode(line)
            return True
        return False

    # ----------------------------------------------------------------------
    # Execute a single command
    # ----------------------------------------------------------------------
    def executeCommand(self, line):
        #if self.sio_status != False or self.sio_diagnose != False:      #wait for the ? or * command
        #    time.sleep(0.5)
        if self.stream and line:
            try:
                if line[-1] != '\n':
                    line += "\n"
                self.stream.send(line.encode())
                if self.execCallback:
                    # 检查文件名是否以 ".lz" 结尾
                    if line.endswith(".lz\n"):
                        # 删除 ".lz" 后缀
                        new_line = line[:-4] + "\n"
                    else:
                        # 如果没有 ".lz" 后缀，直接赋值
                        new_line = line
                    self.execCallback(new_line)
            except:
                self.log.put((Controller.MSG_ERROR, str(sys.exc_info()[1])))

    # ----------------------------------------------------------------------
    def autoCommand(self, margin=False, zprobe=False, zprobe_abs=False, leveling=False, goto_origin=False, z_probe_offset_x=0, z_probe_offset_y=0, i=3, j=3, h=5, buffer=False):
        if not (margin or zprobe or leveling or goto_origin):
            return
        if abs(CNC.vars['xmin']) > CNC.vars['worksize_x'] or abs(CNC.vars['ymin']) > CNC.vars['worksize_y']:
            return
        cmd = "M495 X%gY%g" % (CNC.vars['xmin'], CNC.vars['ymin'])
        if margin:
            cmd = cmd + "C%gD%g" % (CNC.vars['xmax'], CNC.vars['ymax'])
        if zprobe:
            if zprobe_abs:
                cmd = cmd + "O0"
            else:
                cmd = cmd + "O%gF%g" % (z_probe_offset_x, z_probe_offset_y)
        if leveling:
            cmd = cmd + "A%gB%gI%dJ%dH%d" % (CNC.vars['xmax'] - CNC.vars['xmin'], CNC.vars['ymax'] - CNC.vars['ymin'], i, j, h)
        if goto_origin:
            cmd = cmd + "P1"
        cmd = cmd + "\n"
        if buffer:
            cmd = "buffer " + cmd
        self.executeCommand(cmd)

    def xyzProbe(self, height=9.0, diameter=3.175, buffer=False):
        cmd = "M495.3 H%g D%g" % (height, diameter)
        if buffer:
            cmd = "buffer " + cmd
        self.executeCommand(cmd)

    def pairWP(self):
        self.executeCommand("M471")

    def syncTime(self, *args):
        self.executeCommand("time " + str(int(time.time()) - time.timezone))

    def queryTime(self, *args):
        self.executeCommand("time")

    def queryVersion(self, *args):
        self.executeCommand("version")

    def queryModel(self, *args):
        self.executeCommand("model")

    def queryFtype(self, *args):
        self.executeCommand("ftype")


    # # ----------------------------------------------------------------------
    # def zProbeCommand(self, c=0, d=0, buffer=False):
    #     cmd = "M494 X%gY%gC%gD%g\n" % (CNC.vars['xmin'], CNC.vars['ymin'], c, d)
    #     if buffer:
    #         cmd = "buffer " + cmd
    #     self.executeCommand(cmd)

    # def autoLevelCommand(self, i=3, j=3, buffer=False):
    #     cmd = "M495 X%gY%gA%gB%gI%dJ%d\n" % (CNC.vars['xmin'], CNC.vars['ymin'], CNC.vars['xmax'] - CNC.vars['xmin'], CNC.vars['ymax'] - CNC.vars['ymin'], i, j)
    #     if buffer:
    #         cmd = "buffer " + cmd
    #     self.executeCommand(cmd)

    # def probeLevelCommand(self, i=3, j=3, buffer=False):
    #     cmd = "M496 X%gY%gA%gB%gI%dJ%d\n" % (CNC.vars['xmin'], CNC.vars['ymin'], CNC.vars['xmax'] - CNC.vars['xmin'], CNC.vars['ymax'] - CNC.vars['ymin'], i, j)
    #     if buffer:
    #         cmd = "buffer " + cmd
    #     self.executeCommand(cmd)

    def gotoPosition(self, position, buffer=False):
        if position is None:
            return
        cmd = ""
        if position == "Clearance":
            cmd = "M496.1\n"
        elif position == "Work Origin":
            cmd = "M496.2\n"
        elif position == "Anchor1":
            cmd = "M496.3\n"
        elif position == "Anchor2":
            cmd = "M496.4\n"
        elif position == "Path Origin":
            if abs(CNC.vars['xmin']) <= CNC.vars['worksize_x'] and abs(CNC.vars['ymin']) <= CNC.vars['worksize_y']:
                cmd = "M496.5 X%gY%g\n" % (CNC.vars['xmin'], CNC.vars['ymin'])
        if buffer:
            cmd = "buffer " + cmd
        self.executeCommand(cmd)

    def reset(self):
        self.executeCommand("reset\n")

    def change(self):
        self.executeCommand("M490.2\n")

    def setFeedScale(self, scale):
        self.executeCommand("M220 S%d\n" % (scale))

    def setLaserScale(self, scale):
        self.executeCommand("M325 S%d\n" % (scale))

    def setSpindleScale(self, scale):
        self.executeCommand("M223 S%d\n" % (scale))

    def clearAutoLeveling(self):
        self.executeCommand("M370\n")

    def setSpindleSwitch(self, switch, rpm):
        if switch:
            self.executeCommand("M3 S%d\n" % (rpm))
        else:
            self.executeCommand("M5\n")

    def setVacuumSwitch(self, switch, power):
        if switch:
            self.executeCommand("M801 S%d\n" % (power))
        else:
            self.executeCommand("M802\n")

    def setSpindlefanSwitch(self, switch, power):
        if switch:
            self.executeCommand("M811 S%d\n" % (power))
        else:
            self.executeCommand("M812\n")

    def setLaserSwitch(self, switch, power):
        if switch:
            self.executeCommand("M3 S%g\n" % (power * 1.0 / 100))
        else:
            self.executeCommand("M5\n")

    def setLightSwitch(self, switch):
        if switch:
            self.executeCommand("M821\n")
        else:
            self.executeCommand("M822\n")

    def setToolSensorSwitch(self, switch):
        if switch:
            self.executeCommand("M831\n")
        else:
            self.executeCommand("M832\n")

    def setAirSwitch(self, switch):
        if switch:
            self.executeCommand("M7\n")
        else:
            self.executeCommand("M9\n")

    def setPWChargeSwitch(self, switch):
        if switch:
            self.executeCommand("M841\n")
        else:
            self.executeCommand("M842\n")

    def setVacuumMode(self, mode):
        if mode:
            self.executeCommand("M331\n")
        else:
            self.executeCommand("M332\n")

    def setLaserMode(self, mode):
        if mode:
            self.executeCommand("M321\n")
        else:
            self.executeCommand("M322\n")

    def setLaserTest(self, test):
        if test:
            self.executeCommand("M323\n")
        else:
            self.executeCommand("M324\n")

    def setConfigValue(self, key, value):
        if key and value:
            self.executeCommand("config-set sd %s %s\n" % (key, value))


    def dropToolCommand(self):
        self.executeCommand("M6T-1\n")

    def calibrateToolCommand(self):
        self.executeCommand("M491\n")

    def changeToolCommand(self, tool):
        if tool == 'e':
            self.executeCommand("M6T0\n")
        elif tool == 'r':
            self.executeCommand("M6T8888\n")
        else:
            self.executeCommand("M6T%s\n" % tool)

    def setToolCommand(self, tool):
        if tool == 'e':
            self.executeCommand("M493.2T0\n")
        elif tool == 'r':
            self.executeCommand("M493.2T8888\n")
        elif tool == 'y':
            self.executeCommand("M493.2T-1\n")
        else:
            self.executeCommand("M493.2T%s\n" % tool)

    def bufferChangeToolCommand(self, tool):
        self.executeCommand("buffer M6T%s\n" % tool)

    # ------------------------------------------------------------------------------
    # escape special characters
    # ------------------------------------------------------------------------------
    def escape(self, value):
        return value.replace('?', '\x02').replace('&', '\x03').replace('!', '\x04').replace('~', '\x05')

    def lsCommand(self, ls_dir):
        ls_command = "ls -e -s %s\n" % ls_dir.replace(' ', '\x01')
        if '\\' in ls_dir:
            ls_command = "ls -e -s %s\n" % '/'.join(ls_dir.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(ls_command))

    def catCommand(self, filename):
        cat_command = "cat %s -e\n" % filename.replace(' ', '\x01')
        if '\\' in filename:
            cat_command = "cat %s -e\n" % '/'.join(filename.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(cat_command))

    def rmCommand(self, filename):
        rm_command = "rm %s -e\n" % filename.replace(' ', '\x01')
        if '\\' in filename:
            rm_command = "rm %s -e\n" % '/'.join(filename.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(rm_command))

    def mvCommand(self, file, newfile):
        mv_command = "mv %s %s -e\n" % (file.replace(' ', '\x01'), newfile.replace(' ', '\x01'))
        if '\\' in file or '\\' in newfile:
            mv_command = "mv %s %s -e\n" % ('/'.join(file.split('\\')).replace(' ', '\x01'), '/'.join(newfile.split('\\')).replace(' ', '\x01'))
        self.executeCommand(self.escape(mv_command))

    def mkdirCommand(self, dirname):
        mkdir_command = "mkdir %s -e\n" % dirname.replace(' ', '\x01')
        if '\\' in dirname:
            mkdir_command = "mkdir %s -e\n" % '/'.join(dirname.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(mkdir_command))

    def md5Command(self, filename):
        md5_command = "md5sum %s -e\n" % filename.replace(' ', '\x01')
        if '\\' in filename:
            md5_command = "md5sum %s -e\n" % '/'.join(filename.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(md5_command))

    def loadWiFiCommand(self):
        self.executeCommand("wlan -e\n")

    def disconnectWiFiCommand(self):
        self.executeCommand("wlan -d disconnect\n")

    def connectWiFiCommand(self, ssid, password):
        wifi_command = "wlan %s %s -e\n" % (ssid.replace(' ', '\x01'), password.replace(' ', '\x01'))
        self.executeCommand(self.escape(wifi_command))

    def loadConfigCommand(self):
        self.executeCommand("config-get-all -e\n")

    def restoreConfigCommand(self):
        self.executeCommand("config-restore\n")

    def defaultConfigCommand(self):
        self.executeCommand("config-default\n")

    def uploadCommand(self, filename):
        upload_command = "upload %s\n" % filename.replace(' ', '\x01')
        if '\\' in filename:
            upload_command = "upload %s\n" % '/'.join(filename.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(upload_command))

    def downloadCommand(self, filename):
        download_command = "download %s\n" % filename.replace(' ', '\x01')
        if '\\' in filename:
            download_command = "download %s\n" % '/'.join(filename.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(download_command))

    def suspendCommand(self):
        self.executeCommand("suspend\n")

    def resumeCommand(self):
        self.executeCommand("resume\n")

    def playCommand(self, filename):
        play_command = "play %s\n" % filename.replace(' ', '\x01')
        if '\\' in filename:
            play_command = "play %s\n" % '/'.join(filename.split('\\')).replace(' ', '\x01')
        self.executeCommand(self.escape(play_command))

    def abortCommand(self):
        self.executeCommand("abort\n")

    def feedholdCommand(self):
        if self.stream:
            self.stream.send('!'.encode())

    def toggleFeedholdCommand(self, holding):
        if self.stream:
            if holding:
                self.stream.send('~'.encode())
            else:
                self.stream.send('!'.encode())

    def cyclestartCommand(self):
        if self.stream:
            self.stream.send('~'.encode())

    def estopCommand(self):
        if self.stream:
            self.stream.send(b'\x18')

    # ----------------------------------------------------------------------
    def hardResetPre(self):
        self.stream.send(b"reset\n")

    def hardResetAfter(self):
        time.sleep(6)

    def parseBracketAngle(self, line,):
        # <Idle|MPos:68.9980,-49.9240,40.0000,12.3456|WPos:68.9980,-49.9240,40.0000,5.3|F:12345.12,100.0|S:1.2,100.0|T:1|L:0>
        # F: Feed, overide | S: Spindle RPM
        ln = line[1:-1]  # strip off < .. >

        # split fields
        l = ln.split('|')

        # strip off status
        CNC.vars["state"] = l[0]

        # strip of rest into a dict of name: [values,...,]
        d = {a: [float(y) for y in b.split(',')] for a, b in [x.split(':') for x in l[1:]]}
        CNC.vars["mx"] = float(d['MPos'][0])
        CNC.vars["my"] = float(d['MPos'][1])
        CNC.vars["mz"] = float(d['MPos'][2])
        if len(d['MPos']) > 3:
            CNC.vars["ma"] = float(d['MPos'][3])
        else:
            CNC.vars["ma"] = 0.0
        CNC.vars["wx"] = float(d['WPos'][0])
        CNC.vars["wy"] = float(d['WPos'][1])
        CNC.vars["wz"] = float(d['WPos'][2])
        CNC.vars["wa"] = 0.0
        CNC.vars["wcox"] = round(CNC.vars["mx"] - CNC.vars["wx"], 3)
        CNC.vars["wcoy"] = round(CNC.vars["my"] - CNC.vars["wy"], 3)
        CNC.vars["wcoz"] = round(CNC.vars["mz"] - CNC.vars["wz"], 3)
        CNC.vars["wcoa"] = round(CNC.vars["mz"] - CNC.vars["wz"], 3)
        if 'F' in d:
           CNC.vars["curfeed"] = float(d['F'][0])
           CNC.vars["tarfeed"] = float(d['F'][1])
           CNC.vars["OvFeed"]  = int(d['F'][2])
        if 'S' in d:
            CNC.vars["curspindle"]  = float(d['S'][0])
            CNC.vars["tarspindle"]  = float(d['S'][1])
            CNC.vars["OvSpindle"]   = float(d['S'][2])
            if len(d['S']) > 3:
                CNC.vars["vacuummode"] = int(d['S'][3])
            if len(d['S']) > 4:
                CNC.vars["spindletemp"] = float(d['S'][4])
        if 'T' in d:
            CNC.vars["tool"] = int(d['T'][0])
            CNC.vars["tlo"] = float(d['T'][1])
            if len(d['T']) > 2:
                CNC.vars["target_tool"] = int(d['T'][2])
            else:
                CNC.vars["target_tool"] = -1
        else:
            CNC.vars["tool"] = -1
            CNC.vars["tlo"] = 0.0
            CNC.vars["target_tool"] = -1
        if 'W' in d:
            CNC.vars["wpvoltage"] = float(d['W'][0])
        if 'L' in d:
            CNC.vars["lasermode"]  = int(d['L'][0])
            CNC.vars["laserstate"] = int(d['L'][1])
            CNC.vars["lasertesting"] = int(d['L'][2])
            CNC.vars["laserpower"] = float(d['L'][3])
            CNC.vars["laserscale"] = float(d['L'][4])
        if 'P' in d:
            CNC.vars["playedlines"] = int(d['P'][0])
            CNC.vars["playedpercent"] = int(d['P'][1])
            CNC.vars["playedseconds"] = int(d['P'][2])
        else:
            # not playing file
            CNC.vars["playedlines"] = -1

        if 'A' in d:
            CNC.vars["atc_state"] = int(d['A'][0])
        else:
            CNC.vars["atc_state"] = 0

        if 'O' in d:
            CNC.vars["max_delta"] = float(d['O'][0])
        else:
            CNC.vars["max_delta"] = 0.0

        if 'H' in d:
            CNC.vars["halt_reason"] = int(d['H'][0])

        self.posUpdate = True

    def parseBigParentheses(self, line):
        # {S:0,5000|L:0,0|F:1,0|V:0,1|G:0|T:0|E:0,0,0,0,0,0|P:0,0|A:1,0}
        ln = line[1:-1]  # strip off < .. >

        # split fields
        l = ln.split('|')

        # strip of rest into a dict of name: [values,...,]
        d = {a: [int(y) for y in b.split(',')] for a, b in [x.split(':') for x in l]}
        if 'S' in d:
            CNC.vars["sw_spindle"] = int(d['S'][0])
            CNC.vars["sl_spindle"] = int(d['S'][1])
        if 'L' in d:
            CNC.vars["sw_laser"]  = int(d['L'][0])
            CNC.vars["sl_laser"]  = int(d['L'][1])
        if 'F' in d:
            CNC.vars["sw_spindlefan"] = int(d['F'][0])
            CNC.vars["sl_spindlefan"] = int(d['F'][1])
        if 'V' in d:
            CNC.vars["sw_vacuum"] = int(d['V'][0])
            CNC.vars["sl_vacuum"] = int(d['V'][1])
        if 'G' in d:
            CNC.vars["sw_light"] = int(d['G'][0])
        if 'T' in d:
            CNC.vars["sw_tool_sensor_pwr"] = int(d['T'][0])
        if 'R' in d:
            CNC.vars["sw_air"] = int(d['R'][0])
        if 'C' in d:
            CNC.vars["sw_wp_charge_pwr"] = int(d['C'][0])

        if 'E' in d:
            CNC.vars["st_x_min"] = int(d['E'][0])
            CNC.vars["st_x_max"] = int(d['E'][1])
            CNC.vars["st_y_min"] = int(d['E'][2])
            CNC.vars["st_y_max"] = int(d['E'][3])
            CNC.vars["st_z_max"] = int(d['E'][4])
            CNC.vars["st_cover"] = int(d['E'][5])
        if 'P' in d:
            CNC.vars["st_probe"] = int(d['P'][0])
            CNC.vars["st_calibrate"] = int(d['P'][1])
        if 'A' in d:
            CNC.vars["st_atc_home"] = int(d['A'][0])
            CNC.vars["st_tool_sensor"] = int(d['A'][1])
        if 'I' in d:
            CNC.vars["st_e_stop"] = int(d['I'][0])


        self.diagnoseUpdate = True

    # ----------------------------------------------------------------------
    def help(self, event=None):
        webbrowser.open(WIKI, new=2)

    # ----------------------------------------------------------------------
    # Open serial port or wifi connect
    # ----------------------------------------------------------------------
    def open(self, conn_type, address):
        # init connection
        if conn_type == CONN_USB:
            self.stream = self.usb_stream
        else:
            self.stream = self.wifi_stream

        if self.stream.open(address):
            CNC.vars["state"] = CONNECTED
            CNC.vars["color"] = STATECOLOR[CNC.vars["state"]]
            self.log.put((self.MSG_NORMAL, 'Connected to machine!'))
            #self.stream.send(b"\n")
            self._gcount = 0
            self._alarm = True
            try:
                self.clearRun()
            except:
                self.log.put((self.MSG_ERROR, 'Controller clear thread error!'))
            self.thread = threading.Thread(target=self.streamIO)
            self.thread.start()
            return True
        else:
            self.log.put((self.MSG_ERROR, 'Connection Failed!'))

    # ----------------------------------------------------------------------
    # Close connection port
    # ----------------------------------------------------------------------
    def close(self):
        if self.stream is None: return
        try:
            self.stopRun()
        except:
            self.log.put((self.MSG_ERROR, 'Controller stop thread error!'))
        self._runLines = 0
        time.sleep(0.5)
        self.thread = None
        try:
            self.stream.close()
        except:
            self.log.put((self.MSG_ERROR, 'Controller close stream error!'))
        self.stream = None
        CNC.vars["state"] = NOT_CONNECTED
        CNC.vars["color"] = STATECOLOR[CNC.vars["state"]]

    # ----------------------------------------------------------------------
    def stopRun(self):
        self.stop.set()

    # ----------------------------------------------------------------------
    def clearRun(self):
        self.stop.clear()

    # ----------------------------------------------------------------------
    # Send to controller a gcode or command
    # WARNING: it has to be a single line!
    # ----------------------------------------------------------------------
    def sendGCode(self, cmd):
        self.executeCommand(cmd)

    # ----------------------------------------------------------------------
    def sendHex(self, hexcode):
        if self.stream is None: return
        self.stream.send(chr(int(hexcode, 16)))
        self.stream.flush()

    def viewStatusReport(self, sio_status):
        if self.loadNUM == 0 and self.sendNUM == 0:
            self.stream.send(b"?")
            self.sio_status = sio_status

    def viewDiagnoseReport(self, sio_diagnose):
        if self.loadNUM == 0 and self.sendNUM == 0:
            self.stream.send(b"diagnose\n")
            self.sio_diagnose = sio_diagnose

    # ----------------------------------------------------------------------
    def hardReset(self):
        self.busy()
        if self.stream is not None:
            self.hardResetPre()
            self.openClose()
            self.hardResetAfter()
        self.openClose()
        self.stopProbe()
        self._alarm = False
        CNC.vars["_OvChanged"] = True  # force a feed change if any
        self.notBusy()

    def softReset(self, clearAlarm=True):
        if self.stream:
            self.stream.send(b"\030")
        self.stopProbe()
        if clearAlarm: self._alarm = False
        CNC.vars["_OvChanged"] = True  # force a feed change if any

    def unlock(self, clearAlarm=True):
        if clearAlarm: self._alarm = False
        self.sendGCode("$X")

    def home(self, event=None):
        self.sendGCode("$H")

    def viewSettings(self):
        pass

    def viewParameters(self):
        self.sendGCode("$#")

    def viewState(self):
        self.sendGCode("$G")

    def viewBuild(self):
        self.stream.send(b"version\n")
        self.sendGCode("$I")

    def viewStartup(self):
        pass

    def checkGcode(self):
        pass

    def grblHelp(self):
        self.stream.send(b"help\n")

    def grblRestoreSettings(self):
        pass

    def grblRestoreWCS(self):
        pass

    def grblRestoreAll(self):
        pass

    # ----------------------------------------------------------------------
    def jog(self, _dir):
        self.executeCommand("G91G0{}".format(_dir))

    # ----------------------------------------------------------------------
    def goto(self, x=None, y=None, z=None):
        cmd = "G90G0"
        if x is not None: cmd += "X%g" % (x)
        if y is not None: cmd += "Y%g" % (y)
        if z is not None: cmd += "Z%g" % (z)
        self.sendGCode("%s" % (cmd))

    def wcsSetA(self, a = None):
        cmd = "G92.4"
        if a is not None and abs(a) < 3600000.0: cmd += "A" + str(round(a, 5))

        self.sendGCode(cmd)

    def shrinkA(self):
        self.sendGCode("G92.4 A0 S0")

    def RapMoveA(self, a = None):
        cmd = "G90G0"
        cmd += "X"  + str(round(a, 5))
        cmd = "G92.4"
        cmd += " A " + str(round(a, 5)) + " R0"
        if a is not None and abs(a) < 3600000.0: self.sendGCode(cmd)

    def wcsSet(self, x = None, y = None, z = None, a = None):
        cmd = "G10L20P0"

        pos = ""
        if x is not None and abs(x) < 10000.0: pos += "X" + str(round(x, 4))
        if y is not None and abs(y) < 10000.0: pos += "Y" + str(round(y, 4))
        if z is not None and abs(z) < 10000.0: pos += "Z" + str(round(z, 4))
        if a is not None and abs(a) < 3600000.0: pos += "A" + str(round(a, 4))
        cmd += pos

        self.sendGCode(cmd)

    def wcsSetM(self, x = None, y = None, z = None, a = None):
        # p = WCS.index(CNC.vars["WCS"])
        cmd = "G10L2P0"

        pos = ""
        if x is not None and abs(x) < 10000.0: pos += "X" + str(round(x, 4))
        if y is not None and abs(y) < 10000.0: pos += "Y" + str(round(y, 4))
        if z is not None and abs(z) < 10000.0: pos += "Z" + str(round(z, 4))
        if a is not None and abs(a) < 3600000.0: pos += "A" + str(round(a, 4))
        cmd += pos

        self.sendGCode(cmd)

    def feedHold(self, event=None):
        if event is not None and not self.acceptKey(True): return
        if self.stream is None: return
        self.stream.send(b"!")
        self.stream.flush()
        self._pause = True

    def resume(self, event=None):
        if event is not None and not self.acceptKey(True): return
        if self.stream is None: return
        self.stream.send(b"~")
        self.stream.flush()
        self._alarm = False
        self._pause = False

    def pause(self, event=None):
        if self.stream is None: return
        if self._pause:
            self.resume()
        else:
            self.feedHold()

    # ----------------------------------------------------------------------
    def parseLine(self, line):
        if not line:
            return True
        elif line[0] == "<":
            self.parseBracketAngle(line)
            self.sio_status = False
        elif line[0] == "{":
            if not self.sio_diagnose:
                self.log.put((self.MSG_NORMAL, line))
            else:
                self.parseBigParentheses(line)
                self.sio_diagnose = False
        elif line[0] == "#":
            self.log.put((self.MSG_INTERIOR, line))
        elif "error" in line.lower() or "alarm" in line.lower():
            self.log.put((self.MSG_ERROR, line))
        else:
            self.log.put((self.MSG_NORMAL, line))

    # ----------------------------------------------------------------------
    def g28Command(self):
        self.sendGCode("G28.1")  # FIXME: ???

    def g30Command(self):
        self.sendGCode("G30.1")  # FIXME: ???

    # ----------------------------------------------------------------------
    def emptyQueue(self):
        while self.queue.qsize() > 0:
            try:
                self.queue.get_nowait()
            except Empty:
                break

    def pauseStream(self, wait_s):
        self.pausing = True
        time.sleep(wait_s)
        self.paused = True
        self.pausing = False

    def resumeStream(self):
        self.paused = False
        self.pausing = False

    # ----------------------------------------------------------------------
    # thread performing I/O on serial line
    # ----------------------------------------------------------------------
    def streamIO(self):
        self.sio_status = False
        self.sio_diagnose = False
        dynamic_delay = 0.1
        tr = td = time.time()
        line = b''
        last_error = ''

        while not self.stop.is_set():
            if not self.stream or self.paused:
                time.sleep(1)
                continue
            t = time.time()
            # refresh machine position?
            running = self.sendNUM > 0 or self.loadNUM > 0 or self.pausing
            try:
                if not running:
                    if t - tr > STREAM_POLL:
                        self.viewStatusReport(True)
                        tr = t
                    if self.diagnosing and t - td > DIAGNOSE_POLL:
                        self.viewDiagnoseReport(True)
                        td = t
                else:
                    tr = t
                    td = t

                if self.stream.waiting_for_recv():
                    received = [bytes([b]) for b in self.stream.recv()]
                    for c in received:
                        if c == EOT or c == CAN:
                            # Ctrl + Z means transmission complete, Ctrl + D means transmission cancel or error
                            if len(line) > 0:
                                self.load_buffer.put(line.decode(errors='ignore'))
                                if self.loadNUM > 0:
                                    self.load_buffer_size += len(line)
                            line = b''
                            if c == EOT:
                                self.loadEOF = True
                            else:
                                self.loadERR = True
                        else:
                            if c == b'\n':
                                # (line.decode(errors='ignore'))
                                if self.loadNUM == 0 or '|MPos' in line.decode(errors='ignore'):
                                    self.parseLine(line.decode(errors='ignore'))
                                else:
                                    # 将字节串解码为字符串
                                    decoded_line = line.decode(errors='ignore')
                                    # 使用正则表达式去除以"<"开头，以">"结尾的部分
                                    cleaned_line = re.sub(r'<.*?>', '', decoded_line)
                                    # 去除多余的空格（如果需要）
                                    cleaned_line = cleaned_line.strip()
                                    if len(cleaned_line) != 0:
                                        self.load_buffer.put(cleaned_line)
                                        self.load_buffer_size += len(cleaned_line) + 1
                                line = b''
                            else:
                                line += c
                    dynamic_delay = 0
                else:
                    if self.sendNUM == 0 and self.loadNUM == 0:
                        dynamic_delay = (0.1 if dynamic_delay >= 0.09 else dynamic_delay + 0.01)
                    else:
                        dynamic_delay = 0

            except:
                line = b''
                if last_error != str(sys.exc_info()[1]) :
                    self.log.put((Controller.MSG_ERROR, str(sys.exc_info()[1])))
                    last_error = str(sys.exc_info()[1])

            if dynamic_delay > 0:
                time.sleep(dynamic_delay)