"""Клиент для управления платой Duet (RepRapFirmware) по HTTP.

Поддерживает две основные схемы API:
- RRF 3.x: POST /machine/code, GET /machine/axes, GET /machine/status
- RRF 2.x: GET /rr_gcode, GET /rr_status, GET /rr_reply, GET /rr_connect
"""

from __future__ import annotations

import json
import re
import time
from typing import Dict, Iterable, List, Optional, Set

import requests


class DuetError(Exception):
    """Ошибка общения с платой Duet."""


class DuetAuthError(DuetError):
    """Ошибка авторизации (HTTP 401) — потеряна сессия или неверный пароль."""


class DuetBusyError(DuetError):
    """Плата занята (HTTP 503)."""


class DuetClient:
    """Обёртка над HTTP API платы Duet."""

    def __init__(self, ip: str, password: str = "", port: int = 80, timeout: float = 5.0):
        self.ip = ip
        self.password = password
        self.port = port
        self.timeout = timeout
        self.base = f"http://{ip}:{port}"
        self.major: Optional[int] = None
        self.session = requests.Session()

    def _request(self, method: str, path: str, *, params: Optional[dict] = None, json_payload: Optional[dict] = None) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self.session.request(
                    method,
                    self.base + path,
                    params=params,
                    json=json_payload,
                    timeout=self.timeout,
                )
                if response.status_code == 401:
                    raise DuetAuthError(f"Не авторизован (HTTP 401) при {method} {path} на {self.base}")
                if response.status_code == 503:
                    raise DuetBusyError(f"Плата занята (HTTP 503) при {method} {path} на {self.base}")
                response.raise_for_status()
                return response.text
            except (DuetAuthError, DuetBusyError):
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
        raise DuetError(f"HTTP {method} {path} для {self.base} не удался: {last_error}")

    def _get(self, path: str, params: Optional[dict] = None) -> str:
        return self._request("GET", path, params=params)

    def _post_json(self, path: str, payload: dict) -> str:
        return self._request("POST", path, json_payload=payload)

    def connect(self) -> int:
        """Подключается к плате и возвращает мажорную версию прошивки (2 или 3)."""
        params = {"password": self.password} if self.password else None
        try:
            if self.password:
                self._get("/machine/connect", params)
            self._get("/machine/status")
            self.major = 3
            return 3
        except DuetError:
            pass
        try:
            if self.password:
                self._get("/rr_connect", params)
            self._get("/rr_status")
            self.major = 2
            return 2
        except DuetError as exc:
            raise DuetError(f"Не удалось подключиться к плате {self.ip}: {exc}") from exc

    def _send_once(self, code: str) -> None:
        if self.major == 3:
            self._post_json("/machine/code", {"code": code})
        else:
            self._get("/rr_gcode", {"gcode": code})

    def send_gcode(self, code: str) -> None:
        """Отправляет одну G-команду, восстанавливая сессию после HTTP 401."""
        if self.major is None:
            self.connect()
        code = code.strip()
        if not code:
            return
        try:
            self._send_once(code)
        except DuetAuthError:
            self.major = None
            self.connect()
            self._send_once(code)

    def send_gcodes(self, codes: Iterable[str]) -> None:
        for code in codes:
            self.send_gcode(code)

    def get_position(self) -> Dict[str, float]:
        """Возвращает текущие координаты осей (словарь {'X': .., 'Y': .., ...})."""
        if self.major is None:
            self.connect()
        if self.major == 3:
            raw = self._get("/machine/axes")
            data = json.loads(raw)
            axes = data.get("axes", data if isinstance(data, list) else [])
            pos: Dict[str, float] = {}
            for ax in axes:
                if isinstance(ax, dict):
                    letter = ax.get("letter", "")
                    if letter:
                        pos[letter] = float(ax.get("userPosition", 0.0))
            return pos
        # RRF 2.x: отправляем M114 и читаем накопленный reply через /rr_reply
        self.send_gcode("M114")
        return self._parse_m114(self._get_rrf2_reply())

    def _get_rrf2_reply(self) -> str:
        """Читает ответ платы на G-команды в схеме RRF 2.x (GET /rr_reply)."""
        raw = self._get("/rr_reply")
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "reply" in data:
                return str(data["reply"])
        except ValueError:
            pass
        return raw

    def get_homed_axes(self) -> Optional[Set[str]]:
        """Возвращает множество homed-осей, если это можно определить, иначе None."""
        if self.major is None:
            self.connect()
        if self.major != 3:
            return None
        try:
            raw = self._get("/machine/status")
            data = json.loads(raw)
            homed: Set[str] = set()
            for ax in data.get("move", {}).get("axes", []):
                if isinstance(ax, dict) and ax.get("homed") and ax.get("letter"):
                    homed.add(ax["letter"])
            if homed:
                return homed
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_m114(text: str) -> Dict[str, float]:
        pos: Dict[str, float] = {}
        for token in text.replace(",", " ").split():
            if len(token) >= 2 and ":" in token and token[0] in "XYZUVWABC":
                try:
                    pos[token[0]] = float(token.split(":", 1)[1])
                except ValueError:
                    pass
        return pos

    def set_currents(self, currents: Dict[str, float]) -> None:
        parts = " ".join(f"{k}{int(v)}" for k, v in currents.items())
        if parts:
            self.send_gcode(f"M906 {parts}")

    def set_speeds(self, speeds: Dict[str, float]) -> None:
        parts = " ".join(f"{k}{int(v)}" for k, v in speeds.items())
        if parts:
            self.send_gcode(f"M203 {parts}")

    def motors_off(self) -> None:
        self.send_gcode("M18")

    def motors_on(self) -> None:
        self.send_gcode("M17")


