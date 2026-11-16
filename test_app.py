"""Регрессионные тесты безопасного поведения (настоящий Flask test_client).

Тесты изолированы: CONFIG_PATH/MACROS_PATH переопределяются на временные
каталоги, поэтому реальные config.json/macros.json не затрагиваются.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

import requests

import app as app_module
from app import app
from duet_client import DuetClient, DuetError


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeSession:
    """Подставная requests.Session: фиксирует запросы и возвращает заданные ответы."""

    def __init__(self, handler=None):
        self.handler = handler
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.handler is not None:
            return self.handler(method, url, kwargs)
        return FakeResponse()
class BaseAppTest(unittest.TestCase):
    """База: временные CONFIG_PATH/MACROS_PATH, сброс клиента и кэша."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_config = app_module.CONFIG_PATH
        self._orig_macros = app_module.MACROS_PATH
        app_module.CONFIG_PATH = os.path.join(self._tmpdir.name, "config.json")
        app_module.MACROS_PATH = os.path.join(self._tmpdir.name, "macros.json")
        app_module.save_json(app_module.CONFIG_PATH, self.make_config())
        app_module.save_json(app_module.MACROS_PATH, [])
        app_module.reset_client()
        app_module._position_cache.clear()
        app_module._position_cache_valid = False
        self.client = app.test_client()

    def tearDown(self):
        app_module.reset_client()
        app_module._position_cache.clear()
        app_module._position_cache_valid = False
        app_module.CONFIG_PATH = self._orig_config
        app_module.MACROS_PATH = self._orig_macros
        self._tmpdir.cleanup()

    @staticmethod
    def _axis(letter, **kw):
        axis = {
            "letter": letter,
            "name": letter,
            "current_mA": 1000.0,
            "speed_mm_min": 600.0,
            "max_step_mm": 1.0,
            "allow_absolute": True,
            "require_homed": True,
            "has_endstop": False,
            "steps": [{"plus": 0.1, "minus": 0.1}],
        }
        axis.update(kw)
        return axis

    @staticmethod
    def make_config(axes=None, defaults=None):
        cfg = json.loads(json.dumps(app_module.DEFAULT_CONFIG))
        cfg["board"]["mock"] = True
        cfg["axes"] = axes if axes is not None else [
            BaseAppTest._axis("X"), BaseAppTest._axis("Y"), BaseAppTest._axis("Z"),
        ]
        cfg["defaults"]["home_position"] = {a["letter"]: 0.0 for a in cfg["axes"]}
        if defaults:
            cfg["defaults"].update(defaults)
        return cfg

    def _set_reference(self, *axes, positions=None):
        """Ручная установка опоры для осей без концевика (POST /api/set_position)."""
        body = {"confirm": True}
        if positions is not None:
            body["positions"] = positions
        else:
            body["axes"] = list(axes)
        r = self.client.post("/api/set_position", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r


def _move_body(**overrides):
    body = {"axis": "X", "direction": 1, "step": 0.1, "feedrate": 100}
    body.update(overrides)
    return body
class AppRegressionTests(BaseAppTest):
    def test_01_control_normal_jog(self):
        self._set_reference("X")
        r = self.client.post("/api/move", json=_move_body(step=0.5))
        self.assertEqual(r.status_code, 200)
        pos = self.client.get("/api/position").get_json()["position"]
        self.assertAlmostEqual(pos["X"], 0.5, places=6)

    def test_02_oversize_step_rejected(self):
        r = self.client.post("/api/move", json=_move_body(step=5.0))
        self.assertEqual(r.status_code, 400)

    def test_05_firmware_error_returns_502(self):
        app_module.reset_client()
        mock = app_module.get_client()
        mock.firmware_error = "Error: Must home axes first"
        self._set_reference("X")
        r = self.client.post("/api/move", json=_move_body(step=0.5))
        self.assertEqual(r.status_code, 502)

    def test_06_relative_move_respects_limits(self):
        cfg = self.make_config(axes=[self._axis("X", max_position=1.0)])
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        self._set_reference(positions={"X": 0.9})
        r = self.client.post("/api/move", json=_move_body(step=0.5))
        self.assertEqual(r.status_code, 400)

    def test_07_goto_outside_limits_rejected(self):
        cfg = self.make_config(axes=[self._axis("X", has_endstop=False, max_position=1.0)])
        cfg["defaults"]["home_position"] = {"X": 20.0}
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        self._set_reference("X")
        r = self.client.post("/api/home", json={})
        self.assertEqual(r.status_code, 400)

    def test_08_incomplete_position_keep_position_rejected(self):
        app_module.reset_client()
        mock = app_module.get_client()
        mock.get_position = lambda: {"X": 4.0}
        before_macros = app_module.load_json(app_module.MACROS_PATH, [])
        before_config = app_module.load_json(app_module.CONFIG_PATH, {})
        r = self.client.post("/api/shutdown", json={"mode": "keep_position"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(app_module.load_json(app_module.MACROS_PATH, []), before_macros)
        self.assertEqual(app_module.load_json(app_module.CONFIG_PATH, {}), before_config)

    def test_09_nan_inf_rejected(self):
        r = self.client.post("/api/move", json=_move_body(step="NaN"))
        self.assertEqual(r.status_code, 400)
        with self.assertRaises(app_module.ValidationError):
            app_module._ensure_number(float("inf"), "x")
        with self.assertRaises(app_module.ValidationError):
            app_module._ensure_number(float("nan"), "x")

    def test_10_empty_homed_set_blocks_even_with_allow_unverified(self):
        cfg = self.make_config(axes=[self._axis("X", has_endstop=True)], defaults={"allow_unverified_homing": True})
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        mock = app_module.get_client()
        mock.homed = set()  # достоверно «ни одна ось не homed»
        mock.homed_unknown = False
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 400)

    def test_10b_unknown_homed_allowed_with_allow_unverified(self):
        cfg = self.make_config(axes=[self._axis("X", has_endstop=True)], defaults={"allow_unverified_homing": True})
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        mock = app_module.get_client()
        mock.homed_unknown = True  # None — статус неизвестен
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 200)

    def test_15_overlapping_jogs_serialized(self):
        self._set_reference(positions={"X": 10.0})
        mock = app_module.get_client()
        barrier = threading.Barrier(3)
        statuses = []

        def do_move():
            c = app.test_client()
            barrier.wait()
            r = c.post("/api/move", json=_move_body(step=0.5))
            statuses.append(r.status_code)

        threads = [threading.Thread(target=do_move) for _ in range(2)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        self.assertEqual(sorted(statuses), [200, 200])
        self.assertAlmostEqual(mock.position["X"], 11.0, places=6)

    def test_16_failure_on_g1_restores_g90(self):
        self._set_reference("X")
        mock = app_module.get_client()

        def failing_send(code):
            line = code.strip()
            mock.log.append(line)
            if line == "G91":
                mock.relative = True
            elif line == "G90":
                mock.relative = False
            elif line.startswith("G1"):
                raise DuetError("Error: boom")

        mock.send_gcode = failing_send
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 502)
        self.assertFalse(mock.relative)
        self.assertIn("G90", mock.log)

    def test_blocker2_unconfirmed_blocks_motion_until_recover(self):
        self._set_reference("X")
        mock = app_module.get_client()
        mock.motion_unconfirmed = True
        # движение заблокировано, пока не подтверждено
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 502)
        self.assertIn("восстановление", r.get_json()["error"])
        # восстановление без подтверждения — 409
        r = self.client.post("/api/recover", json={})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(mock.motion_unconfirmed)
        # явное восстановление с подтверждением снимает флаг
        r = self.client.post("/api/recover", json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(mock.motion_unconfirmed)
        # движение снова работает
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 200)

    def test_18_goto_without_reference_rejected(self):
        cfg = self.make_config(axes=[self._axis("X", has_endstop=False)])
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        mock = app_module.get_client()
        # goto сохранённой позиции без установленной опоры отклоняется
        r_home = self.client.post("/api/home", json={})
        self.assertEqual(r_home.status_code, 400)
        self.assertEqual(mock.homed, set())
        r_move = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r_move.status_code, 400)

    def test_19_restart_blocks_motion_without_reference(self):
        self._set_reference("X")
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 200)
        # перезапуск/переподключение сбрасывает ручную опору
        app_module.reset_client()
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 400)

    def test_20_cache_cleared_and_stale_on_reconnect(self):
        app_module.reset_client()
        mock = app_module.get_client()
        mock.position = {"X": 0.0, "Y": 8.0, "Z": 9.0}
        r = self.client.get("/api/position")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["position"], {"X": 0.0, "Y": 8.0, "Z": 9.0})
        app_module.reset_client()
        mock2 = app_module.get_client()
        mock2.position = {"X": 0.0}
        r = self.client.get("/api/position")
        self.assertEqual(r.get_json()["position"], {"X": 0.0})
        mock2.position = {"X": 5.0}
        self.client.get("/api/position")

        def boom():
            raise DuetError("no reply")

        mock2.get_position = boom
        r = self.client.get("/api/position")
        data = r.get_json()
        self.assertEqual(r.status_code, 502)
        self.assertTrue(data["stale"])
        self.assertEqual(data["position"], {"X": 5.0})

    def test_21_bad_axis_and_no_reference_rejected(self):
        r = self.client.post("/api/move", json=_move_body(axis="X M18"))
        self.assertEqual(r.status_code, 400)
        # без установленной опоры движение оси без концевика отклоняется
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 400)

    def test_22_home_uses_homing_feedrate(self):
        cfg = self.make_config(axes=[self._axis("X", has_endstop=False)])
        cfg["defaults"]["homing_feedrate_mm_min"] = 50
        cfg["defaults"]["macro_feedrate_mm_min"] = 600
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        self._set_reference("X")
        mock = app_module.get_client()
        r = self.client.post("/api/home", json={})
        self.assertEqual(r.status_code, 200)
        log_text = " ".join(mock.log)
        self.assertIn("F50", log_text)
        self.assertNotIn("F600", log_text)

    def test_set_position_manual_reference(self):
        # без подтверждения — 409
        r = self.client.post("/api/set_position", json={"axes": ["X"]})
        self.assertEqual(r.status_code, 409)
        # ось с концевиком нельзя задавать через G92
        cfg = self.make_config(axes=[self._axis("X", has_endstop=True)])
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        r = self.client.post("/api/set_position", json={"confirm": True, "axes": ["X"]})
        self.assertEqual(r.status_code, 400)
        # ось без концевика — 200, G92 отправлен, опора установлена
        cfg = self.make_config(axes=[self._axis("X", has_endstop=False)])
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        r = self.client.post("/api/set_position", json={"confirm": True, "axes": ["X"]})
        self.assertEqual(r.status_code, 200)
        mock = app_module.get_client()
        self.assertIn("G92", " ".join(mock.log))
        r = self.client.post("/api/move", json=_move_body(step=0.1))
        self.assertEqual(r.status_code, 200)
