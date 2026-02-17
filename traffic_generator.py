"""
Traffic Generator – Simulates dynamic network traffic patterns
for the SDN simulator. Maps Packet-In rates to CPU, Memory,
and Bandwidth consumption on controllers.
"""

import math
import random
import time
from typing import Dict, List
from dlbmt_engine import Switch, Controller, DLBMTEngine


class TrafficGenerator:
    """
    Generates dynamic traffic for switches in the SDN simulation.
    Supports multiple traffic patterns:
    - uniform: All switches generate similar traffic
    - hotspot: Specific switches generate much heavier traffic
    - burst: Random bursts of traffic on different switches
    - wave: Sinusoidal traffic pattern that shifts over time
    """

    def __init__(self, engine: DLBMTEngine):
        self.engine = engine
        self.pattern = "wave"
        self.intensity = 1.0        # Global traffic intensity multiplier
        self.tick = 0
        self.burst_targets: Dict[str, float] = {}
        self.burst_timer = 0

        # Per-switch resource consumption factors
        # These map packet_in_rate to CPU/Mem/BW consumption
        # Tuned so that moderate traffic (50-100 pps per switch) on a controller
        # with 5-6 switches produces loads in the 40-80% range
        self.cpu_per_packet = 1.2      # CPU units per pkt/s
        self.mem_per_packet = 2.5      # MB per pkt/s
        self.bw_per_packet = 0.08      # Mbps per pkt/s

    def set_pattern(self, pattern: str, intensity: float = 1.0):
        """Change the traffic generation pattern."""
        valid = ["uniform", "hotspot", "burst", "wave", "stress"]
        if pattern not in valid:
            raise ValueError(f"Invalid pattern: {pattern}. Choose from: {valid}")
        self.pattern = pattern
        self.intensity = max(0.1, min(intensity, 5.0))

    def generate_tick(self):
        """
        Generate one tick of traffic for all switches.
        Updates switch load_cpu, load_mem, load_bw based on simulated Packet-In rates.
        """
        self.tick += 1

        switches = list(self.engine.switches.values())
        controllers = self.engine.get_active_controllers()

        if not switches or not controllers:
            return

        # Generate base packet-in rates based on pattern
        rates = self._generate_rates(switches)

        # Apply rates to switches and compute resource consumption
        for switch in switches:
            rate = rates.get(switch.id, 10.0) * self.intensity

            # Add some noise
            rate *= (1.0 + random.gauss(0, 0.05))
            rate = max(0, rate)

            switch.packet_in_rate = rate

            # Map packet-in rate to resource consumption
            # Each packet-in request consumes CPU, memory, and bandwidth
            switch.load_cpu = rate * self.cpu_per_packet * (0.9 + random.random() * 0.2)
            switch.load_mem = rate * self.mem_per_packet * (0.9 + random.random() * 0.2)
            switch.load_bw = rate * self.bw_per_packet * (0.9 + random.random() * 0.2)

    def _generate_rates(self, switches: List[Switch]) -> Dict[str, float]:
        """Generate packet-in rates based on current pattern."""
        rates = {}

        if self.pattern == "uniform":
            base = 50.0
            for sw in switches:
                rates[sw.id] = base + random.gauss(0, 5)

        elif self.pattern == "hotspot":
            # First 30% of switches in first controller domain get heavy traffic
            all_switch_ids = sorted([s.id for s in switches])
            n_hot = max(1, len(all_switch_ids) // 4)
            hot_switches = set(all_switch_ids[:n_hot])

            for sw in switches:
                if sw.id in hot_switches:
                    # Heavy traffic: 3-5x normal
                    rates[sw.id] = 150.0 + random.gauss(0, 20)
                else:
                    rates[sw.id] = 30.0 + random.gauss(0, 5)

        elif self.pattern == "burst":
            # Periodic bursts on random switches
            if self.tick % 15 == 0 or not self.burst_targets:
                # New burst targets
                n_burst = max(1, len(switches) // 5)
                burst_sws = random.sample(switches, n_burst)
                self.burst_targets = {sw.id: 200.0 + random.random() * 100
                                       for sw in burst_sws}
                self.burst_timer = 10

            self.burst_timer -= 1
            if self.burst_timer <= 0:
                self.burst_targets = {}

            for sw in switches:
                if sw.id in self.burst_targets:
                    rates[sw.id] = self.burst_targets[sw.id]
                else:
                    rates[sw.id] = 30.0 + random.gauss(0, 5)

        elif self.pattern == "wave":
            # Sinusoidal wave that moves across controllers
            t = self.tick * 0.15
            all_ctrls = sorted(self.engine.controllers.keys())

            for sw in switches:
                ctrl_idx = all_ctrls.index(sw.controller_id) if sw.controller_id in all_ctrls else 0
                phase = ctrl_idx * (2 * math.pi / len(all_ctrls))
                wave = math.sin(t + phase)
                # wave goes from -1 to 1, map to traffic rate
                rate = 40.0 + 60.0 * (wave + 1) / 2  # Range: 40 - 100
                rates[sw.id] = rate

        elif self.pattern == "stress":
            # Very heavy traffic on all switches to trigger migrations
            for sw in switches:
                rates[sw.id] = 120.0 + random.gauss(0, 30)

        return rates

    def get_traffic_summary(self) -> dict:
        """Get summary of current traffic state."""
        switches = list(self.engine.switches.values())
        if not switches:
            return {"total_pps": 0, "avg_pps": 0, "max_pps": 0, "pattern": self.pattern}

        total = sum(s.packet_in_rate for s in switches)
        avg = total / len(switches)
        max_rate = max(s.packet_in_rate for s in switches)

        return {
            "total_pps": round(total, 2),
            "avg_pps": round(avg, 2),
            "max_pps": round(max_rate, 2),
            "pattern": self.pattern,
            "intensity": self.intensity,
            "tick": self.tick,
        }
