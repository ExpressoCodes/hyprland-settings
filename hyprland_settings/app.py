import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio

class HyprlandSettingsApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="dev.hyprland.settings",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        from hyprland_settings.ui.window import MainWindow
        win = MainWindow(application=app)
        win.present()
