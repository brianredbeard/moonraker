# SimplyPrint Connection Support
#
# Copyright (C) 2022  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import json
import logging
import time
import pathlib
import tornado.websocket
from websockets import Subscribable, WebRequest
# XXX: The below imports are for inital dev and
# debugging.  They are used to create a logger for
# messages sent to and received from the simplyprint
# backend
import logging.handlers
import tempfile
from queue import SimpleQueue
from utils import LocalQueueHandler

from typing import (
    TYPE_CHECKING,
    Callable,
    Optional,
    Dict,
    List,
    Union,
    Any,
)
if TYPE_CHECKING:
    from confighelper import ConfigHelper
    from websockets import WebsocketManager, WebSocket
    from tornado.websocket import WebSocketClientConnection
    from components.database import MoonrakerDatabase
    from components.klippy_apis import KlippyAPI
    from components.job_state import JobState
    from components.machine import Machine
    from components.file_manager.file_manager import FileManager
    from klippy_connection import KlippyConnection

COMPONENT_VERSION = "0.0.1"
SP_VERSION = "0.1"
TEST_ENDPOINT = f"wss://testws.simplyprint.io/{SP_VERSION}/p"
PROD_ENDPOINT = f"wss://ws.simplyprint.io/{SP_VERSION}/p"
KEEPALIVE_TIME = 96.0
# TODO: Increase this time to something greater, perhaps 30 minutes
CONNECTION_ERROR_LOG_TIME = 60.

