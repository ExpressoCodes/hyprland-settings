-- hyprland-settings managed sections
-- Add these lines to your ~/.config/hypr/hyprland.lua (once, at the end).
-- hyprland-settings will write/update each sibling file automatically.

local hypr = (os.getenv("XDG_CONFIG_HOME") or (os.getenv("HOME") .. "/.config")) .. "/hypr/"

local function load_if_exists(path)
    local f = io.open(path, "r")
    if f then f:close(); dofile(path) end
end

load_if_exists(hypr .. "appearance.lua")
load_if_exists(hypr .. "animations.lua")
load_if_exists(hypr .. "input.lua")
