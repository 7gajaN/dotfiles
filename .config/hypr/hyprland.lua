-- Hyprland config, Lua format (0.55+).
-- Ported from hyprland.conf; hyprlang/.conf is deprecated upstream.
-- Docs: https://wiki.hypr.land/Configuring/Start/

------------------
---- MONITORS ----
------------------

hl.monitor({
    output   = "DP-0",
    mode     = "1920x1080@239.76",
    position = "0x0",
    scale    = 1,
})


-------------------
---- AUTOSTART ----
-------------------

hl.on("hyprland.start", function()
    hl.exec_cmd("awww-daemon")
    hl.exec_cmd("sleep 1 && awww img $(find ~/.config/pics/wallpaper.png -type f | shuf -n 1)")
    hl.exec_cmd("waybar")
end)


---------------
---- INPUT ----
---------------

hl.config({
    input = {
        kb_layout = "us",
    },
})

hl.device({
    name        = "epic-mouse-v1",
    sensitivity = -0.5,
})


-----------------------
---- LOOK AND FEEL ----
-----------------------

hl.config({
    general = {
        gaps_in     = 5,
        gaps_out    = 5,
        border_size = 2,

        col = {
            active_border   = { colors = { "rgba(b22222ff)", "rgba(3b4252ff)" }, angle = 45 },
            inactive_border = { colors = { "rgba(3b4252ff)", "rgba(d8dee9ff)" }, angle = 45 },
        },

        layout = "dwindle",
    },

    decoration = {
        rounding = 10,

        -- Global opacity stays at 1.0 so browsers/games don't flicker.
        -- Per-app transparency goes through window rules instead.
        active_opacity     = 1.0,
        inactive_opacity   = 1.0,
        fullscreen_opacity = 1.0,

        blur = {
            enabled = false,
            size    = 7,
            passes  = 3,
        },

        shadow = {
            enabled      = false,
            range        = 4,
            render_power = 3,
            color        = 0xee1a1a1a,
        },
    },

    animations = {
        enabled = true,
    },

    dwindle = {
        preserve_split = true,
    },

    master = {
        new_status = "master",
    },

    misc = {
        force_default_wallpaper = 0,
    },
})


----------------------
---- ANIMATIONS   ----
----------------------

hl.curve("myBezier", { type = "bezier", points = { { 0.05, 0.9 }, { 0.1, 1.05 } } })

hl.animation({ leaf = "windows",     enabled = true, speed = 7,  bezier = "myBezier" })
hl.animation({ leaf = "windowsOut",  enabled = true, speed = 7,  bezier = "default", style = "popin 80%" })
hl.animation({ leaf = "border",      enabled = true, speed = 10, bezier = "default" })
hl.animation({ leaf = "borderangle", enabled = true, speed = 8,  bezier = "default" })
hl.animation({ leaf = "fade",        enabled = true, speed = 7,  bezier = "default" })
hl.animation({ leaf = "workspaces",  enabled = true, speed = 6,  bezier = "default" })


---------------------
---- KEYBINDINGS ----
---------------------

local mod = "ALT"

-- Apps
hl.bind(mod .. " + RETURN",         hl.dsp.exec_cmd("kitty"))
hl.bind(mod .. " + SHIFT + RETURN", hl.dsp.exec_cmd("firefox"))
hl.bind(mod .. " + SHIFT + V",      hl.dsp.exec_cmd("kitty -e ranger"))

-- System
hl.bind(mod .. " + F",         hl.dsp.window.fullscreen())
hl.bind(mod .. " + SHIFT + F", hl.dsp.window.float({ action = "toggle" }))
hl.bind(mod .. " + SHIFT + Q", hl.dsp.window.close())
hl.bind(mod .. " + SHIFT + R", hl.dsp.exec_cmd("hyprctl reload"))
hl.bind(mod .. " + SHIFT + E", hl.dsp.exit())

-- Focus
hl.bind(mod .. " + H", hl.dsp.focus({ direction = "left" }))
hl.bind(mod .. " + J", hl.dsp.focus({ direction = "down" }))
hl.bind(mod .. " + K", hl.dsp.focus({ direction = "up" }))
hl.bind(mod .. " + L", hl.dsp.focus({ direction = "right" }))

-- Move
hl.bind(mod .. " + SHIFT + H", hl.dsp.window.swap({ direction = "left" }))
hl.bind(mod .. " + SHIFT + J", hl.dsp.window.swap({ direction = "down" }))
hl.bind(mod .. " + SHIFT + K", hl.dsp.window.swap({ direction = "up" }))
hl.bind(mod .. " + SHIFT + L", hl.dsp.window.swap({ direction = "right" }))

-- Resize (split)
hl.bind(mod .. " + V", hl.dsp.layout("splitratio -0.1"))
hl.bind(mod .. " + B", hl.dsp.layout("splitratio +0.1"))

-- Submap: resize
hl.bind(mod .. " + R", hl.dsp.submap("resize"))

hl.define_submap("resize", function()
    hl.bind("H", hl.dsp.window.resize({ x = -10, y = 0,   relative = true }))
    hl.bind("J", hl.dsp.window.resize({ x = 0,   y = 10,  relative = true }))
    hl.bind("K", hl.dsp.window.resize({ x = 0,   y = -10, relative = true }))
    hl.bind("L", hl.dsp.window.resize({ x = 10,  y = 0,   relative = true }))

    hl.bind("RETURN",      hl.dsp.submap("reset"))
    hl.bind("ESCAPE",      hl.dsp.submap("reset"))
    hl.bind(mod .. " + R", hl.dsp.submap("reset"))
end)

-- Workspaces
for i = 1, 10 do
    local key = i % 10 -- 10 maps to key 0
    hl.bind(mod .. " + " .. key,             hl.dsp.focus({ workspace = i }))
    hl.bind(mod .. " + SHIFT + " .. key,     hl.dsp.window.move({ workspace = i }))
end

-- Screenshots
hl.bind("Print",                   hl.dsp.exec_cmd([[grim ~/Pictures/$(date +%Y%m%d_%H%M%S).png]]))
hl.bind("SHIFT + Print",           hl.dsp.exec_cmd([[grim -g "$(slurp)" ~/Pictures/$(date +%Y%m%d_%H%M%S).png]]))
hl.bind(mod .. " + Print",         hl.dsp.exec_cmd([[grim - | wl-copy]]))
hl.bind(mod .. " + SHIFT + Print", hl.dsp.exec_cmd([[grim -g "$(slurp)" - | wl-copy]]))
