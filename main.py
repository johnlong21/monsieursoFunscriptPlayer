# By monsieur_so.
import mpv
import sys
import click
import os.path
import pathlib
import json
import bisect
import copy
import buttplug
import logging
import asyncio
import queue
import threading
import datetime

# Configuration.

# Device.
SPEED_RANGE = [0, 100]
SPEED_MULTIPLIER = 1.0

# Internal tweaks.
# In ms.
THRESHOLD_VIDEO_JUMP = 100
# In ms.
DELAY_BETWEEN_DEVICE_CMD = 50

DEFAULT_STARTING_INSTRUCTION = 0.0
# In seconds.
KEEP_ALIVE_DEVICE_DELAY = 5.0

DEVICES_PREFERENCES = {
    "Lovense Max": {"actuators": {"Air Pump": {"range": [0, 0.25]}}},
    # TODO Allows to offset or invert actuators,
    # so we can have wave feelings?
    "Lovense Edge": {"actuators": {0: {"range": [0, 1.0]}, 1: {"range": [0, 0.2]}}},
    "Lovense Gush": {"actuators": {0: {"range": [0, 0.7]}}},
}

# TODO Try a VLC implementation.


def position_to_vibrator_speed(speed):
    # https://github.com/martinAlt335/Funscript-Player/blob/a4b5b0949147b438cc7019de12a1646d54dba8d0/src/app/core/services/buttplug/buttplug.service.ts#L152
    # https://github.com/FredTungsten/ScriptPlayer/blob/4da4bd73b0f9bf028403b2589df0e93c0b89036c/ScriptPlayer/ScriptPlayer.Shared/Devices/CommandConverter.cs#L20
    return (
        1.0 - max(min(speed * SPEED_MULTIPLIER, SPEED_RANGE[1]), SPEED_RANGE[0]) * 0.01
    )


def position_to_speed(progress, from_pos, to_pos):
    # https://github.com/FredTungsten/ScriptPlayer/blob/4da4bd73b0f9bf028403b2589df0e93c0b89036c/ScriptPlayer/ScriptPlayer.Shared/Devices/ButtplugAdapter.cs#L158
    speed_from = position_to_vibrator_speed(from_pos)
    speed_to = position_to_vibrator_speed(to_pos)
    #  Progress seems the progress of the current action?
    # https://github.com/FredTungsten/ScriptPlayer/blob/4da4bd73b0f9bf028403b2589df0e93c0b89036c/ScriptPlayer/ScriptPlayer.Shared/Scripts/ScriptHandler.cs#L710
    # https://github.com/FredTungsten/ScriptPlayer/blob/4da4bd73b0f9bf028403b2589df0e93c0b89036c/ScriptPlayer/ScriptPlayer/ViewModels/MainViewModel.cs#L3936
    # https://github.com/FredTungsten/ScriptPlayer/blob/4da4bd73b0f9bf028403b2589df0e93c0b89036c/ScriptPlayer/ScriptPlayer.Shared/Beats/BeatGroup.cs#L62
    speed = speed_from * (1 - progress) + speed_to * progress
    # logging.info(f"{from_pos}->{speed_from} {to_pos}->{speed_to} = {speed} for {progress}%")
    return speed


def process_instruction_for_action(
    last_ts_ms,
    current_ts_ms,
    last_action,
    current_action,
    next_action,
    previous_action_change_ts_ms,
    last_instruction,
):
    # TODO Check funscript inverted, range, etc.
    # Convert position following the speed interval.
    pos = min(max(current_action["pos"], SPEED_RANGE[1]), SPEED_RANGE[0])
    # instruction = 1 - pos * 0.01
    instruction = pos * 0.01
    delta_previous_action = (
        current_ts_ms - previous_action_change_ts_ms
        if previous_action_change_ts_ms
        else 0.0
    )
    duration = next_action["at"] - current_action["at"] if next_action else None
    progress = (current_ts_ms - current_action["at"]) / duration if duration else None
    # If next action is at more than 15 seconds,
    # do not use position to speed but set instruction to current level.
    if duration and duration > 15 * 1000:
        # TODO Display a label "Next action in N seconds"
        # on player.
        return instruction
    # Use position to speed.
    instruction = (
        position_to_speed(progress, last_action["pos"], current_action["pos"])
        if last_action
        else position_to_vibrator_speed(current_action["pos"])
    )
    # logging.info(f"Speed {speed:.2f} {progress}")
    # if instruction != last_instruction:
    #     logging.info(f"dur={duration}ms progress={progress}")
    #     logging.info(
    #         f"Delta {delta_previous_action:.1f}ms pos={current_action['pos']} -> {pos} -> {instruction:.2f}"
    #     )
    return instruction