class DuetClientProtocolTests(unittest.TestCase):
    def test_03_standalone_rrf3_detected_homed_from_model(self):
        model = {
            "firmwareName": "RepRapFirmware",
            "firmwareVersion": "3.4.5",
            "move": {"axes": [
                {"letter": "X", "homed": True, "userPosition": 1.5},
                {"letter": "Y", "homed": False, "userPosition": 0.0},
            ]},
        }

        def handler(method, url, kwargs):
            if url.endswith("/rr_status"):
                return FakeResponse(text='{"status":"I"}')
            if url.endswith("/rr_model"):
                return FakeResponse(text=json.dumps(model))
            return FakeResponse(status_code=404)

        client = DuetClient("127.0.0.1", session=FakeSession(handler))
        self.assertEqual(client.connect(), 3)
        self.assertEqual(client.protocol, "rrf")
        self.assertEqual(client.get_homed_axes(), {"X"})
        self.assertEqual(client.get_position(), {"X": 1.5, "Y": 0.0})

    def test_04_dropped_ack_does_not_repeat_motion(self):
        def handler(method, url, kwargs):
            if url.endswith("/rr_status"):
                return FakeResponse(text='{"status":"I"}')
            if url.endswith("/rr_gcode"):
                raise requests.ConnectionError("connection reset")
            if url.endswith("/rr_reply"):
                return FakeResponse(text='{"reply":""}')
            return FakeResponse()

        session = FakeSession(handler)
        client = DuetClient("127.0.0.1", session=session)
        client.protocol = "rrf"
        with self.assertRaises(DuetError):
            client.send_gcode("G1 X0.5 F100")
        self.assertTrue(client.motion_unconfirmed)
        gcode_calls = [u for (m, u, k) in session.requests if u.endswith("/rr_gcode")]
        self.assertEqual(len(gcode_calls), 1)

    def test_11_sbc_code_text_plain(self):
        def handler(method, url, kwargs):
            if url.startswith("http://127.0.0.1/rr_"):
                return FakeResponse(status_code=404)
            if url.endswith("/machine/connect"):
                return FakeResponse(text='{"sessionKey":"abc"}')
            if url.endswith("/machine/status"):
                return FakeResponse(text='{}')
            if url.endswith("/machine/model"):
                return FakeResponse(text=json.dumps({"firmwareName": "RepRapFirmware", "firmwareVersion": "3.4.5"}))
            if url.endswith("/machine/code"):
                return FakeResponse()
            return FakeResponse(status_code=404)

        session = FakeSession(handler)
        client = DuetClient("127.0.0.1", session=session)
        client.connect()
        client.send_gcode("G1 X0.5 F100")
        code_calls = [(m, u, k) for (m, u, k) in session.requests if u.endswith("/machine/code")]
        self.assertEqual(len(code_calls), 1)
        m, u, k = code_calls[0]
        self.assertEqual(m, "POST")
        self.assertEqual(k.get("data"), "G1 X0.5 F100")
        self.assertEqual(k.get("headers", {}).get("Content-Type"), "text/plain")

    def test_12_sbc_session_key_header(self):
        def handler(method, url, kwargs):
            if url.startswith("http://127.0.0.1/rr_"):
                return FakeResponse(status_code=404)
            if url.endswith("/machine/connect"):
                return FakeResponse(text='{"sessionKey":"secret-123"}')
            if url.endswith("/machine/status"):
                return FakeResponse(text='{}')
            if url.endswith("/machine/model"):
                return FakeResponse(text=json.dumps({"firmwareName": "RepRapFirmware", "firmwareVersion": "3.4.5"}))
            if url.endswith("/machine/code"):
                return FakeResponse()
            return FakeResponse(status_code=404)

        session = FakeSession(handler)
        client = DuetClient("127.0.0.1", session=session)
        client.connect()
        client.send_gcode("M114")
        code_calls = [(m, u, k) for (m, u, k) in session.requests if u.endswith("/machine/code")]
        self.assertEqual(code_calls[0][2].get("headers", {}).get("X-Session-Key"), "secret-123")

    def test_13_position_from_machine_model(self):
        model = {"move": {"axes": [{"letter": "X", "userPosition": 12.0}]}}

        def handler(method, url, kwargs):
            if url.startswith("http://127.0.0.1/rr_"):
                return FakeResponse(status_code=404)
            if url.endswith("/machine/connect"):
                return FakeResponse(text='{"sessionKey":"abc"}')
            if url.endswith("/machine/status"):
                return FakeResponse(text='{}')
            if url.endswith("/machine/model"):
                return FakeResponse(text=json.dumps(model))
            return FakeResponse(status_code=404)

        client = DuetClient("127.0.0.1", session=FakeSession(handler))
        client.connect()
        self.assertEqual(client.get_position(), {"X": 12.0})

    def test_14_delayed_empty_m114_is_error(self):
        def handler(method, url, kwargs):
            if url.endswith("/rr_status"):
                return FakeResponse(text='{"status":"I"}')
            if url.endswith("/rr_model"):
                return FakeResponse(text='{}')
            if url.endswith("/rr_gcode"):
                return FakeResponse(text='{"buff":0}')
            if url.endswith("/rr_reply"):
                return FakeResponse(text='{"reply":""}')
            return FakeResponse()

        client = DuetClient("127.0.0.1", session=FakeSession(handler))
        client.protocol = "rrf"
        with self.assertRaises(DuetError):
            client.get_position()

    def test_17_parser_handles_list(self):
        self.assertEqual(DuetClient._extract_axes([]), [])
        self.assertEqual(DuetClient._extract_axes(["a", "b"]), [])
        self.assertEqual(DuetClient._position_from_model([]), {})
        self.assertEqual(DuetClient._position_from_model({"axes": ["x"]}), {})

    def test_blocker1_no_mutating_retry_after_401(self):
        def handler(method, url, kwargs):
            if url.endswith("/rr_status"):
                return FakeResponse(text='{"status":"I"}')
            if url.endswith("/rr_model"):
                return FakeResponse(text='{"firmwareName":"RepRapFirmware","firmwareVersion":"3.4.5"}')
            if url.endswith("/rr_gcode"):
                return FakeResponse(status_code=401)
            if url.endswith("/rr_reply"):
                return FakeResponse(text='{"reply":""}')
            return FakeResponse()

        session = FakeSession(handler)
        client = DuetClient("127.0.0.1", session=session)
        client.connect()
        with self.assertRaises(DuetError):
            client.send_gcode("G1 X0.5 F100")
        self.assertTrue(client.motion_unconfirmed)
        gcode_calls = [u for (m, u, k) in session.requests if u.endswith("/rr_gcode")]
        self.assertEqual(len(gcode_calls), 1)

    def test_blocker3_reply_bound_to_current_command(self):
        replies = ["Error: old stale reply", "X:12.000"]

        def handler(method, url, kwargs):
            if url.endswith("/rr_status"):
                return FakeResponse(text='{"status":"I"}')
            if url.endswith("/rr_model"):
                return FakeResponse(text='{}')
            if url.endswith("/rr_reply"):
                r = replies.pop(0) if replies else ""
                return FakeResponse(text=json.dumps({"reply": r}))
            if url.endswith("/rr_gcode"):
                return FakeResponse(text='{"buff":0}')
            return FakeResponse()

        client = DuetClient("127.0.0.1", session=FakeSession(handler))
        client.protocol = "rrf"
        # старый ошибочный reply должен быть осушён и не перепутан с ответом M114
        self.assertEqual(client.get_position(), {"X": 12.0})
