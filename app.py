"""Веб-приложение для юстировки оптики шаговыми двигателями (плата Duet).

Сервер Flask проксирует HTTP-запросы к плате Duet, хранит настройки и макросы
и отдаёт веб-интерфейс. Все операции с платой сериализуются одной глобальной
блокировкой, чтобы последовательности G91→G1→G90 не перемешивались.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
from typing import Any, Dict, Iterable, List, Optional, Set

from flask import Flask, abort, jsonify, request, send_from_directory

from duet_client import DuetClient, DuetError, MockDuetClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
MACROS_PATH = os.path.join(BASE_DIR, "macros.json")

AXIS_RE = re.compile(r"^[A-Z]$")
SAFE_RAW_GCODE_RE = re.compile(r"^(G90|G91|M17|M18|M114)$")

DEFAULT_CONFIG = {
    "board": {
        "ip": "192.168.1.100",
        "password": "",
        "mock": True,
        "allow_raw_gcode": False,
        "bind_host": "127.0.0.1",
        "port": 5000,
        "debug": False,
    },
    "axes": [],
    "defaults": {
        "step_mm": 0.1,
        "feedrate_mm_min": 600,
        "macro_feedrate_mm_min": 600,
        "homing_feedrate_mm_min": 300,
        "require_homed_before_motion": True,
        "allow_unverified_homing": False,
        "confirm_macros": True,
        "home_position": {},
    },
}

DEFAULT_STEPS = [
    {"plus": 0.01, "minus": 0.01},
    {"plus": 0.05, "minus": 0.05},
    {"plus": 0.1, "minus": 0.1},
    {"plus": 0.25, "minus": 0.25},
    {"plus": 0.5, "minus": 0.5},
    {"plus": 1.0, "minus": 1.0},
]

app = Flask(__name__, static_folder="static")

# Одна глобальная блокировка сериализует ВСЕ операции с платой и её клиентом.
_op_lock = threading.RLock()
_file_lock = threading.RLock()
_client = None
_position_cache: Dict[str, float] = {}
_position_cache_valid: bool = False
# Оси без концевика, для которых оператор вручную установил опору (G92).
_manual_reference: Set[str] = set()


class ValidationError(Exception):
    """Ошибка валидации пользовательского ввода или конфигурации."""
def load_json(path: str, default: Any) -> Any:
    with _file_lock:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def save_json(path: str, data: Any) -> None:
    """Атомарная запись JSON: временный файл + os.replace."""
    with _file_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def _ensure_number(value: Any, name: str, *, min_value: Optional[float] = None, max_value: Optional[float] = None) -> float:
    """Строгая проверка числа: только bool/int/float, конечное значение."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"Поле '{name}' должно быть числом")
    num = float(value)
    if not math.isfinite(num):
        raise ValidationError(f"Поле '{name}' должно быть конечным числом")
    if min_value is not None and num < min_value:
        raise ValidationError(f"Поле '{name}' должно быть >= {min_value}")
    if max_value is not None and num > max_value:
        raise ValidationError(f"Поле '{name}' должно быть <= {max_value}")
    return num


def _ensure_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    raise ValidationError(f"Поле '{name}' должно быть логическим (true/false)")


def _validate_steps(steps_src: Any, max_step: float, idx: int) -> List[Dict[str, float]]:
    if not isinstance(steps_src, list):
        raise ValidationError(f"axes[{idx}].steps должен быть списком")
    if not 1 <= len(steps_src) <= 6:
        raise ValidationError(f"axes[{idx}].steps должен содержать от 1 до 6 элементов")
    steps: List[Dict[str, float]] = []
    for s_idx, step in enumerate(steps_src):
        if not isinstance(step, dict):
            raise ValidationError(f"axes[{idx}].steps[{s_idx}] должен быть объектом")
        plus = _ensure_number(step.get("plus"), f"axes[{idx}].steps[{s_idx}].plus", min_value=0.0001, max_value=max_step)
        minus = _ensure_number(step.get("minus"), f"axes[{idx}].steps[{s_idx}].minus", min_value=0.0001, max_value=max_step)
        steps.append({"plus": plus, "minus": minus})
    return steps


