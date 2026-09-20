-- hyprland-settings managed sections
-- Add these lines to your hyprland.lua (once, at the end).
-- hyprland-settings will write each file automatically when you change settings.

local settings_dir = (os.getenv("XDG_CONFIG_HOME") or (os.getenv("HOME") .. "/.config"))
    .. "/hypr/hyprland-settings/"

local function load_section(name)
    local path = settings_dir .. name .. ".lua"
    local f = io.open(path, "r")
    if f then
        f:close()
        dofile(path)
    end
end

load_section("appearance")
load_section("animations")
load_section("input")
