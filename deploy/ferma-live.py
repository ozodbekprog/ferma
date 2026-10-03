#!/usr/bin/env python3
"""Ferma jonli ekran: Mutter ScreenCast -> PipeWire -> gst -> JPEG kadrlar.

Natija: /tmp/ferma-live/frame-XXXXXXXX.jpg (oxirgi 8 ta)
        /tmp/ferma-live/.alive  (heartbeat — har 5s)
Xato bo'lsa avtomatik qayta urinadi.
"""
import os
import subprocess
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

LIVE_DIR = "/tmp/ferma-live"
MONITORS = ("eDP-1", "eDP-2", "HDMI-A-1", "HDMI-A-2", "DP-1", "DP-2")


def log(msg):
    print("[ferma-live] %s" % msg, flush=True)


def cv(bus, name, path, iface, method, params=None, timeout=8000):
    return bus.call_sync(name, path, iface, method, params, None,
                         Gio.DBusCallFlags.NONE, timeout, None)


def newest_frames():
    try:
        return sorted(f for f in os.listdir(LIVE_DIR) if f.endswith(".jpg"))
    except OSError:
        return []


def run_once():
    os.makedirs(LIVE_DIR, exist_ok=True)
    for f in os.listdir(LIVE_DIR):
        try:
            os.remove(os.path.join(LIVE_DIR, f))
        except OSError:
            pass

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    r = cv(bus, "org.gnome.Mutter.ScreenCast", "/org/gnome/Mutter/ScreenCast",
           "org.gnome.Mutter.ScreenCast", "CreateSession",
           GLib.Variant("(a{sv})", ({},)))
    sess = r.unpack()[0]
    log("session: %s" % sess)

    stream = None
    for mon in MONITORS:
        try:
            r2 = cv(bus, "org.gnome.Mutter.ScreenCast", sess,
                    "org.gnome.Mutter.ScreenCast.Session", "RecordMonitor",
                    GLib.Variant("(sa{sv})", (mon, {})))
            stream = r2.unpack()[0]
            log("monitor: %s -> %s" % (mon, stream))
            break
        except Exception as e:
            log("RecordMonitor %s: %s" % (mon, str(e)[:90]))
    if stream is None:
        raise RuntimeError("monitor topilmadi")

    state = {"node": None}
    loop = GLib.MainLoop()

    def on_signal(conn, sender, path, iface, signal, params):
        try:
            (nid,) = params.unpack()
        except Exception:
            nid = params
        state["node"] = nid
        loop.quit()

    sub = bus.signal_subscribe(
        "org.gnome.Mutter.ScreenCast",
        "org.gnome.Mutter.ScreenCast.Stream",
        "PipeWireStreamAdded", stream, None,
        Gio.DBusSignalFlags.NONE, on_signal)

    try:
        cv(bus, "org.gnome.Mutter.ScreenCast", sess,
           "org.gnome.Mutter.ScreenCast.Session", "Start")
        log("session started")
    except Exception as e:
        log("Start: %s" % str(e)[:90])

    if state["node"] is None:
        GLib.timeout_add_seconds(15, lambda: (loop.quit(), False)[1])
        loop.run()
    bus.signal_unsubscribe(sub)

    nid = state["node"]
    if nid is None:
        raise RuntimeError("pipewire node kelmadi")
    log("node id: %s" % nid)

    alive = os.path.join(LIVE_DIR, ".alive")

    def touch():
        try:
            with open(alive, "w") as f:
                f.write("%d\n" % int(time.time()))
        except OSError:
            pass

    touch()
    gst_cmd = ["gst-launch-1.0", "-e",
               "pipewiresrc", "path=%d" % nid,
               "!", "videorate", "max-rate=4",
               "!", "videoconvert",
               "!", "jpegenc", "quality=70",
               "!", "multifilesink",
               "location=%s/frame-%%08d.jpg" % LIVE_DIR, "max-files=8"]

    child = subprocess.Popen(gst_cmd)
    t0 = time.time()
    fail_times = []
    nudged = False
    try:
        while True:
            if child.poll() is not None:
                now = time.time()
                fail_times = [t for t in fail_times if now - t < 60] + [now]
                log("gst chiqdi rc=%s (60s ichida %d marta)"
                    % (child.returncode, len(fail_times)))
                if len(fail_times) >= 4:
                    raise RuntimeError("gst barqaror emas")
                time.sleep(3)
                child = subprocess.Popen(gst_cmd)
                continue
            touch()
            if not nudged and time.time() - t0 > 25 and not newest_frames():
                try:
                    subprocess.run(["notify-send", "-t", "1500",
                                    "Ferma", "ekran oqimi ishga tushdi"],
                                   timeout=10)
                except Exception:
                    pass
                nudged = True
            time.sleep(5)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except Exception:
                child.kill()


def main():
    while True:
        try:
            run_once()
        except Exception as e:
            log("xato: %s — 10s dan keyin qaytadan" % str(e)[:180])
            time.sleep(10)


if __name__ == "__main__":
    main()