def _validate_ip_or_host(value: Any) -> str:
    value = str(value).strip()
    if not value:
        raise ValidationError("IP/hostname платы не должен быть пустым")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValidationError("IP/hostname платы содержит недопустимые символы")
        return value
def validate_config(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValidationError("Конфигурация должна быть JSON-объектом")

    board_src = data.get("board", {}) if isinstance(data.get("board", {}), dict) else {}
    defaults_src = data.get("defaults", {}) if isinstance(data.get("defaults", {}), dict) else {}
    axes_src = data.get("axes", [])
    if not isinstance(axes_src, list):
        raise ValidationError("Поле 'axes' должно быть списком")

    board = dict(DEFAULT_CONFIG["board"])
    board["ip"] = _validate_ip_or_host(board_src.get("ip", board["ip"]))
    board["password"] = str(board_src.get("password", ""))
    board["mock"] = _ensure_bool(board_src.get("mock", board["mock"]), "board.mock")
    board["allow_raw_gcode"] = _ensure_bool(board_src.get("allow_raw_gcode", board["allow_raw_gcode"]), "board.allow_raw_gcode")
    board["bind_host"] = str(board_src.get("bind_host", board["bind_host"]))
    board["port"] = int(_ensure_number(board_src.get("port", board["port"]), "board.port", min_value=1, max_value=65535))
    board["debug"] = _ensure_bool(board_src.get("debug", board["debug"]), "board.debug")

    defaults = dict(DEFAULT_CONFIG["defaults"])
    defaults["step_mm"] = _ensure_number(defaults_src.get("step_mm", defaults["step_mm"]), "defaults.step_mm", min_value=0.0001, max_value=100)
    defaults["feedrate_mm_min"] = _ensure_number(defaults_src.get("feedrate_mm_min", defaults["feedrate_mm_min"]), "defaults.feedrate_mm_min", min_value=1, max_value=50000)
    defaults["macro_feedrate_mm_min"] = _ensure_number(defaults_src.get("macro_feedrate_mm_min", defaults["macro_feedrate_mm_min"]), "defaults.macro_feedrate_mm_min", min_value=1, max_value=50000)
    defaults["homing_feedrate_mm_min"] = _ensure_number(defaults_src.get("homing_feedrate_mm_min", defaults["homing_feedrate_mm_min"]), "defaults.homing_feedrate_mm_min", min_value=1, max_value=50000)
    defaults["require_homed_before_motion"] = _ensure_bool(defaults_src.get("require_homed_before_motion", defaults["require_homed_before_motion"]), "defaults.require_homed_before_motion")
    defaults["allow_unverified_homing"] = _ensure_bool(defaults_src.get("allow_unverified_homing", defaults["allow_unverified_homing"]), "defaults.allow_unverified_homing")
    defaults["confirm_macros"] = _ensure_bool(defaults_src.get("confirm_macros", defaults["confirm_macros"]), "defaults.confirm_macros")

    axes: List[Dict[str, Any]] = []
    seen_letters = set()
    for idx, item in enumerate(axes_src):
        if not isinstance(item, dict):
            raise ValidationError(f"axes[{idx}] должен быть объектом")
        letter = str(item.get("letter", "")).strip().upper()
        if not AXIS_RE.fullmatch(letter):
            raise ValidationError(f"axes[{idx}].letter должен быть одной заглавной буквой")
        if letter in seen_letters:
            raise ValidationError(f"Ось '{letter}' встречается более одного раза")
        seen_letters.add(letter)
        axis: Dict[str, Any] = {
            "letter": letter,
            "name": str(item.get("name", letter)).strip(),
            "current_mA": _ensure_number(item.get("current_mA", 1000), f"axes[{idx}].current_mA", min_value=0, max_value=5000),
            "speed_mm_min": _ensure_number(item.get("speed_mm_min", 600), f"axes[{idx}].speed_mm_min", min_value=1, max_value=50000),
            "max_step_mm": _ensure_number(item.get("max_step_mm", 1.0), f"axes[{idx}].max_step_mm", min_value=0.0001, max_value=1000),
            "allow_absolute": _ensure_bool(item.get("allow_absolute", True), f"axes[{idx}].allow_absolute"),
            "require_homed": _ensure_bool(item.get("require_homed", True), f"axes[{idx}].require_homed"),
            "has_endstop": _ensure_bool(item.get("has_endstop", False), f"axes[{idx}].has_endstop"),
        }
        if "min_position" in item:
            axis["min_position"] = _ensure_number(item["min_position"], f"axes[{idx}].min_position")
        if "max_position" in item:
            axis["max_position"] = _ensure_number(item["max_position"], f"axes[{idx}].max_position")
        if "min_position" in axis and "max_position" in axis and axis["min_position"] > axis["max_position"]:
            raise ValidationError(f"Для оси '{letter}' min_position не может быть больше max_position")
        axis["steps"] = _validate_steps(item.get("steps", DEFAULT_STEPS), axis["max_step_mm"], idx)
        axes.append(axis)

    home_src = defaults_src.get("home_position", {})
    if not isinstance(home_src, dict):
        raise ValidationError("Поле 'defaults.home_position' должно быть объектом")
    home_position: Dict[str, float] = {letter: 0.0 for letter in seen_letters}
    for key, value in home_src.items():
        axis = str(key).strip().upper()
        if axis not in seen_letters:
            raise ValidationError(f"defaults.home_position содержит неизвестную ось '{axis}'")
        home_position[axis] = _ensure_number(value, f"defaults.home_position.{axis}")
    defaults["home_position"] = home_position

    return {"board": board, "axes": axes, "defaults": defaults}
def validate_macros(data: Any, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(data, list):
        raise ValidationError("Макросы должны быть списком")
    axis_map = get_axis_map(config)
    seen_names = set()
    result: List[Dict[str, Any]] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValidationError(f"macros[{idx}] должен быть объектом")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValidationError(f"macros[{idx}].name не должен быть пустым")
        if name in seen_names:
            raise ValidationError(f"Макрос '{name}' встречается более одного раза")
        seen_names.add(name)
        position_src = item.get("position", {})
        if not isinstance(position_src, dict) or not position_src:
            raise ValidationError(f"macros[{idx}].position должен быть непустым объектом")
        position: Dict[str, float] = {}
        for raw_axis, raw_value in position_src.items():
            axis = normalize_axis(raw_axis, config)
            if not axis_map[axis].get("allow_absolute", True):
                raise ValidationError(f"Ось '{axis}' запрещена для абсолютных макросов")
            value = _ensure_number(raw_value, f"macros[{idx}].position.{axis}")
            _validate_position_limits(axis_map[axis], value)
            position[axis] = value
        result.append({"name": name, "position": position})
    return result


def load_config() -> Dict[str, Any]:
    raw = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    config = validate_config(raw)
    if raw != config:
        save_json(CONFIG_PATH, config)
    return config


def load_macros() -> List[Dict[str, Any]]:
    return validate_macros(load_json(MACROS_PATH, []), load_config())


def get_axis_map(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {axis["letter"]: axis for axis in config.get("axes", [])}


def normalize_axis(raw_axis: Any, config: Dict[str, Any]) -> str:
    axis = str(raw_axis).strip().upper()
    if not AXIS_RE.fullmatch(axis):
        raise ValidationError("Ось должна быть одной заглавной буквой")
    axis_map = get_axis_map(config)
    if axis not in axis_map:
        raise ValidationError(f"Ось '{axis}' отсутствует в конфигурации")
    return axis


def _validate_position_limits(axis_cfg: Dict[str, Any], value: float) -> None:
    if "min_position" in axis_cfg and value < axis_cfg["min_position"]:
        raise ValidationError(f"Значение для оси '{axis_cfg['letter']}' меньше min_position")
    if "max_position" in axis_cfg and value > axis_cfg["max_position"]:
        raise ValidationError(f"Значение для оси '{axis_cfg['letter']}' больше max_position")


def _validate_feedrate(axis_map: Dict[str, Dict[str, Any]], axes: Iterable[str], rate: float, label: str) -> None:
    for axis in axes:
        limit = axis_map[axis].get("speed_mm_min")
        if limit is not None and rate > limit:
            raise ValidationError(f"Скорость {label} ({rate:.0f} мм/мин) превышает предел оси '{axis}' ({limit:.0f} мм/мин)")
def _build_absolute_move(position: Dict[str, float], config: Dict[str, Any],
                         check_limits: bool = True, feedrate: Optional[float] = None) -> List[str]:
    """Собирает G-команды абсолютного перемещения (G90 + G1) в заданную позицию."""
    axis_map = get_axis_map(config)
    parts = []
    for raw_axis, value in position.items():
        axis = normalize_axis(raw_axis, config)
        if not axis_map[axis].get("allow_absolute", True):
            raise ValidationError(f"Ось '{axis}' запрещена для абсолютных перемещений")
        value = _ensure_number(value, f"position.{axis}")
        if check_limits:
            _validate_position_limits(axis_map[axis], value)
        parts.append(f"{axis}{value:.4f}")
    if not parts:
        raise ValidationError("Нет осей для перемещения")
    rate = feedrate if feedrate is not None else config["defaults"]["macro_feedrate_mm_min"]
    return ["G90", f"G1 {' '.join(parts)} F{rate:.0f}"]


def _home_commands(config: Dict[str, Any], requested_axes: List[str], client: Any = None) -> tuple:
    """Собирает команды хоуминга.

    - оси с концевиком: G28 — физическое установление опоры;
    - оси без концевика: goto сохранённой позиции (НЕ установление опоры,
      требует уже установленной ручной опоры и достоверной позиции).
    """
    axis_map = get_axis_map(config)
    requested_axes = [normalize_axis(a, config) for a in requested_axes]
    endstop_axes = [a for a in requested_axes if axis_map[a].get("has_endstop", False)]
    goto_axes = [a for a in requested_axes if not axis_map[a].get("has_endstop", False)]
    commands: List[str] = []
    if endstop_axes:
        rate = config["defaults"].get("homing_feedrate_mm_min", 300)
        commands.append(f"G28 {' '.join(endstop_axes)} F{rate:.0f}")
    if goto_axes:
        # goto — абсолютное перемещение: опора и позиция должны быть известны.
        if client is not None:
            ensure_homing_allowed(client, config, goto_axes)
            client.get_position()  # подтвердить достоверность позиции
        home = config["defaults"].get("home_position", {})
        goto_target: Dict[str, float] = {}
        for a in goto_axes:
            target = float(home.get(a, 0.0))
            _validate_position_limits(axis_map[a], target)
            goto_target[a] = target
        rate = config["defaults"].get("homing_feedrate_mm_min", 300)
        _validate_feedrate(axis_map, goto_axes, rate, "перехода")
        commands += _build_absolute_move(goto_target, config, check_limits=True, feedrate=rate)
    return commands, endstop_axes, goto_axes


def ensure_homing_allowed(client: Any, config: Dict[str, Any], axes: Iterable[str]) -> None:
    checked_axes = list(axes)
    axis_map = get_axis_map(config)
    required_axes = [axis for axis in checked_axes if axis_map[axis].get("require_homed", True)]
    if not required_axes or not config["defaults"].get("require_homed_before_motion", True):
        return
    endstop_required = [axis for axis in required_axes if axis_map[axis].get("has_endstop", False)]
    manual_required = [axis for axis in required_axes if not axis_map[axis].get("has_endstop", False)]
    if endstop_required:
        homed = client.get_homed_axes()
        if homed is None:
            # Неизвестно — разрешаем только при явном allow_unverified_homing.
            if config["defaults"].get("allow_unverified_homing", False):
                pass
            else:
                raise ValidationError("Не удалось проверить выполнение команды Home для осей. Либо выполните команду Home, либо явно разрешите движение без проверки (allow_unverified_homing)")
        else:
            missing = [axis for axis in endstop_required if axis not in homed]
            if missing:
                raise ValidationError(f"Оси {', '.join(missing)} не прошли поиск начальной точки (команда Home)")
    if manual_required:
        missing = [axis for axis in manual_required if axis not in _manual_reference]
        if missing:
            raise ValidationError(
                f"Оси {', '.join(missing)} не имеют заданной нулевой точки — "
                "задайте её вручную (кнопка «Задать нулевую точку»)"
            )


def _validate_relative_target(axis_cfg: Dict[str, Any], pos: Dict[str, float], axis: str, delta: float) -> None:
    if "min_position" not in axis_cfg and "max_position" not in axis_cfg:
        return
    if axis not in pos:
        raise DuetError(f"Текущая позиция оси '{axis}' неизвестна — невозможно проверить пределы")
    current = pos[axis]
    if not isinstance(current, (int, float)) or isinstance(current, bool) or not math.isfinite(float(current)):
        raise DuetError(f"Текущая позиция оси '{axis}' некорректна")
    target = float(current) + delta
    if "min_position" in axis_cfg and target < axis_cfg["min_position"] - 1e-9:
        raise ValidationError(f"Целевая координата {target:.4f} для оси '{axis}' меньше min_position={axis_cfg['min_position']}")
    if "max_position" in axis_cfg and target > axis_cfg["max_position"] + 1e-9:
        raise ValidationError(f"Целевая координата {target:.4f} для оси '{axis}' больше max_position={axis_cfg['max_position']}")
def get_client() -> Any:
    global _client
    with _op_lock:
        if _client is None:
            board = load_config().get("board", {})
            if board.get("mock", False):
                _client = MockDuetClient()
            else:
                _client = DuetClient(board.get("ip", ""), board.get("password", ""))
        return _client


def reset_client() -> None:
    global _client, _position_cache_valid, _manual_reference
    with _op_lock:
        _client = None
        _position_cache.clear()
        _position_cache_valid = False
        _manual_reference.clear()


def _ensure_motion_confirmed(client: Any) -> None:
    """Блокирует движение, если состояние не подтверждено (BLOCKER-2).

    Неподтверждённое движение не «прощается» автоматически: до явного
    восстановления (POST /api/recover) state-changing операции отклоняются.
    """
    if getattr(client, "motion_unconfirmed", False) or getattr(client, "inconsistent", False):
        raise DuetError("Состояние движения не подтверждено — требуется явное восстановление (POST /api/recover)")


def _run_motion_sequence(client: Any, commands: List[str]) -> None:
    """Отправляет последовательность команд; при сбое пытается восстановить G90."""
    try:
        client.send_gcodes(commands)
    except DuetError as exc:
        recovered = False
        try:
            client.send_gcode("G90")
            recovered = True
        except DuetError:
            pass
        if not recovered:
            client.inconsistent = True
            raise DuetError(f"{exc}; также не удалось восстановить абсолютный режим (G90) — переподключитесь") from exc
        raise DuetError(f"{exc}; абсолютный режим (G90) восстановлен") from exc


def _json_object_body() -> Dict[str, Any]:
    """Читает тело запроса как JSON-объект: 415 для не-JSON, 400 для не-объекта."""
    if not request.is_json:
        abort(415, description="Требуется Content-Type: application/json")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Тело запроса должно быть JSON-объектом")
    return data


def _json_body() -> Any:
    """Читает тело запроса как JSON (объект или список): 415 для не-JSON."""
    if not request.is_json:
        abort(415, description="Требуется Content-Type: application/json")
    return request.get_json(silent=True)


@app.errorhandler(415)
def _handle_unsupported_media_type(error):
    return jsonify({"ok": False, "error": "Требуется Content-Type: application/json"}), 415


@app.before_request
def _reject_cross_origin_mutations():
    """Минимальная защита изменяющих API от cross-site запросов по Origin/Referer."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return None
    origin_host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    request_host = (request.host or "").split(":", 1)[0]
    if origin_host and request_host and origin_host.lower() != request_host.lower():
        return jsonify({"ok": False, "error": "Запрос с недоверенного источника (Origin) отклонён"}), 403
    return None


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    try:
        if request.method == "GET":
            return jsonify(load_config())
        data = _json_object_body()
        config = validate_config(data)
        with _op_lock:
            save_json(CONFIG_PATH, config)
            reset_client()
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global _client
    try:
        data = _json_object_body()
        config = load_config()
        board = config.get("board", {})
        with _op_lock:
            reset_client()
            if board.get("mock", False):
                client = get_client()
                return jsonify({"ok": True, "mock": True, "firmware": client.connect()})
            ip = _validate_ip_or_host(data.get("ip", board.get("ip")))
            _client = DuetClient(ip, board.get("password", ""))
            major = _client.connect()
            return jsonify({"ok": True, "mock": False, "firmware": major})
    except ValidationError as exc:
        reset_client()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        reset_client()
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/recover", methods=["POST"])
def api_recover():
    """Явное восстановление после неподтверждённого движения/сбоя (BLOCKER-2).

    Требует подтверждения оператора; выполняет жёсткую проверку: принудительный
    G90 + чтение позиции. Только при успехе снимает motion_unconfirmed/inconsistent.
    """
    try:
        data = _json_object_body()
        confirm = _ensure_bool(data.get("confirm", False), "confirm")
        if not confirm:
            return jsonify({"ok": False, "error": "Для восстановления требуется подтверждение (confirm=true)"}), 409
        with _op_lock:
            client = get_client()
            client.recover()
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/move", methods=["POST"])
def api_move():
    try:
        data = _json_object_body()
        config = load_config()
        axis = normalize_axis(data.get("axis"), config)
        direction = _ensure_number(data.get("direction", 1), "direction")
        if direction not in (-1.0, 1.0):
            raise ValidationError("direction должен быть равен 1 или -1")
        step = _ensure_number(data.get("step", config["defaults"]["step_mm"]), "step", min_value=0.0001)
        axis_cfg = get_axis_map(config)[axis]
        if step > axis_cfg["max_step_mm"]:
            raise ValidationError(f"Шаг для оси '{axis}' превышает max_step_mm={axis_cfg['max_step_mm']}")
        feedrate = _ensure_number(data.get("feedrate", config["defaults"]["feedrate_mm_min"]), "feedrate", min_value=1)
        if feedrate > axis_cfg["speed_mm_min"]:
            raise ValidationError(f"Скорость для оси '{axis}' превышает разрешённую {axis_cfg['speed_mm_min']} мм/мин")
        with _op_lock:
            client = get_client()
            _ensure_motion_confirmed(client)
            ensure_homing_allowed(client, config, [axis])
            pos = client.get_position()
            _validate_relative_target(axis_cfg, pos, axis, direction * step)
            commands = ["G91", f"G1 {axis}{direction * step:.4f} F{feedrate:.0f}", "G90"]
            _run_motion_sequence(client, commands)
        return jsonify({"ok": True, "commands": commands})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/position")
def api_position():
    global _position_cache_valid
    try:
        with _op_lock:
            pos = get_client().get_position()
            _position_cache.clear()
            _position_cache.update(pos)
            _position_cache_valid = True
        return jsonify({"ok": True, "position": dict(_position_cache), "stale": False})
    except DuetError as exc:
        with _op_lock:
            cached = dict(_position_cache) if _position_cache_valid else {}
            stale = _position_cache_valid
        return jsonify({"ok": False, "error": str(exc), "position": cached, "stale": stale}), 502
@app.route("/api/gcode", methods=["POST"])
def api_gcode():
    try:
        data = _json_object_body()
        config = load_config()
        if not config["board"].get("allow_raw_gcode", False):
            return jsonify({"ok": False, "error": "Raw G-code endpoint отключён в конфигурации"}), 403
        code = str(data.get("code", "")).strip().upper()
        if not SAFE_RAW_GCODE_RE.fullmatch(code):
            raise ValidationError("Разрешены только безопасные одиночные команды: G90, G91, M17, M18, M114")
        with _op_lock:
            client = get_client()
            _ensure_motion_confirmed(client)
            client.send_gcode(code)
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/settings/apply", methods=["POST"])
def api_settings_apply():
    try:
        data = _json_object_body()
        config = load_config()
        axis_map = get_axis_map(config)
        currents: Dict[str, float] = {}
        currents_src = data.get("currents", {})
        if not isinstance(currents_src, dict):
            raise ValidationError("Поле 'currents' должно быть объектом")
        for raw_axis, raw_value in currents_src.items():
            axis = normalize_axis(raw_axis, config)
            currents[axis] = _ensure_number(raw_value, f"currents.{axis}", min_value=0, max_value=5000)
        speeds: Dict[str, float] = {}
        speeds_src = data.get("speeds", {})
        if not isinstance(speeds_src, dict):
            raise ValidationError("Поле 'speeds' должно быть объектом")
        for raw_axis, raw_value in speeds_src.items():
            axis = normalize_axis(raw_axis, config)
            speed = _ensure_number(raw_value, f"speeds.{axis}", min_value=1, max_value=50000)
            if speed > axis_map[axis]["speed_mm_min"]:
                raise ValidationError(f"Скорость для оси '{axis}' превышает разрешённую {axis_map[axis]['speed_mm_min']} мм/мин")
            speeds[axis] = speed
        with _op_lock:
            client = get_client()
            _ensure_motion_confirmed(client)
            if currents:
                client.set_currents(currents)
            if speeds:
                client.set_speeds(speeds)
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/macros", methods=["GET", "POST"])
def api_macros():
    try:
        if request.method == "GET":
            return jsonify(load_macros())
        data = _json_body()
        macros = validate_macros(data, load_config())
        with _op_lock:
            save_json(MACROS_PATH, macros)
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/macros/run", methods=["POST"])
def api_macro_run():
    try:
        data = _json_object_body()
        name = str(data.get("name", "")).strip()
        confirm = _ensure_bool(data.get("confirm", False), "confirm")
        config = load_config()
        macros = load_macros()
        with _op_lock:
            for macro in macros:
                if macro["name"] == name:
                    if config["defaults"].get("confirm_macros", True) and not confirm:
                        return jsonify({"ok": False, "error": "Для запуска макроса требуется подтверждение"}), 409
                    axes = list(macro["position"].keys())
                    client = get_client()
                    _ensure_motion_confirmed(client)
                    ensure_homing_allowed(client, config, axes)
                    client.get_position()
                    _validate_feedrate(get_axis_map(config), axes, config["defaults"]["macro_feedrate_mm_min"], "макроса")
                    commands = _build_absolute_move(macro["position"], config, check_limits=True)
                    _run_motion_sequence(client, commands)
                    return jsonify({"ok": True, "commands": commands})
            return jsonify({"ok": False, "error": "Макрос не найден"}), 404
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
@app.route("/api/home", methods=["POST"])
def api_home():
    """Хоуминг: G28 для осей с концевиками, goto сохранённой позиции для остальных."""
    try:
        data = _json_object_body()
        config = load_config()
        requested = data.get("axes") if isinstance(data.get("axes"), list) else list(get_axis_map(config).keys())
        with _op_lock:
            client = get_client()
            _ensure_motion_confirmed(client)
            commands, endstop_axes, goto_axes = _home_commands(config, requested, client)
            _run_motion_sequence(client, commands)
        return jsonify({"ok": True, "commands": commands, "endstop_axes": endstop_axes, "goto_axes": goto_axes})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/set_position", methods=["POST"])
def api_set_position():
    """Ручная установка опоры/текущей позиции для осей БЕЗ концевика (G92, BLOCKER-4).

    Тело: {"confirm": true, "positions": {"X": 0.0}} или {"confirm": true, "axes": ["X"]}.
    G92 задаёт координату без движения и без homed-флага; опора хранится в памяти
    и сбрасывается при переподключении/перезапуске.
    """
    global _manual_reference
    try:
        data = _json_object_body()
        confirm = _ensure_bool(data.get("confirm", False), "confirm")
        if not confirm:
            return jsonify({"ok": False, "error": "Для задания нулевой точки требуется подтверждение (confirm=true)"}), 409
        config = load_config()
        axis_map = get_axis_map(config)
        positions: Dict[str, float] = {}
        positions_src = data.get("positions")
        axes_src = data.get("axes")
        if isinstance(positions_src, dict):
            for raw_axis, raw_value in positions_src.items():
                axis = normalize_axis(raw_axis, config)
                if axis_map[axis].get("has_endstop", False):
                    raise ValidationError(f"Ось '{axis}' имеет концевик — используйте команду Home (G28)")
                positions[axis] = _ensure_number(raw_value, f"positions.{axis}")
        elif isinstance(axes_src, list):
            for raw_axis in axes_src:
                axis = normalize_axis(raw_axis, config)
                if axis_map[axis].get("has_endstop", False):
                    raise ValidationError(f"Ось '{axis}' имеет концевик — используйте команду Home (G28)")
                positions[axis] = 0.0
        else:
            raise ValidationError("Укажите 'positions' (объект ось->значение) или 'axes' (список осей для нуля)")
        with _op_lock:
            client = get_client()
            _ensure_motion_confirmed(client)
            for axis, value in positions.items():
                client.send_gcode(f"G92 {axis}{value:.4f}")
            _manual_reference.update(positions.keys())
        return jsonify({"ok": True, "positions": positions})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Подготовка к выключению платы: вернуть в начальную точку или запомнить текущую позицию."""
    try:
        data = _json_object_body()
        mode = str(data.get("mode", "")).strip()
        if mode not in ("go_home", "keep_position"):
            raise ValidationError("mode должен быть 'go_home' или 'keep_position'")
        config = load_config()
        with _op_lock:
            client = get_client()
            _ensure_motion_confirmed(client)
            if mode == "go_home":
                commands, _, _ = _home_commands(config, list(get_axis_map(config).keys()), client)
                commands.append("M18")
                _run_motion_sequence(client, commands)
                return jsonify({"ok": True, "mode": mode, "commands": commands})
            # keep_position: свежий полный набор координат обязателен, затем M18.
            axis_map = get_axis_map(config)
            position = client.get_position()
            new_home: Dict[str, float] = {}
            for axis in axis_map:
                if axis not in position:
                    raise DuetError(f"Нет координаты оси '{axis}' — сохранение новой начальной точки отменено")
                value = position[axis]
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                    raise DuetError(f"Координата оси '{axis}' некорректна — сохранение отменено")
                new_home[axis] = float(value)
            old_home = dict(config["defaults"].get("home_position", {}))
            original_macros = load_macros()
            macros = [m for m in original_macros if m.get("name") != "предыдущая начальная точка"]
            if old_home:
                macros.append({"name": "предыдущая начальная точка", "position": old_home})
            save_json(MACROS_PATH, macros)
            config["defaults"]["home_position"] = new_home
            save_json(CONFIG_PATH, config)
            try:
                client.motors_off()
            except DuetError:
                # Откат при частичном сбое.
                save_json(MACROS_PATH, original_macros)
                config["defaults"]["home_position"] = old_home
                save_json(CONFIG_PATH, config)
                raise
            return jsonify({"ok": True, "mode": mode, "new_home": new_home, "previous_home_macro": "предыдущая начальная точка"})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


if __name__ == "__main__":
    config = load_config()
    board = config["board"]
    if board["bind_host"] not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"ВНИМАНИЕ: сервер слушает {board['bind_host']}:{board['port']} без проверки API-ключа. "
            "Ограничьте доступ файрволом или настройте Origin/пароль."
        )
    app.run(host=board["bind_host"], port=board["port"], debug=board["debug"])








