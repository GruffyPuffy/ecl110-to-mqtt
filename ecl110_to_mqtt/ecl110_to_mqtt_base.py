import argparse
import logging
import asyncio
import json
import time
import serial
import threading
from enum import Enum
from concurrent.futures._base import CancelledError

import minimalmodbus

from .__version import __version__
from .__mqtt import MqttClient

LOGGER = logging.getLogger("ecl110-to-mqtt")

def main():
    try:
        loop = asyncio.get_event_loop()
        runner = Ecl1102MQTT()
        loop.run_until_complete(runner.run(loop))
        loop.close()
    except KeyboardInterrupt:
        pass


class Ecl110Mode(Enum):
    manual = 0
    auto = 1
    comfort = 2
    setback = 3
    standby = 4
    unknown = 255


class Ecl1102MQTT:
    def __init__(self) -> None:
        mqtt = None  # type: MqttClient
        loop = None  # type: asyncio.AbstractEventLoop
        bus = None  # type: minimalmodbus.Instrument
        self.lock = threading.Lock()

    def __add_from_config(self, cmdArgs: dict, config: dict, name: str):
        if name in config:
            setattr(cmdArgs, name, config[name])

    def _send_temperature_sensor_config(self, name, nice_name):
            _config = {
                "~": "ecl110/"+str(name),
                "name": nice_name,
                "state_topic": "ecl110/"+str(name)+"/value",
                "unit_of_measurement": "°C",
                "device": "ecl110",
                "uniq_id": "ecl110-"+str(name),
                "device_class": "temperature",
                "availability": "ecl110/state"
            }
            LOGGER.info(_config)
            self.mqtt.publish(
                f"homeassistant/sensor/ecl110/{name}/config",
                json.dumps(_config),
                retain=True,
            )

    async def run(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        parser = argparse.ArgumentParser(prog="ecl110-to-mqtt", description="Commandline Interface to interact with ECL 110 Comfort regulator")
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
        parser.add_argument("--releaseName", type=str, dest="releaseName", help="Name of the current release")
        parser.add_argument("--configFile", type=str, dest="configFile", help="File where config is stored (JSON)")

        args = parser.parse_args()

        if args.configFile is not None:
            with open(args.configFile) as f:
                config = json.load(f)
            self.__add_from_config(args, config, "loglevel")
            self.__add_from_config(args, config, "interval")

            self.__add_from_config(args, config, "mqttbroker")
            self.__add_from_config(args, config, "mqttport")
            self.__add_from_config(args, config, "mqttclientid")
            self.__add_from_config(args, config, "mqttkeepalive")
            self.__add_from_config(args, config, "mqttusername")
            self.__add_from_config(args, config, "mqttpassword")
            self.__add_from_config(args, config, "modbusport")

            self.__add_from_config(args, config, "num_tempsensors")
            self.__add_from_config(args, config, "tempsensors")

        valid_loglevels = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
        if args.loglevel not in valid_loglevels:
            LOGGER.info(f'Invalid log level given: {args.loglevel}, allowed values: {", ".join(valid_loglevels)}')
            return

        logging.basicConfig(level=args.loglevel)

        LOGGER.debug("Starting!")
        LOGGER.debug(f"Version: {__version__}")
        if args.releaseName is not None:
            LOGGER.debug(f"Release name: {args.releaseName}")

        if args.mqttbroker is None:
            LOGGER.error(f"no mqtt broker given")
            return
        if float(args.interval) < 1:
            LOGGER.error(f"interval must be >= 1")
            return

        try:
            self.mqtt = MqttClient(LOGGER, self.loop, args.mqttbroker, args.mqttport, args.mqttclientid, args.mqttkeepalive, args.mqttusername, args.mqttpassword, "")
            await self.mqtt.start()
            self.mqtt.subscribe_to("/ecl110/mode/set", self.__on_set_mode)
            self.mqtt.subscribe_to("/ecl110/register/write", self.__on_write_register)
            self.mqtt.subscribe_to("/ecl110/register/read", self.__on_read_register)


            # Send Home-assistant config
            #self._send_temperature_sensor_config("temp_outdoor", "Outdoor temperature")
            #self.mqtt.publish("ecl110/state", "Online")


            try:
                self.bus = minimalmodbus.Instrument(args.modbusport, 5)
                self.bus.serial.baudrate = 19200         # Baud
                self.bus.serial.bytesize = 8
                self.bus.serial.parity   = serial.PARITY_EVEN
                self.bus.serial.stopbits = 1
                self.bus.serial.timeout  = 0.205          # seconds
                LOGGER.info(f"address={self.bus.address}")
                LOGGER.info(f"mode={self.bus.mode}")
            except:
                LOGGER.error("Failed to find SERIAL PORT...no MODBUS AVAILABLE")
                self.bus = None

            self.valvePosition = 0;

            last_data_json = ""
            last_data_temp_json = ""
            last_cycle = 0.0

            modbus_error_counter = 0

            while True:
                await asyncio.sleep(max(0, args.interval - (time.time() - last_cycle)))
                last_cycle = time.time()

                if not self.mqtt.is_connected:
                    LOGGER.error(f"mqtt not connected")
                    continue

                if modbus_error_counter > 5:
                    LOGGER.error("Restarting modbus driver")
                    raise SystemExit
                    # self.bus = minimalmodbus.Instrument(args.modbusport, 5)
                    # self.bus.serial.baudrate = 19200         # Baud
                    # self.bus.serial.bytesize = 8
                    # self.bus.serial.parity   = serial.PARITY_EVEN
                    # self.bus.serial.stopbits = 1
                    # self.bus.serial.timeout  = 0.05          # seconds
                    # LOGGER.error(f"address={self.bus.address}")
                    # LOGGER.error(f"mode={self.bus.mode}")
                    # modbus_error_counter = 0


                try:
                    #
                    # MODBUS data reading
                    #
                    self.lock.acquire()
                    try:
                        
                        data = {
                            "mode_desired": self.__read_mode(4200).name,
                            "mode": self.__read_mode(4210).name,

                            "temp_outdoor": self.__read_int16(11200) / 10.0,
                            "temp_outdoor_accu": self.__read_int16(11099)  / 10.0,
                            "temp_room": self.__read_int16(11201) / 10.0,
                            "temp_flow": self.__read_int16(11202) / 10.0,
                            "temp_flow_return": self.__read_int16(11203) / 10.0,
                            "temp_room_desired": self.__read_int16(11228) / 10.0,
                            "temp_flow_desired": self.__read_int16(11229) / 10.0,

                            "valveOpen": self.__read_uint16(4100),
                            "valveClose": self.__read_uint16(4101),
                            "valvePosition": self.valvePosition,

                            #"auto_reduct": self.__read_int16(11010),
                            #"boost": self.__read_int16(11011),
                            #"ramp": self.__read_int16(11012),
                            #"optimizer": self.__read_int16(11013),
                            #"integration_time": self.__read_int16(11014),

                            #"based_on_optimizer": self.__read_int16(11019),
                            #"total_stop": self.__read_int16(11020),

                            #"pump_exercise": self.__read_int16(11021),
                            #"valve_exercise": self.__read_int16(11022),

                            "slope": self.__read_int16(11174) / 10.0,
                            "offset": self.__read_int16(11175),

                            "comfort_desired_temp": self.__read_int16(11179),
                            "setback_desired_temp": self.__read_int16(11180),

                            # "monday_start1": self.__read_uint16(1109),
                            # "monday_stop1": self.__read_uint16(1110),
                            # "monday_start2": self.__read_uint16(1111),
                            # "monday_stop2": self.__read_uint16(1112),

                            # "tuesday_start1": self.__read_uint16(1119),
                            # "tuesday_stop1": self.__read_uint16(1120),
                            # "tuesday_start2": self.__read_uint16(1121),
                            # "tuesday_stop2": self.__read_uint16(1122),

                            # "wednesday_start1": self.__read_uint16(1129),
                            # "wednesday_stop1": self.__read_uint16(1130),
                            # "wednesday_start2": self.__read_uint16(1131),
                            # "wednesday_stop2": self.__read_uint16(1132),

                            # "thursday_start1": self.__read_uint16(1139),
                            # "thursday_stop1": self.__read_uint16(1140),
                            # "thursday_start2": self.__read_uint16(1141),
                            # "thursday_stop2": self.__read_uint16(1142),

                            # "friday_start1": self.__read_uint16(1149),
                            # "friday_stop1": self.__read_uint16(1150),
                            # "friday_start2": self.__read_uint16(1151),
                            # "friday_stop2": self.__read_uint16(1152),

                            # "saturday_start1": self.__read_uint16(1159),
                            # "saturday_stop1": self.__read_uint16(1160),
                            # "saturday_start2": self.__read_uint16(1161),
                            # "saturday_stop2": self.__read_uint16(1162),

                            # "sunday_start1": self.__read_uint16(1169),
                            # "sunday_stop1": self.__read_uint16(1170),
                            # "sunday_start2": self.__read_uint16(1171),
                            # "sunday_stop2": self.__read_uint16(1172),

                        }

                        # Update / recalc valvePosition based on readings
                        if data["valveOpen"] > 0:
                            self.valvePosition =  self.valvePosition + 1
                        if data["valveClose"] > 0:
                            self.valvePosition =  self.valvePosition - 1
                        if self.valvePosition < 0:
                            self.valvePosition = 0
                        
                        data["valvePosition"] = self.valvePosition

                        data_json = json.dumps(data)
                        LOGGER.debug("Got info on modbus")
                        LOGGER.debug("=>" + str(data))
                        if data_json != last_data_json:
                            LOGGER.debug("Publishing: ecl110/value")
                            self.mqtt.publish("ecl110/value", data)
                            last_data_json = data_json

                        modbus_error_counter = 0
                        
                    except:
                        modbus_error_counter = modbus_error_counter + 1
                        LOGGER.debug("Could not use serial port for modbus: " + str(modbus_error_counter))
                    finally:
                        self.lock.release()

                except minimalmodbus.NoResponseError:
                    LOGGER.error("no response on ModBus")
                    modbus_error_counter = modbus_error_counter + 1
                except minimalmodbus.InvalidResponseError:
                    LOGGER.error("invalid response on ModBus")
                    modbus_error_counter = modbus_error_counter + 1

                #
                # Extra tempsensor reading (one-wire sensors added to the PI as well...)
                #
                try:
                    data_temp = {}
                    for i in range(0, args.num_tempsensors):
                        sensor = args.tempsensors[i]
                        device_file = "/sys/bus/w1/devices/" + str(sensor['address']) + "/temperature"
                        f = open(device_file, 'r')
                        result = f.read()
                        f.close()
                        temp = float(result)/1000.0
                        LOGGER.debug("Temp-sensor reading-time: " + str(sensor['address']) + " => " + str(temp))
                        data_temp[sensor['name']] = temp
                        
                    if args.num_tempsensors > 0:
                        data_temp_json = json.dumps(data_temp)
                        if data_temp_json != last_data_temp_json:
                            LOGGER.debug("Publishing: heating/value =>" + str(data_temp))
                            self.mqtt.publish("heating/value", data_temp)
                            last_data_temp_json = data_temp_json

                except:
                    LOGGER.error("could not read temp-sensors")


        except KeyboardInterrupt:
            pass  # do nothing, close requested
        except CancelledError:
            pass  # do nothing, close requested
        except Exception as e:
            LOGGER.exception(f"exception in main loop")
        finally:
            LOGGER.info(f"shutdown requested")
            await self.mqtt.stop()

    ## For ECL we assume SIGNED values
    def __on_write_register(self, client, userdata, msg):
        address = getattr(msg.payload, "address", None)
        if address == None:
            LOGGER.error('no value for "address" provided')
            return
        value = getattr(msg.payload, "value", None)
        if value == None:
            LOGGER.error('no value for "value" provided')
            return

        LOGGER.info(f"REQ WRITE: {address} = {value}")
        self.lock.acquire()
        try:
            self.bus.write_register(address, value, functioncode=6, signed=True)
        except minimalmodbus.NoResponseError:
            LOGGER.error("no response on ModBus")
        except minimalmodbus.InvalidResponseError:
            LOGGER.error("invalid response on ModBus")
        finally:
            self.lock.release()

    ## For ECL we assume SIGNED values
    def __on_read_register(self, client, userdata, msg):
        address = getattr(msg.payload, "address", None)
        if address == None:
            LOGGER.error('no value for "address" provided')
            return

        self.lock.acquire()
        try:
            value = self.bus.read_register(address, signed=True)
        except minimalmodbus.NoResponseError:
            LOGGER.error("no response on ModBus")
        except minimalmodbus.InvalidResponseError:
            LOGGER.error("invalid response on ModBus")
        finally:
            self.lock.release()

        LOGGER.info(f"REQ READ: {address} = {value}")
        self.mqtt.publish(f"ecl110/register/{address}", value)

    def __on_set_mode(self, client, userdata, msg):
        requested_mode = getattr(msg.payload, "mode", None)
        if requested_mode == None:
            LOGGER.error('no value for "requested_mode" provided')
            return

        elif requested_mode == Ecl110Mode.auto:
            write_data = {4200: 1}
        elif requested_mode == Ecl110Mode.comfort:
            write_data = {4200: 2}
        elif requested_mode == Ecl110Mode.setback:
            write_data = {4200: 3}
        elif requested_mode == Ecl110Mode.standby:
            write_data = {4200: 4}
        else:
            LOGGER.error(f"Not implemented for {requested_mode}")
            return

        self.lock.acquire()

        try:
            for key, value in write_data.items():
                self.bus.write_register(key, value, functioncode=6)
        finally:
            self.lock.release()

    def __read_mode(self, address) -> Ecl110Mode:
        mode_val = self.__read_uint16(address)
        if mode_val == 0:
            return Ecl110Mode.manual
        elif mode_val == 1:
            return Ecl110Mode.auto
        elif mode_val == 2:
            return Ecl110Mode.comfort
        elif mode_val == 3:
            return Ecl110Mode.setback
        elif mode_val == 4:
            return Ecl110Mode.standby
        else:
            return Ecl110Mode.unknown

        return Ecl110Mode.auto

    def __read_uint8(self, register_address: int):
        return self.bus.read_register(register_address)

    def __read_uint16(self, register_address: int):
        return self.bus.read_register(register_address)

    def __read_int16(self, register_address: int):
        return self.bus.read_register(register_address, signed=True)

    def __read_uint32(self, register_address: int):
        return self.bus.read_long(register_address)

    def __read_float(self, register_address: int):
        return self.bus.read_float(register_address, byteorder=minimalmodbus.BYTEORDER_LITTLE_SWAP)
