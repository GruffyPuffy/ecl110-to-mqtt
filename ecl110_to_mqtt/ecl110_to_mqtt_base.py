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

DEFAULT_ARGS = {
    "loglevel": "WARNING",
    "interval": 5.0,
    "mqttport": 1883,
    "mqttkeepalive": 60,
   
    "modbusport": "/dev/ttyS0",
}


def main():
    try:
        loop = asyncio.get_event_loop()
        runner = Ecl1102MQTT()
        loop.run_until_complete(runner.run(loop))
        loop.close()
    except KeyboardInterrupt:
        pass


class Ecl110Mode(Enum):
    auto = 0
    comfort = 1
    setback = 2
    standby = 3
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
            print(_config)
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
        parser.add_argument("--loglevel", type=str, dest="loglevel", help='Minimum log level, DEBUG/INFO/WARNING/ERROR/CRITICAL"', default=DEFAULT_ARGS["loglevel"])
        parser.add_argument("--interval", type=float, dest="interval", help="Interval in seconds in which data is requested. Minimum: 1.0", default=DEFAULT_ARGS["interval"])

        parser.add_argument("--mqtt-broker", type=str, dest="mqttbroker", help="Address of MQTT Broker to connect to")
        parser.add_argument("--mqtt-port", type=int, dest="mqttport", help="Port of MQTT Broker. Default is 1883 (8883 for TLS)", default=DEFAULT_ARGS["mqttport"])
        parser.add_argument("--mqtt-clientid", type=str, dest="mqttclientid", help="Id of the client. Default is a random id")
        parser.add_argument("--mqtt-keepalive", type=int, dest="mqttkeepalive", help="Time between keep-alive messages", default=DEFAULT_ARGS["mqttkeepalive"])
        parser.add_argument("--mqtt-username", type=str, dest="mqttusername", help="Username for MQTT broker")
        parser.add_argument("--mqtt-password", type=str, dest="mqttpassword", help="Password for MQTT broker")

        parser.add_argument("--modbus-port", type=str, dest="modbusport", help="Modbus serial port", default=DEFAULT_ARGS["modbusport"])

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

        valid_loglevels = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
        if args.loglevel not in valid_loglevels:
            print(f'Invalid log level given: {args.loglevel}, allowed values: {", ".join(valid_loglevels)}')
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
            self.mqtt.subscribe_to("/mode/set", self.__on_set_mode)
            self.mqtt.subscribe_to("/register/write", self.__on_write_register)
            self.mqtt.subscribe_to("/register/read", self.__on_read_register)


            # Send Home-assistant config
            #self._send_temperature_sensor_config("temp_outdoor", "Outdoor temperature")
            #self.mqtt.publish("ecl110/state", "Online")


            self.bus = minimalmodbus.Instrument(args.modbusport, 5)
            self.bus.serial.baudrate = 19200         # Baud
            self.bus.serial.bytesize = 8
            self.bus.serial.parity   = serial.PARITY_EVEN
            self.bus.serial.stopbits = 1
            self.bus.serial.timeout  = 0.05          # seconds

            print(f"address={self.bus.address}")
            print(f"mode={self.bus.mode}")

            self.valvePosition = 0;

            last_data_json = ""
            last_cycle = 0.0

            while True:
                await asyncio.sleep(max(0, args.interval - (time.time() - last_cycle)))
                last_cycle = time.time()

                if not self.mqtt.is_connected:
                    LOGGER.error(f"mqtt not connected")
                    continue

                try:

                    self.lock.acquire()
                    try:
                        
                        data = {
                            "mode_desired": self.__read_mode().name,
                            "mode": self.__read_uint16(4210),

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
                        if data_json != last_data_json:
                            print(last_cycle)
                            print(data)
                            self.mqtt.publish("ecl110/value", data)

                            # for key in data:
                            #     topic = f"ecl110/{key}/value"
                            #     value = data[key]
                            #     print(f"mqtt: {topic} - {value}")
                            #     self.mqtt.publish(topic, value)

                            last_data_json = data_json
                    finally:
                        self.lock.release()

                except minimalmodbus.NoResponseError:
                    LOGGER.error("no response on ModBus")
                except minimalmodbus.InvalidResponseError:
                    LOGGER.error("invalid response on ModBus")

        except KeyboardInterrupt:
            pass  # do nothing, close requested
        except CancelledError:
            pass  # do nothing, close requested
        except Exception as e:
            LOGGER.exception(f"exception in main loop")
        finally:
            LOGGER.info(f"shutdown requested")
            await self.mqtt.stop()

    def __on_write_register(self, client, userdata, msg):
        address = getattr(msg.payload, "address", None)
        if address == None:
            LOGGER.error('no value for "address" provided')
            return
        value = getattr(msg.payload, "value", None)
        if value == None:
            LOGGER.error('no value for "value" provided')
            return

        self.lock.acquire()
        try:
            self.bus.write_register(address, value, functioncode=6)
        except minimalmodbus.NoResponseError:
            LOGGER.error("no response on ModBus")
        except minimalmodbus.InvalidResponseError:
            LOGGER.error("invalid response on ModBus")
        finally:
            self.lock.release()

    def __on_read_register(self, client, userdata, msg):
        address = getattr(msg.payload, "address", None)
        if address == None:
            LOGGER.error('no value for "address" provided')
            return

        self.lock.acquire()
        try:
            value = self.bus.read_register(address)
        except minimalmodbus.NoResponseError:
            LOGGER.error("no response on ModBus")
        except minimalmodbus.InvalidResponseError:
            LOGGER.error("invalid response on ModBus")
        finally:
            self.lock.release()

        self.mqtt.publish(f"register/{address}", value)

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

    def __read_mode(self) -> Ecl110Mode:
        mode_val = self.__read_uint16(4200)
        if mode_val == 1:
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