class SimplyPrint(Subscribable):
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self.job_state: JobState
        self.job_state = self.server.lookup_component("job_state")
        self.klippy_apis: KlippyAPI
        self.klippy_apis = self.server.lookup_component("klippy_apis")
        database: MoonrakerDatabase = self.server.lookup_component("database")
        database.register_local_namespace("simplyprint", forbidden=True)
        self.spdb = database.wrap_namespace("simplyprint")
        self.sp_info = self.spdb.as_dict()
        self.is_closing = False
        self.test = config.get("sp_test", True)
        self.ws: Optional[WebSocketClientConnection] = None
        self.cache = ReportCache()
        self.amb_detect = AmbientDetect(
            config, self.cache, self._on_ambient_changed,
            self.sp_info.get("ambient_temp", INITIAL_AMBIENT)
        )
        self.layer_detect = LayerDetect()
        self.last_received_temps: Dict[str, float] = {}
        self.last_err_log_time: float = 0.
        self.last_cpu_update_time: float = 0.
        self.intervals: Dict[str, float] = {
            "job": 1.,
            "temps": 1.,
            "temps_target": .25,
            "cpu": 10.
        }
        self.printer_status: Dict[str, Dict[str, Any]] = {}
        self.heaters: Dict[str, str] = {}
        self.missed_job_events: List[Dict[str, Any]] = []
        self.keepalive_hdl: Optional[asyncio.TimerHandle] = None
        self.reconnect_hdl: Optional[asyncio.TimerHandle] = None
        self.reconnect_delay: float = 1.
        self.reconnect_token: Optional[str] = None
        self.printer_info_timer = self.eventloop.register_timer(
            self._handle_printer_info_update)
        self.next_temp_update_time: float = 0.
        self._last_pong: float = 0.
        self.gcode_terminal_enabled: bool = False
        self.connected = False
        self.is_set_up = False
        # XXX: The configurable connect url is for testing,
        # remove before release
        connect_url = config.get("url", None)
        if connect_url is not None:
            self.connect_url = connect_url
            self.is_set_up = True
        else:
            self._set_ws_url()

        # Register State Events
        self.server.register_event_handler(
            "server:klippy_started", self._on_klippy_startup)
        self.server.register_event_handler(
            "server:klippy_ready", self._on_klippy_ready)
        self.server.register_event_handler(
            "server:klippy_shutdown", self._on_klippy_shutdown)
        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnected)
        self.server.register_event_handler(
            "job_state:started", self._on_print_start)
        self.server.register_event_handler(
            "job_state:paused", self._on_print_paused)
        self.server.register_event_handler(
            "job_state:resumed", self._on_print_resumed)
        self.server.register_event_handler(
            "job_state:standby", self._on_print_standby)
        self.server.register_event_handler(
            "job_state:complete", self._on_print_complete)
        self.server.register_event_handler(
            "job_state:error", self._on_print_error)
        self.server.register_event_handler(
            "job_state:cancelled", self._on_print_cancelled)
        self.server.register_event_handler(
            "klippy_apis:pause_requested", self._on_pause_requested)
        self.server.register_event_handler(
            "klippy_apis:resume_requested", self._on_resume_requested)
        self.server.register_event_handler(
            "klippy_apis:cancel_requested", self._on_cancel_requested)
        self.server.register_event_handler(
            "proc_stats:proc_stat_update", self._on_proc_update)
        self.server.register_event_handler(
            "websockets:websocket_identified",
            self._on_websocket_identified)
        self.server.register_event_handler(
            "websockets:websocket_removed",
            self._on_websocket_removed)
        self.server.register_event_handler(
            "server:gcode_response", self._on_gcode_response)
        self.server.register_event_handler(
            "klippy_connection:gcode_received", self._on_gcode_received
        )

        # XXX: The call below is for dev, remove before release
        self._setup_simplyprint_logging()

        # TODO: We need the ability to show users the activation code.
        # Hook into announcements?  Create endpoint to get
        # the connection code?  We could render something basic here
        # and present it at http://hostname/server/simplyprint

    async def component_init(self) -> None:
        connected = await self._do_connect(try_once=True)
        if not connected:
            self.reconnect_hdl = self.eventloop.delay_callback(
                5., self._do_connect)

    async def _do_connect(self, try_once=False) -> bool:
        url = self.connect_url
        if self.reconnect_token is not None:
            url = f"{self.connect_url}/{self.reconnect_token}"
        self._logger.info(f"Connecting To SimplyPrint: {url}")
        while not self.is_closing:
            try:
                self.ws = await tornado.websocket.websocket_connect(
                    url, connect_timeout=5.,
                    ping_interval=15., ping_timeout=45.,
                    on_message_callback=self._on_ws_message)
                setattr(self.ws, "on_pong", self._on_ws_pong)
                self._last_pong = self.eventloop.get_loop_time()
            except Exception:
                curtime = self.eventloop.get_loop_time()
                timediff = curtime - self.last_err_log_time
                if timediff > CONNECTION_ERROR_LOG_TIME:
                    self.last_err_log_time = curtime
                    logging.exception(
                        f"Failed to connect to SimplyPrint")
                if try_once:
                    self.reconnect_hdl = None
                    return False
                await asyncio.sleep(self.reconnect_delay)
            else:
                break
        logging.info("Connected to SimplyPrint Cloud")
        self.reconnect_hdl = None
        return True

    def _on_ws_message(self, message: Union[str, bytes, None]) -> None:
        if isinstance(message, str):
            self._process_message(message)
        elif message is None and not self.is_closing:
            pong_time: float = self.eventloop.get_loop_time() - self._last_pong
            reason = code = None
            if self.ws is not None:
                reason = self.ws.close_reason
                code = self.ws.close_code
            msg = (
                f"SimplyPrint Disconnected - Code: {code}, Reason: {reason}, "
                f"Pong Time Elapsed: {pong_time}"
            )
            logging.info(msg)
            self._logger.info(msg)
            self.connected = False
            self.ws = None
            if self.reconnect_hdl is None:
                self.reconnect_hdl = self.eventloop.delay_callback(
                    self.reconnect_delay, self._do_connect)
            if self.keepalive_hdl is not None:
                self.keepalive_hdl.cancel()
                self.keepalive_hdl = None

    def _on_ws_pong(self, data: bytes = b"") -> None:
        self._last_pong = self.eventloop.get_loop_time()

    def _process_message(self, msg: str) -> None:
        self._logger.info(f"received: {msg}")
        self._reset_keepalive()
        try:
            packet: Dict[str, Any] = json.loads(msg)
        except json.JSONDecodeError:
            logging.debug(f"Invalid message, not JSON: {msg}")
            return
        event: str = packet.get("type", "")
        data: Optional[Dict[str, Any]] = packet.get("data")
        if event == "connected":
            logging.info("SimplyPrint Reports Connection Success")
            self.connected = True
            self.reconnect_token = None
            if data is not None:
                interval = data.get("interval")
                if isinstance(interval, dict):
                    for key, val in interval.items():
                        self.intervals[key] = val / 1000.
                    self._logger.info(f"Intervals Updated: {self.intervals}")
                self.reconnect_token = data.get("reconnect_token")
                name = data.get("name")
                if name is not None:
                    self._save_item("printer_name", name)
            self.reconnect_delay = 1.
            self._push_initial_state()
        elif event == "error":
            logging.info(f"SimplyPrint Connection Error: {data}")
            self.reconnect_delay = 30.
            self.reconnect_token = None
        elif event == "new_token":
            if data is None:
                self._logger.info("Invalid message, no data")
                return
            token = data.get("token")
            if not isinstance(token, str):
                self._logger.info(f"Invalid token in message")
                return
            logging.info(f"SimplyPrint Token Received")
            self._save_item("printer_token", token)
            self._set_ws_url()
        elif event == "set_up":
            # TODO: This is a stubbed event to receive the printer ID,
            # it could change
            if data is None:
                self._logger.info(f"Invalid message, no data")
                return
            printer_id = data.get("id")
            if not isinstance(token, str):
                self._logger.info(f"Invalid printer id in message")
                return
            logging.info(f"SimplyPrint Printer ID Received: {printer_id}")
            self._save_item("printer_id", printer_id)
            self._set_ws_url()
            name = data.get("name")
            if not isinstance(name, str):
                self._logger.info(f"Invalid name in message: {msg}")
                return
            logging.info(f"SimplyPrint Printer ID Received: {name}")
            self._save_item("printer_name", name)
        elif event == "demand":
            if data is None:
                self._logger.info(f"Invalid message, no data")
                return
            demand = data.pop("demand", "unknown")
            self._process_demand(demand, data)
        elif event == "interval_change":
            if isinstance(data, dict):
                for key, val in data.items():
                    self.intervals[key] = val / 1000.
                self._logger.info(f"Intervals Updated: {self.intervals}")
        else:
            # TODO: It would be good for the backend to send an
            # event indicating that it is ready to recieve printer
            # status.
            self._logger.info(f"Unknown event: {msg}")

    def _process_demand(self, demand: str, args: Dict[str, Any]) -> None:
        kconn: KlippyConnection
        kconn = self.server.lookup_component("klippy_connection")
        if not kconn.is_connected():
            return
        if demand in ["pause", "resume", "cancel"]:
            self.eventloop.create_task(self._request_print_action(demand))
        elif demand == "terminal":
            if "enabled" in args:
                self.gcode_terminal_enabled = args["enabled"]
        elif demand == "gcode":
            script_list = args.get("list", [])
            if script_list:
                script = "\n".join(script_list)
                coro = self.klippy_apis.run_gcode(script, None)
                self.eventloop.create_task(coro)
        else:
            self._logger.info(f"Unknown demand: {demand}")

    def _save_item(self, name: str, data: Any):
        self.sp_info[name] = data
        self.spdb[name] = data

    def _set_ws_url(self):
        token: Optional[str] = self.sp_info.get("printer_token")
        printer_id: Optional[str] = self.sp_info.get("printer_id")
        ep = TEST_ENDPOINT if self.test else PROD_ENDPOINT
        self.connect_url = f"{ep}/0/0"
        if token is not None:
            if printer_id is None:
                self.connect_url = f"{ep}/0/{token}"
            else:
                self.is_set_up = True
                self.connect_url = f"{ep}/{printer_id}/{token}"

    async def _request_print_action(self, action: str) -> None:
        cur_state = self.cache.state
        ret: Optional[str] = ""
        if action == "pause":
            if cur_state == "printing":
                ret = await self.klippy_apis.pause_print(None)
        elif action == "resume":
            if cur_state == "paused":
                ret = await self.klippy_apis.resume_print(None)
        elif action == "cancel":
            if cur_state in ["printing", "paused"]:
                ret = await self.klippy_apis.cancel_print(None)
        if ret is None:
            # Make sure the event fired so we can reset
            await asyncio.sleep(.05)
            self._update_state(cur_state)

    async def _on_klippy_ready(self):
        last_stats: Dict[str, Any] = self.job_state.get_last_stats()
        if last_stats["state"] == "printing":
            self._on_print_start(last_stats, last_stats, False)
        else:
            self._update_state("operational")
        query: Dict[str] = await self.klippy_apis.query_objects(
            {"heaters": None}, None)
        sub_objs = {
            "display_status": ["progress"],
            "bed_mesh": ["mesh_matrix", "mesh_min", "mesh_max"],
            "toolhead": ["extruder"],
            "gcode_move": ["gcode_position"]
        }
        if query is not None:
            heaters: Dict[str, Any] = query.get("heaters", {})
            avail_htrs: List[str]
            avail_htrs = sorted(heaters.get("available_heaters", []))
            self._logger.info(f"SimplyPrint: Heaters Detected: {avail_htrs}")
            for htr in avail_htrs:
                if htr.startswith("extruder"):
                    sub_objs[htr] = ["temperature", "target"]
                    if htr == "extruder":
                        tool_id = "tool0"
                    else:
                        tool_id = "tool" + htr[8:]
                    self.heaters[htr] = tool_id
                elif htr == "heater_bed":
                    sub_objs[htr] = ["temperature", "target"]
                    self.heaters[htr] = "bed"
        if not sub_objs:
            return
        status: Dict[str, Any]
        # Create our own subscription rather than use the host sub
        args = {'objects': sub_objs}
        klippy: KlippyConnection
        klippy = self.server.lookup_component("klippy_connection")
        try:
            resp: Dict[str, Dict[str, Any]] = await klippy.request(
                WebRequest("objects/subscribe", args, conn=self))
            status: Dict[str, Any] = resp.get("status", {})
        except self.server.error:
            status = {}
        if status:
            self._logger.info(f"SimplyPrint: Got Initial Status: {status}")
            self.printer_status = status
            self._update_temps(1.)
            self.next_temp_update_time = 0.
            if "bed_mesh" in status:
                self._send_mesh_data()
            if "toolhead" in status:
                self._send_active_extruder(status["toolhead"]["extruder"])
            if "gcode_move" in status:
                self.layer_detect.update(
                    status["gcode_move"]["gcode_position"]
                )
        self.amb_detect.start()
        self.printer_info_timer.start(delay=1.)

    def _on_websocket_identified(self, ws: WebSocket) -> None:
        if (
            self.cache.current_wsid is None and
            ws.client_data.get("type", "") == "web"
        ):
            ui_data: Dict[str, Any] = {
                "ui": ws.client_data["name"],
                "ui_version": ws.client_data["version"]
            }
            self.cache.firmware_info.update(ui_data)
            self.cache.current_wsid = ws.uid
            self._send_sp("machine_data", ui_data)

    def _on_websocket_removed(self, ws: WebSocket) -> None:
        if self.cache.current_wsid is None or self.cache.current_wsid != ws.uid:
            return
        ui_data = self._get_ui_info()
        diff = self._get_object_diff(ui_data, self.cache.firmware_info)
        if diff:
            self.cache.firmware_info.update(ui_data)
            self._send_sp("machine_data", ui_data)

    def _on_klippy_startup(self, state: str) -> None:
        if state != "ready":
            self._update_state("error")
            self._send_sp("printer_error", None)
        self._send_sp("connection", {"new": "connected"})
        self._send_firmware_data()

    def _on_klippy_shutdown(self) -> None:
        self._send_sp("printer_error", None)

    def _on_klippy_disconnected(self) -> None:
        self._update_state("offline")
        self._send_sp("connection", {"new": "disconnected"})
        self.amb_detect.stop()
        self.printer_info_timer.stop()
        self.cache.reset_print_state()
        self.printer_status = {}

    def _on_print_start(
        self,
        prev_stats: Dict[str, Any],
        new_stats: Dict[str, Any],
        need_start_event: bool = True
    ) -> None:
        # inlcludes started and resumed events
        self._update_state("printing")
        filename = new_stats["filename"]
        job_info: Dict[str, Any] = {"filename": filename}
        fm: FileManager = self.server.lookup_component("file_manager")
        metadata = fm.get_file_metadata(filename)
        filament: Optional[float] = metadata.get("filament_total")
        if filament is not None:
            job_info["filament"] = round(filament)
        est_time = metadata.get("estimated_time")
        if est_time is not None:
            job_info["time"] = est_time
        self.cache.metadata = metadata
        self.cache.job_info.update(job_info)
        if need_start_event:
            job_info["started"] = True
        self.layer_detect.start(metadata)
        self._send_job_event(job_info)

    def _on_print_paused(self, *args) -> None:
        self._send_sp("job_info", {"paused": True})
        self._update_state("paused")
        self.layer_detect.stop()

    def _on_print_resumed(self, *args) -> None:
        self._update_state("printing")
        self.layer_detect.resume()

    def _on_print_cancelled(self, *args) -> None:
        self._send_job_event({"cancelled": True})
        self._update_state_from_klippy()
        self.cache.job_info = {}
        self.layer_detect.stop()

    def _on_print_error(self, *args) -> None:
        self._send_job_event({"failed": True})
        self._update_state_from_klippy()
        self.cache.job_info = {}
        self.layer_detect.stop()

    def _on_print_complete(self, *args) -> None:
        self._send_job_event({"finished": True})
        self._update_state_from_klippy()
        self.cache.job_info = {}
        self.layer_detect.stop()

    def _on_print_standby(self, *args) -> None:
        self._update_state_from_klippy()
        self.cache.job_info = {}
        self.layer_detect.stop()

    def _on_pause_requested(self) -> None:
        if self.cache.state == "printing":
            self._update_state("pausing")

    def _on_resume_requested(self) -> None:
        if self.cache.state == "paused":
            self._update_state("resuming")

    def _on_cancel_requested(self) -> None:
        if self.cache.state in ["printing", "paused", "pausing"]:
            self._update_state("cancelling")

    def _on_gcode_response(self, response: str):
        if self.gcode_terminal_enabled:
            resp = [
                r.strip() for r in response.strip().split("\n") if r.strip()
            ]
            self._send_sp("term_update", {"response": resp})

    def _on_gcode_received(self, script: str):
        if self.gcode_terminal_enabled:
            cmds = [s.strip() for s in script.strip().split() if s.strip()]
            self._send_sp("term_update", {"command": cmds})

    def _on_proc_update(self, proc_stats: Dict[str, Any]) -> None:
        cpu = proc_stats["system_cpu_usage"]
        if not cpu:
            return
        curtime = self.eventloop.get_loop_time()
        if curtime - self.last_cpu_update_time < self.intervals["cpu"]:
            return
        self.last_cpu_update_time = curtime
        sys_mem = proc_stats["system_memory"]
        mem_pct: float = 0.
        if sys_mem:
            mem_pct = sys_mem["used"] / sys_mem["total"] * 100
        cpu_data = {
            "usage": int(cpu["cpu"] + .5),
            "temp": int(proc_stats["cpu_temp"] + .5),
            "memory": int(mem_pct + .5)
        }
        diff = self._get_object_diff(cpu_data, self.cache.cpu_info)
        if diff:
            self.cache.cpu_info = cpu_data
            self._send_sp("cpu", diff)

    def _on_ambient_changed(self, new_ambient: int) -> None:
        self._save_item("ambient_temp", new_ambient)
        self._send_sp("ambient", {"new": new_ambient})

    def send_status(self, status: Dict[str, Any], eventtime: float) -> None:
        for printer_obj, vals in status.items():
            self.printer_status[printer_obj].update(vals)
        self._update_temps(eventtime)
        if "bed_mesh" in status:
            self._send_mesh_data()
        if "toolhead" in status and "extruder" in status["toolhead"]:
            self._send_active_extruder(status["toolhead"]["extruder"])
        if "gcode_move" in status:
            self.layer_detect.update(status["gcode_move"]["gcode_position"])

    def _handle_printer_info_update(self, eventtime: float) -> float:
        # Job Info Timer handler
        if self.cache.state == "printing":
            self._update_job_progress()
        return eventtime + self.intervals["job"]

    def _update_job_progress(self) -> None:
        job_info: Dict[str, Any] = {}
        est_time = self.cache.metadata.get("estimated_time")
        if est_time is not None:
            last_stats: Dict[str, Any] = self.job_state.get_last_stats()
            duration: float = last_stats["print_duration"]
            time_left = max(0, int(est_time - duration + .5))
            last_time_left = self.cache.job_info.get("time", time_left + 60.)
            time_diff = last_time_left - time_left
            if (
                (time_left < 60 or time_diff >= 30) and
                time_left != last_time_left
            ):
                job_info["time"] = time_left
        if "display_status" in self.printer_status:
            progress = self.printer_status["display_status"]["progress"]
            pct_prog = int(progress * 100 + .5)
            if pct_prog != self.cache.job_info.get("progress", 0):
                job_info["progress"] = int(progress * 100 + .5)
        layer = self.layer_detect.layer
        if layer != self.cache.job_info.get("layer", -1):
            job_info["layer"] = layer
        if job_info:
            self.cache.job_info.update(job_info)
            self._send_sp("job_info", job_info)

    def _update_temps(self, eventtime: float) -> None:
        if eventtime < self.next_temp_update_time:
            return
        need_rapid_update: bool = False
        temp_data: Dict[str, List[int]] = {}
        for printer_obj, key in self.heaters.items():
            reported_temp = self.printer_status[printer_obj]["temperature"]
            ret = [
                int(reported_temp + .5),
                int(self.printer_status[printer_obj]["target"] + .5)
            ]
            last_temps = self.cache.temps.get(key, [-100., -100.])
            if ret[1] == last_temps[1]:
                if ret[1]:
                    seeking_target = abs(ret[1] - ret[0]) > 5
                else:
                    seeking_target = ret[0] >= self.amb_detect.ambient + 25
                need_rapid_update |= seeking_target
                # The target hasn't changed and not heating, debounce temp
                if key in self.last_received_temps and not seeking_target:
                    last_reported = self.last_received_temps[key]
                    if abs(reported_temp - last_reported) < .75:
                        self.last_received_temps.pop(key)
                        continue
                if ret[0] == last_temps[0]:
                    self.last_received_temps[key] = reported_temp
                    continue
                temp_data[key] = ret[:1]
            else:
                # target has changed, send full data
                temp_data[key] = ret
            self.last_received_temps[key] = reported_temp
            self.cache.temps[key] = ret
        if need_rapid_update:
            self.next_temp_update_time = (
                0. if self.intervals["temps_target"] < .2501 else
                eventtime + self.intervals["temps_target"]
            )
        else:
            self.next_temp_update_time = eventtime + self.intervals["temps"]
        if not temp_data:
            return
        if self.is_set_up:
            self._send_sp("temps", temp_data)

    def _update_state_from_klippy(self) -> None:
        kstate = self.server.get_klippy_state()
        if kstate == "ready":
            sp_state = "operational"
        elif kstate in ["error", "shutdown"]:
            sp_state = "error"
        else:
            sp_state = "offline"
        self._update_state(sp_state)

    def _update_state(self, new_state: str) -> None:
        if self.cache.state == new_state:
            return
        self.cache.state = new_state
        self._send_sp("state_change", {"new": new_state})

    def _send_mesh_data(self) -> None:
        mesh = self.printer_status["bed_mesh"]
        # TODO: We are probably going to have to reformat the mesh
        self.cache.mesh = mesh
        self._send_sp("mesh_data", mesh)

    def _send_job_event(self, job_info: Dict[str, Any]) -> None:
        if self.connected:
            self._send_sp("job_info", job_info)
        else:
            job_info.update(self.cache.job_info)
            job_info["delay"] = self.eventloop.get_loop_time()
            self.missed_job_events.append(job_info)
            if len(self.missed_job_events) > 10:
                self.missed_job_events.pop(0)

    def _get_ui_info(self) -> Dict[str, Any]:
        ui_data: Dict[str, Any] = {"ui": None, "ui_version": None}
        self.cache.current_wsid = None
        websockets: WebsocketManager
        websockets = self.server.lookup_component("websockets")
        conns = websockets.get_websockets_by_type("web")
        if conns:
            longest = conns[0]
            ui_data["ui"] = longest.client_data["name"]
            ui_data["ui_version"] = longest.client_data["version"]
            self.cache.current_wsid = longest.uid
        return ui_data

    async def _send_machine_data(self):
        app_args = self.server.get_app_args()
        data = self._get_ui_info()
        data["api"] = "Moonraker"
        data["api_version"] = app_args["software_version"]
        data["sp_version"] = COMPONENT_VERSION
        machine: Machine = self.server.lookup_component("machine")
        sys_info = machine.get_system_info()
        pyver = sys_info["python"]["version"][:3]
        data["python_version"] = ".".join([str(part) for part in pyver])
        model: str = sys_info["cpu_info"].get("model", "")
        if not model or model.isdigit():
            model = sys_info["cpu_info"].get("cpu_desc", "Unknown")
        data["machine"] = model
        data["os"] = sys_info["distribution"].get("name", "Unknown")
        pub_intf = await machine.get_public_network()
        data["is_ethernet"] = int(not pub_intf["is_wifi"])
        data["ssid"] = pub_intf.get("ssid", "")
        data["local_ip"] = pub_intf.get("address", "Unknown")
        data["hostname"] = pub_intf["hostname"]
        self._logger.info(f"calculated machine data: {data}")
        diff = self._get_object_diff(data, self.cache.machine_info)
        if diff:
            self.cache.machine_info = data
            self._send_sp("machine_data", diff)

    def _send_firmware_data(self):
        kinfo = self.server.get_klippy_info()
        if "software_version" not in kinfo:
            return
        firmware_date: str = ""
        # Approximate the firmware "date" using the last modified
        # time of the Klippy source folder
        kpath = pathlib.Path(kinfo["klipper_path"]).joinpath("klippy")
        if kpath.is_dir():
            mtime = kpath.stat().st_mtime
            firmware_date = time.asctime(time.gmtime(mtime))
        version: str = kinfo["software_version"]
        unsafe = version.endswith("-dirty") or version == "?"
        if unsafe:
            version = version.rsplit("-", 1)[0]
        fw_info = {
            "firmware": "Klipper",
            "firmware_version": version,
            "firmware_date": firmware_date,
            "firmware_link": "https://github.com/Klipper3d/klipper",
            "firmware_unsafe": unsafe
        }
        diff = self._get_object_diff(fw_info, self.cache.firmware_info)
        if diff:
            self.cache.firmware_info = fw_info
            self._send_sp("firmware", {"fw": diff, "raw": False})

    def _send_active_extruder(self, new_extruder: str):
        tool = "T0" if new_extruder == "extruder" else f"T{new_extruder[8:]}"
        if tool == self.cache.active_extruder:
            return
        self.cache.active_extruder = tool
        self._send_sp("tool", {"new": tool})

    def _push_initial_state(self):
        # TODO: This method is called after SP is connected
        # and ready to receive state.  We need a list of items
        # we can safely send if the printer is not setup (ie: has no
        # printer ID)
        #
        # The firmware data and machine data is likely saved by
        # simplyprint.  It might be better for SP to request it
        # rather than for the client to send it on every connection.
        self._send_sp("state_change", {"new": self.cache.state})
        if self.cache.temps and self.is_set_up:
            self._send_sp("temps", self.cache.temps)
        if self.cache.firmware_info:
            self._send_sp(
                "firmware",
                {"fw": self.cache.firmware_info, "raw": False})
        curtime = self.eventloop.get_loop_time()
        for evt in self.missed_job_events:
            evt["delay"] = int((curtime - evt["delay"]) + .5)
            self._send_sp("job_info", evt)
        if self.cache.active_extruder:
            self._send_sp("tool", {"new": self.cache.active_extruder})
        self.missed_job_events = []
        if self.cache.cpu_info:
            self._send_sp("cpu_info", self.cache.cpu_info)
        self._send_sp("ambient", {"new": self.amb_detect.ambient})
        self.eventloop.create_task(self._send_machine_data())

    def _send_sp(self, evt_name: str, data: Any) -> asyncio.Future:
        if not self.connected or self.ws is None:
            fut = self.eventloop.create_future()
            fut.set_result(False)
            return fut
        packet = {"type": evt_name, "data": data}
        self._logger.info(f"sent: {packet}")
        self._reset_keepalive()
        return self.ws.write_message(json.dumps(packet))

    def _reset_keepalive(self):
        if self.keepalive_hdl is not None:
            self.keepalive_hdl.cancel()
        self.keepalive_hdl = self.eventloop.delay_callback(
            KEEPALIVE_TIME, self._do_keepalive)

    def _do_keepalive(self):
        self.keepalive_hdl = None
        self._send_sp("keepalive", None)

    def _setup_simplyprint_logging(self):
        fm: FileManager = self.server.lookup_component("file_manager")
        log_root = fm.get_directory("logs")
        if log_root:
            log_parent = pathlib.Path(log_root)
        else:
            log_parent = pathlib.Path(tempfile.gettempdir())
        log_path = log_parent.joinpath("simplyprint.log")
        queue: SimpleQueue = SimpleQueue()
        queue_handler = LocalQueueHandler(queue)
        self._logger = logging.getLogger("simplyprint")
        self._logger.addHandler(queue_handler)
        self._logger.propagate = False
        file_hdlr = logging.handlers.TimedRotatingFileHandler(
            log_path, when='midnight', backupCount=2)
        formatter = logging.Formatter(
            '%(asctime)s [%(funcName)s()] - %(message)s')
        file_hdlr.setFormatter(formatter)
        self.qlistner = logging.handlers.QueueListener(queue, file_hdlr)
        self.qlistner.start()

    def _get_object_diff(
        self, new_obj: Dict[str, Any], cached_obj: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not cached_obj:
            return new_obj
        diff: Dict[str, Any] = {}
        for key, val in new_obj.items():
            if key in cached_obj and val == cached_obj[key]:
                continue
            diff[key] = val
        return diff

    async def close(self):
        await self._send_sp("shutdown", None)
        self.qlistner.stop()
        self.amb_detect.stop()
        self.printer_info_timer.stop()
        self.is_closing = True
        if self.reconnect_hdl is not None:
            # TODO, would be good to cancel the reconnect task as well
            self.reconnect_hdl.cancel()
        if self.keepalive_hdl is not None:
            self.keepalive_hdl.cancel()
            self.keepalive_hdl = None
        if self.ws is not None:
            self.ws.close(1001, "Client Shutdown")

class ReportCache:
    def __init__(self) -> None:
        self.state = "offline"
        self.temps: Dict[str, Any] = {}
        self.metadata: Dict[str, Any] = {}
        self.mesh: Dict[str, Any] = {}
        self.job_info: Dict[str, Any] = {}
        self.active_extruder: str = ""
        # Persistent state across connections
        self.firmware_info: Dict[str, Any] = {}
        self.machine_info: Dict[str, Any] = {}
        self.cpu_info: Dict[str, Any] = {}
        self.current_wsid: Optional[int] = None

    def reset_print_state(self) -> None:
        self.temps = {}
        self.mesh = {}
        self.job_info = {}


INITIAL_AMBIENT = 85
AMBIENT_CHECK_TIME = 5. * 60.
TARGET_CHECK_TIME = 60. * 60.
SAMPLE_CHECK_TIME = 20.

class AmbientDetect:
    CHECK_INTERVAL = 5
    def __init__(
        self,
        config: ConfigHelper,
        cache: ReportCache,
        changed_cb: Callable[[int], None],
        initial_ambient: int
    ) -> None:
        self.server = config.get_server()
        self.cache = cache
        self._initial_sample: int = -1000
        self._ambient = initial_ambient
        self._on_ambient_changed = changed_cb
        self._last_sample_time: float = 0.
        self._update_interval = AMBIENT_CHECK_TIME
        eventloop = self.server.get_event_loop()
        self._detect_timer = eventloop.register_timer(self._handle_detect_timer)

    @property
    def ambient(self) -> int:
        return self._ambient

    def _handle_detect_timer(self, eventtime: float) -> float:
        if "tool0" not in self.cache.temps:
            self._initial_sample = -1000
            return eventtime + self.CHECK_INTERVAL
        temp, target = self.cache.temps["tool0"]
        if target:
            self._initial_sample = -1000
            self._last_sample_time = eventtime
            self._update_interval = TARGET_CHECK_TIME
            return eventtime + self.CHECK_INTERVAL
        if eventtime - self._last_sample_time < self._update_interval:
            return eventtime + self.CHECK_INTERVAL
        if self._initial_sample == -1000:
            self._initial_sample = temp
            self._update_interval = SAMPLE_CHECK_TIME
        else:
            diff = abs(temp - self._initial_sample)
            if diff <= 2:
                last_ambient = self._ambient
                self._ambient = int((temp + self._initial_sample) / 2 + .5)
                self._initial_sample = -1000
                self._last_sample_time = eventtime
                self._update_interval = AMBIENT_CHECK_TIME
                if last_ambient != self._ambient:
                    logging.debug(f"SimplyPrint: New Ambient: {self._ambient}")
                    self._on_ambient_changed(self._ambient)
            else:
                self._initial_sample = temp
                self._update_interval = SAMPLE_CHECK_TIME
        return eventtime + self.CHECK_INTERVAL

    def start(self) -> None:
        if self._detect_timer.is_running():
            return
        if "tool0" in self.cache.temps:
            cur_temp = self.cache.temps["tool0"][0]
            if cur_temp < self._ambient:
                self._ambient = cur_temp
                self._on_ambient_changed(self._ambient)
        self._detect_timer.start()

    def stop(self) -> None:
        self._detect_timer.stop()

class LayerDetect:
    def __init__(self) -> None:
        self._layer: int = 0
        self._layer_z: float = 0.
        self._active: bool = False
        self._layer_height: float = 0.
        self._fl_height: float = 0.
        self._layer_count: int = 99999999999
        self._check_next: bool = False

    @property
    def layer(self) -> int:
        return self._layer

    def update(self, new_pos: List[float]) -> None:
        if not self._active or self._layer_z == new_pos[2]:
            self._check_next = False
            return
        if not self._check_next:
            # Try to avoid z-hops by skipping the first detected change
            self._check_next = True
            return
        self._check_next = False
        layer = 1 + int(
            (new_pos[2] - self._fl_height) / self._layer_height + .5
        )
        self._layer = min(layer, self._layer_count)
        self._layer_z = new_pos[2]

    def start(self, metadata: Dict[str, Any]) -> None:
        self.reset()
        lh: Optional[float] = metadata.get("layer_height")
        flh: Optional[float] = metadata.get("first_layer_height", lh)
        if lh is not None and flh is not None:
            self._active = True
            self._layer_height = lh
            self._fl_height = flh
            layer_count: Optional[int] = metadata.get("layer_count")
            obj_height: Optional[float] = metadata.get("object_height")
            if layer_count is not None:
                self._layer_count = layer_count
            elif obj_height is not None:
                self._layer_count = int((obj_height - flh) / lh + .5)

    def resume(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def reset(self) -> None:
        self._active = False
        self._layer = 0
        self._layer_z = 0.
        self._layer_height = 0.
        self._fl_height = 0.
        self._layer_count = 99999999999
        self._check_next = False

def load_component(config: ConfigHelper) -> SimplyPrint:
    return SimplyPrint(config)
