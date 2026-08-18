-- Hyprland config, Lua format (0.55+).
-- Ported from hyprland.conf; hyprlang/.conf is deprecated upstream.
-- Docs: https://wiki.hypr.land/Configuring/Start/

-----------------
---- PLUGINS ----
-----------------

-- hy3 gives i3-style tiling: windows opened into a group become equal-sized
-- siblings, so a portrait workspace stacks them at 33/33/33, 25% each, etc.
-- Only workspace 11 uses it (see WORKSPACES); DP-2 stays on dwindle.
--
-- Provided by the AUR package hyprland-plugin-hy3, version-locked to Hyprland
-- 0.56.2. If Hyprland is updated and the plugin is not rebuilt to match,
-- Hyprland refuses to load it and workspace 11 silently falls back to dwindle.
-- NOTE: hl.plugin.load() is a no-op on `hyprctl reload` -- verified by
-- unloading hy3, reloading, and seeing nothing load. It may work on a true
-- cold start, so it stays, but the autostart loader below is what actually
-- guarantees the plugin is present. Do not remove that one.
hl.plugin.load("/usr/lib/libhy3.so")


------------------
---- MONITORS ----
------------------

hl.monitor({
    output   = "DP-2",
    mode     = "1920x1080@239.76",
    position = "0x0",
    scale    = 1,
})

-- Physically rotated on its stand, sitting to the LEFT of DP-2.
-- transform 3 = 90 deg counter-clockwise; logical size becomes 1080x1920,
-- hence x = -1080 to place its right edge flush with DP-2's left edge.
-- y = -840 aligns the BOTTOM edges: DP-2 ends at y=1080, so DP-3 (1920 tall)
-- starts at 1080 - 1920 = -840 and ends at the same 1080.
hl.monitor({
    output    = "DP-3",
    mode      = "1920x1080@143.99",
    position  = "-1080x-840",
    scale     = 1,
    transform = 3,
})


--------------------
---- WORKSPACES ----
--------------------

-- Workspaces 1-10 belong to the main monitor, 11 is the portrait monitor's.
-- Rules only apply when a workspace is CREATED; existing ones aren't relocated,
-- so a running session may need `hl.dsp.workspace.move` to catch up.
for i = 1, 10 do
    hl.workspace_rule({
        workspace = tostring(i),
        monitor   = "DP-2",
        default   = (i == 1),
    })
end

-- persistent keeps 11 alive while empty, so DP-3 is never left workspace-less.
-- layout "hy3" applies to this workspace only -- DP-2 keeps dwindle. hy3 lays
-- the portrait monitor out as equal full-width rows with no extra config;
-- it picks a vertical root group from the monitor's shape on its own.
hl.workspace_rule({
    workspace  = "11",
    monitor    = "DP-3",
    default    = true,
    persistent = true,
    layout     = "hy3",
})


-------------------
---- AUTOSTART ----
-------------------

hl.on("hyprland.start", function()
    -- Guaranteed hy3 load: idempotent, so it is harmless if the plugin was
    -- already brought in during config parsing.
    hl.exec_cmd("hyprctl plugin list | grep -q 'Plugin hy3' || hyprctl plugin load /usr/lib/libhy3.so")
    hl.exec_cmd("awww-daemon")
    -- Per-output wallpapers: without -o, the last call wins on every monitor.
    hl.exec_cmd("sleep 1 && awww img -o DP-2 $(find ~/.config/pics/wallpaper.png -type f | shuf -n 1)")
    hl.exec_cmd("sleep 1 && awww img -o DP-3 ~/.config/pics/takamura.jpg")
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

        -- Drag a window's border or the gap next to it to resize, no modifier.
        -- border_size is only 2px, so extend_border_grab_area does the real
        -- work: it widens the grabbable strip to 15px around each edge.
        resize_on_border       = true,
        extend_border_grab_area = 15,
        hover_icon_on_border   = true,

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

-- Mouse: hold ALT and drag anywhere in a window.
--   left  = move, right = resize.
-- hl.dsp.window.resize() with NO arguments is the mouse resizewindow
-- dispatcher; passing {x=,y=} instead gives the keyboard resize used by the
-- resize submap further down. Resize direction follows which quadrant of the
-- window the cursor is in (dwindle/master smart_resizing, on by default).
hl.bind(mod .. " + mouse:272", hl.dsp.window.drag())
hl.bind(mod .. " + mouse:273", hl.dsp.window.resize())

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

-- Workspace 11 (portrait monitor) is outside the 1-10 range and has no number
-- key of its own. ALT+SHIFT+1 was not an option: the loop above already uses
-- ALT+SHIFT+<n> to move a window to workspace n.
hl.bind(mod .. " + CTRL + 1", hl.dsp.focus({ workspace = 11 }))

-- Screenshots
hl.bind("Print",                   hl.dsp.exec_cmd([[grim ~/Pictures/$(date +%Y%m%d_%H%M%S).png]]))
hl.bind("SHIFT + Print",           hl.dsp.exec_cmd([[grim -g "$(slurp)" ~/Pictures/$(date +%Y%m%d_%H%M%S).png]]))
hl.bind(mod .. " + Print",         hl.dsp.exec_cmd([[grim - | wl-copy]]))
hl.bind(mod .. " + SHIFT + Print", hl.dsp.exec_cmd([[grim -g "$(slurp)" - | wl-copy]]))
