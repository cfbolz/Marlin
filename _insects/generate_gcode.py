#!/usr/bin/env python3
"""Generate a daily auto0.g G-code payload for the eight-winch artwork.

Each of the eight axes (X Y Z A B C U V) runs an independent random walk:
the axis picks a random leg (a target angle to travel and a duration,
in ticks, over which to travel it), steps toward that target one tick at a
time, clamped to the configured rotation bounds, and picks a new leg
when the current one finishes. All eight axes are emitted together as one
G1 line per tick, at a constant feedrate.

The day is divided into alternating action and break periods. During a
break, all axes park at 0 and the file dwells for the break length.

Each action period is split into --num-blocks equal-length blocks. Every
block's random walk starts and ends at 0 (the parked position), with the
same content across all boards for a given --seed. The order of blocks
within an action period is shuffled per board using --permutation-seed.
Since every block takes the same wall-clock time to execute regardless of
order (each starts and ends at rest at 0), all boards finish an action
period at approximately the same time, even though Marlin's actual
execution time for a block of G-code is not known in advance and varies
with travel distance. This keeps independently-running boards' breaks
roughly aligned without any inter-board communication.

Units are degrees of motor/drum rotation (matching the firmware
configuration, where 1 unit = 1 degree). 360 degrees = 1 full drum
rotation. Bounds and leg distances below are derived from a nominal 1cm
drum diameter (~31.4mm of cable per rotation) and are placeholders until
the hardware team measures real endstop limits.
"""

import argparse
import random

AXES = "XYZABCUV"

# Nominal conversion factor from mm of cable to degrees of drum rotation,
# based on a 1cm drum diameter (circumference = pi * 10mm). This is only
# used to derive the placeholder constants below; the generator itself
# operates entirely in degrees.
MM_TO_DEG = 360.0 / (3.141592653589793 * 10.0)

# Drum rotation bounds, in degrees, shared by all axes.
MIN_POS = 0.0
MAX_POS = 700.0 * MM_TO_DEG  # ~17189 deg, ~47.7 rotations

# Random-walk leg parameters.
LEG_MIN_DISTANCE = 100.0 * MM_TO_DEG   # deg, ~1146 deg
LEG_MAX_DISTANCE = 400.0 * MM_TO_DEG   # deg, ~11459 deg
LEG_MIN_TICKS = 5
LEG_MAX_TICKS = 30

TICK_SECONDS = 1.0

# Constant feedrate (deg/min) used for every G1 move.
FEEDRATE = 3200.0 * MM_TO_DEG


class AxisWalk:
    """Independent random-walk state for a single axis.

    Generates a fixed number of ticks, starting and ending at 0: legs are
    picked normally until too few ticks remain to safely fit another leg
    plus the final return-to-0 leg, at which point a single leg back to 0
    is used to fill the remaining ticks.
    """

    def __init__(self, rng, total_ticks, start=0.0):
        self.pos = start
        self.rng = rng
        self.target = start
        self.step = 0.0
        self.ticks_remaining = 0
        self.total_ticks_remaining = total_ticks

    def _start_new_leg(self):
        # Leave enough ticks for at least one more leg after this one,
        # unless we're already down to the final return-to-0 leg.
        if self.total_ticks_remaining <= LEG_MAX_TICKS:
            target = 0.0
            ticks = self.total_ticks_remaining
        else:
            distance = self.rng.uniform(LEG_MIN_DISTANCE, LEG_MAX_DISTANCE)
            direction = self.rng.choice((-1.0, 1.0))
            target = self.pos + direction * distance
            if target < MIN_POS or target > MAX_POS:
                # Out of bounds: flip direction instead of clamping to a
                # zero-length leg that would leave the axis motionless.
                target = self.pos - direction * distance
            target = min(max(target, MIN_POS), MAX_POS)

            # Leave at least LEG_MIN_TICKS remaining for the final
            # return-to-0 leg, so its step size stays bounded too.
            max_ticks = min(LEG_MAX_TICKS, self.total_ticks_remaining - LEG_MIN_TICKS)
            ticks = self.rng.randint(LEG_MIN_TICKS, max_ticks)

        self.target = target
        self.step = (target - self.pos) / ticks
        self.ticks_remaining = ticks

    def tick(self):
        """Advance by one tick. Returns (new position, started new leg)."""
        new_leg = self.ticks_remaining == 0
        if new_leg:
            self._start_new_leg()

        self.ticks_remaining -= 1
        self.total_ticks_remaining -= 1
        if self.ticks_remaining == 0:
            # Land exactly on the leg target to avoid drift from
            # accumulated floating-point steps.
            self.pos = self.target
        else:
            self.pos += self.step

        return self.pos, new_leg


def format_axis_values(values):
    return " ".join(f"{axis}{value:.2f}" for axis, value in zip(AXES, values))


