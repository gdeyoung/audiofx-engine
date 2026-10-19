#!/usr/bin/env python3
"""audiofx engine — system-wide audio effects as a PipeWire filter-chain.

Model (mirrors omarchy-audio-tuning, the proven Omarchy pattern):
  - One virtual sink (gdy_audiofx) hosted by its own PipeWire *client*
    (pipewire -c audiofx.conf), run as a user systemd unit. Preset switches
    restart the client, never the daemon -- Pulse clients (Spotify, browsers,
    Steam) keep their connection.
  - Effects compose: [ 3D convolver bank ] -> [ bass EQ IR ] -> [ night EQ IR ].
    Bypassed stages are wired straight through.
  - Strength: bass/night regenerate their IR (biquad cascade baked to a wav);
    3D strength is a wet/dry mixer after the convolver bank.

Usage:
  audiofx.py status | presets | apply '<json>' | rules <system|apps> '<excluded-json>' | watch | off
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.expanduser("~/.local/share/audiofx")
CONF_HOME = os.path.expanduser("~/.config/pipewire")
HOST_CONF = os.path.join(CONF_HOME, "audiofx.conf")
FRAGMENT_DIR = os.path.join(CONF_HOME, "audiofx.conf.d")
FRAGMENT = os.path.join(FRAGMENT_DIR, "90-filter.conf")
UNIT_NAME = "audiofx.service"
UNIT_PATH = os.path.expanduser("~/.config/systemd/user/audiofx.service")
WATCH_UNIT = "audiofx-watch.service"
WATCH_UNIT_PATH = os.path.expanduser("~/.config/systemd/user/audiofx-watch.service")
STATE_PATH = os.path.join(BASE, "state.json")
IR_DIR = os.path.join(BASE, "ir")
HRIR = os.path.join(BASE, "hrir", "atmos.wav")
HRIR_URL = ("https://raw.githubusercontent.com/loteran/Arctis-Sound-Manager/"
            "main/hrir/HRIR_wav_files/atmos.wav")
SINK_NAME = "gdy_audiofx"
PLAYBACK_NAME = "gdy_audiofx_out"
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

PRESETS = [
    {"id": "off", "label": "Off", "spatial": 0, "bass": 0, "night": 0},
    {"id": "3d", "label": "3D", "spatial": 60, "bass": 0, "night": 0},
    {"id": "bass", "label": "Bass", "spatial": 0, "bass": 60, "night": 0},
    {"id": "night", "label": "Night", "spatial": 0, "bass": 0, "night": 50},
    {"id": "movie", "label": "Movie", "spatial": 40, "bass": 45, "night": 35},
]


def die(msg, code=1):
    print("[ERROR:] %s" % msg, file=sys.stderr)
    sys.exit(code)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def emit(v, indent=0):
    """Render a python value in PipeWire's relaxed config syntax (k = v)."""
    pad = " " * indent
    if isinstance(v, dict):
        lines = ["{"]
        for k, val in v.items():
            key = '"%s"' % k if (" " in k or not k.replace(".", "").replace("-", "").replace("_", "").isalnum()) else k
            lines.append("%s  %s = %s" % (pad, key, emit(val, indent + 2)))
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(v, list):
        items = ", ".join(emit(x, 0) for x in v)
        return "[ %s ]" % items if "\n" not in items else "[\n%s%s\n%s]" % (
            "".join(" " * (indent + 2) + emit(x, indent + 2) + "\n" for x in v), pad, pad)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


# ---------------- graph construction ----------------

# HeSuVi 14-channel wav layout — verified against the shipped working config
# /usr/share/pipewire/filter-chain/sink-virtual-surround-7.1-hesuvi.conf.
# Position order matches capture audio.position [FL FR FC LFE RL RR SL SR];
# mixer port N corresponds to position index N-1.
HRIR_MAP = {
    "FL": (0, 1), "FR": (8, 7), "FC": (6, 13), "LFE": (6, 13),
    "RL": (4, 5), "RR": (12, 11), "SL": (2, 3), "SR": (10, 9),
}
POSITIONS = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]


