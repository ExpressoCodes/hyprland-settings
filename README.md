# Hyprland Settings

A GTK4/libadwaita GUI for configuring the [Hyprland](https://hyprland.org/)
Wayland compositor — adjust appearance, input, keybindings and more from a
graphical settings window instead of hand-editing config files.

## What it does

Hyprland Settings reads your Hyprland configuration, lets you change common
options through a native GTK4/libadwaita interface, and writes the changes back
to your config. It talks to the running compositor via `hyprctl` so many changes
apply live.

## Features

- **Appearance** — gaps, borders, rounding and related look-and-feel options
- **Animations** — toggle and tune Hyprland animations
- **Input** — keyboard and pointer input settings
- **Cursor** — cursor theme/size configuration
- **Keybindings** — view and edit keybindings
- **Wallpaper** — set your wallpaper (via `hyprpaper`)
- **Window rules** — manage window behaviour

## Install

```bash
pip install -e .
```

Requires Python 3.11+ and PyGObject (GTK4 / libadwaita).

## Run

```bash
hyprland-settings
# or
python -m hyprland_settings
```

## License

Released under the [MIT License](LICENSE).
