"""
DLBMT Dashboard – Flask REST API + WebSocket Backend
Provides real-time network monitoring, topology data, and automatic migration control.
"""

import os
import time
import json
import logging
import threading
from flask import Flask, jsonify, request, send_from_directory
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
# App Setup – serve React build from frontend/dist
# ---------------------------------------------------------------------------
DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")
app = Flask(__name__, static_folder=DIST_DIR, static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------
simulator: SDNSimulator = None
traffic_gen: TrafficGenerator = None
auto_migration_enabled = True
simulation_speed = 1.0   # ticks per second
simulation_running = True
sim_lock = threading.Lock()


def init_simulation(topology_name: str = "atlanta"):
    """Initialize or reset the simulation."""
    global simulator, traffic_gen
    with sim_lock:
        simulator = SDNSimulator(topology_name)
        traffic_gen = TrafficGenerator(simulator.engine)
        traffic_gen.set_pattern("wave", 1.0)
        # Initial traffic tick to populate data
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

                # Generate traffic
                traffic_gen.generate_tick()

                # Update controller levels
                level_changes = simulator.engine.update_controller_levels()

                # Run automatic migration if enabled
                migration_record = None
                if auto_migration_enabled:
                    migration_record = simulator.engine.run_load_balancing()

                # Take snapshot for time-series
                snapshot = simulator.engine.take_snapshot()

                # Emit real-time updates via WebSocket
                try:
                    socketio.emit("state_update", {
                        "snapshot": snapshot,
                        "traffic": traffic_gen.get_traffic_summary(),
                        "migration": migration_record.to_dict() if migration_record else None,
                        "level_changes": {k: v for k, v in level_changes.items() if v},
                    })
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Simulation loop error: {e}")
            time.sleep(1)


# ---------------------------------------------------------------------------
# REST API Routes
# ---------------------------------------------------------------------------

@app.route("/api/topology", methods=["GET"])
def get_topology():
    """Get complete topology data for visualization."""
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        data = simulator.get_topology_data()
    return jsonify(data)


@app.route("/api/controllers", methods=["GET"])
def get_controllers():
    """Get all controller statuses."""
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        controllers = []
        for ctrl_id, ctrl in simulator.engine.controllers.items():
            info = ctrl.to_dict()
            info["switch_count"] = len(simulator.engine.get_switches_in_domain(ctrl_id))
            # Add per-resource totals
            switches = simulator.engine.get_switches_in_domain(ctrl_id)
            info["total_cpu_used"] = round(sum(s.load_cpu for s in switches), 2)
            info["total_mem_used"] = round(sum(s.load_mem for s in switches), 2)
            info["total_bw_used"] = round(sum(s.load_bw for s in switches), 2)
            info["cpu_utilization"] = round(info["total_cpu_used"] / ctrl.capacity_cpu * 100, 2) if ctrl.capacity_cpu > 0 else 0
            info["mem_utilization"] = round(info["total_mem_used"] / ctrl.capacity_mem * 100, 2) if ctrl.capacity_mem > 0 else 0
            info["bw_utilization"] = round(info["total_bw_used"] / ctrl.capacity_bw * 100, 2) if ctrl.capacity_bw > 0 else 0
            controllers.append(info)
    return jsonify(controllers)


@app.route("/api/switches", methods=["GET"])
def get_switches():
    """Get all switch details."""
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
    """Get migration history log."""
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        limit = request.args.get("limit", 50, type=int)
        history = [r.to_dict() for r in simulator.engine.migration_history[-limit:]]
    return jsonify(history)


@app.route("/api/migration/trigger", methods=["POST"])
def trigger_migration():
    """Manually trigger one round of DLBMT load balancing."""
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        # Update levels first
        simulator.engine.update_controller_levels()
        record = simulator.engine.run_load_balancing()

        if record:
            return jsonify({"success": True, "migration": record.to_dict()})
        else:
            return jsonify({"success": False, "message": "No migration needed or possible"})


@app.route("/api/migration/auto", methods=["POST"])
def toggle_auto_migration():
    """Toggle automatic migration on/off."""
    global auto_migration_enabled
    data = request.get_json() or {}
    if "enabled" in data:
        auto_migration_enabled = bool(data["enabled"])
    else:
        auto_migration_enabled = not auto_migration_enabled

    return jsonify({"auto_migration_enabled": auto_migration_enabled})


@app.route("/api/stats/timeseries", methods=["GET"])
def get_timeseries():
    """Get time-series load history for charts."""
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        limit = request.args.get("limit", 60, type=int)
        history = simulator.engine.load_history[-limit:]
    return jsonify(history)


@app.route("/api/stats/summary", methods=["GET"])
def get_stats_summary():
    """Get current stats summary."""
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        stats = simulator.engine.get_stats()
        stats["traffic"] = traffic_gen.get_traffic_summary()
        stats["auto_migration"] = auto_migration_enabled
        stats["simulation_speed"] = simulation_speed
    return jsonify(stats)


@app.route("/api/stats/comparison", methods=["GET"])
def get_comparison_stats():
    """Get comparison data matching paper metrics (Tables 4-7)."""
    with sim_lock:
        if simulator is None:
            return jsonify({"error": "Simulation not initialized"}), 500

        stats = simulator.engine.get_stats()
        history = simulator.engine.migration_history

        # Calculate average migration cost
        avg_cost = sum(r.migration_cost for r in history) / len(history) if history else 0

        # Calculate average imbalance improvement
        imbalance_improvements = []
        for r in history:
            if r.imbalance_before > 0:
                improvement = (r.imbalance_before - r.imbalance_after) / r.imbalance_before * 100
                imbalance_improvements.append(improvement)
        avg_improvement = sum(imbalance_improvements) / len(imbalance_improvements) if imbalance_improvements else 0

        return jsonify({
            "current_topology": simulator.topo_config["name"],
            "avg_load": stats["avg_load"],
            "global_imbalance": stats["global_imbalance"],
            "total_migrations": len(history),
            "avg_migration_cost": round(avg_cost, 4),
            "avg_imbalance_improvement": round(avg_improvement, 2),
            "controller_loads": stats["controller_loads"],
            "controller_levels": stats["controller_levels"],
            "domain_sizes": stats["domain_sizes"],
        })


@app.route("/api/config/topology", methods=["POST"])
def change_topology():
    """Change the network topology."""
    data = request.get_json() or {}
    topology = data.get("topology", "atlanta")
    if topology not in TOPOLOGIES:
        return jsonify({"error": f"Unknown topology. Choose from: {list(TOPOLOGIES.keys())}"}), 400

    init_simulation(topology)
    return jsonify({"success": True, "topology": topology})


@app.route("/api/config/topologies", methods=["GET"])
def list_topologies():
    """List available topologies."""
    topos = {}
    for key, val in TOPOLOGIES.items():
        topos[key] = {
            "name": val["name"],
            "nodes": val["nodes"],
            "edges": val["edges"],
            "controllers": val["controllers"],
        }
    return jsonify(topos)


@app.route("/api/config/traffic", methods=["POST"])
def configure_traffic():
    """Change traffic pattern and intensity."""
    data = request.get_json() or {}
    pattern = data.get("pattern", "wave")
    intensity = data.get("intensity", 1.0)

    with sim_lock:
        if traffic_gen is None:
            return jsonify({"error": "Simulation not initialized"}), 500
        try:
            traffic_gen.set_pattern(pattern, intensity)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    return jsonify({"pattern": pattern, "intensity": intensity})


@app.route("/api/config/speed", methods=["POST"])
def configure_speed():
    """Change simulation speed."""
    global simulation_speed
    data = request.get_json() or {}
    speed = data.get("speed", 1.0)
    simulation_speed = max(0.1, min(speed, 10.0))
    return jsonify({"speed": simulation_speed})


# ---------------------------------------------------------------------------
# WebSocket Events
# ---------------------------------------------------------------------------

@socketio.on("connect")
def handle_connect():
    logger.info("Client connected via WebSocket")
    with sim_lock:
        if simulator:
            emit("topology", simulator.get_topology_data())
            emit("state_update", {
                "snapshot": simulator.engine.take_snapshot(),
                "traffic": traffic_gen.get_traffic_summary(),
                "migration": None,
                "level_changes": {},
            })


@socketio.on("request_topology")
def handle_request_topology():
    with sim_lock:
        if simulator:
            emit("topology", simulator.get_topology_data())


# ---------------------------------------------------------------------------
# SPA Serving
# ---------------------------------------------------------------------------

@app.route("/")
def serve_index():
    return send_from_directory(DIST_DIR, "index.html")

@app.errorhandler(404)
def fallback(e):
    """Serve index.html for SPA client-side routing."""
    return send_from_directory(DIST_DIR, "index.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Initialize simulation
    init_simulation("atlanta")

    # Start background simulation thread
    sim_thread = threading.Thread(target=simulation_loop, daemon=True)
    sim_thread.start()

    logger.info("Starting DLBMT Dashboard Backend on port 5000...")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
