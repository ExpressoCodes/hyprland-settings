from hyprland_settings.app import HyprlandSettingsApp
import sys

def main():
    app = HyprlandSettingsApp()
    sys.exit(app.run(sys.argv))

if __name__ == "__main__":
    main()