def index_funscript(fs_json):
    actions_indexed = copy.deepcopy(fs_json["actions"])
    actions_indexed.sort(key=lambda a: a["at"])
    timestamps = [a["at"] for a in actions_indexed]

    def find_after(t):
        idx = bisect.bisect_left(timestamps, t)
        # If timestamp is before the found index.
        if t < actions_indexed[idx]["at"]:
            idx -= 1
        # Handle end of file indexes.
        # TODO In this case send a zero instruction?
        if idx == len(timestamps):
            idx -= 1
        return idx, actions_indexed[idx]

    def find_after_with_last_idx(t, last_idx, video_jumped):
        if video_jumped or last_idx is None:
            return find_after(t)
        # Check the current timestamp is in last action or next action interval.
        last_action = actions_indexed[last_idx]
        last_next_action = (
            actions_indexed[last_idx + 1]
            if last_idx + 1 < len(actions_indexed)
            else None
        )
        last_next_next_action = (
            actions_indexed[last_idx + 2]
            if last_idx + 2 < len(actions_indexed)
            else None
        )
        if last_next_action and t < last_next_action["at"]:
            # Certainly always the same action.
            if t >= last_action["at"]:
                return last_idx, last_action
        elif last_next_next_action:
            # Maybe be the next action (or the next next action).
            # Check though this isn't after the next next action.
            if t < last_next_next_action["at"]:
                # logging.debug("Economy: next action %s %s", t, last_next_action)
                return last_idx + 1, last_next_action
        # We're not sure, do a proper lookup.
        return find_after(t)

    fs_json["actions_indexed"] = actions_indexed
    fs_json["find_after"] = find_after
    fs_json["find_after_with_last_idx"] = find_after_with_last_idx
    return fs_json


def load_funscript(funscript_path):
    if not os.path.isfile(funscript_path):
        raise RuntimeError(f"Funscript not found at path: {funscript_path}")
    fs_json = None
    with open(funscript_path, "r") as fs_fp:
        fs_json = json.load(fs_fp)
    return fs_json


async def send_instruction_to_devices(devices, instruction):
    # TODO Settings to change level following devices.
    # TODO Handle exception as a task group?
    # (For the inner await)
    for device_idx, device in devices:
        device_preferences = (
            DEVICES_PREFERENCES.get(device.name)
            or DEVICES_PREFERENCES.get(device_idx)
            or {}
        )
        for actuactor in device.actuators:
            actuactor_preferences = device_preferences.get("actuators", {}).get(
                actuactor.description
            ) or device_preferences.get("actuators", {}).get(actuactor.index)
            actual_instruction = instruction
            if actuactor_preferences:
                if actuactor_preferences.get("range"):
                    actual_instruction = max(
                        min(instruction, actuactor_preferences["range"][1]),
                        actuactor_preferences["range"][0],
                    )
            try:
                await actuactor.command(actual_instruction)
            # TODO Handle DeviceNotAvailable error only.
            # except BaseException as be:
            #     logging.error("Cautch BaseException")
            #     logging.exception(be)
            #     raise be
            except Exception as e:
                logging.exception(e)