def generate_block(block_ticks, block_seed):
    """Generate one block: block_ticks ticks of G1 lines, all axes starting
    and ending at 0. Returns (lines, deltas, new_leg_markers), where deltas
    is a list of per-tick [positions...] (length block_ticks, not including
    the starting 0) and new_leg_markers is a list of (tick_index, axis_index,
    position) for ticks where that axis started a new leg."""
    rng = random.Random(block_seed)
    walks = [AxisWalk(rng, block_ticks) for _ in AXES]

    lines = []
    deltas = []
    new_leg_markers = []

    for tick in range(block_ticks):
        results = [w.tick() for w in walks]
        positions = [pos for pos, _ in results]
        lines.append(f"G1 {format_axis_values(positions)} F{FEEDRATE:.0f}")
        deltas.append(positions)
        for i, (pos, new_leg) in enumerate(results):
            if new_leg:
                new_leg_markers.append((tick, i, pos))

    return lines, deltas, new_leg_markers


def emit_sleep(lines, samples, elapsed, sleep_seconds):
    """Append a parked dwell of sleep_seconds to lines/samples. M400 waits
    for any queued moves to actually finish before continuing, M18 then
    disables all stepper drivers for the dwell; the next G1 re-enables them
    automatically. Returns the new elapsed time."""
    if not sleep_seconds:
        return elapsed
    lines.append("M400")
    lines.append("M18")
    lines.append(f"G4 S{sleep_seconds:.1f}")
    elapsed += sleep_seconds
    samples.append((elapsed, [0.0] * len(AXES)))
    return elapsed


def generate(total_seconds, action_length, break_length, seed, num_blocks, permutation_seed, extra_sleep=0.0):
    block_ticks = max(1, round(action_length / num_blocks / TICK_SECONDS))

    perm_rng = random.Random(permutation_seed)

    lines = []
    lines.append("G90")
    lines.append("G92 " + format_axis_values([0.0] * len(AXES)))

    samples = [(0.0, [0.0] * len(AXES))]
    new_leg_markers = []  # list of (time, axis_index, position)

    elapsed = 0.0
    action_period = 0
    while elapsed < total_seconds:
        # Action period: generate num_blocks fresh blocks (same content on
        # every board, derived from --seed and the action period index),
        # then play them in a per-board, per-action-period shuffled order.
        # Every block starts and ends at 0, so the total wall-clock time
        # for the action period does not depend on the order, keeping
        # independently-running boards' breaks aligned.
        blocks = [generate_block(block_ticks, f"{seed}-{action_period}-{i}") for i in range(num_blocks)]

        # The first block of each action period is the one most likely to
        # visually align across boards, since all boards start it right
        # after a synchronized break. Pick it deterministically from
        # permutation_seed so that boards with different permutation seeds
        # are spread across different first blocks, then shuffle the rest.
        first = permutation_seed % num_blocks
        rest = [i for i in range(num_blocks) if i != first]
        perm_rng.shuffle(rest)
        order = [first] + rest
        assert set(order) == set(range(num_blocks))

        # Split --extra-sleep between the start and end of the action
        # period, so even boards whose first block is identical don't
        # start moving at exactly the same moment. The split is random per
        # board (perm_rng), but the total is the same for every board, so
        # it doesn't affect the alignment of breaks across boards.
        sleep_before = perm_rng.uniform(0.0, extra_sleep)
        sleep_after = extra_sleep - sleep_before

        lines.append(f"; --- action period {action_period}, block order {order}, "
                      f"extra sleep {sleep_before:.1f}s/{sleep_after:.1f}s ---")
        elapsed = emit_sleep(lines, samples, elapsed, sleep_before)

        for block_index in order:
            block_lines, block_deltas, block_new_legs = blocks[block_index]
            lines.append(f"; block {block_index} start")
            lines.extend(block_lines)
            for tick, positions in enumerate(block_deltas):
                elapsed += TICK_SECONDS
                samples.append((elapsed, positions))
            for tick, axis_i, pos in block_new_legs:
                new_leg_markers.append((elapsed - len(block_deltas) + tick + 1, axis_i, pos))
            lines.append(f"; block {block_index} end")

        elapsed = emit_sleep(lines, samples, elapsed, sleep_after)

        if elapsed >= total_seconds:
            break

        # Break period: all axes are already at 0 (parked) after the last
        # block's forced return-to-0 leg.
        lines.append(f"; --- break {action_period} ---")
        elapsed = emit_sleep(lines, samples, elapsed, break_length)

        action_period += 1

    # End of day: all axes are parked at 0 after the last block's forced
    # return-to-0 leg. Wait for that move to finish and disable steppers,
    # so the installation ends the day powered down rather than holding
    # position.
    lines.append("; --- end of day ---")
    lines.append("M400")
    lines.append("M18")

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
                         help="random seed for the block contents, shared "
                              "across all boards (default: 0)")
    parser.add_argument("--num-blocks", type=int, default=6,
                         help="number of equal-length blocks per action "
                              "period; each block starts and ends parked "
                              "at 0, and blocks are shuffled per action "
                              "period (default: 6)")
    parser.add_argument("--permutation-seed", type=int, default=0,
                         help="random seed for shuffling block order; use "
                              "a different value per board so boards look "
                              "different while staying time-aligned "
                              "(default: 0)")
    parser.add_argument("--extra-sleep", type=parse_duration, default="0s",
                         help="extra dwell time per action period, randomly "
                              "split between the start and end (split chosen "
                              "per board via --permutation-seed), so boards "
                              "with the same first block don't move in exact "
                              "lockstep (default: 0s)")
    parser.add_argument("--plot", metavar="FILE",
                         help="write a plot of axis position vs. time to FILE "
                              "(e.g. plot.png) instead of showing it interactively "
                              "if FILE is '-'")
    parser.add_argument("--video", metavar="FILE",
                         help="write an animation of winch bar heights vs. time "
                              "to FILE (e.g. preview.mp4)")
    parser.add_argument("--video-speedup", type=float, default=60.0,
                         help="how much faster than real time the video plays "
                              "(default: 60, i.e. 1 minute of motion per second "
                              "of video)")
    parser.add_argument("--video-fps", type=float, default=24.0,
                         help="frames per second for --video (default: 24)")
    args = parser.parse_args()

    total_seconds = parse_duration(args.total_length)
    action_length = parse_duration(args.action_length)
    break_length = parse_duration(args.break_length)

    lines, samples, new_leg_markers = generate(total_seconds, action_length, break_length,
                                                args.seed, args.num_blocks, args.permutation_seed,
                                                args.extra_sleep)

    with open(args.output, "w") as f:
        for line in lines:
            f.write(line + "\n")

    print(f"wrote {len(lines)} lines to {args.output}")

    if args.plot:
        plot_samples(samples, new_leg_markers, args.plot)

    if args.video:
        render_video(samples, args.video, args.video_speedup, args.video_fps)


