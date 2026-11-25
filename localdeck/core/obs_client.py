"""OBS Studio WebSocket v5 client locked to loopback 127.0.0.1."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from abc import ABC, abstractmethod
from typing import Any

from websockets.sync.client import connect as ws_connect


class ObsBackend(ABC):
    """Abstract OBS Studio WebSocket v5 control backend."""

    @abstractmethod
    def execute(
        self,
        command: str,
        scene: str | None = None,
        input_name: str | None = None,
        port: int = 4455,
        password: str = "",
    ) -> dict[str, Any]:
        """Execute an OBS control command."""

    @abstractmethod
    def fetch_resources(self, port: int = 4455, password: str = "") -> dict[str, Any]:
        """Return connection status, scenes, and audio input names."""

    @abstractmethod
    def fetch_status(self, port: int = 4455, password: str = "") -> dict[str, Any]:
        """Return live recording, streaming, studio mode, and active scene status."""


def _compute_obs_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class ObsWebSocketClient(ObsBackend):
    """Minimal synchronous OBS WebSocket v5 RPC client (`ws://127.0.0.1:<port>`)."""

    def _open_identified(self, port: int, password: str):
        ws = ws_connect(f"ws://127.0.0.1:{int(port)}", open_timeout=1.5, close_timeout=1.0)
        try:
            hello = json.loads(ws.recv(timeout=2.0))
            if hello.get("op") != 0:
                raise RuntimeError("Unexpected OBS greeting")
            auth_info = hello.get("d", {}).get("authentication")
            identify_d: dict[str, Any] = {"rpcVersion": 1}
            if auth_info:
                if not password:
                    raise PermissionError("OBS WebSocket password required")
                identify_d["authentication"] = _compute_obs_auth(
                    password, auth_info["salt"], auth_info["challenge"]
                )
            ws.send(json.dumps({"op": 1, "d": identify_d}))
            identified = json.loads(ws.recv(timeout=2.0))
            if identified.get("op") != 2:
                raise PermissionError("OBS authentication failed")
            return ws
        except Exception:
            ws.close()
            raise

    @staticmethod
    def _rpc(ws, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = secrets.token_hex(6)
        payload: dict[str, Any] = {
            "op": 6,
            "d": {"requestType": request_type, "requestId": req_id},
        }
        if request_data is not None:
            payload["d"]["requestData"] = request_data
        ws.send(json.dumps(payload))
        while True:
            msg = json.loads(ws.recv(timeout=2.5))
            if msg.get("op") == 7 and msg.get("d", {}).get("requestId") == req_id:
                status = msg["d"].get("requestStatus", {})
                if not status.get("result", False):
                    raise RuntimeError(status.get("comment") or f"OBS request failed: {request_type}")
                return msg["d"].get("responseData") or {}

    def execute(
        self,
        command: str,
        scene: str | None = None,
        input_name: str | None = None,
        port: int = 4455,
        password: str = "",
    ) -> dict[str, Any]:
        with self._open_identified(port=port, password=password) as ws:
            if command == "switch_scene":
                if not scene:
                    raise ValueError("OBS scene name is required")
                self._rpc(ws, "SetCurrentProgramScene", {"sceneName": scene})
                return {"obs": "switch_scene", "scene": scene}
            if command == "toggle_record":
                return self._rpc(ws, "ToggleRecord")
            if command == "toggle_stream":
                return self._rpc(ws, "ToggleStream")
            if command == "save_replay":
                self._rpc(ws, "SaveReplayBuffer")
                return {"obs": "save_replay"}
            if command == "toggle_mute":
                if not input_name:
                    raise ValueError("OBS input name is required")
                return self._rpc(ws, "ToggleInputMute", {"inputName": input_name})
            if command == "toggle_studio_mode":
                state = self._rpc(ws, "GetStudioModeEnabled")
                enabled = not bool(state.get("studioModeEnabled", False))
                self._rpc(ws, "SetStudioModeEnabled", {"studioModeEnabled": enabled})
                return {"studioModeEnabled": enabled}
            raise ValueError(f"Unsupported OBS command: {command}")

    def fetch_resources(self, port: int = 4455, password: str = "") -> dict[str, Any]:
        try:
            with self._open_identified(port=port, password=password) as ws:
                scene_data = self._rpc(ws, "GetSceneList")
                scenes = [s["sceneName"] for s in scene_data.get("scenes", []) if "sceneName" in s]
                scenes.reverse()
                input_data = self._rpc(ws, "GetInputList")
                inputs = [i["inputName"] for i in input_data.get("inputs", []) if "inputName" in i]
                return {"connected": True, "scenes": scenes, "inputs": inputs}
        except Exception as exc:
            return {"connected": False, "scenes": [], "inputs": [], "error": str(exc)}

    def fetch_status(self, port: int = 4455, password: str = "") -> dict[str, Any]:
        status: dict[str, Any] = {
            "connected": False,
            "recording": False,
            "streaming": False,
            "studio_mode": False,
            "scene": "",
        }
        try:
            with ws_connect(f"ws://127.0.0.1:{int(port)}", open_timeout=0.8, close_timeout=0.5) as ws:
                hello = json.loads(ws.recv(timeout=1.2))
                if hello.get("op") != 0:
                    return status
                auth_info = hello.get("d", {}).get("authentication")
                identify_d: dict[str, Any] = {"rpcVersion": 1}
                if auth_info:
                    if not password:
                        return status
                    identify_d["authentication"] = _compute_obs_auth(
                        password, auth_info["salt"], auth_info["challenge"]
                    )
                ws.send(json.dumps({"op": 1, "d": identify_d}))
                identified = json.loads(ws.recv(timeout=1.2))
                if identified.get("op") != 2:
                    return status
                status["connected"] = True
                try:
                    rec = self._rpc(ws, "GetRecordStatus")
                    status["recording"] = bool(rec.get("outputActive", False))
                except Exception:
                    pass
                try:
                    stream = self._rpc(ws, "GetStreamStatus")
                    status["streaming"] = bool(stream.get("outputActive", False))
                except Exception:
                    pass
                try:
                    scene = self._rpc(ws, "GetCurrentProgramScene")
                    status["scene"] = str(scene.get("currentProgramSceneName") or "")
                except Exception:
                    pass
                try:
                    studio = self._rpc(ws, "GetStudioModeEnabled")
                    status["studio_mode"] = bool(studio.get("studioModeEnabled", False))
                except Exception:
                    pass
                return status
        except Exception:
            return status


class NoopObsBackend(ObsBackend):
    """In-memory OBS mock for tests."""

    def __init__(self, scenes: list[str] | None = None, inputs: list[str] | None = None) -> None:
        self.scenes = scenes or ["Main Scene", "Gameplay", "BRB"]
        self.inputs = inputs or ["Mic/Aux", "Desktop Audio"]
        self.calls: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {
            "connected": True,
            "recording": False,
            "streaming": False,
            "studio_mode": False,
            "scene": self.scenes[0] if self.scenes else "",
        }

    def execute(
        self,
        command: str,
        scene: str | None = None,
        input_name: str | None = None,
        port: int = 4455,
        password: str = "",
    ) -> dict[str, Any]:
        call = {
            "command": command,
            "scene": scene,
            "input_name": input_name,
            "port": port,
            "password": password,
        }
        self.calls.append(call)
        if command == "switch_scene" and scene:
            self.status["scene"] = scene
        elif command == "toggle_record":
            self.status["recording"] = not bool(self.status["recording"])
        elif command == "toggle_stream":
            self.status["streaming"] = not bool(self.status["streaming"])
        elif command == "toggle_studio_mode":
            self.status["studio_mode"] = not bool(self.status["studio_mode"])
        return {"ok": True, "command": command}

    def fetch_resources(self, port: int = 4455, password: str = "") -> dict[str, Any]:
        del port, password
        return {"connected": True, "scenes": list(self.scenes), "inputs": list(self.inputs)}

    def fetch_status(self, port: int = 4455, password: str = "") -> dict[str, Any]:
        del port, password
        return dict(self.status)