def build_graph(spatial, bass, night, loud=0):
    """Compose [ 3D bank ] -> [ bass IR ] -> [ night IR ] -> [ Loud stage ].

    Returns (nodes, links, inputs, outputs, capture_positions).
    Bypassed stages are simply absent from the chain.

    Loud stage (loud 1..100): a fixed 120 Hz high-pass IR pair feeding one
    stereo SWH fastLookaheadLimiter whose Input gain carries the loudness
    (+loud*6/100 dB) and Limit pins the ceiling at -1 dBFS. The high-pass
    removes sub-120 Hz energy these drivers cannot reproduce, which is what
    trips the MAX98390 amps' excursion limiter — without it the amp DSP
    normalizes any software gain away (measured, 2026-10-18). Same shape as
    upstream's GalaxyBook EasyEffects "Loud" presets (limiter input gain +
    120 Hz HP, validated by mic recordings).
    """
    nodes = []
    links = []
    inputs = None
    src_l = src_r = None  # node names feeding the next stage
    out_l = out_r = None  # full output port names of the last stage

    if spatial > 0:
        w = spatial / 100.0
        for pos in POSITIONS:
            nodes.append({"type": "builtin", "label": "copy", "name": "copy" + pos})
            cl, cr = HRIR_MAP[pos]
            nodes.append({"type": "builtin", "label": "convolver",
                          "name": "conv%s_L" % pos,
                          "config": {"filename": HRIR, "channel": cl}})
            nodes.append({"type": "builtin", "label": "convolver",
                          "name": "conv%s_R" % pos,
                          "config": {"filename": HRIR, "channel": cr}})
            links.append({"output": "copy%s:Out" % pos, "input": "conv%s_L:In" % pos})
            links.append({"output": "copy%s:Out" % pos, "input": "conv%s_R:In" % pos})
        # Wet/dry strength in two mixer stages: the builtin mixer plugin is
        # capped at 8 inputs/8 gains (verified: "unknown input port In 9"),
        # so the bank mixers stay exactly stock (8 gains = w) and a 2-input
        # blend mixer follows: In 1 = wet bank (1.0), In 2 = dry FL/FR (1-w).
        # At w=1 this is the stock graph plus a unity adder.
        mix_ctrl_wet = {"Gain %d" % (i + 1): w for i in range(8)}
        nodes.append({"type": "builtin", "label": "mixer", "name": "mixL",
                      "control": mix_ctrl_wet})
        nodes.append({"type": "builtin", "label": "mixer", "name": "mixR",
                      "control": dict(mix_ctrl_wet)})
        nodes.append({"type": "builtin", "label": "mixer", "name": "blendL",
                      "control": {"Gain 1": 1.0, "Gain 2": 1.0 - w}})
        nodes.append({"type": "builtin", "label": "mixer", "name": "blendR",
                      "control": {"Gain 1": 1.0, "Gain 2": 1.0 - w}})
        for i, pos in enumerate(POSITIONS):
            links.append({"output": "conv%s_L:Out" % pos, "input": "mixL:In %d" % (i + 1)})
            links.append({"output": "conv%s_R:Out" % pos, "input": "mixR:In %d" % (i + 1)})
        links.append({"output": "mixL:Out", "input": "blendL:In 1"})
        links.append({"output": "mixR:Out", "input": "blendR:In 1"})
        links.append({"output": "copyFL:Out", "input": "blendL:In 2"})
        links.append({"output": "copyFR:Out", "input": "blendR:In 2"})
        src_l, src_r = "blendL", "blendR"
        inputs = ["copy%s:In" % p for p in POSITIONS]
        capture_positions = POSITIONS
    else:
        capture_positions = ["FL", "FR"]

    if src_l:
        out_l, out_r = "%s:Out" % src_l, "%s:Out" % src_r

    for label, amount, ir in (("Bass", bass, "bass.wav"), ("Night", night, "night.wav")):
        if amount <= 0:
            continue
        nl, nr = "conv%s_L" % label, "conv%s_R" % label
        for n in (nl, nr):
            nodes.append({"type": "builtin", "label": "convolver", "name": n,
                          "config": {"filename": os.path.join(IR_DIR, ir), "channel": 0}})
        if src_l:
            links.append({"output": "%s:Out" % src_l, "input": "%s:In" % nl})
            links.append({"output": "%s:Out" % src_r, "input": "%s:In" % nr})
        else:
            inputs = [nl + ":In", nr + ":In"]  # first stage consumes capture directly
        src_l, src_r = nl, nr
        out_l, out_r = nl + ":Out", nr + ":Out"

    if loud > 0:
        gain_db = round(loud * 6.0 / 100.0, 2)
        nl, nr = "convHp_L", "convHp_R"
        for n in (nl, nr):
            nodes.append({"type": "builtin", "label": "convolver", "name": n,
                          "config": {"filename": os.path.join(IR_DIR, "hp.wav"), "channel": 0}})
        if src_l:
            links.append({"output": "%s:Out" % src_l, "input": "%s:In" % nl})
            links.append({"output": "%s:Out" % src_r, "input": "%s:In" % nr})
        else:
            inputs = [nl + ":In", nr + ":In"]
        nodes.append({"type": "ladspa", "name": "loudLim",
                      "plugin": "fast_lookahead_limiter_1913",
                      "label": "fastLookaheadLimiter",
                      "control": {"Input gain (dB)": gain_db,
                                  "Limit (dB)": -1.0,
                                  "Release time (s)": 0.02}})
        links.append({"output": "%s:Out" % nl, "input": "loudLim:Input 1"})
        links.append({"output": "%s:Out" % nr, "input": "loudLim:Input 2"})
        out_l, out_r = "loudLim:Output 1", "loudLim:Output 2"

    if out_l is None:
        die("no active effects in preset")
    outputs = [out_l, out_r]
    return nodes, links, inputs, outputs, capture_positions