async def buttplug_loop(intiface_central_ws, debug, q):
    buttplug_client = buttplug.Client(
        "PyMPV Funscript Player", buttplug.ProtocolSpec.v3
    )
    try:
        connector = buttplug.WebsocketConnector(
            intiface_central_ws, logger=buttplug_client.logger
        )
        await buttplug_client.connect(connector)
    except Exception as e:
        logging.error(f"Could not connect to Intiface Central server, exiting: {e}")
        return

    try:
        buttplug_client.logger.info(f"Devices: {buttplug_client.devices}")
        if len(buttplug_client.devices) == 0:
            logging.warning("No device found. Just playing video without sync")
        last_instruction = None
        last_reconnect = datetime.datetime.now()
        is_scanning = False
        devices = buttplug_client.devices.items()
        last_devices = devices
        while True:
            # Scan to get new devices on the fly.
            if is_scanning and (datetime.datetime.now() - last_reconnect).total_seconds() > 5:
                await buttplug_client.stop_scanning()
            if (datetime.datetime.now() - last_reconnect).total_seconds() > 10:
                # await buttplug_client.disconnect()
                # await buttplug_client.reconnect()
                # logging.info("Scanning new devices...")
                await buttplug_client.start_scanning()
                is_scanning = True
                devices = buttplug_client.devices.items()
                last_reconnect = datetime.datetime.now()
                if len(devices) > len(last_devices):
                    logging.info(
                        "%d new device(s) detected", len(devices) - len(last_devices)
                    )
                elif len(devices) < len(last_devices):
                    logging.info(
                        "%d device(s) deconnected", len(last_devices) - len(devices)
                    )
                last_devices = devices
                await buttplug_client.stop_scanning()
            try:
                current_ts, instruction = q.get(
                    block=True, timeout=KEEP_ALIVE_DEVICE_DELAY
                )
            except queue.Empty:
                logging.debug(
                    "Keep buttplug alive, re-sending last instruction %s",
                    str(last_instruction),
                )
                await send_instruction_to_devices(
                    devices,
                    (
                        last_instruction
                        if last_instruction is not None
                        else DEFAULT_STARTING_INSTRUCTION
                    ),
                )
                continue
            # logging.info(f"Queue: received instruction {instruction}")
            q.task_done()
            if instruction == "close":
                logging.info("Closing Intiface Central client...")
                break
            # TODO Limit the consecutive commands sent to the device.
            #   (use DELAY_BETWEEN_DEVICE_CMD to filter or delay)
            # Play on all devices.
            await send_instruction_to_devices(devices, instruction)
            last_instruction = instruction
    finally:
        await buttplug_client.disconnect()


def mpv_log_handler(loglevel, component, message):
    logging.info("[{}] {}: {}".format(loglevel, component, message))


