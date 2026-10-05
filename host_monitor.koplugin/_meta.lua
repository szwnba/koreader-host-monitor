local _ = require("gettext")
return {
    fullname = _("Host Monitor"),
    description = _([[Pull the PC-side host-resource status (CPU, memory, disk,
network, top processes) as a HUD cover image every 3 minutes over the local
network and show it on the Kindle. Fork of RC-APC's workbuddy_monitor
plugin: all collection + rendering happens on the PC side (res-bridge.py);
the Kindle only downloads a grayscale PNG and blits it. Optionally follows a
remote host that pushes its own stats (res_report.py) via a host= config line.]]),
}
