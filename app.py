"""Веб-приложение для юстировки оптики шаговыми двигателями (плата Duet).

Сервер Flask проксирует HTTP-запросы к плате Duet по её IP,
хранит настройки и макросы и отдаёт веб-интерфейс.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from typing import Any, Dict, Iterable, List, Optional

from flask import Flask, jsonify, request, send_from_directory

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

_lock = threading.Lock()
_client = None
_position_cache: Dict[str, float] = {}


class ValidationError(Exception):
    """Ошибка валидации пользовательского ввода или конфигурации."""


def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _ensure_number(value: Any, name: str, *, min_value: Optional[float] = None, max_value: Optional[float] = None) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Поле '{name}' должно быть числом") from exc
    if min_value is not None and num < min_value:
        raise ValidationError(f"Поле '{name}' должно быть >= {min_value}")
    if max_value is not None and num > max_value:
        raise ValidationError(f"Поле '{name}' должно быть <= {max_value}")
    return num


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


def _validate_ip_or_host(value: str) -> str:
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
    board["mock"] = bool(board_src.get("mock", board["mock"]))
    board["allow_raw_gcode"] = bool(board_src.get("allow_raw_gcode", board["allow_raw_gcode"]))
    board["bind_host"] = str(board_src.get("bind_host", board["bind_host"]))
    board["port"] = int(_ensure_number(board_src.get("port", board["port"]), "board.port", min_value=1, max_value=65535))
    board["debug"] = bool(board_src.get("debug", board["debug"]))

    defaults = dict(DEFAULT_CONFIG["defaults"])
    defaults["step_mm"] = _ensure_number(defaults_src.get("step_mm", defaults["step_mm"]), "defaults.step_mm", min_value=0.0001, max_value=100)
    defaults["feedrate_mm_min"] = _ensure_number(defaults_src.get("feedrate_mm_min", defaults["feedrate_mm_min"]), "defaults.feedrate_mm_min", min_value=1, max_value=50000)
    defaults["macro_feedrate_mm_min"] = _ensure_number(defaults_src.get("macro_feedrate_mm_min", defaults["macro_feedrate_mm_min"]), "defaults.macro_feedrate_mm_min", min_value=1, max_value=50000)
    defaults["homing_feedrate_mm_min"] = _ensure_number(defaults_src.get("homing_feedrate_mm_min", defaults["homing_feedrate_mm_min"]), "defaults.homing_feedrate_mm_min", min_value=1, max_value=50000)
    defaults["require_homed_before_motion"] = bool(defaults_src.get("require_homed_before_motion", defaults["require_homed_before_motion"]))
    defaults["allow_unverified_homing"] = bool(defaults_src.get("allow_unverified_homing", defaults["allow_unverified_homing"]))
    defaults["confirm_macros"] = bool(defaults_src.get("confirm_macros", defaults["confirm_macros"]))

    axes: List[Dict[str, Any]] = []
    seen_letters = set()
    for idx, item in enumerate(axes_src):
        if not isinstance(item, dict):
            raise ValidationError(f"axes[{idx}] должен быть объектом")
        letter = str(item.get("letter", "")).strip().upper()
        if not AXIS_RE.fullmatch(letter):
            raise ValidationError(f"axes[{idx}].letter должен быть одной заглавной буквой")
        if letter in seen_letters:
            raise ValidationError(f"Ось '{letter}' описана в config.json более одного раза")
        seen_letters.add(letter)
        name = str(item.get("name", letter)).strip() or letter
        max_step = _ensure_number(item.get("max_step_mm", defaults["step_mm"] * 10), f"axes[{idx}].max_step_mm", min_value=0.0001, max_value=100)
        axis = {
            "letter": letter,
            "name": name,
            "current_mA": _ensure_number(item.get("current_mA", 1000), f"axes[{idx}].current_mA", min_value=0, max_value=5000),
            "speed_mm_min": _ensure_number(item.get("speed_mm_min", defaults["feedrate_mm_min"]), f"axes[{idx}].speed_mm_min", min_value=1, max_value=50000),
            "max_step_mm": max_step,
            "steps": _validate_steps(item.get("steps", DEFAULT_STEPS), max_step, idx),
            "allow_absolute": bool(item.get("allow_absolute", True)),
            "require_homed": bool(item.get("require_homed", True)),
            "has_endstop": bool(item.get("has_endstop", False)),
        }
        if "min_position" in item and item["min_position"] not in (None, ""):
            axis["min_position"] = _ensure_number(item["min_position"], f"axes[{idx}].min_position")
        if "max_position" in item and item["max_position"] not in (None, ""):
            axis["max_position"] = _ensure_number(item["max_position"], f"axes[{idx}].max_position")
        if "min_position" in axis and "max_position" in axis and axis["min_position"] > axis["max_position"]:
            raise ValidationError(f"Для оси '{letter}' min_position не может быть больше max_position")
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
    result = []
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
            axis_cfg = axis_map[axis]
            if not axis_cfg.get("allow_absolute", True):
                raise ValidationError(f"Ось '{axis}' запрещена для абсолютных макросов")
            value = _ensure_number(raw_value, f"macros[{idx}].position.{axis}")
            _validate_position_limits(axis_cfg, value)
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
    macros = validate_macros(load_json(MACROS_PATH, []), load_config())
    return macros


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


def _build_absolute_move(position: Dict[str, float], config: Dict[str, Any], check_limits: bool = True) -> List[str]:
    """Собирает G-команды абсолютного перемещения (G90 + G1) в заданную позицию."""
    axis_map = get_axis_map(config)
    parts = []
    for raw_axis, value in position.items():
        axis = normalize_axis(raw_axis, config)
        if not axis_map[axis].get("allow_absolute", True):
            raise ValidationError(f"Ось '{axis}' запрещена для абсолютных перемещений")
        if check_limits:
            _validate_position_limits(axis_map[axis], float(value))
        parts.append(f"{axis}{float(value):.4f}")
    if not parts:
        raise ValidationError("Нет осей для перемещения")
    return ["G90", f"G1 {' '.join(parts)} F{config['defaults']['macro_feedrate_mm_min']:.0f}"]


def _home_commands(config: Dict[str, Any], requested_axes: List[str]) -> tuple:
    """Собирает команды хоуминга: G28 для осей с концевиками, soft home для остальных."""
    axis_map = get_axis_map(config)
    requested_axes = [normalize_axis(a, config) for a in requested_axes]
    endstop_axes = [a for a in requested_axes if axis_map[a].get("has_endstop", False)]
    soft_axes = [a for a in requested_axes if not axis_map[a].get("has_endstop", False)]
    commands: List[str] = []
    if endstop_axes:
        rate = config["defaults"].get("homing_feedrate_mm_min", 300)
        commands.append(f"G28 {' '.join(endstop_axes)} F{rate:.0f}")
    if soft_axes:
        home = config["defaults"].get("home_position", {})
        soft_home = {a: home.get(a, 0.0) for a in soft_axes}
        commands += _build_absolute_move(soft_home, config, check_limits=False)
    return commands, endstop_axes, soft_axes


def ensure_homing_allowed(client: Any, config: Dict[str, Any], axes: Iterable[str]) -> None:
    checked_axes = list(axes)
    required_axes = [axis for axis in checked_axes if get_axis_map(config)[axis].get("require_homed", True)]
    if not required_axes or not config["defaults"].get("require_homed_before_motion", True):
        return
    homed = client.get_homed_axes()
    if homed is None:
        if config["defaults"].get("allow_unverified_homing", False):
            return
        raise ValidationError("Не удалось проверить homing осей. Либо выполните homing, либо явно разрешите allow_unverified_homing")
    missing = [axis for axis in required_axes if axis not in homed]
    if missing:
        raise ValidationError(f"Оси {', '.join(missing)} не находятся в состоянии homed")


def get_client() -> Any:
    global _client
    with _lock:
        if _client is None:
            board = load_config().get("board", {})
            if board.get("mock", False):
                _client = MockDuetClient()
            else:
                _client = DuetClient(board.get("ip", ""), board.get("password", ""))
        return _client


def reset_client() -> None:
    global _client
    with _lock:
        _client = None


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    try:
        if request.method == "GET":
            return jsonify(load_config())
        data = request.get_json(force=True)
        config = validate_config(data)
        save_json(CONFIG_PATH, config)
        reset_client()
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/connect", methods=["POST"])
def api_connect():
    try:
        body = request.get_json(silent=True) or {}
        config = load_config()
        board = config.get("board", {})
        if board.get("mock", False):
            reset_client()
            client = get_client()
            return jsonify({"ok": True, "mock": True, "firmware": client.connect()})
        ip = _validate_ip_or_host(body.get("ip", board.get("ip")))
        with _lock:
            global _client
            _client = DuetClient(ip, board.get("password", ""))
            major = _client.connect()
        return jsonify({"ok": True, "mock": False, "firmware": major})
    except ValidationError as exc:
        reset_client()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        reset_client()
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/move", methods=["POST"])
def api_move():
    try:
        data = request.get_json(force=True)
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
        client = get_client()
        ensure_homing_allowed(client, config, [axis])
        commands = ["G91", f"G1 {axis}{direction * step:.4f} F{feedrate:.0f}", "G90"]
        client.send_gcodes(commands)
        return jsonify({"ok": True, "commands": commands})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/position")
def api_position():
    try:
        pos = get_client().get_position()
        with _lock:
            _position_cache.update(pos)
            cached = dict(_position_cache)
        return jsonify({"ok": True, "position": cached})
    except DuetError as exc:
        with _lock:
            cached = dict(_position_cache)
        return jsonify({"ok": False, "error": str(exc), "position": cached}), 502


@app.route("/api/gcode", methods=["POST"])
def api_gcode():
    try:
        config = load_config()
        if not config["board"].get("allow_raw_gcode", False):
            return jsonify({"ok": False, "error": "Raw G-code endpoint отключён в конфигурации"}), 403
        data = request.get_json(force=True)
        code = str(data.get("code", "")).strip().upper()
        if not SAFE_RAW_GCODE_RE.fullmatch(code):
            raise ValidationError("Разрешены только безопасные одиночные команды: G90, G91, M17, M18, M114")
        get_client().send_gcode(code)
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/settings/apply", methods=["POST"])
def api_settings_apply():
    try:
        data = request.get_json(force=True)
        config = load_config()
        axis_map = get_axis_map(config)
        currents: Dict[str, float] = {}
        for raw_axis, raw_value in (data.get("currents", {}) or {}).items():
            axis = normalize_axis(raw_axis, config)
            currents[axis] = _ensure_number(raw_value, f"currents.{axis}", min_value=0, max_value=5000)
        speeds: Dict[str, float] = {}
        for raw_axis, raw_value in (data.get("speeds", {}) or {}).items():
            axis = normalize_axis(raw_axis, config)
            speed = _ensure_number(raw_value, f"speeds.{axis}", min_value=1, max_value=50000)
            if speed > axis_map[axis]["speed_mm_min"]:
                raise ValidationError(f"Скорость для оси '{axis}' превышает разрешённую {axis_map[axis]['speed_mm_min']} мм/мин")
            speeds[axis] = speed
        client = get_client()
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
        data = request.get_json(force=True)
        macros = validate_macros(data, load_config())
        save_json(MACROS_PATH, macros)
        return jsonify({"ok": True})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/macros/run", methods=["POST"])
def api_macro_run():
    try:
        data = request.get_json(force=True)
        name = str(data.get("name", "")).strip()
        confirm = bool(data.get("confirm", False))
        config = load_config()
        macros = load_macros()
        for macro in macros:
            if macro["name"] == name:
                if config["defaults"].get("confirm_macros", True) and not confirm:
                    return jsonify({"ok": False, "error": "Для запуска абсолютного макроса требуется подтверждение"}), 409
                axes = list(macro["position"].keys())
                client = get_client()
                ensure_homing_allowed(client, config, axes)
                commands = _build_absolute_move(macro["position"], config)
                client.send_gcodes(commands)
                return jsonify({"ok": True, "commands": commands})
        return jsonify({"ok": False, "error": "Макрос не найден"}), 404
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/home", methods=["POST"])
def api_home():
    """Хоуминг: G28 для осей с концевиками, soft home для остальных."""
    try:
        config = load_config()
        body = request.get_json(silent=True) or {}
        requested = body.get("axes") if isinstance(body.get("axes"), list) else list(get_axis_map(config).keys())
        commands, endstop_axes, soft_axes = _home_commands(config, requested)
        get_client().send_gcodes(commands)
        return jsonify({"ok": True, "commands": commands, "endstop_axes": endstop_axes, "soft_axes": soft_axes})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Подготовка к выключению платы: вернуть в хоум или запомнить текущую позицию."""
    try:
        data = request.get_json(force=True)
        mode = str(data.get("mode", "")).strip()
        if mode not in ("go_home", "keep_position"):
            raise ValidationError("mode должен быть 'go_home' или 'keep_position'")
        config = load_config()
        client = get_client()
        if mode == "go_home":
            commands, _, _ = _home_commands(config, list(get_axis_map(config).keys()))
            commands.append("M18")
            client.send_gcodes(commands)
            return jsonify({"ok": True, "mode": mode, "commands": commands})
        # keep_position: оставить на месте, текущая позиция становится новым хоумом
        position = client.get_position()
        old_home = dict(config["defaults"].get("home_position", {}))
        macros = load_macros()
        macros = [m for m in macros if m.get("name") != "предыдущий хоум"]
        if old_home:
            macros.append({"name": "предыдущий хоум", "position": old_home})
        save_json(MACROS_PATH, macros)
        axis_map = get_axis_map(config)
        new_home = {axis: float(position.get(axis, 0.0)) for axis in axis_map}
        config["defaults"]["home_position"] = new_home
        save_json(CONFIG_PATH, config)
        client.send_gcode("M18")
        return jsonify({"ok": True, "mode": mode, "new_home": new_home, "previous_home_macro": "предыдущий хоум"})
    except ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DuetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


if __name__ == "__main__":
    config = load_config()
    board = config["board"]
    app.run(host=board["bind_host"], port=board["port"], debug=board["debug"])