#!/usr/bin/env python3
"""Generate a daily auto0.g G-code payload for the eight-winch artwork.

Each of the eight axes (X Y Z A B C U V) runs an independent random walk:
the axis picks a random leg (a target distance to travel and a duration,
in ticks, over which to travel it), steps toward that target one tick at a
time, clamped to the configured cable-length bounds, and picks a new leg
when the current one finishes. All eight axes are emitted together as one
G1 line per tick, at a constant feedrate.

The day is divided into alternating action and break periods. During a
break, all axes park at 0 and the file dwells for the break length.
"""

import argparse
import random

AXES = "XYZABCUV"

# Cable length bounds, in mm, shared by all axes.
MIN_POS = 0.0
MAX_POS = 1500.0

# Random-walk leg parameters.
LEG_MIN_DISTANCE = 100.0   # mm
LEG_MAX_DISTANCE = 1000.0  # mm
LEG_MIN_TICKS = 5
LEG_MAX_TICKS = 30

TICK_SECONDS = 1.0

# Constant feedrate (mm/min) used for every G1 move.
FEEDRATE = 1200.0
PARK_FEEDRATE = 1200.0


class AxisWalk:
    """Independent random-walk state for a single axis."""

    def __init__(self, rng, start=0.0):
        self.pos = start
        self.rng = rng
        self.target = start
        self.step = 0.0
        self.ticks_remaining = 0

    def _start_new_leg(self):
        distance = self.rng.uniform(LEG_MIN_DISTANCE, LEG_MAX_DISTANCE)
        direction = self.rng.choice((-1.0, 1.0))
        target = self.pos + direction * distance
        if target < MIN_POS or target > MAX_POS:
            # Out of bounds: flip direction instead of clamping to a
            # zero-length leg that would leave the axis motionless.
            target = self.pos - direction * distance
        target = min(max(target, MIN_POS), MAX_POS)

        ticks = self.rng.randint(LEG_MIN_TICKS, LEG_MAX_TICKS)
        self.target = target
        self.step = (target - self.pos) / ticks
        self.ticks_remaining = ticks

    def tick(self):
        """Advance by one tick. Returns (new position, started new leg)."""
        new_leg = self.ticks_remaining == 0
        if new_leg:
            self._start_new_leg()

        self.ticks_remaining -= 1
        if self.ticks_remaining == 0:
            # Land exactly on the leg target to avoid drift from
            # accumulated floating-point steps.
            self.pos = self.target
        else:
            self.pos += self.step

        return self.pos, new_leg


def format_axis_values(values):
    return " ".join(f"{axis}{value:.2f}" for axis, value in zip(AXES, values))


def generate(total_seconds, action_length, break_length, seed):
    rng = random.Random(seed)
    walks = [AxisWalk(rng) for _ in AXES]

    lines = []
    lines.append("G90")
    lines.append("G92 " + format_axis_values([0.0] * len(AXES)))

    samples = [(0.0, [0.0] * len(AXES))]
    new_leg_markers = []  # list of (time, axis_index, position)

    elapsed = 0.0
    while elapsed < total_seconds:
        # Action period.
        action_end = min(elapsed + action_length, total_seconds)
        while elapsed < action_end:
            results = [w.tick() for w in walks]
            positions = [pos for pos, _ in results]
            lines.append(f"G1 {format_axis_values(positions)} F{FEEDRATE:.0f}")
            elapsed += TICK_SECONDS
            samples.append((elapsed, positions))
            for i, (pos, new_leg) in enumerate(results):
                if new_leg:
                    new_leg_markers.append((elapsed, i, pos))

        if elapsed >= total_seconds:
            break

        # Break period: park all axes and dwell.
        lines.append(f"G1 {format_axis_values([0.0] * len(AXES))} F{PARK_FEEDRATE:.0f}")
        lines.append("M400")
        lines.append(f"G4 S{break_length:.0f}")

        # The park move takes some time at PARK_FEEDRATE; the rest of the
        # break is spent dwelling at the parked (zero) position.
        max_delta = max(abs(w.pos) for w in walks)
        park_seconds = min(max_delta / (PARK_FEEDRATE / 60.0), break_length)

        for w in walks:
            w.pos = 0.0
            w.ticks_remaining = 0
        elapsed += park_seconds
        samples.append((elapsed, [0.0] * len(AXES)))
        elapsed += break_length - park_seconds
        samples.append((elapsed, [0.0] * len(AXES)))

    return lines, samples, new_leg_markers


def parse_duration(value):
    """Parse a duration like '30m', '5m', '10s', or a plain number of seconds."""
    value = value.strip().lower()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000.0
    if value.endswith("h"):
        return float(value[:-1]) * 3600.0
    if value.endswith("m"):
        return float(value[:-1]) * 60.0
    if value.endswith("s"):
        return float(value[:-1])
    return float(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="auto0.g", help="output G-code file")
    parser.add_argument("--total-length", default="10h",
                         help="total duration of the generated day (default: 10h)")
    parser.add_argument("--action-length", default="30m",
                         help="duration of each motion period (default: 30m)")
    parser.add_argument("--break-length", default="5m",
                         help="duration of each parked break (default: 5m)")
    parser.add_argument("--seed", type=int, default=0,
                         help="random seed for reproducibility (default: 0)")
    parser.add_argument("--plot", metavar="FILE",
                         help="write a plot of axis position vs. time to FILE "
                              "(e.g. plot.png) instead of showing it interactively "
                              "if FILE is '-'")
    args = parser.parse_args()

    total_seconds = parse_duration(args.total_length)
    action_length = parse_duration(args.action_length)
    break_length = parse_duration(args.break_length)

    lines, samples, new_leg_markers = generate(total_seconds, action_length, break_length, args.seed)

    with open(args.output, "w") as f:
        for line in lines:
            f.write(line + "\n")

    print(f"wrote {len(lines)} lines to {args.output}")

    if args.plot:
        plot_samples(samples, new_leg_markers, args.plot)


def plot_samples(samples, new_leg_markers, plot_target):
    import matplotlib
    if plot_target != "-":
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times = [t for t, _ in samples]
    fig, ax = plt.subplots(figsize=(12, 6))
    for i, axis in enumerate(AXES):
        positions = [pos[i] for _, pos in samples]
        line, = ax.plot(times, positions, label=axis)

        marker_times = [t for t, axis_i, _ in new_leg_markers if axis_i == i]
        marker_positions = [pos for _, axis_i, pos in new_leg_markers if axis_i == i]
        #ax.scatter(marker_times, marker_positions, color=line.get_color(), s=20, zorder=3)

    ax.set_xlabel("time (s)")
    ax.set_ylabel("position (mm)")
    ax.set_title("Winch axis position vs. time")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    if plot_target == "-":
        plt.show()
    else:
        fig.savefig(plot_target)
        print(f"wrote plot to {plot_target}")


if __name__ == "__main__":
    main()
