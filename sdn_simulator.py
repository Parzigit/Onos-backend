"""
SDN Simulator – Multi-controller SDN environment simulation
Creates realistic topologies with controllers and switches,
computes distance matrices, and manages the network state.
"""

import math
import random
import networkx as nx
from typing import Dict, List, Tuple
from dlbmt_engine import Controller, Switch, DLBMTEngine, ControllerLevel


# ---------------------------------------------------------------------------
# Topology Definitions (from Table 3 of the paper)
# ---------------------------------------------------------------------------

TOPOLOGIES = {
    "atlanta": {
        "name": "Atlanta",
        "nodes": 15,
        "edges": 22,
        "controllers": 3,
        "capacities": [
            {"cpu": 2000, "mem": 4096, "bw": 1000},
            {"cpu": 2000, "mem": 4096, "bw": 1000},
            {"cpu": 2500, "mem": 4096, "bw": 1200},
        ],
    },
    "arn": {
        "name": "ARN",
        "nodes": 30,
        "edges": 29,
        "controllers": 4,
        "capacities": [
            {"cpu": 2500, "mem": 4096, "bw": 1200},
            {"cpu": 2500, "mem": 4096, "bw": 1200},
            {"cpu": 2500, "mem": 4096, "bw": 1200},
            {"cpu": 3000, "mem": 8192, "bw": 1500},
        ],
    },
    "germany50": {
        "name": "Germany50",
        "nodes": 50,
        "edges": 88,
        "controllers": 5,
        "capacities": [
            {"cpu": 3000, "mem": 8192, "bw": 1500},
            {"cpu": 3000, "mem": 8192, "bw": 1500},
            {"cpu": 3000, "mem": 8192, "bw": 1500},
            {"cpu": 3500, "mem": 8192, "bw": 2000},
            {"cpu": 4000, "mem": 16384, "bw": 2000},
        ],
    },
    "interroute": {
        "name": "Interroute",
        "nodes": 110,
        "edges": 159,
        "controllers": 7,
        "capacities": [
            {"cpu": 4000, "mem": 16384, "bw": 2000},
            {"cpu": 4000, "mem": 16384, "bw": 2000},
            {"cpu": 4000, "mem": 16384, "bw": 2000},
            {"cpu": 4000, "mem": 16384, "bw": 2000},
            {"cpu": 4000, "mem": 16384, "bw": 2000},
            {"cpu": 5000, "mem": 32768, "bw": 3000},
            {"cpu": 5000, "mem": 32768, "bw": 3000},
        ],
    },
}


def generate_topology_graph(num_nodes: int, num_edges: int) -> nx.Graph:
    """Generate a connected graph resembling a network topology."""
    # Start with a spanning tree for connectivity
    G = nx.Graph()
    nodes = list(range(num_nodes))
    G.add_nodes_from(nodes)

    # Create spanning tree
    shuffled = nodes.copy()
    random.shuffle(shuffled)
    for i in range(1, len(shuffled)):
        G.add_edge(shuffled[i - 1], shuffled[i])

    # Add extra edges to reach desired count
    extra_needed = num_edges - (num_nodes - 1)
    attempts = 0
    while G.number_of_edges() < num_edges and attempts < num_edges * 10:
        u = random.randint(0, num_nodes - 1)
        v = random.randint(0, num_nodes - 1)
        if u != v and not G.has_edge(u, v):
            G.add_edge(u, v)
        attempts += 1

    return G


def assign_positions(G: nx.Graph) -> Dict[int, Tuple[float, float]]:
    """Assign 2D positions to nodes using force-directed layout."""
    pos = nx.spring_layout(G, k=2.0, iterations=100, seed=42)
    # Scale to [100, 900] range for SVG
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    range_x = max_x - min_x if max_x > min_x else 1
    range_y = max_y - min_y if max_y > min_y else 1

    scaled = {}
    for node, (x, y) in pos.items():
        sx = 100 + 700 * (x - min_x) / range_x
        sy = 100 + 500 * (y - min_y) / range_y
        scaled[node] = (round(sx, 1), round(sy, 1))
    return scaled


