-- host_monitor.koplugin/main.lua
-- KOReader plugin: pull the PC-side host-resource status as a monochrome
-- HUD cover and show it full-screen, auto-refreshing every 3 minutes.
-- The plugin is a fork of RC-APC's workbuddy_monitor.koplugin: the Kindle
-- side only downloads a grayscale PNG and blits it; ALL data collection
-- (CPU / mem / disk / net / top processes) and rendering happens on the
-- PC side (res-bridge.py + res_collect.py + res_cover.py).
--
-- REMOTE HOST: a `host=NAME` line in config.txt makes the board follow a
-- machine that pushes its own stats to the bridge via res_report.py.
--
-- RENDERING
--   Everything is rendered PC-side into ONE 8-bit grayscale PNG (black-on-white
--   or white-on-black, see theme= in config.txt); the Kindle only downloads and
--   blits that image. A native text board is still implemented, but it is used
--   only AUTOMATICALLY as a fallback if the image cannot be fetched or blitted,
--   so a dead bridge never leaves a blank screen.
--
-- COLOUR THEMES (?theme=dark|light on /cover.png)
--   dark  = black background, white text
--   light = white background, black text -- easier in daylight on e-ink,
--           because the page is lit rather than glowing.
--
-- EXITING THE ALWAYS-ON DASHBOARD
--   Tap / long-press / swipe anywhere, or the page-turn keys, or the menu item.
--   The small ✕ in the corner is a HINT drawn into the PNG, not a tappable
--   widget. The auto-refresh tick checks an `_exiting` guard before rebuilding,
--   so pressing exit mid-refresh can never bounce you back into the cover.
--
--   There is deliberately NO plugin-internal hotkey: on a touch-only device a
--   key binding is unusable, and it only added a dead settings entry.
--
-- GESTURES (the supported way to bind a swipe / tap zone)
--   The plugin registers three actions into KOReader's Dispatcher, so they
--   show up in  Settings -> Gestures -> (add gesture) -> 动作 :
--       "Host 常驻看板"        -> opens the board (or exits it if already
--                                 open, i.e. it toggles)
--       "Host 退出常驻看板"    -> exits unconditionally
--       "Host 设置休眠壁纸"    -> arms the lock-screen cover file
--   Registered with Dispatcher:registerAction(); the matching handlers are
--   onHostDashboard / onHostExitDashboard / onHostSetLock. Restart KOReader
--   once after installing so the gesture list picks the new actions up.
--
-- SETUP
--   1. PC:    run  python3 res-bridge.py  (serves /status.json on :8865)
--             keep it running, same WiFi, allow the port through the firewall.
--   2. Kindle: copy this folder into  koreader/plugins/  (must end in .koplugin),
--             restart KOReader.
--   3. Kindle: menu (top-left) -> Host Monitor -> "设置桥地址" -> your PC IP:8865.
--      - If the Kindle is on the PC's own hotspot, use http://192.168.137.1:8865
--      - If on the same router WiFi, use the PC's LAN IP (e.g. http://192.168.1.20:8865)
--      (If your KOReader build lacks a text-input dialog, instead edit the
--       plugin's config.txt first line to the bridge URL and restart.)
--   4. Menu -> "常驻看板" to view; it auto-refreshes every 3 minutes.
--      Optional: bind a swipe in Settings -> Gestures (see GESTURES above).
--
-- Bridge address resolution order: saved setting (hb_bridge_base) >
-- config.txt in this plugin folder > HB_BRIDGE env var > default below.

local WidgetContainer = require("ui/widget/container/widgetcontainer")
local InputContainer = require("ui/widget/container/inputcontainer")
local CenterContainer = require("ui/widget/container/centercontainer")
local TextWidget = require("ui/widget/textwidget")
local VerticalGroup = require("ui/widget/verticalgroup")
local InfoMessage = require("ui/widget/infomessage")
local UIManager = require("ui/uimanager")
local GestureRange = require("ui/gesturerange")
local Device = require("device")
local Font = require("ui/font")
local logger = require("logger")

local REFRESH_SEC = 180      -- 3 minutes: live dashboard tick.
                             -- The lock-screen cover FILE (hb_ss/cover.png) is
                             -- mirrored from the latest dashboard cover on every
                             -- board refresh and on resume (see _updateLockFile).
                             -- It is NEVER fetched inside onSuspend, so a sleep
                             -- can never race the network or time out.

local DEFAULT_BASE = os.getenv("HB_BRIDGE") or "http://192.168.137.1:8865"
local BRIDGE_BASE = (G_reader_settings and G_reader_settings:readSetting("hb_bridge_base"))
                   or DEFAULT_BASE
-- Optional remote host (a machine pushing its own stats via res_report.py):
-- set `host=NAME` in config.txt; the board then follows that host's data.
local HOST = nil

-- The image is the only user-selectable rendering; MODE is kept solely to
-- flag the automatic text fallback.
local MODE = "image"     -- the text board is no longer user-selectable; it is
                         -- kept ONLY as an automatic fallback if the PNG
                         -- cannot be downloaded or blitted (see _showBoard)
local function setMode(m)
    if m == "image" or m == "text" then
        MODE = m
        if G_reader_settings then
            pcall(function() G_reader_settings:saveSetting("hb_bridge_mode", m) end)
        end
    end
end

-- ---- THEME: dark = black bg / white text, light = white bg / black text ---
-- The cover is rendered PC-side and fetched as a grayscale PNG, so this only
-- decides which PNG we ask for. Persisted in KOReader settings.
local THEME = "dark"
local function setTheme(t)
    if t == "dark" or t == "light" then
        THEME = t
        if G_reader_settings then
            pcall(function() G_reader_settings:saveSetting("hb_theme", t) end)
        end
    end
end

local function host_param()
    return HOST and ("&host=" .. HOST) or ""
end
local function status_url()
    local q = HOST and ("?host=" .. HOST) or ""
    return BRIDGE_BASE .. "/status.json" .. q
end

-- Network + JSON modules are loaded LAZILY (inside the functions that use
-- them) and wrapped in pcall. A missing module must NOT abort plugin load,
-- otherwise the whole plugin silently disappears from KOReader's menu.
local function get_http()
    local ok, http = pcall(require, "socket.http")
    if not ok or not http then return nil end
    http.TIMEOUT = 8
    return http
end
local function get_ltn12()
    local ok, ltn12 = pcall(require, "ltn12")
    if not ok or not ltn12 then return nil end
    return ltn12
end
-- KOReader bundles "json"; some builds also have "cjson". Try both.
local function json_decode(s)
    local ok, mod = pcall(require, "cjson")
    if ok and mod and mod.decode then
        local d_ok, data = pcall(mod.decode, s)
        if d_ok then return data, nil end
    end
    local ok2, mod2 = pcall(require, "json")
    if ok2 and mod2 and mod2.decode then
        local d_ok, data = pcall(mod2.decode, s)
        if d_ok then return data, nil end
        return nil, "json decode error"
    end
    return nil, "no json module available"
end

local HostMonitor = WidgetContainer:extend{
    name = "host_monitor",
    is_doc_only = false,   -- show in file browser / home, not only when a doc is open
}

function HostMonitor:init()
    -- config.txt in the plugin folder can pin the bridge base and the colour
    -- theme without needing any input dialog. A saved setting always
    -- overrides the file.
    if self.path then
        local f = io.open(self.path .. "/config.txt", "r")
        if f then
            local lines = {}
            for ln in f:lines() do lines[#lines + 1] = ln end
            f:close()
            -- bridge base: only if not already saved in settings
            local saved_base = G_reader_settings and G_reader_settings:readSetting("hb_bridge_base")
            if not (saved_base and saved_base ~= "") then
                for _, ln in ipairs(lines) do
                    ln = ln:match("^%s*(.-)%s*$")
                    if ln ~= "" and ln:sub(1, 1) ~= "#" then
                        BRIDGE_BASE = ln
                        break
                    end
                end
            end
            -- the cover is ALWAYS the PC-rendered PNG now (the text board was
            -- removed), so image mode is the only mode
            MODE = "image"
            -- remote host to follow (a machine pushing via res_report.py);
            -- `host=vm2` in config.txt. Restricted charset: no URL-encoding
            -- needed, and a value with spaces/; is simply rejected.
            for _, ln in ipairs(lines) do
                local hm = ln:match("^%s*host=%s*(.-)%s*$")
                if hm and hm:match("^[%w%.-]+$") and #hm > 0 then
                    HOST = hm
                    break
                end
            end
            -- theme: saved setting > config.txt "theme=" line > default dark
            local saved_theme = G_reader_settings and
                                G_reader_settings:readSetting("hb_theme")
            if saved_theme == "light" or saved_theme == "dark" then
                THEME = saved_theme
            else
                for _, ln in ipairs(lines) do
                    local t = ln:match("^%s*theme=%s*(%w+)%s*$")
                    if t == "light" or t == "dark" then THEME = t break end
                end
            end
            -- (hotkey support was removed: unusable on touch-only devices)
        end
    end
    -- Publish our two actions to KOReader's Dispatcher so they can be bound
    -- to a gesture: 设置 -> 手势 -> 添加手势 -> 动作 -> "Host ...".
    self:onDispatcherRegisterActions()
    -- self.ui is set by the framework before init() is called.
    if self.ui and self.ui.menu then
        self.ui.menu:registerToMainMenu(self)
    end
end

-- ---- Dispatcher actions (gestures / profiles / quick menu) ----------------
-- registerAction() is the ONLY way a plugin shows up in KOReader's gesture
-- list. The `event` name becomes the Event that Dispatcher sends, so the
-- handler below must be named on<event>. Everything is pcall-guarded: on a
-- build without ui/dispatcher the plugin must still load and work from menu.
function HostMonitor:onDispatcherRegisterActions()
    pcall(function()
        local ok, Dispatcher = pcall(require, "dispatcher")
        if not (ok and Dispatcher and Dispatcher.registerAction) then return end
        Dispatcher:registerAction("host_dashboard", {
            category = "none",
            event = "HostDashboard",
            title = "Host 常驻看板",
            general = true,
            device = true,
            separator = true,
        })
        Dispatcher:registerAction("host_dashboard_exit", {
            category = "none",
            event = "HostExitDashboard",
            title = "Host 退出常驻看板",
            general = true,
            device = true,
            separator = true,
        })
        Dispatcher:registerAction("host_set_lock", {
            category = "none",
            event = "HostSetLock",
            title = "Host 设置休眠壁纸",
            general = true,
            device = true,
            separator = false,
        })
    end)
    -- NOTE: standby auto-refresh is intentionally NOT started here. The user
    -- only asked for the standby picture to be readable (dark on light), not
    -- for it to keep re-fetching in the background.
end

-- Gesture handler: toggles the board, so the same swipe opens and closes it.
function HostMonitor:onHostDashboard()
    if self._dashboard and not self._exiting then
        self:exitDashboard()
    else
        self:startDashboard()
    end
    return true
end

-- Gesture handler: unconditional exit (for people who prefer two gestures).
function HostMonitor:onHostExitDashboard()
    if self._dashboard or self.active_widget then
        self:exitDashboard()
    end
    return true
end

-- Gesture handler: arm the lock-screen cover and generate it now.
function HostMonitor:onHostSetLock()
    self:updateLockScreenFile(true)
    return true
end

function HostMonitor:addToMainMenu(menu_items)
    self._modeItem = {
        text = "配色: " .. (THEME == "light" and "白底黑字" or "黑底白字"),
        callback = function()
            local next_ = (THEME == "dark") and "light" or "dark"
            setTheme(next_)
            if self._modeItem then
                self._modeItem.text = "配色: " ..
                    (THEME == "light" and "白底黑字" or "黑底白字")
            end
            -- apply immediately if the board is on screen
            if self.active_widget then
                if next_ == "light" then setMode("image") end
                self:showCover(true)
            else
                UIManager:show(InfoMessage:new{
                    text = "配色已切到" .. (next_ == "light" and "『白底黑字』" or
                        "『黑底白字』") .. "。\n打开常驻看板后生效。",
                })
            end
            pcall(function()
                if self.ui and self.ui.menu and self.ui.menu.updateItems then
                    self.ui.menu:updateItems()
                end
            end)
        end,
    }
    local lock_submenu = {
        text = "锁屏封面文件",
        sub_item_table = {
            { text = "设置休眠壁纸 (生成并跟随看板)",
              callback = function() self:updateLockScreenFile(true) end },
            { text = "查看锁屏文件位置与设置说明",
              callback = function() self:showLockScreenHelp() end },
            { text = "取消休眠壁纸 (停止更新文件)",
              callback = function() self:disableLockScreenFile() end },
        },
    }
    menu_items.host_monitor = {
        text = "Host Monitor",
        sorting_hint = "more_tools",
        sub_item_table = {
            { text = "常驻看板", callback = function() self:startDashboard() end },
            { text = "✕ 退出常驻看板", callback = function() self:exitDashboard() end },
            lock_submenu,
            self._modeItem,
            { text = "设置桥地址 (IP:端口)", callback = function() self:configure() end },
        },
    }
end

-- ---- always-on dashboard: keep the device awake + WiFi up so the 3-min ----
-- ---- fetch loop can actually run. NOTE: e-ink cannot fetch over WiFi   ----
-- ---- while suspended, so "autonomous refresh" REQUIRES staying awake.  ----
-- The device may still be suspended by hand (power button / cover). Nothing
-- can run while asleep, so on wake-up pull a fresh cover immediately instead
-- of waiting for the next 3-minute tick.
function HostMonitor:onResume()
    -- Keep the lock-screen cover file fresh on wake (network is up here),
    -- but only if the user has armed it via "设置休眠壁纸".
    pcall(function() self:_updateLockFile() end)
    if not self._dashboard or self._exiting then return end
    local plugin = self
    UIManager:scheduleIn(2, function()
        if plugin._dashboard and not plugin._exiting then
            plugin:_showImage(true, true)
        end
    end)
end

-- The lock-screen cover is now a plain FILE we keep at a fixed path
-- (hb_ss/cover.png); the user points KOReader's lock screen at that folder
-- themselves (设置 -> 屏幕 -> 锁屏类型=随机图片, 锁屏图片文件夹=hb_ss).
-- We never read/write KOReader's screensaver_* settings, so we never clobber
-- the user's own lock-screen config. The file is mirrored from the latest
-- dashboard cover in _updateLockFile() (called from the board refresh and
-- onResume) -- there is no fetch inside onSuspend.

-- NOTE: intentionally no onSuspend logic. The lock-screen cover file is
-- already on disk (mirrored from the dashboard in _updateLockFile), so a sleep
-- never needs the network and can never show a timed-out / stale picture.

function HostMonitor:startDashboard()
    self._dashboard = true          -- remember we are in always-on mode
    -- NOTE: we deliberately do NOT touch screensaver_type any more. An older
    -- build parked it on "disable" while the board was up and restored it to a
    -- made-up "light" value on exit; "light" is not a real KOReader standby
    -- mode, so the device fell back to a pale grey screen and the configured
    -- cover never appeared. Staying awake is handled by _keepAwake() alone.
    pcall(function()
        if G_reader_settings then
            G_reader_settings:saveSetting("wifi_auto_restore", true)
        end
    end)
    -- The REAL guard against falling asleep: see _keepAwake().
    -- Make sure WiFi is actually on.
    pcall(function()
        local NetworkMgr = require("ui/network/networkmanager")
        if not NetworkMgr then return end
        local on = (NetworkMgr.isWifiOn and NetworkMgr:isWifiOn())
                   or (NetworkMgr.isConnected and NetworkMgr:isConnected())
        if not on and NetworkMgr.turnOnWifi then
            NetworkMgr:turnOnWifi()
        end
    end)
    self:showCover(true)
end

-- ---- LEAVE the always-on dashboard and return to KOReader proper.---------
-- This is the ONLY exit path: the tap / long-press / swipe, the page-turn
-- keys, the menu item and the auto-refresh cancellation all funnel through
-- here, so the device can never get stuck in a screen the user cannot leave.
--
-- WHY THE TOKEN + REPEATED SWEEPS:
--   The PNG download is SYNCHRONOUS and blocks the UI for up to ~12s. A tap
--   made during that freeze is only delivered afterwards, and the code that
--   was already in flight would then go on to UIManager:show() a brand-new
--   board -- so the user saw "已退出常驻看板" pop up and then got dropped
--   straight back into the dashboard, with the screen apparently dead. Two
--   things fix it:
--     * `self._gen` is bumped on every exit AND every (re)entry. _showImage
--       captures it before downloading and refuses to show anything if it
--       changed while it was blocked -- the late widget is discarded, not shown.
--     * a few delayed sweeps re-close anything that still slipped through.
function HostMonitor:exitDashboard()
    self._dashboard = false
    self._exiting = true          -- stop any in-flight refresh tick
    self._gen = (self._gen or 0) + 1
    self:_cancelTimer()
    self:_closeAllBoards()
    self:_keepAwake(false)
    -- Sweep again shortly: covers the "download finished after the tap" case,
    -- where a widget is created a second or two AFTER this function returns.
    local plugin = self
    local my_gen = self._gen
    for _, delay in ipairs({ 1, 3, 8, 20 }) do
        UIManager:scheduleIn(delay, function()
            if plugin._gen ~= my_gen then return end   -- user re-entered: stop
            if not plugin._exiting then return end
            plugin:_closeAllBoards()
        end)
    end
    -- force KOReader to repaint the home screen immediately
    pcall(function()
        if self.ui and self.ui.menu and self.ui.menu.updateItems then
            self.ui.menu:updateItems()
        end
    end)
    pcall(function() UIManager:show(InfoMessage:new{
        text = "已退出常驻看板。", timeout = 2,
    }) end)
end

-- Close EVERY board widget this plugin ever put on screen.
--
-- Why this exists: the old code closed only `self.active_widget`, and on this
-- device the board still stayed on screen after the "已退出常驻看板" popup --
-- i.e. the reference we held was not the widget actually on top (a rebuild
-- raced with the tap), or UIManager:close() silently did nothing for it. So we
-- no longer trust a single reference:
--   1. close the widget we own (if any);
--   2. walk UIManager's window stack and drop EVERYTHING tagged `_wb_board`,
--      using table.remove as a last resort if UIManager:close() left it there;
--   3. force a full repaint, because an e-ink screen keeps showing the stale
--      image until something below it actually refreshes.
function HostMonitor:_closeAllBoards()
    if self.active_widget then
        pcall(function() UIManager:close(self.active_widget) end)
        self.active_widget = nil
    end
    pcall(function()
        local stack = UIManager._window_stack
        if type(stack) ~= "table" then return end
        for i = #stack, 1, -1 do
            local w = stack[i]
            if w and w._wb_board then
                pcall(function() UIManager:close(w) end)
                -- still there? UIManager:close() ignored it -> yank it by hand
                if stack[i] == w then
                    table.remove(stack, i)
                end
            end
        end
    end)
    pcall(function() UIManager:setDirty(nil, "full") end)
    pcall(function()
        if UIManager.forceRePaint then UIManager:forceRePaint() end
    end)
    -- e-ink keeps the stale image until something forces a real waveform
    -- refresh; without this the board can visibly stay on screen even after
    -- its widget is gone.
    pcall(function()
        local ok, Event = pcall(require, "ui/event")
        if ok and Event and Event.new and UIManager.broadcastEvent then
            UIManager:broadcastEvent(Event:new("SetFullScreenRefresh"))
        end
    end)
end

function HostMonitor:configure()
    -- TextInputDialog is missing on some KOReader builds; fall back to
    -- InputDialog, then to a config-file instruction.
    local ok, Dlg = pcall(require, "ui/widget/textinputdialog")
    if not (ok and Dlg) then
        ok, Dlg = pcall(require, "ui/widget/inputdialog")
    end
    if not (ok and Dlg) then
        UIManager:show(InfoMessage:new{
            text = "此 KOReader 版本没有文本输入对话框，无法在此输入地址。\n\n请在插件目录的 config.txt 第一行写入桥地址，例如：\nhttp://192.168.137.1:8865  (连电脑热点)\n或 http://192.168.1.20:8865  (连路由器)\n然后重启 KOReader。",
        })
        return
    end
    -- Canonical KOReader pattern: declare `dialog` FIRST, then assign inside
    -- the constructor. The button callbacks close over this single local, so
    -- they always see the live object (avoids the "dialog is nil" crash).
    local dialog
    local function build()
        dialog = Dlg:new{
            title = "Host 桥地址",
            input = BRIDGE_BASE,
            input_type = "string",
            buttons = {
                {
                    {
                        text = "取消",
                        callback = function()
                            UIManager:close(dialog)
                        end,
                    },
                    {
                        text = "保存",
                        is_enter_default = true,
                        callback = function()
                            local v = (dialog:getInputText() or ""):match("^%s*(.-)%s*$")
                            if v ~= "" then
                                BRIDGE_BASE = v
                                if G_reader_settings then
                                    G_reader_settings:saveSetting("hb_bridge_base", v)
                                end
                            end
                            UIManager:close(dialog)
                        end,
                    },
                },
            },
        }
    end
    local ok2, err = pcall(build)
    if not ok2 then
        UIManager:show(InfoMessage:new{
            text = "无法创建输入对话框（此 KOReader 版本缺少该组件）。\n\n请直接编辑插件目录的 config.txt 第一行写入桥地址，例如：\nhttp://192.168.137.1:8865  (连电脑热点)\n或 http://192.168.1.20:8865  (连路由器)\n保存后重启 KOReader。",
        })
        return
    end
    UIManager:show(dialog)
end

-- ---- fetch status JSON ----
local function fetch_json(url)
    local http = get_http()
    if not http then return nil, "LuaSocket 不可用 (此 KOReader 版本缺 socket.http)" end
    local body, code = http.request(url)
    if not body then return nil, "HTTP " .. tostring(code) end
    return json_decode(body)
end

-- Safe face lookup: cfont (CJK) may be missing on some builds; fall back to
-- ffont (always present) so TextWidget never gets a nil face and crashes.
local function safe_face(size)
    local ok, face = pcall(Font.getFace, Font, "cfont", size)
    if ok and face then return face end
    ok, face = pcall(Font.getFace, Font, "ffont", size)
    if ok and face then return face end
    return nil
end

-- LuaSocket http.request in TABLE form (with a sink): on SUCCESS it returns
-- `1, status_code, headers, status_line` -- the first value is the NUMBER 1,
-- NOT the status code (status code is the SECOND value). On failure: nil,
-- error_message. The old code compared the FIRST value against 200, which is
-- never true -> image mode always "failed" and fell back to the text board
-- even though the PNG had downloaded fine. Accept (1, 200) and also tolerate
-- builds that return the status code first. Final sanity: the downloaded file
-- must start with the PNG signature.
local function cover_fetch_ok(rok, rinfo, path)
    if rok == nil then return false, tostring(rinfo) end
    local code = tonumber(rinfo) or tonumber(rok)
    if rok == 1 and code == 200 then
        local f = io.open(path, "rb")
        if not f then return false, "cache file missing" end
        local sig = f:read(4) or ""
        f:close()
        if sig == "\137PNG" then return true end
        return false, "downloaded file is not a PNG"
    end
    if code == 200 then return true end
    return false, "HTTP " .. tostring(code or rinfo or "?")
end

-- ---- build the monochrome status board (NO colors, NO borders) ----
-- Everything is default-black TextWidget + ASCII, which is exactly what
-- KOReader's own UI uses and is safe on this broken BlitBuffer.
local function f1(x)
    local n = tonumber(x)
    if n == nil then return "?" end
    return string.format("%.1f", n)
end

function HostMonitor:buildBoard(data, err)
    local L = {}
    local function add(t, s)
        local face = safe_face(s)
        if face then
            L[#L + 1] = TextWidget:new{ text = t, face = face }
        end
    end
    if err then
        add(">> BRIDGE ERROR <<", 22)
        add(err, 16)
        add("res-bridge 未运行 / IP 错误 / 防火墙未放行 8865", 14)
        add("addr: " .. tostring(BRIDGE_BASE)
            .. (HOST and ("   host=" .. HOST) or ""), 14)
        local vg = VerticalGroup:new{ align = "left" }
        for _, w in ipairs(L) do vg[#vg + 1] = w end
        return vg
    end

    add(">> HOST STATUS <<", 26)
    add(tostring((data and data.host) or "?")
        .. "  updated " .. tostring((data and data.updatedAt) or "?"), 14)
    if data and data.error then
        add("ERR: " .. tostring(data.error), 14)
    end
    add("", 6)
    local cpu = (data and data.cpu) or {}
    add(string.format("CPU %s%%   LOAD %s/%s/%s",
        f1(cpu.pct), f1(cpu.load1), f1(cpu.load5), f1(cpu.load15)), 20)
    add("", 4)
    local mem = (data and data.mem) or {}
    add(string.format("MEM %sG/%sG (%s%%)%s",
        f1(mem.usedGB), f1(mem.totalGB), f1(mem.pct),
        (tonumber(mem.swapTotalGB) or 0) > 0 and
        string.format("   SWAP %s%%", f1(mem.swapPct)) or ""), 18)
    add("", 4)
    local disks = (data and data.disk) or {}
    for i, d in ipairs(disks) do
        if i > 3 then break end
        add(string.format("DISK %s  %s/%sG (%s%%)",
            tostring(d.mnt or "?"), f1(d.totalGB), f1(d.usedGB),
            f1(d.pct)), 16)
    end
    add("", 4)
    local procs = (data and data.procs) or {}
    if #procs > 0 then
        add("TOP MEM:", 16)
        for i, p in ipairs(procs) do
            if i > 10 then break end
            local nm = tostring(p.name or "?")
            if #nm > 24 then nm = nm:sub(1, 23) .. ".." end
            add(string.format("  %-24s %s%%  %sM",
                nm, f1(p.cpu), f1(p.memMB)), 14)
        end
    end
    local alarms = (data and data.alarms) or {}
    if #alarms > 0 then
        add("", 4)
        add("ALARM: " .. table.concat(alarms, " | "), 16)
    end
    if data and data.uptime then
        add("", 4)
        add("UP " .. tostring(data.uptime), 14)
    end

    add("", 6)
    add("-- 点『退出常驻看板』或任意处返回 --", 14)

    local vg = VerticalGroup:new{ align = "left" }
    for _, w in ipairs(L) do vg[#vg + 1] = w end
    return vg
end

function HostMonitor:_cancelTimer()
    if self.refresh_timer then
        UIManager:unschedule(self.refresh_timer)
        self.refresh_timer = nil
    end
end

-- Close the full-screen view currently on screen (if any) so re-opening a
-- cover never stacks a second board on top of the first one.
function HostMonitor:_closeActive()
    if self.active_widget then
        pcall(function() UIManager:close(self.active_widget) end)
        self.active_widget = nil
    end
end

-- Keep the device awake (disable auto-suspend) while a live cover is shown,
-- restoring the previous screensaver setting when the cover is closed. Used by
-- image mode so the "封面" stays put and never blanks out ("不息屏").
-- Keeping the dashboard alive is NOT just about the screensaver.
--
-- crash.log evidence (10/03 22:44:08 -> 23:07:53):
--     INFO  Inhibiting user input
--     ... 23 minutes with no "Restoring user input handling" ...
--
-- "Inhibiting user input" is logged by Input:inhibitInput(true), which
-- Device:_beforeSuspend() calls -- i.e. the device had SUSPENDED. While
-- suspended every tap and key is routed to the void, so the ✕ appeared dead
-- and the only way out was a reboot. `screensaver_type = "disable"` does NOT
-- stop that: the autosuspend plugin has its own 15-minute timer
-- (auto_suspend_timeout_seconds) and suspends regardless of the screensaver.
--
-- So we also (a) set PluginShare.pause_auto_suspend, which autosuspend checks
-- on every tick and honours immediately, and (b) park the timeout itself at 0
-- (= disabled) as a belt-and-braces fallback. Both are undone on exit.
function HostMonitor:_keepAwake(on)
    -- (a) tell the autosuspend plugin to stand down, right now
    pcall(function()
        local ok, PluginShare = pcall(require, "pluginshare")
        if ok and PluginShare then
            PluginShare.pause_auto_suspend = on and true or nil
        end
    end)
    pcall(function()
        if not G_reader_settings then return end
        -- (b) and neutralise the timeout setting itself
        if on then
            if self._prev_autosuspend == nil then
                self._prev_autosuspend =
                    G_reader_settings:readSetting("auto_suspend_timeout_seconds")
            end
            G_reader_settings:saveSetting("auto_suspend_timeout_seconds", 0)
        else
            local prev = self._prev_autosuspend
            if prev == nil then prev = 15 * 60 end   -- KOReader default
            G_reader_settings:saveSetting("auto_suspend_timeout_seconds", prev)
            self._prev_autosuspend = nil
        end
    end)
    -- We no longer touch screensaver_type at all: the lock-screen cover is a
    -- plain file the user points KOReader at, so there is nothing to heal here.
end

-- ---- primary entry: pick the render mode then show ----
function HostMonitor:showCover(auto)
    auto = auto or false
    self._exiting = false         -- (re)entering the board clears the exit guard
    self._gen = (self._gen or 0) + 1
    self:_cancelTimer()
    self:_closeActive()
    -- The PNG cover is the only mode now. _showImage silently falls back to
    -- the text board if the download or the blit fails, so there is still a
    -- readable screen in every failure mode.
    self:_keepAwake(true)
    self:_showImage(true, false)
end

-- Is there a previously downloaded cover we can keep showing while the bridge
-- is unreachable? A transient bridge blip must NOT blank the dashboard.
local function is_png(path)
    local f = io.open(path, "rb")
    if not f then return false end
    local sig = f:read(4) or ""
    f:close()
    return sig == "\137PNG"
end

-- ---- image mode: PNG cover fetched from the PC bridge ----
-- `quiet` = we are re-rendering from the auto-refresh tick, not because the
-- user just asked for the board: on a transient failure we keep the last good
-- picture and retry silently instead of flashing "BRIDGE ERROR".
function HostMonitor:_showImage(auto, quiet)
    auto = true  -- image mode is always a live, refreshing cover
    self:_cancelTimer()
    local my_gen = self._gen
    local http = get_http()
    local ltn12 = get_ltn12()
    if not http or not ltn12 then
        self:_showBoard(true, quiet); return
    end
    local w = Device.screen:getWidth()
    local h = Device.screen:getHeight()
    -- NEVER reuse a fixed file name. KOReader's image renderer caches the
    -- decoded bitmap under a hash of (path, width, height) -- it does NOT look
    -- at the file's mtime. So downloading fresh bytes into one fixed path
    -- (upstream's old wb_cover.png) over and over kept showing the FIRST
    -- picture ever fetched (the cover looked frozen at one timestamp).
    -- A unique path per fetch busts that cache.
    local path = self:_newCoverPath()
    -- Download into a SCRATCH file first: if the transfer dies halfway we must
    -- not have truncated the cached PNG we are currently displaying.
    local tmp = path .. ".tmp"
    local f = io.open(tmp, "wb")
    if not f then self:_showBoard(true, quiet); return end
    http.TIMEOUT = 12  -- keep the UI-freeze window short; retries are cheap
    local rok, rinfo = http.request{
        url = string.format("%s/cover.png?w=%d&h=%d&mono=1&theme=%s", BRIDGE_BASE, w, h, THEME)
              .. host_param(),
        sink = ltn12.sink.file(f),
    }
    -- ltn12.sink.file() closes the handle when the transfer finishes, so the
    -- file is ALREADY closed here. A second f:close() throws "closed file" ->
    -- guard it (this was the image-mode crash).
    pcall(function() f:close() end)
    -- The user may have tapped 退出 during the (blocking) download: a widget
    -- built now must never reach the screen.
    if self._exiting then self:_discardTmp(tmp); return end
    local ok_dl, derr = cover_fetch_ok(rok, rinfo, tmp)
    if ok_dl then
        pcall(function() os.remove(path) end)
        if not os.rename(tmp, path) then
            -- rename unsupported (rare): fall back to using the scratch file
            path = tmp
        end
        self._fail_count = 0
        -- unique names would pile up: drop the previous one
        if self._last_cover and self._last_cover ~= path then
            pcall(function() os.remove(self._last_cover) end)
        end
        self._last_cover = path
        self:_updateLockFile()
    else
        self:_discardTmp(tmp)
        logger.dbg("HostMonitor cover fetch failed: " .. tostring(derr))
        -- transient blip? keep the last good cover and retry quietly.
        if quiet and self._last_cover and is_png(self._last_cover) then
            self._fail_count = (self._fail_count or 0) + 1
            -- Tolerate one or two blips silently, but surface a real outage
            -- instead of leaving a stale cover on screen forever: with no
            -- visible cue the board just looks "stuck at an old timestamp".
            if self._fail_count <= 2 then
                if not self._exiting then
                    self.refresh_timer = UIManager:scheduleIn(20, function()
                        self:_showImage(true, true)
                    end)
                end
                return      -- leave the current (good) cover on screen
            end
        end
        self:_showBoard(true, quiet); return
    end
    if self._exiting or self._gen ~= my_gen then return end
    local ok, ImageWidget = pcall(require, "ui/widget/imagewidget")
    if not (ok and ImageWidget) then
        self:_showBoard(true, quiet); return
    end
    local img = ImageWidget:new{ file = path }
    local plugin = self
    local scr = Device.screen:getSize()
    local widget
    local function closeSelf()
        plugin:exitDashboard()
    end
    -- LAYOUT NOTE: this MUST stay a single CenterContainer holding just the
    -- image. An earlier version added a TopContainer with a real Button above
    -- it, but passing `dimen = scr` made that TopContainer consume the WHOLE
    -- screen height, pushing the cover image off-screen -- the user saw only
    -- the exit bar and no dashboard at all. The exit affordance is therefore
    -- DRAWN INTO the cover PNG itself (see the bridge's ?exit=1 overlay), and
    -- every gesture/key exits, so no extra widget is needed here.
    widget = InputContainer:new{
        dimen = scr,
        ges_events = {
            TapSelect  = { GestureRange:new{ ges = "tap",   range = scr } },
            HoldSelect = { GestureRange:new{ ges = "hold",  range = scr } },
            -- swipe is the most reliable gesture on a touch-only e-ink screen,
            -- so it exits too (tap can be swallowed while the screen is busy).
            SwipeClose = { GestureRange:new{ ges = "swipe", range = scr } },
        },
        key_events = {
            ExitPgDn = { { "Press", "PGDN" }, { "Press", "NextPage" } },
            ExitPgUp = { { "Press", "PGUP" }, { "Press", "PrevPage" } },
        },
    }
    widget[1] = CenterContainer:new{ dimen = scr, img }
    widget._wb_board = true   -- so _closeAllBoards() can find it on any rebuild
    function widget:onTapSelect()  closeSelf(); return true end
    function widget:onHoldSelect() closeSelf(); return true end
    function widget:onSwipeClose() closeSelf(); return true end
    function widget:onExitPgDn()   closeSelf(); return true end
    function widget:onExitPgUp()   closeSelf(); return true end
    -- last checkpoint: the download was blocking, so an exit may have landed
    -- while this widget was being built -- never show it in that case.
    if self._exiting or self._gen ~= my_gen then return end
    -- Only now, with a complete PNG ready, drop the previous board -- so a
    -- failed refresh leaves the old cover up instead of a blank screen.
    self:_closeActive()
    -- "full" refresh is REQUIRED on e-ink. With the default partial refresh a
    -- newly rendered cover can leave the previous waveform ghosted on screen,
    -- so the board looks frozen at an old timestamp even though a fresh PNG
    -- was downloaded and shown.
    UIManager:show(widget, "full")
    pcall(function() UIManager:setDirty(nil, "full") end)
    self.active_widget = widget
    -- Live board: keep polling while it is on screen. The lock-screen cover
    -- file is mirrored from this same fresh cover via _updateLockFile() (called
    -- right after this fetch succeeds), so it stays in sync without any
    -- suspend-time fetch.
    self.refresh_timer = UIManager:scheduleIn(REFRESH_SEC, function()
        self:_autoRefresh(widget)
    end)
end

-- Drop a half-written scratch download so it can never be mistaken for a
-- usable cached cover.
function HostMonitor:_discardTmp(tmp)
    pcall(function() os.remove(tmp) end)
end

-- ---- automatic fallback: native monochrome text board ----
function HostMonitor:_showBoard(auto, quiet)
    auto = auto or false
    self:_cancelTimer()
    local my_gen = self._gen
    local data, err = fetch_json(status_url())
    if self._exiting or self._gen ~= my_gen then return end
    -- Drop the previous board BEFORE building its replacement. Without this,
    -- falling back from the image mode stacked a SECOND board on top of the
    -- cover that was still on screen (two widgets on the window stack).
    self:_closeActive()
    local board = self:buildBoard(data, err)
    local plugin = self
    local scr = Device.screen:getSize()
    local Button = require("ui/widget/button")
    local widget
    local function closeSelf()
        plugin:exitDashboard()
    end
    -- A REAL tappable button, with bordersize=0 so it never draws a frame.
    -- This is the primary exit path in the fallback board; the full-screen
    -- InputContainer tap can be unreliable on some e-ink builds, so we do not
    -- depend on it alone.
    local exit_btn = Button:new{
        text = "✕ 退出常驻看板（也可点任意处 / 翻页键）",
        width = math.min(scr.w - 16, 420),
        bordersize = 0,
        radius = 0,
        callback = closeSelf,
    }
    local TopContainer = require("ui/widget/container/topcontainer")
    -- Pin the exit button to the very TOP of the screen, then the board below
    -- it. A real Button widget is used (not just a full-screen gesture) because
    -- the bare InputContainer tap proved unreliable on this e-ink build.
    local content = VerticalGroup:new{ align = "left", exit_btn, board }
    widget = InputContainer:new{
        dimen = scr,
        ges_events = {
            TapSelect  = { GestureRange:new{ ges = "tap",   range = scr } },
            HoldSelect = { GestureRange:new{ ges = "hold",  range = scr } },
            SwipeClose = { GestureRange:new{ ges = "swipe", range = scr } },
        },
        -- Physical page-turn keys (上/下翻页键) also exit, since this device
        -- has no back key. Key names are best-effort across Kindle builds.
        key_events = {
            ExitPgDn = { { "Press", "PGDN" }, { "Press", "NextPage" } },
            ExitPgUp = { { "Press", "PGUP" }, { "Press", "PrevPage" } },
        },
    }
    widget[1] = TopContainer:new{ dimen = scr, content }
    widget._wb_board = true
    function widget:onTapSelect()  closeSelf(); return true end
    function widget:onHoldSelect() closeSelf(); return true end
    function widget:onSwipeClose() closeSelf(); return true end
    function widget:onExitPgDn()   closeSelf(); return true end
    function widget:onExitPgUp()   closeSelf(); return true end
    if self._exiting or self._gen ~= my_gen then return end
    UIManager:show(widget)
    self.active_widget = widget
    if auto then
        self.refresh_timer = UIManager:scheduleIn(REFRESH_SEC, function()
            self:_autoRefresh(widget)
        end)
    end
end

-- Dashboard auto-refresh tick: on success rebuild the full-screen view in the
-- current mode; on failure retry SILENTLY (no error board) so a dead bridge
-- never spams a full-screen "BRIDGE ERROR" over whatever the user is reading.
--
-- RACE GUARD: the user can press 退出 at any moment, including while this tick
-- is mid-flight (the HTTP fetch takes seconds). Without the `_exiting` check
-- below, a tick that started BEFORE the exit would happily rebuild the board
-- and yank the user straight back into the cover -- which is exactly the
-- "can't get out, must reboot" symptom. Every branch therefore bails out if
-- an exit happened in the meantime.
function HostMonitor:_autoRefresh(old_widget)
    local my_gen = self._gen
    if self._exiting or self._gen ~= my_gen then return end
    -- IMPORTANT: do NOT gate the refresh on a status.json probe.
    --
    -- This used to fetch status.json first and bail out for a full 3 minutes
    -- when that one request failed. Since the cover PNG is what actually
    -- carries the timestamp, a single flaky status.json froze the board on a
    -- stale "SYNC 04:59" for 20+ minutes even though /cover.png was perfectly
    -- reachable -- exactly the bug the user reported.
    --
    -- So: go straight for the cover. _showImage() reschedules this tick on
    -- success, and retries quickly (20s) on failure, so the timestamp keeps
    -- advancing whenever the bridge is reachable at all.
    --
    -- NOTE: the old widget is NOT closed here on purpose. _showImage only
    -- replaces it once a new PNG is actually in hand; if the fetch fails we
    -- keep showing the last good cover instead of blanking the screen.
    self:_showImage(true, true)
end

-- ---- optional: persist the (PC-rendered) cover as the screensaver image ----
function HostMonitor:_cacheDir()
    local ok, ds = pcall(require, "datastorage")
    if ok and ds and ds.getDataDir then
        local pcall_ok, dir = pcall(function() return ds:getDataDir() end)
        if pcall_ok and dir then return dir end
    end
    return os.getenv("HOME") or "/tmp"
end

-- A unique file name for every fetch. Some KOReader builds memoise the decoded
-- bitmap per path, so reusing one name could keep showing the first picture
-- ever downloaded; a fresh path guarantees a fresh decode.
function HostMonitor:_newCoverPath()
    self._cover_seq = (self._cover_seq or 0) + 1
    return string.format("%s/hb_cover_%d_%d.png",
                         self:_cacheDir(), os.time(), self._cover_seq)
end

-- ---- lock-screen cover FILE (user points KOReader at it manually) ---------
-- We do NOT write KOReader's screensaver_* settings (that would clobber the
-- user's own lock-screen config and, on this 2026 nightly, those keys are not
-- even consumed by the screensaver module). We just keep a PNG at a fixed path
-- always present and mirrored from the latest dashboard cover, so the user can
-- set KOReader's lock screen (设置 -> 屏幕 -> 锁屏类型=随机图片, 锁屏图片文件夹=hb_ss)
-- once and the file follows the board automatically.
function HostMonitor:_lockDir()
    return self:_cacheDir() .. "/hb_ss"
end

function HostMonitor:_lockFilePath()
    return self:_lockDir() .. "/cover.png"
end

function HostMonitor:_ensureLockDir()
    local dir = self:_lockDir()
    pcall(function()
        local ok, lfs = pcall(require, "libs/libkoreader-lfs")
        if ok and lfs and lfs.mkdir then lfs.mkdir(dir) end
    end)
    return dir
end

-- Copy an existing PNG file to `dst` atomically.
function HostMonitor:_copyFile(src, dst)
    local f = io.open(src, "rb")
    if not f then return false end
    local data = f:read("*a")
    f:close()
    if not data or #data < 8 or data:sub(1, 4) ~= "\137PNG" then return false end
    local tmp = dst .. ".hb.tmp"
    local out = io.open(tmp, "wb")
    if not out then return false end
    out:write(data)
    out:close()
    pcall(function() os.remove(dst) end)
    if not os.rename(tmp, dst) then return false end
    return true
end

-- Download a cover straight into `path` (atomic). exit_hint 0 = no ✕.
function HostMonitor:_fetchCoverFile(path, exit_hint, timeout)
    local http = get_http()
    local ltn12 = get_ltn12()
    if not (http and ltn12) then return false end
    local tmp = path .. ".hb.tmp"
    local f = io.open(tmp, "wb")
    if not f then return false end
    http.TIMEOUT = timeout or 12
    local rok, rinfo = http.request{
        url = string.format(
            "%s/cover.png?w=%d&h=%d&mono=1&theme=%s&exit=%d",
            BRIDGE_BASE, Device.screen:getWidth(),
            Device.screen:getHeight(), THEME, exit_hint or 0)
              .. host_param(),
        sink = ltn12.sink.file(f),
    }
    pcall(function() f:close() end)
    local good = false
    if cover_fetch_ok(rok, rinfo, tmp) then
        pcall(function() os.remove(path) end)
        if os.rename(tmp, path) then good = true end
    end
    if not good then pcall(function() os.remove(tmp) end) end
    return good
end

-- Mirror the latest dashboard cover into the fixed lock-screen file. Only runs
-- when the user has armed it ("设置休眠壁纸"). On any failure we KEEP the
-- previous file, so it is never left missing. Returns true if a file is present.
function HostMonitor:_updateLockFile()
    local armed = false
    pcall(function()
        if G_reader_settings then
            armed = G_reader_settings:readSetting("hb_ss_armed") == true
        end
    end)
    if not armed then return false end
    self:_ensureLockDir()
    local dst = self:_lockFilePath()
    local got = false
    -- Prefer the already-downloaded dashboard image: instant, offline, and
    -- literally "随看板最新更新" (a copy of the board's current cover).
    if type(self._last_cover) == "string" and is_png(self._last_cover) then
        got = self:_copyFile(self._last_cover, dst)
    end
    -- Fall back to a live fetch only when there is no dashboard image yet.
    if not got then
        got = self:_fetchCoverFile(dst, 0, 20)
    end
    return got
end

-- Arm + generate the lock-screen cover now (menu item / gesture).
function HostMonitor:updateLockScreenFile(show_msg)
    pcall(function()
        if G_reader_settings then
            G_reader_settings:saveSetting("hb_ss_armed", true)
        end
    end)
    local got = self:_updateLockFile()
    if show_msg then
        local dir = self:_lockDir()
        if got then
            UIManager:show(InfoMessage:new{
                text = "已生成锁屏封面文件（随看板自动更新）：\n" .. dir ..
                       "\n\n手动设置（一次性）：KOReader 设置 → 屏幕 → " ..
                       "锁屏类型 选「随机图片」，锁屏图片文件夹 选上面的 hb_ss 文件夹。" ..
                       "之后锁屏即显示此图，并随看板每 3 分钟更新。",
            })
        else
            UIManager:show(InfoMessage:new{
                text = "生成失败（桥不可达？）。若文件已存在则保留旧图。",
            })
        end
    end
    return got
end

-- Stop mirroring (keeps the existing file, just no longer updates it).
function HostMonitor:disableLockScreenFile()
    pcall(function()
        if G_reader_settings then
            G_reader_settings:saveSetting("hb_ss_armed", false)
        end
    end)
    UIManager:show(InfoMessage:new{
        text = "已停止更新锁屏文件（原文件保留）。再点「设置休眠壁纸」可恢复跟随。",
    })
end

function HostMonitor:showLockScreenHelp()
    local dir = self:_lockDir()
    UIManager:show(InfoMessage:new{
        text = "锁屏封面文件位置：\n" .. dir .. "/cover.png\n\n" ..
               "手动设置（一次性）：\n" ..
               "KOReader 设置 → 屏幕 → 锁屏类型 选「随机图片」，\n" ..
               "锁屏图片文件夹 选上面的 hb_ss 文件夹。\n\n" ..
               "该文件随常驻看板每 3 分钟自动更新，且始终存在。",
    })
end

return HostMonitor
