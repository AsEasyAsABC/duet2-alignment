# duet2-alignment
Flask app: web UI to jog stepper axes on Duet 2 WiFi (RepRapFirmware) for optics alignment.
Primary: standalone RRF 2.x/3.x via rr_* HTTP API. Optional: DSF (SBC) via /machine/*.

## Files
- app.py          Flask routes (/api/*), serves static/. Validation lives here.
- duet_client.py  Duet HTTP wrapper (rr_connect, rr_gcode, rr_reply, rr_model, M114 fallback) + Mock.
- config.json     (secret, ignored) ip, password, axes[], max_step_mm, homing policy, bind host, raw_gcode flag.
- macros.json     named ABSOLUTE positions (G90 + G1).
- static/         index.html, app.js, style.css, position.html (live coordinates).
- test_app.py     pytest suite, uses the Mock client. test_ui.js: UI test.
- docs/           Duet docs. NEVER read or edit.

## Axis config (config.json "axes[]")
letter (== config.g), name, current_mA (M906), speed_mm_min (M203), max_step_mm,
allow_absolute, require_homed, has_endstop, steps[1..6]{plus,minus} in mm.

## API
/api/home, /api/set_position (G92, needs confirm), /api/shutdown (mode=go_home|keep_position),
/api/gcode (raw, DISABLED by default), move/macro/config endpoints.

## SAFETY INVARIANTS (never break, never relax without asking)
1. Server validates axis letter, step <= max_step_mm, speed vs config. Never trust the client.
2. Relative move = G91, G1, then ALWAYS restore G90.
3. State-changing G-codes (G1/G91/G90/G28/M17/M18/M906/M203) are sent exactly ONCE.
   Lost reply => "result unconfirmed", never auto-retry.
4. require_homed: block motion until G28 (endstop axes) or /api/set_position (no-endstop axes).
5. Macros are absolute => require explicit user confirmation.
6. Default bind 127.0.0.1, debug=false, raw G-code off.
7. Mock mode is for logic only; never claim hardware compatibility.

## Run / test
pip install -r requirements.txt; python app.py -> http://127.0.0.1:5000
python -m pytest -q