@click.command()
@click.argument("video_path", type=click.Path(exists=True))
# TODO Option for funscript path.
@click.option(
    "--intiface-central-ws",
    default="ws://localhost:12345",
    help="The Intiface Central server address.",
)
@click.option("--debug", is_flag=True)
def play_video(video_path, intiface_central_ws, debug):
    """Play a video VIDEO_PATH with MPV and Intiface Central.

    VIDEO_PATH is the path of the video to play.
    """
    logging.basicConfig(
        stream=sys.stdout, level=logging.DEBUG if debug else logging.INFO
    )
    # Load funscript.
    video_path_info = pathlib.Path(os.path.abspath(video_path))
    funscript_path = video_path_info.with_suffix(".funscript")
    fs_indexed = index_funscript(load_funscript(funscript_path))

    # Queue used to communicate between buttplug loop and MPV observer.
    # TODO Manage the MPV threading to do all work with asyncio.
    # https://docs.python.org/3/library/asyncio-sync.html
    # https://docs.python.org/3/library/asyncio-queue.html
    q = queue.Queue()

    # Prepare Buttplug.io device(s).
    threading.Thread(
        target=lambda: asyncio.run(
            buttplug_loop(intiface_central_ws, debug, q), debug=debug
        ),
    ).start()

    # Hook funscript synchro with MPV.
    # https://github.com/jaseg/python-mpv#advanced-usage
    player = mpv.MPV(
        log_handler=mpv_log_handler,
        input_default_bindings=True,
        input_vo_keyboard=True,
        osc=True,
    )
    global is_paused
    is_paused = False
    global last_idx
    last_idx = None

    def time_observer_closure():
        global last_ts_ms
        last_ts_ms = None
        global previous_action_change_ts_ms
        previous_action_change_ts_ms = None
        global previous_changed_idx
        previous_changed_idx = None
        global last_instruction
        last_instruction = None

        def time_observer(_name, current_t_s):
            global last_idx
            global last_ts_ms
            global previous_action_change_ts_ms
            global previous_changed_idx
            global last_instruction
            # Here, _value is either None if nothing is playing or a float containing
            # fractional seconds since the beginning of the file.
            if current_t_s is None or is_paused is True:
                return
            # We seems to have an interval of 33ms between calls.
            # TODO Is that enough resolution or do we want another finer grained
            #   timer for controlling the device(s)? (eg. 5ms resolution)
            #   See threading.Timer and time.time() (16ms reso on Windows)
            current_ts_ms = current_t_s * 1000
            # logging.info(
            #     "Current frame at %.2fms; delta from last frame is %.2fms",
            #     current_ts_ms,
            #     current_ts_ms - last_ts_ms if last_ts_ms else 0.0,
            # )
            video_jumped = (
                abs(current_ts_ms - last_ts_ms) > THRESHOLD_VIDEO_JUMP
                if last_ts_ms
                else False
            )
            idx, action = None, None
            if video_jumped:
                logging.info("Detected video jump")
            # Try to use last action to "follow" the video flow.
            idx, action = fs_indexed["find_after_with_last_idx"](
                current_ts_ms, last_idx, video_jumped
            )
            # Always process current instruction.
            previous_action = (
                fs_indexed["actions_indexed"][previous_changed_idx]
                if last_idx and previous_changed_idx
                else None
            )
            next_action = (
                fs_indexed["actions_indexed"][idx + 1]
                if idx + 1 < len(fs_indexed["actions_indexed"])
                else None
            )
            current_instruction = process_instruction_for_action(
                last_ts_ms,
                current_ts_ms,
                previous_action,
                action,
                next_action,
                previous_action_change_ts_ms,
                last_instruction,
            )
            if last_idx is None or current_instruction != last_instruction:
                # Instruction has changed, send it to Buttplug.io
                q.put((current_ts_ms, current_instruction))
            if idx != last_idx:
                # logging.debug(f"Now playing at {current_ts_ms:.2f}ms: {action}")
                logging.info(
                    f"idx={idx} last_idx={last_idx} previous_changed_idx={previous_changed_idx}"
                )
                logging.info(
                    "Current action %s at %.2fms. Instruction processed %s",
                    action,
                    current_ts_ms,
                    current_instruction,
                )
                previous_action_change_ts_ms = current_ts_ms
                previous_changed_idx = last_idx
            last_instruction = current_instruction
            last_idx = idx
            last_ts_ms = current_ts_ms

        return time_observer

    # https://mpv.io/manual/master/#command-interface-time-pos
    # TODO Get pause action observer (stop the vibration).
    player.observe_property("time-pos/full", time_observer_closure())

    @player.property_observer("core-idle")
    def core_idle_observer(_name, is_idle):
        global is_paused
        global last_idx
        is_paused = is_idle
        if is_idle:
            logging.info("Skipped or paused")
            # We reset the last action index, so we search
            # the new proper action (in case of video jumps).
            last_idx = None
            # Stop vibrator.
            q.put((None, 0))

    # Play video.
    # player.fullscreen = True
    # Option access, in general these require the core to reinitialize
    # player['vo'] = 'gpu'
    player.play(video_path)
    try:
        player.wait_for_playback()
    finally:
        logging.info("Closing MPV...")
        q.put((None, "close"))


if __name__ == "__main__":
    play_video()