# ---------------- config rendering ----------------

def render_fragment(target_sink, spatial, bass, night, loud=0):
    nodes, links, inputs, outputs, cap_pos = build_graph(spatial, bass, night, loud)
    graph = {
        "nodes": nodes,
        "links": links,
        "inputs": inputs,
        "outputs": outputs,
    }
    module = {
        "name": "libpipewire-module-filter-chain",
        "args": {
            "node.description": "Audio FX",
            "media.name": "Audio FX",
            "filter.graph": graph,
            "capture.props": {
                "node.name": SINK_NAME,
                "media.class": "Audio/Sink",
                "audio.channels": len(cap_pos),
                "audio.position": cap_pos,
            },
            "playback.props": {
                "node.name": PLAYBACK_NAME,
                "node.passive": True,
                "audio.position": ["FL", "FR"],
                "target.object": target_sink,
            },
        },
    }
    return "context.modules = %s\n" % emit([module])


HOST_CONF_TEXT = """# audiofx host config — its own PipeWire client, so preset switches never
# restart the daemon (mirrors Omarchy's filter-chain-host.conf).
context.properties = {
    log.level = 0
}
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt
        args = { }
        flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
]
"""

UNIT_TEXT = """[Unit]
Description=audiofx PipeWire filter-chain host
After=pipewire.service wireplumber.service
Requires=pipewire.service
Wants=wireplumber.service
PartOf=pipewire.service

[Service]
Type=simple
ExecStart=/usr/bin/pipewire -c audiofx.conf
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
"""

WATCH_UNIT_TEXT = """[Unit]
Description=audiofx per-stream router (keeps streams on the right sink)
After=audiofx.service
BindsTo=audiofx.service
PartOf=audiofx.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 %s watch
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
""" % os.path.realpath(__file__)


# ---------------- helpers ----------------

def pactl_sink_names():
    r = run(["pactl", "list", "sinks", "short"])
    return [ln.split("\t")[1] for ln in r.stdout.splitlines() if ln.strip()]


