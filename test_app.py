"""Базовые assert-тесты Flask API в mock-режиме."""

from __future__ import annotations

import unittest

import app as app_module
from app import app


class AppApiTests(unittest.TestCase):
    def setUp(self):
        app_module.reset_client()
        app_module._position_cache.clear()
        app_module.save_json(app_module.MACROS_PATH, [])
        with open(app_module.CONFIG_PATH, "r", encoding="utf-8") as f:
            self._config_snapshot = f.read()
        self.client = app.test_client()

    def tearDown(self):
        with open(app_module.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(self._config_snapshot)
        app_module.reset_client()
        app_module._position_cache.clear()
        app_module.save_json(app_module.MACROS_PATH, [])

    def test_config_available(self):
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("axes", data)
        self.assertTrue(data["board"]["mock"])

    def test_move_updates_position(self):
        self.assertEqual(self.client.get("/api/position").status_code, 200)
        response = self.client.post("/api/move", json={"axis": "X", "direction": 1, "step": 0.5, "feedrate": 600})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/api/move", json={"axis": "X", "direction": -1, "step": 0.2, "feedrate": 600})
        self.assertEqual(response.status_code, 200)
        pos = self.client.get("/api/position").get_json()["position"]
        self.assertAlmostEqual(pos["X"], 0.3, places=6)

    def test_invalid_axis_rejected(self):
        response = self.client.post("/api/move", json={"axis": "X M18", "direction": 1, "step": 0.1, "feedrate": 100})
        self.assertEqual(response.status_code, 400)

    def test_macro_requires_confirmation(self):
        save = self.client.post("/api/macros", json=[{"name": "home", "position": {"X": 1.0, "Y": 2.0}}])
        self.assertEqual(save.status_code, 200)
        response = self.client.post("/api/macros/run", json={"name": "home"})
        self.assertEqual(response.status_code, 409)
        response = self.client.post("/api/macros/run", json={"name": "home", "confirm": True})
        self.assertEqual(response.status_code, 200)

    def test_raw_gcode_disabled_by_default(self):
        response = self.client.post("/api/gcode", json={"code": "M18"})
        self.assertEqual(response.status_code, 403)

    def test_max_step_exceeded(self):
        response = self.client.post("/api/move", json={"axis": "X", "direction": 1, "step": 5.0, "feedrate": 100})
        self.assertEqual(response.status_code, 400)

    def test_feedrate_exceeds_speed(self):
        response = self.client.post("/api/move", json={"axis": "X", "direction": 1, "step": 0.1, "feedrate": 10000})
        self.assertEqual(response.status_code, 400)

    def test_non_homed_axis_rejected(self):
        app_module.get_client().homed = set()
        response = self.client.post("/api/move", json={"axis": "X", "direction": 1, "step": 0.1, "feedrate": 100})
        self.assertEqual(response.status_code, 400)

    def test_bad_macro_rejected(self):
        response = self.client.post("/api/macros", json=[{"name": "bad", "position": {"W": 1.0}}])
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/macros", json=[{"name": "", "position": {"X": 1.0}}])
        self.assertEqual(response.status_code, 400)

    def test_raw_gcode_allowlist_when_enabled(self):
        cfg = app_module.load_config()
        cfg["board"]["allow_raw_gcode"] = True
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        self.assertEqual(self.client.post("/api/gcode", json={"code": "M18"}).status_code, 200)
        self.assertEqual(self.client.post("/api/gcode", json={"code": "G1 X10"}).status_code, 400)

    def test_connect_bad_ip_returns_400(self):
        cfg = app_module.load_config()
        cfg["board"]["mock"] = False
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        response = self.client.post("/api/connect", json={"ip": "not a valid ip!!"})
        self.assertEqual(response.status_code, 400)

    def test_position_limits(self):
        config = app_module.validate_config({
            "board": {"ip": "127.0.0.1", "mock": True},
            "axes": [{"letter": "X", "name": "X", "min_position": 0, "max_position": 10, "allow_absolute": True}],
            "defaults": {},
        })
        macros = app_module.validate_macros([{"name": "ok", "position": {"X": 5.0}}], config)
        self.assertEqual(macros[0]["position"]["X"], 5.0)
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_macros([{"name": "bad", "position": {"X": 20.0}}], config)

    def test_config_validation_rejects_bad_input(self):
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_config({"board": {}, "axes": [{"letter": "X"}, {"letter": "x"}]})
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_config({"board": {"ip": "not a valid ip!!"}, "axes": []})
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_config({"board": {}, "axes": [{"letter": "XX"}]})

    def test_home_moves_to_zero(self):
        self.client.post("/api/move", json={"axis": "X", "direction": 1, "step": 0.5, "feedrate": 600})
        response = self.client.post("/api/home", json={})
        self.assertEqual(response.status_code, 200)
        pos = self.client.get("/api/position").get_json()["position"]
        self.assertAlmostEqual(pos["X"], 0.0, places=6)

    def test_shutdown_keep_position_saves_macro(self):
        self.client.post("/api/move", json={"axis": "X", "direction": 1, "step": 0.7, "feedrate": 600})
        response = self.client.post("/api/shutdown", json={"mode": "keep_position"})
        self.assertEqual(response.status_code, 200)
        cfg = app_module.load_config()
        self.assertAlmostEqual(cfg["defaults"]["home_position"]["X"], 0.7, places=6)
        names = [m["name"] for m in app_module.load_macros()]
        self.assertIn("предыдущий хоум", names)

    def test_home_endstop_axis_uses_g28(self):
        cfg = app_module.load_config()
        for a in cfg["axes"]:
            if a["letter"] == "X":
                a["has_endstop"] = True
        app_module.save_json(app_module.CONFIG_PATH, cfg)
        app_module.reset_client()
        self.client.post("/api/move", json={"axis": "X", "direction": 1, "step": 0.5, "feedrate": 600})
        response = self.client.post("/api/home", json={})
        self.assertEqual(response.status_code, 200)
        self.assertIn("X", response.get_json()["endstop_axes"])
        pos = self.client.get("/api/position").get_json()["position"]
        self.assertAlmostEqual(pos["X"], 0.0, places=6)

    def test_config_has_steps(self):
        cfg = app_module.load_config()
        for a in cfg["axes"]:
            self.assertIn("steps", a)
            self.assertGreater(len(a["steps"]), 0)

    def test_config_rejects_bad_steps(self):
        with self.assertRaises(app_module.ValidationError):
            app_module.validate_config({"board": {}, "axes": [{"letter": "X", "steps": "bad"}]})


if __name__ == "__main__":
    unittest.main()