def plot_samples(samples, new_leg_markers, plot_target):
    import matplotlib
    if plot_target != "-":
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times = [t for t, _ in samples]
    fig, (ax_pos, ax_speed) = plt.subplots(2, 1, figsize=(12, 12), sharex=True)
    for i, axis in enumerate(AXES):
        positions = [pos[i] for _, pos in samples]
        line, = ax_pos.plot(times, positions, label=axis)

        marker_times = [t for t, axis_i, _ in new_leg_markers if axis_i == i]
        marker_positions = [pos for _, axis_i, pos in new_leg_markers if axis_i == i]
        #ax_pos.scatter(marker_times, marker_positions, color=line.get_color(), s=20, zorder=3)

        speed_times = []
        speeds = []
        for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
            dt = t1 - t0
            if dt <= 0:
                continue
            speed_times.append((t0 + t1) / 2.0)
            speeds.append((p1[i] - p0[i]) / dt)
        ax_speed.plot(speed_times, speeds, label=axis, color=line.get_color())

    ax_pos.set_ylabel("position (deg)")
    ax_pos.set_title("Winch axis position vs. time")
    ax_pos.legend()
    ax_pos.grid(True, alpha=0.3)

    ax_speed.set_xlabel("time (s)")
    ax_speed.set_ylabel("speed (deg/s)")
    ax_speed.set_title("Winch axis speed vs. time")
    ax_speed.legend()
    ax_speed.grid(True, alpha=0.3)

    fig.tight_layout()

    if plot_target == "-":
        plt.show()
    else:
        fig.savefig(plot_target)
        print(f"wrote plot to {plot_target}")


def render_video(samples, video_target, speedup, fps):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.animation as animation

    times = np.array([t for t, _ in samples])
    positions = np.array([pos for _, pos in samples])  # shape (n_samples, len(AXES))

    total_seconds = times[-1]
    video_seconds = total_seconds / speedup
    n_frames = max(1, int(video_seconds * fps))
    frame_times = np.linspace(0.0, total_seconds, n_frames)

    # Linearly interpolate each axis's position at each frame time.
    frame_positions = np.empty((n_frames, len(AXES)))
    for i in range(len(AXES)):
        frame_positions[:, i] = np.interp(frame_times, times, positions[:, i])

    fig, ax = plt.subplots(figsize=(8, 6))
    x = np.arange(len(AXES))
    bars = ax.bar(x, frame_positions[0], color="tab:blue")
    ax.set_xticks(x)
    ax.set_xticklabels(AXES)
    ax.set_ylim(MIN_POS, MAX_POS)
    ax.set_ylabel("position (deg)")
    title = ax.set_title("")

    def update(frame_i):
        for bar, height in zip(bars, frame_positions[frame_i]):
            bar.set_height(height)
        title.set_text(f"t = {frame_times[frame_i]:.0f}s")
        return list(bars) + [title]

    def progress(current_frame, total_frames):
        print(f"\rwriting video: frame {current_frame + 1}/{total_frames}", end="", flush=True)

    anim = animation.FuncAnimation(fig, update, frames=n_frames, blit=False)
    writer = animation.FFMpegWriter(fps=fps)
    anim.save(video_target, writer=writer, progress_callback=progress)
    print(f"\nwrote video to {video_target}")


if __name__ == "__main__":
    main()
