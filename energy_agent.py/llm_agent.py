#!/usr/bin/env python3
"""
LLM advisor agent (Python) for the multi-agent energy management system.

Role in the system
------------------
The Node-RED agents (generation, load, pricing, storage) and the coordinator publish
their data and decisions over MQTT. This Python agent subscribes to those topics, asks a
LOCAL large language model (served by Ollama on this computer) to explain the
coordinator's latest decision in plain English, and publishes the explanation back to
MQTT on  <prefix>agent/llm_explanation  so the dashboard can display it.

The LLM is advisory only: it explains and gives a tip, it never controls the battery
or the grid. If the local LLM is not running, a clearly labelled rule-based fallback
text is published instead, so nothing breaks.

Usage
-----
  pip install -r requirements.txt
  ollama pull qwen2.5:0.5b        # one-time download of a small local model (about 400 MB)
  python llm_agent.py --test       # asks the LLM once with sample data (no MQTT needed)
  python llm_agent.py              # runs the agent against the MQTT broker

Settings can be changed with environment variables (see the top of this file).
"""
import argparse
import csv
import json
import os
import random
import threading
import time
import urllib.request
from datetime import datetime

BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
PORT = int(os.getenv("MQTT_PORT", "1883"))
PREFIX = os.getenv("MQTT_PREFIX", "shreshthata/energy/")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "12"))  # seconds between LLM calls
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))            # seconds to wait for the LLM
LOG_FILE = os.getenv("LLM_LOG", "llm_log.csv")

# MQTT topic (without prefix) -> name in our state dictionary
WATCH = {
    "clock/hour": "hour",
    "generation/solar_kw": "solar_kw",
    "load/demand_kw": "load_kw",
    "storage/soc_percent": "soc_percent",
    "pricing/rate_per_kwh": "price",
    "agent/command": "command",
    "agent/recommendation": "recommendation",
}


def clock_text(hour):
    try:
        h = float(hour)
    except (TypeError, ValueError):
        return "--:--"
    hh = int(h)
    mm = int(round((h - hh) * 60))
    return f"{hh:02d}:{mm:02d}"


def build_prompt(s):
    """Turn the latest system state into a short, strict prompt for the LLM."""
    cmd = s.get("command") if isinstance(s.get("command"), dict) else {}
    return (
        "You are an energy advisor inside a multi-agent IoT energy management system.\n"
        "A coordinator agent has just made a decision. Use ONLY the numbers given below.\n\n"
        f"Time: {clock_text(s.get('hour'))}\n"
        f"Solar generation: {s.get('solar_kw', '?')} kW\n"
        f"Load: {s.get('load_kw', '?')} kW\n"
        f"Battery state of charge: {s.get('soc_percent', '?')} %\n"
        f"Grid price: Rs {s.get('price', '?')} per kWh\n"
        f"Coordinator decision: {s.get('recommendation', '?')}\n"
        f"Command (kW): battery={cmd.get('battery_kw', '?')}, "
        f"grid import={cmd.get('grid_import_kw', '?')}, "
        f"export={cmd.get('export_kw', '?')}, shifted load={cmd.get('shift_kw', '?')}\n\n"
        "Write exactly two short sentences explaining why this decision is sensible.\n"
        "Then write one sentence starting with 'Tip:' giving practical advice for the next hour.\n"
        "Do not invent any numbers."
    )


def ask_ollama(prompt):
    """Call the local Ollama server and return the generated text."""
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 120},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))["response"]


def one_line(text, limit=400):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def fallback_text(s):
    return ("LLM unavailable, rule-based explanation: "
            f"{s.get('recommendation', 'no decision received yet')}")


def log_row(snap, text, source):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["real_time", "sim_time", "solar_kw", "load_kw", "soc_percent",
                        "price", "decision", "source", "explanation"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), clock_text(snap.get("hour")),
                    snap.get("solar_kw"), snap.get("load_kw"), snap.get("soc_percent"),
                    snap.get("price"), snap.get("recommendation"), source, text])


class Advisor:
    def __init__(self, publish, ask=None):
        self.publish = publish          # function(text) that sends text to MQTT
        self.ask = ask or ask_ollama    # function(prompt) -> text
        self.state = {}
        self.pending = None             # newest decision that has not been explained yet
        self.last_call = 0.0
        self.lock = threading.Lock()

    def on_message(self, topic, payload):
        if not topic.startswith(PREFIX):
            return
        key = topic[len(PREFIX):]
        if key not in WATCH:
            return
        try:
            value = json.loads(payload)
        except (ValueError, TypeError):
            value = payload
        with self.lock:
            self.state[WATCH[key]] = value
            if key == "agent/recommendation":
                self.pending = dict(self.state)  # snapshot of everything at decision time

    def explain(self, snap):
        try:
            return one_line(self.ask(build_prompt(snap))), "llm"
        except Exception as err:  # LLM not running, timeout, bad reply ...
            print(f"[advisor] LLM unavailable ({err}); using fallback text")
            return fallback_text(snap), "fallback"

    def step(self, now=None):
        """Explain the newest decision if enough time has passed. Returns True if it did."""
        now = time.time() if now is None else now
        with self.lock:
            if self.pending is None or now - self.last_call < MIN_INTERVAL:
                return False
            snap, self.pending = self.pending, None
            self.last_call = now
        text, source = self.explain(snap)
        message = f"[{clock_text(snap.get('hour'))}] {text}"
        self.publish(message)
        log_row(snap, text, source)
        print(f"[advisor] ({source}) {message}")
        return True


def run_test():
    sample = {"hour": 21.25, "solar_kw": 0, "load_kw": 2.59, "soc_percent": 20, "price": 10,
              "recommendation": "SHIFT LOAD - peak price Rs 10/kWh: shift 0.78 kW flexible load "
                                "to off-peak, import 1.81 kW from grid",
              "command": {"battery_kw": 0, "grid_import_kw": 1.81, "export_kw": 0, "shift_kw": 0.78}}
    print("Prompt sent to the local LLM:\n")
    print(build_prompt(sample))
    print("\nAnswer:\n")
    print(one_line(ask_ollama(build_prompt(sample))))


def run_agent():
    import paho.mqtt.client as mqtt
    cid = "llm-advisor-" + str(random.randint(1000, 9999))
    try:  # paho-mqtt 2.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client(client_id=cid)

    advisor = Advisor(lambda text: client.publish(PREFIX + "agent/llm_explanation", text))

    def on_connect(client, userdata, flags, rc, properties=None):
        print(f"[advisor] connected to {BROKER}:{PORT}, model {MODEL}")
        client.subscribe([(PREFIX + k, 0) for k in WATCH])

    def on_message(client, userdata, msg):
        advisor.on_message(msg.topic, msg.payload.decode("utf-8", "replace"))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    client.loop_start()
    try:
        while True:
            advisor.step()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[advisor] stopped")
    finally:
        client.loop_stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM advisor agent for the energy system")
    parser.add_argument("--test", action="store_true", help="ask the local LLM once and exit")
    args = parser.parse_args()
    run_test() if args.test else run_agent()
