"""Клиент для управления платой Duet (RepRapFirmware) по HTTP.

Поддерживаются два независимых транспорта:

- standalone RRF 2.x/3.x — основной путь: rr_connect, rr_gcode, rr_reply,
  rr_status, rr_model. Версия прошивки читается из данных платы (rr_model).
- DuetSoftwareFramework (внешний SBC) — опционально: /machine/connect,
  /machine/code (text/plain + X-Session-Key), /machine/status, /machine/model.

Команды, меняющие состояние платы, отправляются ровно один раз; при потере
ответа результат помечается как неподтверждённый.
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set

import requests


class DuetError(Exception):
    """Ошибка общения с платой Duet."""


class DuetAuthError(DuetError):
    """Ошибка авторизации (HTTP 401) — потеряна сессия или неверный пароль."""


class DuetBusyError(DuetError):
    """Плата занята (HTTP 503)."""


PROTOCOL_RRF = "rrf"
PROTOCOL_SBC = "sbc"

# Команды, меняющие состояние платы: их нельзя автоматически повторять.
_MUTATING_PREFIXES = ("G1", "G28", "G90", "G91", "G92", "M17", "M18", "M906", "M203")


def _is_mutating_gcode(code: str) -> bool:
    head = code.strip().upper()
    return any(head.startswith(p) for p in _MUTATING_PREFIXES)
class DuetClient:
    """Обёртка над HTTP API платы Duet."""

    def __init__(self, ip: str, password: str = "", port: int = 80, timeout: float = 5.0, session=None):
        self.ip = ip
        self.password = password
        self.port = port
        self.timeout = timeout
        self.base = f"http://{ip}:{port}"
        self.protocol: Optional[str] = None
        self.major: Optional[int] = None
        self.firmware_name: Optional[str] = None
        self.firmware_version: Optional[str] = None
        self.session_key: Optional[str] = None
        self.motion_unconfirmed: bool = False
        self.inconsistent: bool = False
        self.session = session if session is not None else requests.Session()

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 headers: Optional[dict] = None, data: Any = None,
                 mutating: bool = False) -> str:
        """HTTP-запрос. Чтения повторяются ограниченно; мутации — ровно один раз."""
        retries = 1 if mutating else 3
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                response = self.session.request(
                    method,
                    self.base + path,
                    params=params,
                    headers=headers,
                    data=data,
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
                if mutating:
                    # Потеря ответа на изменяющую команду не означает отказ платы;
                    # повторять команду нельзя.
                    break
                if attempt < retries - 1:
                    time.sleep(0.2 * (attempt + 1))
        if mutating and isinstance(last_error, requests.RequestException):
            raise DuetError(
                f"Результат команды не подтверждён (соединение потеряно до ответа на {method} {path}): {last_error}"
            )
        raise DuetError(f"HTTP {method} {path} для {self.base} не удался: {last_error}")

    def _get(self, path: str, params: Optional[dict] = None, mutating: bool = False) -> str:
        return self._request("GET", path, params=params, mutating=mutating)
    def connect(self) -> int:
        """Подключается и возвращает мажорную версию прошивки (2/3) или 0, если неизвестна."""
        try:
            if self.password:
                self._get("/rr_connect", params={"password": self.password})
            self._get("/rr_status")
            self.protocol = PROTOCOL_RRF
            self._detect_firmware_rr()
            return self.major if self.major is not None else 0
        except DuetError:
            pass
        try:
            self._connect_sbc()
            self.protocol = PROTOCOL_SBC
            self._detect_firmware_sbc()
            return self.major if self.major is not None else 0
        except DuetError as exc:
            self.protocol = None
            raise DuetError(f"Не удалось подключиться к плате {self.ip}: {exc}") from exc

    def _detect_firmware_rr(self) -> None:
        try:
            self._apply_firmware_info(self._rr_model())
        except DuetError:
            self.major = None
            self.firmware_name = None
            self.firmware_version = None

    def _detect_firmware_sbc(self) -> None:
        try:
            self._apply_firmware_info(self._machine_model())
        except DuetError:
            self.major = None

    def _apply_firmware_info(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        name = data.get("firmwareName") or data.get("firmware")
        version = data.get("firmwareVersion")
        self.firmware_name = str(name) if name else None
        self.firmware_version = str(version) if version else None
        text = " ".join(x for x in (self.firmware_name, self.firmware_version) if x)
        match = re.search(r"\b([23])\.\d", text)
        if match:
            self.major = int(match.group(1))
            return
        match = re.search(r"\b([23])(?:\D|$)", text)
        if match:
            self.major = int(match.group(1))
            return
        self.major = None

    def _connect_sbc(self) -> None:
        params = {"password": self.password} if self.password else None
        raw = self._get("/machine/connect", params=params)
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("sessionKey"):
                self.session_key = str(data["sessionKey"])
        except ValueError:
            pass
        self._get("/machine/status")

    def _rr_model(self) -> Dict[str, Any]:
        raw = self._get("/rr_model")
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _machine_model(self) -> Dict[str, Any]:
        raw = self._get("/machine/model")
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _rr_status_json(self) -> Dict[str, Any]:
        raw = self._get("/rr_status")
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _rr_reply_text(self) -> str:
        raw = self._get("/rr_reply")
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "reply" in data:
                return str(data["reply"])
        except ValueError:
            pass
        return raw
    def _send_rr_gcode_once(self, code: str) -> None:
        """Отправляет G-код через /rr_gcode ровно один раз (без повторов)."""
        self._get("/rr_gcode", params={"gcode": code}, mutating=True)

    def _sbc_send_code(self, code: str) -> None:
        headers = {"Content-Type": "text/plain"}
        if self.session_key:
            headers["X-Session-Key"] = self.session_key
        self._request("POST", "/machine/code", headers=headers, data=code, mutating=True)

    def _wait_idle(self, timeout: Optional[float] = None) -> None:
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        while time.time() < deadline:
            status = self._rr_status_json().get("status")
            if status in ("I", "idle", "O", "H", "A", "D"):
                return
            time.sleep(0.05)
        raise DuetError("Таймаут ожидания завершения операции на плате")

    def _check_reply_errors(self, code: str, reply: str) -> None:
        for line in reply.splitlines():
            stripped = line.strip()
            if "Error:" in stripped or stripped.startswith("Error"):
                raise DuetError(f"Плата вернула ошибку на '{code}': {stripped}")

    def _drain_reply(self) -> None:
        """Сбрасывает накопленные старые reply, чтобы не перепутать их с ответом новой команды."""
        self._rr_reply_text()

    def _rr_send(self, code: str, require_reply: bool = False) -> str:
        """Отправляет G-код по rr_* и возвращает ответ, привязанный именно к этой команде.

        Перед отправкой буфер rr_reply осушается, поэтому прочитанный после
        завершения reply гарантированно относится к нашей команде (или пуст,
        если команда не порождает вывода).
        """
        self._drain_reply()
        self._send_rr_gcode_once(code)
        self._wait_idle()
        reply = self._rr_reply_text()
        self._check_reply_errors(code, reply)
        if require_reply and not reply.strip():
            raise DuetError(f"Ответ на команду '{code}' не получен")
        return reply

    def send_gcode(self, code: str) -> None:
        code = code.strip()
        if not code:
            return
        if self.protocol is None:
            self.connect()
        if self.protocol == PROTOCOL_SBC:
            self._sbc_send_code(code)
            return
        try:
            self._rr_send(code)
        except DuetAuthError:
            # Повтор после 401 — только reconnect + повторное чтение статуса.
            self.major = None
            self.connect()
            self._get("/rr_status")
            if _is_mutating_gcode(code):
                # mutating-команду нельзя повторять вслепую: результат не подтверждён.
                self.motion_unconfirmed = True
                raise DuetError("Команда не подтверждена: получен HTTP 401 после отправки; команда не повторена")
            # чтение (например M114) можно повторить после восстановления сессии
            self._rr_send(code)
        except DuetError as exc:
            if _is_mutating_gcode(code):
                self.motion_unconfirmed = True
            raise

    def send_gcodes(self, codes: Iterable[str]) -> None:
        for code in codes:
            self.send_gcode(code)
    @staticmethod
    def _extract_axes(data: Any) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        move = data.get("move")
        if isinstance(move, dict):
            axes = move.get("axes")
        else:
            axes = data.get("axes")
        if isinstance(axes, list):
            return [a for a in axes if isinstance(a, dict)]
        return []

    @staticmethod
    def _position_from_model(data: Any) -> Dict[str, float]:
        pos: Dict[str, float] = {}
        for ax in DuetClient._extract_axes(data):
            letter = ax.get("letter")
            if not (isinstance(letter, str) and letter):
                continue
            value = ax.get("userPosition")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                pos[letter] = float(value)
        return pos

    def get_position(self) -> Dict[str, float]:
        """Возвращает текущие координаты осей; пустой/неполный ответ — ошибка."""
        if self.protocol is None:
            self.connect()
        if self.protocol == PROTOCOL_SBC:
            pos = self._position_from_model(self._machine_model())
            if pos:
                return pos
            raise DuetError("Позиция неизвестна: /machine/model не содержит координат осей")
        try:
            pos = self._position_from_model(self._rr_model())
        except DuetError:
            pos = {}
        if pos:
            return pos
        reply = self._rr_send("M114", require_reply=True)
        pos = self._parse_m114(reply)
        if not pos:
            raise DuetError("Позиция неизвестна: ответ M114 пуст или некорректен")
        return pos

    def get_homed_axes(self) -> Optional[Set[str]]:
        """Множество homed-осей; None — данные недоступны (неизвестно).

        Пустое множество означает достоверно «ни одна ось не homed».
        """
        if self.protocol is None:
            self.connect()
        try:
            model = self._machine_model() if self.protocol == PROTOCOL_SBC else self._rr_model()
        except DuetError:
            return None
        axes = self._extract_axes(model)
        if not axes:
            return None
        homed: Set[str] = set()
        for ax in axes:
            letter = ax.get("letter")
            if isinstance(letter, str) and letter and ax.get("homed"):
                homed.add(letter)
        return homed

    def resync(self) -> None:
        """Читает текущее состояние платы (НЕ снимает флаги неподтверждённости)."""
        if self.protocol == PROTOCOL_SBC:
            self._get("/machine/status")
            self._machine_model()
        else:
            self._get("/rr_status")
            self._rr_model()

    def recover(self) -> None:
        """Явная процедура восстановления после неподтверждённого движения/сбоя.

        Принудительно возвращает абсолютный режим (G90) и проверяет, что позиция
        читается. Флаги снимаются ТОЛЬКО при полном успехе.
        """
        self.send_gcode("G90")
        self.get_position()
        self.motion_unconfirmed = False
        self.inconsistent = False

    @staticmethod
    def _parse_m114(text: str) -> Dict[str, float]:
        pos: Dict[str, float] = {}
        for token in text.replace(",", " ").split():
            if len(token) >= 2 and ":" in token and token[0] in "XYZUVWABC":
                try:
                    value = float(token.split(":", 1)[1])
                    if math.isfinite(value):
                        pos[token[0]] = value
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
    """Имитация платы для отладки без железа и для регрессионных тестов."""

    def __init__(self, *args, **kwargs):
        self.major = 3
        self.protocol = PROTOCOL_RRF
        self.relative = False
        self.position: Dict[str, float] = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.homed: Set[str] = set()  # реалистично: после включения ничего не homed
        self.motors_enabled: bool = True
        self.limits: Dict[str, tuple] = {}
        self.log: List[str] = []
        self.motion_unconfirmed: bool = False
        self.inconsistent: bool = False
        self.homed_unknown: bool = False
        self.firmware_error: Optional[str] = None
        self._home_position: Dict[str, float] = dict(self.position)

    def connect(self) -> int:
        return self.major

    def set_limits(self, limits: Dict[str, tuple]) -> None:
        self.limits = dict(limits)

    def restart(self) -> None:
        """Имитация перезапуска платы: логическая опора (homed) теряется."""
        self.homed = set()
        self.motors_enabled = True
        self.log.append(";restart")

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
        elif line.startswith("G92"):
            self._apply_g92(line)
        elif line.startswith("G1"):
            self._apply_move(line)
        elif line.startswith(("M114", "M906", "M203", "M17", "M18")):
            if line.startswith("M17"):
                self.motors_enabled = True
            elif line.startswith("M18"):
                self.motors_enabled = False
        else:
            raise DuetError(f"Error: unknown command {line}")

    def send_gcodes(self, codes: Iterable[str]) -> None:
        for code in codes:
            self.send_gcode(code)

    def _apply_move(self, line: str) -> None:
        for match in re.finditer(r"([A-Z])(-?\d+(?:\.\d+)?)", line):
            axis, val = match.group(1), float(match.group(2))
            if axis not in self.position:
                continue
            if not self.motors_enabled:
                raise DuetError(f"Error: Motors off for axis {axis}")
            if self.firmware_error:
                err = self.firmware_error
                self.firmware_error = None
                raise DuetError(err)
            target = (self.position.get(axis, 0.0) + val) if self.relative else val
            lo, hi = self.limits.get(axis, (None, None))
            if lo is not None and target < lo - 1e-9:
                raise DuetError(f"Error: Target position below minimum for axis {axis}")
            if hi is not None and target > hi + 1e-9:
                raise DuetError(f"Error: Target position above maximum for axis {axis}")
            self.position[axis] = target

    def _apply_home(self, line: str) -> None:
        axes = re.findall(r"[XYZUVWABC]", line)
        targets = [a for a in axes if a in self.position] if axes else list(self.position.keys())
        for axis in targets:
            self.position[axis] = self._home_position.get(axis, 0.0)
            self.homed.add(axis)

    def _apply_g92(self, line: str) -> None:
        """G92 задаёт текущую позицию без движения и БЕЗ установки homed-флага."""
        for match in re.finditer(r"([A-Z])(-?\d+(?:\.\d+)?)", line):
            axis, val = match.group(1), float(match.group(2))
            if axis in self.position:
                self.position[axis] = val

    def get_position(self) -> Dict[str, float]:
        return dict(self.position)

    def get_homed_axes(self) -> Optional[Set[str]]:
        if self.homed_unknown:
            return None
        return set(self.homed)

    def resync(self) -> None:
        """Читает состояние (НЕ снимает флаги неподтверждённости)."""
        # У mock состояние уже локальное; явно ничего не сбрасываем.

    def recover(self) -> None:
        """Явное восстановление: G90 + проверка позиции, затем снятие флагов."""
        self.send_gcode("G90")
        self.get_position()
        self.motion_unconfirmed = False
        self.inconsistent = False

    def set_currents(self, currents: Dict[str, float]) -> None:
        self.send_gcode("M906 " + " ".join(f"{k}{int(v)}" for k, v in currents.items()))

    def set_speeds(self, speeds: Dict[str, float]) -> None:
        self.send_gcode("M203 " + " ".join(f"{k}{int(v)}" for k, v in speeds.items()))

    def motors_off(self) -> None:
        self.send_gcode("M18")

    def motors_on(self) -> None:
        self.send_gcode("M17")