class MockDuetClient:
    """Имитация платы для отладки без железа (режим mock)."""

    def __init__(self, *args, **kwargs):
        self.major = 3
        self.relative = False
        self.position: Dict[str, float] = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.homed: Set[str] = set(self.position.keys())
        self.log: List[str] = []

    def connect(self) -> int:
        return 3

    def send_gcode(self, code: str) -> None:
        line = code.strip()
        if not line:
            return
        self.log.append(line)
        if line == "G91":
            self.relative = True
        elif line == "G90":
            self.relative = False
        elif line.startswith("G28"):
            self._apply_home(line)
        elif line.startswith("G1"):
            self._apply_move(line)

    def send_gcodes(self, codes: Iterable[str]) -> None:
        for code in codes:
            self.send_gcode(code)

    def _apply_move(self, line: str) -> None:
        for match in re.finditer(r"([A-Z])(-?\d+(?:\.\d+)?)", line):
            axis, val = match.group(1), float(match.group(2))
            if axis not in self.position:
                continue
            if self.relative:
                self.position[axis] = self.position.get(axis, 0.0) + val
            else:
                self.position[axis] = val

    def _apply_home(self, line: str) -> None:
        axes = re.findall(r"[XYZUVWABC]", line)
        targets = [a for a in axes if a in self.position] if axes else list(self.position.keys())
        for axis in targets:
            self.position[axis] = 0.0
            self.homed.add(axis)

    def get_position(self) -> Dict[str, float]:
        return dict(self.position)

    def get_homed_axes(self) -> Set[str]:
        return set(self.homed)

    def set_currents(self, currents: Dict[str, float]) -> None:
        self.send_gcode("M906 " + " ".join(f"{k}{int(v)}" for k, v in currents.items()))

    def set_speeds(self, speeds: Dict[str, float]) -> None:
        self.send_gcode("M203 " + " ".join(f"{k}{int(v)}" for k, v in speeds.items()))

    def motors_off(self) -> None:
        self.send_gcode("M18")

    def motors_on(self) -> None:
        self.send_gcode("M17")