def node_muted(kind, name):
    """True if sink/source `name` is muted. kind: 'sinks'|'sources'."""
    try:
        r = run(["pactl", "list", kind])
    except Exception:
        return False
    cur = None
    muted = False
    for ln in r.stdout.splitlines():
        if ln.startswith(("Sink #", "Source #")):
            cur = None
        elif cur is None and ln.strip().startswith("Name:"):
            cur = ln.split(":", 1)[1].strip()
        elif cur == name and ln.strip().startswith("Mute:"):
            muted = ln.split(":", 1)[1].strip().lower() == "yes"
    return muted


def node_volume(kind, name):
    """Volume of sink/source `name` as 0..1 float (first channel), or None."""
    import re
    try:
        r = run(["pactl", "list", kind])
    except Exception:
        return None
    cur = None
    vol = None
    for ln in r.stdout.splitlines():
        if ln.startswith(("Sink #", "Source #")):
            cur = None
        elif cur is None and ln.strip().startswith("Name:"):
            cur = ln.split(":", 1)[1].strip()
        elif cur == name and "Volume:" in ln and vol is None:
            # Line shape: 'Volume: front-left: 65536 / 100% / 0.00 dB, ...'
            mm = re.search(r":\s*\d+\s*/\s*(\d+)%", ln)
            raw = re.search(r":\s*(\d+)\s*/\s*\d+%", ln)
            if mm:
                vol = int(mm.group(1)) / 100.0
            elif raw:
                vol = int(raw.group(1)) / 65536.0
    return vol


def guardian_pass(state):
    """The virtual sink is plumbing, not a control surface: pin it unmuted at
    1.00 so muting it can never wedge the chain. The PHYSICAL target sink is
    the user's mute/volume control and is never touched here. Every repair is
    logged so 'who muted what' is answerable from journald later."""
    if not sink_present():
        return
    fixes = []
    if node_muted("sinks", SINK_NAME):
        run(["pactl", "set-sink-mute", SINK_NAME, "0"])
        fixes.append("unmuted %s" % SINK_NAME)
    v = node_volume("sinks", SINK_NAME)
    if v is not None and abs(v - 1.0) > 0.01:
        run(["pactl", "set-sink-volume", SINK_NAME, "100%"])
        fixes.append("reset %s volume %.0f%% -> 100%%" % (SINK_NAME, v * 100))
    if fixes:
        print("[guardian] %s" % "; ".join(fixes), flush=True)


def sink_present():
    return SINK_NAME in pactl_sink_names()


def stream_entries():
    """Live playback streams: [(sink_input_id, app_key, sink_name)]."""
    sinks_by_num = {}
    r = run(["pactl", "list", "sinks"])
    num = None
    for ln in r.stdout.splitlines():
        if ln.startswith("Sink #"):
            num = ln.split("#")[1].strip()
        elif num is not None and ln.strip().startswith("Name:"):
            sinks_by_num[num] = ln.split(":", 1)[1].strip()
            num = None
    r = run(["pactl", "list", "sink-inputs"])
    out = []
    sid = app = sink_num = None
    for ln in r.stdout.splitlines():
        if ln.startswith("Sink Input #"):
            if sid is not None and sink_num is not None:
                out.append((sid, (app or "").strip(), sink_num))
            sid = ln.split("#")[1].strip()
            app = None
            sink_num = None
        elif ln.strip().startswith("Sink:"):
            sink_num = ln.split(":", 1)[1].strip()
        elif "application.name = " in ln:
            app = ln.split('"')[1]
    if sid is not None and sink_num is not None:
        out.append((sid, (app or "").strip(), sink_num))
    return [(i, a, sinks_by_num.get(s, "")) for i, a, s in out]


def app_excluded(app, excluded):
    """Case-insensitive substring match both ways, mirroring the panel."""
    a = (app or "").strip().lower()
    if not a:
        return False
    for e in excluded:
        e = e.strip().lower()
        if e and (a == e or e in a or a in e):
            return True
    return False


