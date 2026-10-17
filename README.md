# audiofx — system-wide audio effects for PipeWire

A Boom 3D-style effects engine built natively on PipeWire: one virtual sink
(`gdy_audiofx`) that every app plays into, processed by a filter-chain and
passed through to your real output. No root, no driver changes, no daemon
config. Pairs with the [Audio FX Omarchy panel](https://github.com/gdeyoung/omarchy-audiofx),
which drives it from the desktop's audio dropdown — but it works from any
terminal or window manager.

## Effects

- **Off** — no processing, zero overhead, audio path untouched
- **3D** — binaural virtual surround for headphones (HeSuVi 14-channel HRIR
  convolver bank after the stock PipeWire virtual-surround pattern; games can
  feed it real 7.1, stereo gets upmixed)
- **Bass** — low-shelf boost baked into a convolution IR, regenerated per
  strength
- **Night** — spectral night mode: pulls down lows/highs, lifts dialogue
  presence, for late-hour listening
- **Movie** — 3D 40% + bass 45% + night 35% composed in one graph

Effects compose — the graph is `[ 3D bank ] -> [ bass IR ] -> [ night IR ]`
with bypassed stages absent — and amounts are continuous (0–100), so any mix
is valid. Preset switches restart only the engine's own PipeWire client,
never the daemon, so Spotify, browsers and Steam never drop their
connection. Apply is transactional: config render → unit restart →
sink-present check, with rollback to the previous default sink on failure.

**Routing rules:** effects can cover everything, or everything except a list
of apps (games go direct). A watcher (`audiofx-watch.service`) keeps streams
on the right sink as apps come and go.

## Install

```sh
git clone https://github.com/gdeyoung/audiofx-engine ~/audiofx-engine
mkdir -p ~/.local/bin && ln -s ~/audiofx-engine/audiofx.py ~/.local/bin/audiofx
audiofx apply '{"spatial":60,"bass":50,"night":0}'
```

The first `apply` writes and enables the two systemd user units
(`audiofx.service`, `audiofx-watch.service`) and switches the default sink to
the FX sink. `audiofx off` removes the sink and restores the previous
default.

Requires `pipewire` 1.0+, `wireplumber`, `python-numpy`. First 3D use
downloads a 280 KB HRIR from the Arctis-Sound-Manager repo (HeSuVi atmos)
into `~/.local/share/audiofx/hrir/`; no other network access.

## CLI

```
audiofx status                                # JSON: active, preset, target, mode, excluded
audiofx presets                               # the built-in presets
audiofx apply '{"spatial":60,"bass":50,"night":0}'
audiofx rules system                          # effects on everything
audiofx rules apps '["Steam"]'                # everything except the listed apps
audiofx watch                                 # runs in the unit; keeps streams routed
audiofx off                                   # stop, remove sink, restore default
```

State lives in `~/.local/share/audiofx/` (state.json, generated IRs, HRIR);
configs render to `~/.config/pipewire/audiofx.conf*`. Remove everything:
`audiofx off`, delete the clone, `rm -rf ~/.local/share/audiofx
~/.config/pipewire/audiofx.conf*`.

## Files

- `audiofx.py` — engine: graph builder, config renderer, unit host, apply/
  status/rules/off transaction logic
- `irgen.py` — numpy IR generator for bass/night (48 kHz stereo float WAVs)

## License

MIT — see [LICENSE](LICENSE). HeSuVi HRIR fetched at runtime from the
GPL-3.0 Arctis-Sound-Manager bundle; not redistributed here.