class ValidationTests(BaseAppTest):
    def test_config_available(self):
        data = self.client.get("/api/config").get_json()
        self.assertIn("axes", data)
        self.assertTrue(data["board"]["mock"])

    def test_config_rejects_bad_input(self):
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_config({"board": {}, "axes": [{"letter": "X"}, {"letter": "x"}]})
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_config({"board": {"ip": "not a valid ip!!"}, "axes": []})
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_config({"board": {}, "axes": [{"letter": "XX"}]})

    def test_bool_flag_strict(self):
        with self.assertRaises(app_module.ValidationError):
            app_module._ensure_bool("false", "x")
        with self.assertRaises(app_module.ValidationError):
            app_module._ensure_bool("0", "x")
        self.assertIs(app_module._ensure_bool(True, "x"), True)
        self.assertIs(app_module._ensure_bool(False, "x"), False)

    def test_non_json_body_returns_415(self):
        r = self.client.post("/api/move", data="not json", content_type="text/plain")
        self.assertEqual(r.status_code, 415)

    def test_json_list_body_rejected(self):
        r = self.client.post("/api/move", json=["a", "b"])
        self.assertEqual(r.status_code, 400)

    def test_cross_origin_mutation_rejected(self):
        r = self.client.post("/api/move", json=_move_body(step=0.1), headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_raw_gcode_disabled_by_default(self):
        r = self.client.post("/api/gcode", json={"code": "M18"})
        self.assertEqual(r.status_code, 403)

    def test_macro_requires_confirmation(self):
        self._set_reference("X", "Y")
        self.client.post("/api/macros", json=[{"name": "home", "position": {"X": 1.0, "Y": 2.0}}])
        self.assertEqual(self.client.post("/api/macros/run", json={"name": "home"}).status_code, 409)
        self.assertEqual(self.client.post("/api/macros/run", json={"name": "home", "confirm": True}).status_code, 200)

    def test_bad_macro_rejected(self):
        r = self.client.post("/api/macros", json=[{"name": "bad", "position": {"W": 1.0}}])
        self.assertEqual(r.status_code, 400)

    def test_macros_position_limits(self):
        config = app_module.validate_config({
            "board": {"ip": "127.0.0.1", "mock": True},
            "axes": [{"letter": "X", "name": "X", "min_position": 0, "max_position": 10}],
            "defaults": {},
        })
        macros = app_module.validate_macros([{"name": "ok", "position": {"X": 5.0}}], config)
        self.assertEqual(macros[0]["position"]["X"], 5.0)
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_macros([{"name": "bad", "position": {"X": 20.0}}], config)


if __name__ == "__main__":
    unittest.main()