def reconcile(mode, excluded):
    """Place every playback stream where the active mode wants it.

    system mode: everything on the FX sink.
    apps mode: everything except excluded apps on the FX sink; excluded
    streams go back to the physical target.
    """
    if not sink_present():
        return
    target = (load_state() or {}).get("target", "")
    for sid, app, cur in stream_entries():
        want_fx = mode != "apps" or not app_excluded(app, excluded)
        if want_fx and cur != SINK_NAME:
            run(["pactl", "move-sink-input", sid, SINK_NAME])
        elif not want_fx and cur == SINK_NAME and target and target in pactl_sink_names():
            run(["pactl", "move-sink-input", sid, target])


def ensure_hrir():
    if os.path.isfile(HRIR):
        return
    os.makedirs(os.path.dirname(HRIR), exist_ok=True)
    import urllib.request

    tmp = HRIR + ".tmp"
    try:
        urllib.request.urlretrieve(HRIR_URL, tmp)
    except Exception as e:  # noqa: BLE001 - one clear message beats a traceback
        die("could not fetch HRIR (%s): %s" % (HRIR_URL, e))
    os.replace(tmp, HRIR)


def regen_irs(spatial, bass, night, loud=0):
    os.makedirs(IR_DIR, exist_ok=True)
    calls = []
    if bass > 0:
        calls.append([sys.executable, os.path.join(SCRIPT_DIR, "irgen.py"), IR_DIR, "bass", str(bass)])
    if night > 0:
        calls.append([sys.executable, os.path.join(SCRIPT_DIR, "irgen.py"), IR_DIR, "night", str(night)])
    if loud > 0 and not os.path.isfile(os.path.join(IR_DIR, "hp.wav")):
        # fixed shape, generated once
        calls.append([sys.executable, os.path.join(SCRIPT_DIR, "irgen.py"), IR_DIR, "hp"])
    for cmd in calls:
        r = run(cmd)
        if r.returncode != 0:
            die("irgen failed (%s): %s" % (" ".join(cmd), (r.stderr or r.stdout).strip()))


def resolve_target_sink():
    """Physical sink to play through: follow the current default through any
    DSP sink (audiofx's own target included) to the real device."""
    r = run(["omarchy-audio-output-sink"])
    name = r.stdout.strip() if r.returncode == 0 else ""
    if name and name != SINK_NAME and name in pactl_sink_names():
        return name
    dflt = run(["pactl", "get-default-sink"]).stdout.strip()
    if dflt and dflt != SINK_NAME:
        return dflt
    names = [n for n in pactl_sink_names() if n != SINK_NAME and n != PLAYBACK_NAME]
    if names:
        return names[0]
    die("no physical sink present")