class SDNSimulator:
    """
    Simulates a multi-controller SDN environment.
    """

    def __init__(self, topology_name: str = "atlanta"):
        self.topology_name = topology_name
        self.topo_config = TOPOLOGIES[topology_name]
        self.graph: nx.Graph = None
        self.engine = DLBMTEngine()
        self.positions: Dict[int, Tuple[float, float]] = {}
        self.edges: List[Tuple[str, str]] = []
        self.tick_count = 0

        self._build_topology()

    def _build_topology(self):
        """Build the network topology with controllers and switches."""
        num_nodes = self.topo_config["nodes"]
        num_edges = self.topo_config["edges"]
        num_controllers = self.topo_config["controllers"]
        capacities = self.topo_config["capacities"]

        # Generate graph
        self.graph = generate_topology_graph(num_nodes, num_edges)
        self.positions = assign_positions(self.graph)

        # Select controller nodes (spread across the graph)
        # Use nodes with highest degree as controllers
        degrees = sorted(self.graph.degree(), key=lambda x: x[1], reverse=True)
        controller_nodes = [degrees[i][0] for i in range(num_controllers)]

        # Create controllers
        for i, node_id in enumerate(controller_nodes):
            cap = capacities[i]
            pos = self.positions[node_id]
            ctrl = Controller(
                id=f"C{i+1}",
                capacity_cpu=cap["cpu"],
                capacity_mem=cap["mem"],
                capacity_bw=cap["bw"],
                x=pos[0],
                y=pos[1],
            )
            self.engine.add_controller(ctrl)

        # Assign remaining nodes as switches to nearest controller
        switch_nodes = [n for n in range(num_nodes) if n not in controller_nodes]

        for node_id in switch_nodes:
            # Find nearest controller by graph distance
            min_dist = float('inf')
            nearest_ctrl = None
            pos = self.positions[node_id]

            for ctrl_node, ctrl_id in zip(controller_nodes,
                                           [f"C{i+1}" for i in range(num_controllers)]):
                try:
                    dist = nx.shortest_path_length(self.graph, node_id, ctrl_node)
                except nx.NetworkXNoPath:
                    dist = float('inf')
                if dist < min_dist:
                    min_dist = dist
                    nearest_ctrl = ctrl_id

            switch = Switch(
                id=f"S{node_id+1}",
                controller_id=nearest_ctrl,
                x=pos[0],
                y=pos[1],
            )
            self.engine.add_switch(switch)

        # Compute distance matrix (hop count between each switch and each controller)
        for switch_id, switch in self.engine.switches.items():
            s_node = int(switch_id[1:]) - 1  # Convert S1 -> 0
            for ctrl_id, ctrl in self.engine.controllers.items():
                c_idx = int(ctrl_id[1:]) - 1
                c_node = controller_nodes[c_idx]
                try:
                    dist = nx.shortest_path_length(self.graph, s_node, c_node)
                except nx.NetworkXNoPath:
                    dist = 10  # Default large distance
                self.engine.set_distance(switch_id, ctrl_id, max(dist, 1))

        # Store edges for topology visualization
        self.edges = []
        for u, v in self.graph.edges():
            node_u = self._node_to_id(u, controller_nodes)
            node_v = self._node_to_id(v, controller_nodes)
            self.edges.append((node_u, node_v))

    def _node_to_id(self, node: int, controller_nodes: List[int]) -> str:
        """Convert graph node index to controller/switch ID."""
        if node in controller_nodes:
            idx = controller_nodes.index(node)
            return f"C{idx+1}"
        return f"S{node+1}"

    def get_topology_data(self) -> dict:
        """Get complete topology data for visualization."""
        nodes = []

        for ctrl_id, ctrl in self.engine.controllers.items():
            nodes.append({
                "id": ctrl.id,
                "type": "controller",
                "x": ctrl.x,
                "y": ctrl.y,
                "load": round(ctrl.load_percentage, 2),
                "level": ctrl.level.value,
                "level_label": ctrl.level.label,
                "level_color": ctrl.level.color,
                "active": ctrl.active,
                "capacity_cpu": ctrl.capacity_cpu,
                "capacity_mem": ctrl.capacity_mem,
                "capacity_bw": ctrl.capacity_bw,
                "switch_count": len(self.engine.get_switches_in_domain(ctrl.id)),
            })

        for sw_id, sw in self.engine.switches.items():
            ctrl = self.engine.controllers.get(sw.controller_id)
            usage = self.engine.compute_switch_resource_usage(sw, ctrl) if ctrl else 0
            nodes.append({
                "id": sw.id,
                "type": "switch",
                "x": sw.x,
                "y": sw.y,
                "controller_id": sw.controller_id,
                "load_cpu": round(sw.load_cpu, 2),
                "load_mem": round(sw.load_mem, 2),
                "load_bw": round(sw.load_bw, 2),
                "packet_in_rate": round(sw.packet_in_rate, 2),
                "resource_usage": round(usage * 100, 2),
            })

        links = []
        for u, v in self.edges:
            links.append({"source": u, "target": v})

        # Add domain links (switch to controller)
        for sw_id, sw in self.engine.switches.items():
            links.append({
                "source": sw.id,
                "target": sw.controller_id,
                "type": "domain",
            })

        return {
            "nodes": nodes,
            "links": links,
            "topology_name": self.topo_config["name"],
        }

    def change_topology(self, topology_name: str):
        """Switch to a different topology."""
        if topology_name not in TOPOLOGIES:
            raise ValueError(f"Unknown topology: {topology_name}. Choose from: {list(TOPOLOGIES.keys())}")

        self.topology_name = topology_name
        self.topo_config = TOPOLOGIES[topology_name]
        self.engine = DLBMTEngine()
        self.tick_count = 0
        self._build_topology()
