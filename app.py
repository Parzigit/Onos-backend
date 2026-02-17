"""
DLBMT Dashboard – Flask REST API + WebSocket Backend
Backend-only version for Render deployment.
Frontend is deployed separately on Vercel.
"""

import os
import time
import logging
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit

from dlbmt_engine import DLBMTEngine, ControllerLevel
from sdn_simulator import SDNSimulator, TOPOLOGIES
from traffic_generator import TrafficGenerator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App Setup (Backend only)
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------
simulator: SDNSimulator = None
traffic_gen: TrafficGenerator = None
auto_migration_enabled = True
simulation_speed = 1.0
simulation_running = True
sim_lock = threading.Lock()


def init_simulation(topology_name: str = "atlanta"):
    """Initialize or reset the simulation."""
    global simulator, traffic_gen
    with sim_lock:
        simulator = SDNSimulator(topology_name)
        traffic_gen = TrafficGenerator(simulator.engine)
        traffic_gen.set_pattern("wave", 1.0)
        traffic_gen.generate_tick()
        simulator.engine.update_controller_levels()
    logger.info(f"Simulation initialized with topology: {topology_name}")


# ---------------------------------------------------------------------------
# Background Simulation Loop
# ---------------------------------------------------------------------------
def simulation_loop():
    """Background thread that runs the simulation."""
    global simulation_running

    while simulation_running:
        try:
            interval = 1.0 / simulation_speed if simulation_speed > 0 else 1.0
            time.sleep(interval)

            with sim_lock:
                if simulator is None:
                    continue

                traffic_gen.generate_tick()
                level_changes = simulator.engine.update_controller_levels()

                migration_record = None
                if auto_migration_enabled:
                    migration_record = simulator.engine.run_load_balancing()

                snapshot = simulator.engine.take_snapshot()

                socketio.emit("state_update", {
                    "snapshot": snapshot,
                    "traffic": traffic_gen.get_traffic_summary(),
                    "migration": migration_record.to_dict() if migration_record else None,
                    "level_changes": {k: v for k, v in level_changes.items() if v},
                })

        except Exception as e:
            logger.error(f"Simulation loop error: {e}")
            time.sleep(1)


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------
@app.route("/")
def health():
    return {
        "service": "DLBMT Backend",
        "status": "running"
    }


# ---------------------------------------------------------------------------
# REST API Routes
# ---------------------------------------------------------------------------
@app.route("/api/topology", methods=["GET"])
def get_topology():
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        return jsonify(simulator.get_topology_data())


@app.route("/api/controllers", methods=["GET"])
def get_controllers():
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        controllers = []
        for ctrl_id, ctrl in simulator.engine.controllers.items():
            info = ctrl.to_dict()
            switches = simulator.engine.get_switches_in_domain(ctrl_id)

            info["switch_count"] = len(switches)
            info["total_cpu_used"] = round(sum(s.load_cpu for s in switches), 2)
            info["total_mem_used"] = round(sum(s.load_mem for s in switches), 2)
            info["total_bw_used"] = round(sum(s.load_bw for s in switches), 2)

            info["cpu_utilization"] = round(info["total_cpu_used"] / ctrl.capacity_cpu * 100, 2)
            info["mem_utilization"] = round(info["total_mem_used"] / ctrl.capacity_mem * 100, 2)
            info["bw_utilization"] = round(info["total_bw_used"] / ctrl.capacity_bw * 100, 2)

            controllers.append(info)

        return jsonify(controllers)


@app.route("/api/switches", methods=["GET"])
def get_switches():
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        switches = []
        for sw_id, sw in simulator.engine.switches.items():
            ctrl = simulator.engine.controllers.get(sw.controller_id)
            usage = simulator.engine.compute_switch_resource_usage(sw, ctrl) if ctrl else 0

            info = sw.to_dict()
            info["resource_usage"] = round(usage * 100, 2)
            info["distance_to_controller"] = simulator.engine.get_distance(sw_id, sw.controller_id)
            switches.append(info)

        return jsonify(switches)


@app.route("/api/migration/history", methods=["GET"])
def get_migration_history():
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        limit = request.args.get("limit", 50, type=int)
        history = [r.to_dict() for r in simulator.engine.migration_history[-limit:]]
        return jsonify(history)


@app.route("/api/migration/trigger", methods=["POST"])
def trigger_migration():
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        simulator.engine.update_controller_levels()
        record = simulator.engine.run_load_balancing()

        if record:
            return jsonify({"success": True, "migration": record.to_dict()})
        return jsonify({"success": False, "message": "No migration needed"})


@app.route("/api/migration/auto", methods=["POST"])
def toggle_auto_migration():
    global auto_migration_enabled
    data = request.get_json() or {}
    auto_migration_enabled = bool(data.get("enabled", not auto_migration_enabled))
    return jsonify({"auto_migration_enabled": auto_migration_enabled})


@app.route("/api/stats/timeseries", methods=["GET"])
def get_timeseries():
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        limit = request.args.get("limit", 60, type=int)
        return jsonify(simulator.engine.load_history[-limit:])


@app.route("/api/stats/summary", methods=["GET"])
def get_stats_summary():
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        stats = simulator.engine.get_stats()
        stats["traffic"] = traffic_gen.get_traffic_summary()
        stats["auto_migration"] = auto_migration_enabled
        stats["simulation_speed"] = simulation_speed
        return jsonify(stats)


@app.route("/api/config/topology", methods=["POST"])
def change_topology():
    data = request.get_json() or {}
    topology = data.get("topology", "atlanta")

    if topology not in TOPOLOGIES:
        return jsonify({"error": f"Unknown topology: {topology}"}), 400

    init_simulation(topology)
    return jsonify({"success": True, "topology": topology})


@app.route("/api/config/traffic", methods=["POST"])
def configure_traffic():
    data = request.get_json() or {}
    pattern = data.get("pattern", "wave")
    intensity = data.get("intensity", 1.0)

    with sim_lock:
        if traffic_gen is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        traffic_gen.set_pattern(pattern, intensity)

    return jsonify({"pattern": pattern, "intensity": intensity})


@app.route("/api/config/speed", methods=["POST"])
def configure_speed():
    global simulation_speed
    data = request.get_json() or {}
    simulation_speed = max(0.1, min(float(data.get("speed", 1.0)), 10.0))
    return jsonify({"speed": simulation_speed})


# ---------------------------------------------------------------------------
# WebSocket Events
# ---------------------------------------------------------------------------
@socketio.on("connect")
def handle_connect():
    logger.info("WebSocket client connected")
    with sim_lock:
        if simulator:
            emit("topology", simulator.get_topology_data())
            emit("state_update", {
                "snapshot": simulator.engine.take_snapshot(),
                "traffic": traffic_gen.get_traffic_summary(),
                "migration": None,
                "level_changes": {},
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_simulation("atlanta")

    sim_thread = threading.Thread(target=simulation_loop, daemon=True)
    sim_thread.start()

    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting DLBMT backend on port {port}")

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True
    )