def wait_for(predicate, timeout_s=10.0, interval=0.25):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def save_state(preset):
    os.makedirs(BASE, exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        json.dump(preset, fh)


def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------- actions ----------------

def action_status():
    state = load_state() or {}
    active = sink_present()
    unit = run(["systemctl", "--user", "is-active", UNIT_NAME]).stdout.strip()
    out = {
        "active": active,
        "unit": unit or "inactive",
        "preset": {k: state.get(k, 0) for k in ("spatial", "bass", "night", "loud")},
        "target": state.get("target", ""),
        "sink": SINK_NAME,
        "hrir": os.path.isfile(HRIR),
        "mode": state.get("mode", "system") if active else "off",
        "excluded": state.get("excluded", []) if active else [],
    }
    print(json.dumps(out))


def action_rules(mode, excluded):
    """Set routing without touching the effect graph: system | apps."""
    state = load_state() or {}
    if not state or not sink_present():
        die("fx not active")
    mode = "apps" if mode == "apps" else "system"
    state["mode"] = mode
    state["excluded"] = excluded
    save_state(state)
    target = state.get("target", "")
    if sink_present():
        if mode == "system":
            run(["pactl", "set-default-sink", SINK_NAME])
        elif target and target in pactl_sink_names():
            run(["pactl", "set-default-sink", target])
    reconcile(mode, excluded)
    print(json.dumps({"ok": True, "mode": mode, "excluded": excluded}))


def action_watch():
    """Long-running: route new streams per state.json (event + settle)."""
    last_mode = None
    last_excluded = None
    p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    if p.stdout is None:
        die("could not subscribe to pactl events")
    try:
        while True:
            state = load_state() or {}
            mode = state.get("mode", "system")
            excluded = state.get("excluded", [])
            if not sink_present():
                time.sleep(1.0)
                continue
            if mode != last_mode or excluded != last_excluded:
                reconcile(mode, excluded)
                guardian_pass(state)
                last_mode, last_excluded = mode, excluded
                continue
            line = p.stdout.readline()
            if not line:
                time.sleep(0.5)
                p.poll()
                if p.returncode is not None:
                    return
                continue
            if "'change' on sink-input" in line or "'new' on sink-input" in line:
                time.sleep(0.15)
                reconcile(mode, excluded)
            elif "'change' on sink #" in line or "'change' on source #" in line:
                # Mute/volume/default flips (panel, keys, apps like Steam)
                # reconcile instantly; the virtual sink is pinned as plumbing.
                time.sleep(0.1)
                guardian_pass(state)
    finally:
        p.terminate()


def action_doctor():
    """One-shot audit of the whole audio tree. Exit 1 if anything is wedged:
    the split-brain states that silently kill sound or mic input."""
    state = load_state() or {}
    active = sink_present()
    default_sink = run(["pactl", "get-default-sink"]).stdout.strip()
    default_source = run(["pactl", "get-default-source"]).stdout.strip()
    target = state.get("target", "")
    out = {
        "engine": "active" if active else "off",
        "mode": state.get("mode", "system"),
        "defaultSink": default_sink,
        "defaultSource": default_source,
        "target": target,
        "faults": [],
        "warnings": [],
    }
    if active:
        if node_muted("sinks", SINK_NAME):
            out["faults"].append(
                "virtual sink %s is MUTED (plumbing; guardian should heal — restart audiofx-watch)" % SINK_NAME)
        v = node_volume("sinks", SINK_NAME)
        out["virtualVolume"] = round(v, 3) if v is not None else None
        if v is not None and abs(v - 1.0) > 0.01:
            out["faults"].append(
                "virtual sink volume %.0f%% != 100%% (plumbing; guardian should heal)" % (v * 100))
        if state.get("mode", "system") == "system" and default_sink != SINK_NAME:
            out["warnings"].append(
                "mode=system but default sink is %s, not %s" % (default_sink, SINK_NAME))
    if target:
        if target not in pactl_sink_names():
            out["warnings"].append("target sink %s not present (undocked/headphones?)" % target)
        else:
            out["targetMuted"] = node_muted("sinks", target)
            tv = node_volume("sinks", target)
            out["targetVolume"] = round(tv, 3) if tv is not None else None
            if out["targetMuted"] and stream_entries():
                out["faults"].append(
                    "%d stream(s) playing into MUTED %s — audible-to-nobody (unmute output)" %
                    (len(stream_entries()), target))
    out["sourceMuted"] = node_muted("sources", default_source)
    if out["sourceMuted"]:
        out["faults"].append(
            "default source %s is MUTED — no mic input (voxtype/calls dead)" % default_source)
    out["ok"] = not out["faults"]
    print(json.dumps(out))
    if not out["ok"]:
        sys.exit(1)


def action_presets():
    print(json.dumps(PRESETS))


def action_apply(arg):
    try:
        preset = json.loads(arg)
    except ValueError:
        # allow bare preset ids from PRESETS
        match = [p for p in PRESETS if p["id"] == arg.strip()]
        if not match:
            die("apply needs preset json or one of: %s" % ", ".join(p["id"] for p in PRESETS))
        preset = dict(match[0])
    spatial = int(preset.get("spatial", 0))
    bass = int(preset.get("bass", 0))
    night = int(preset.get("night", 0))
    for v in (spatial, bass, night):
        if not 0 <= v <= 100:
            die("effect amounts must be 0..100")

    prev = load_state() or {}
    # loud is orthogonal to the presets: absent key inherits the previous
    # loud amount so preset switches never stomp it; explicit 0 disables it.
    loud = int(preset["loud"]) if "loud" in preset else int(prev.get("loud", 0) or 0)
    if not 0 <= loud <= 100:
        die("loud must be 0..100")
    if spatial == 0 and bass == 0 and night == 0 and loud == 0:
        return action_off()

    mode = prev.get("mode", "system")
    excluded = prev.get("excluded", [])

    if spatial > 0:
        ensure_hrir()
    regen_irs(spatial, bass, night, loud)
    target = resolve_target_sink()
    prev_default = run(["pactl", "get-default-sink"]).stdout.strip()

    os.makedirs(FRAGMENT_DIR, exist_ok=True)
    with open(HOST_CONF, "w") as fh:
        fh.write(HOST_CONF_TEXT)
    with open(FRAGMENT, "w") as fh:
        fh.write(render_fragment(target, spatial, bass, night, loud))
    os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
    with open(UNIT_PATH, "w") as fh:
        fh.write(UNIT_TEXT)
    with open(WATCH_UNIT_PATH, "w") as fh:
        fh.write(WATCH_UNIT_TEXT)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", UNIT_NAME])
    run(["systemctl", "--user", "restart", UNIT_NAME])
    run(["systemctl", "--user", "enable", WATCH_UNIT])
    run(["systemctl", "--user", "restart", WATCH_UNIT])

    if not wait_for(sink_present):
        rollback(prev_default)
        die("audiofx sink never appeared; rolled back. Check: journalctl --user -u %s" % UNIT_NAME)
    if mode == "system":
        run(["pactl", "set-default-sink", SINK_NAME])
    reconcile(mode, excluded)
    save_state({"spatial": spatial, "bass": bass, "night": night, "loud": loud,
                "target": target, "mode": mode, "excluded": excluded})
    print(json.dumps({"ok": True, "preset": {"spatial": spatial, "bass": bass, "night": night,
                                             "loud": loud},
                      "target": target, "mode": mode}))


def rollback(prev_default):
    run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
    run(["systemctl", "--user", "daemon-reload"])
    if prev_default and prev_default != SINK_NAME:
        run(["pactl", "set-default-sink", prev_default])


def action_off():
    prev_default = run(["pactl", "get-default-sink"]).stdout.strip()
    state = load_state() or {}
    target = state.get("target", "")
    run(["systemctl", "--user", "disable", "--now", UNIT_NAME, WATCH_UNIT])
    run(["systemctl", "--user", "daemon-reload"])
    wait_for(lambda: not sink_present(), 5.0)
    # Streams left on the vanished sink reconnect wherever PipeWire puts
    # them; move them to a real device explicitly.
    phys = (prev_default if prev_default and prev_default != SINK_NAME
            and prev_default in pactl_sink_names() else "") or \
        (target if target and target in pactl_sink_names() else "")
    if not phys and pactl_sink_names():
        phys = resolve_target_sink() if pactl_sink_names() else ""
    if phys:
        run(["pactl", "set-default-sink", phys])
        for sid, _app, cur in stream_entries():
            if cur != phys:
                run(["pactl", "move-sink-input", sid, phys])
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
    print(json.dumps({"ok": True, "off": True}))


def main(argv):
    if len(argv) < 2:
        die("usage: audiofx.py status|presets|doctor|apply '<json>'|rules <mode> '<excluded-json>'|watch|off", 2)
    cmd = argv[1]
    if cmd == "status":
        action_status()
    elif cmd == "presets":
        action_presets()
    elif cmd == "doctor":
        action_doctor()
    elif cmd == "apply":
        if len(argv) < 3:
            die("apply needs an argument")
        action_apply(argv[2])
    elif cmd == "rules":
        if len(argv) < 3:
            die("rules needs a mode: system|apps")
        excluded = []
        if len(argv) > 3:
            try:
                excluded = json.loads(argv[3])
            except ValueError:
                excluded = [a for a in argv[3].split(",") if a.strip()]
        action_rules(argv[2], excluded)
    elif cmd == "watch":
        action_watch()
    elif cmd == "off":
        action_off()
    else:
        die("unknown action: %s" % cmd, 2)


if __name__ == "__main__":
    main(sys.argv)
