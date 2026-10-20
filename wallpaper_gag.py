"""
Wallpaper-Gag für Windows 11
----------------------------
Wechselt das Wallpaper je nach Ereignis (Laden, Akku, WLAN, CPU, Uhrzeit ...).
Tagsüber wird das neutrale Bild immer stärker horizontal aus dem Zentrum gestreckt.
"""
import ctypes
import datetime as dt
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import psutil
from PIL import Image, ImageFilter, ImageOps

# ---------------------------------------------------------------- Einstellungen
BASE = Path(__file__).resolve().parent
IMG_DIR = BASE / "bilder"
CACHE_DIR = BASE / "cache"
CACHE_DIR.mkdir(exist_ok=True)

CHECK_INTERVAL = 1        # Sekunden zwischen den Prüfungen
LOW_BATTERY = 30          # unter X % -> traurig
CRITICAL_BATTERY = 5      # unter X % -> weinend
CPU_LIMIT = 75            # % CPU
RAM_LIMIT = 75            # % RAM
STRESS_SAMPLES = 3        # so oft hintereinander über dem Limit -> gestresst
NIGHT_START = 16          # ab dieser Uhrzeit -> müde
NIGHT_END = 8             # bis zu dieser Uhrzeit -> müde
STRETCH_START_HOUR = 7    # ab hier beginnt das Strecken (davor Faktor 1.0)
MAX_STRETCH = 2.2         # Streckfaktor kurz vor Mitternacht
STRETCH_STATES = {"neutral", "tired"}   # welche Zustände gestreckt werden
HELLO_SECONDS = 5         # Dauer des Hallo-Bilds nach dem Entsperren
SHOCK_SECONDS = 5        # Dauer des Schock-Bilds nach Kabel abstecken

# Wenn True, springt der "cool"-Zustand bei JEDEM verbundenen Bluetooth- oder externen Audiogerät an
AUTO_DETECT_BLUETOOTH_AUDIO = True

STATE_IMAGES = {
    "neutral": "neutral.jpg",
    "happy": "happy.jpg",
    "sad": "sad.jpg",
    "crying": "crying.jpg",
    "shock": "shock.jpg",
    "confused": "confused.jpg",
    "stressed": "stressed.jpg",
    "tired": "tired.jpg",
    "cool": "cool.jpg",
    "hello": "hello.jpg",
}

# ---------------------------------------------------------------- Windows-API
user32 = ctypes.windll.user32
user32.OpenDesktopW.restype = ctypes.c_void_p
user32.SwitchDesktop.argtypes = [ctypes.c_void_p]
user32.CloseDesktop.argtypes = [ctypes.c_void_p]


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def is_unlocked():
    """True, wenn der Rechner nicht gesperrt ist."""
    handle = user32.OpenDesktopW("Default", 0, False, 0x0100)
    if not handle:
        return False
    ok = user32.SwitchDesktop(handle)
    user32.CloseDesktop(handle)
    return bool(ok)


_toggle = 0


def set_wallpaper(image):
    """Speichert das Bild und setzt es als Wallpaper (abwechselnd 2 Dateien)."""
    global _toggle
    _toggle ^= 1
    path = CACHE_DIR / f"wallpaper_{_toggle}.jpg"
    image.save(path, quality=92)
    user32.SystemParametersInfoW(20, 0, str(path), 3)


# ---------------------------------------------------------------- Bildbearbeitung
def load_image(state):
    path = IMG_DIR / STATE_IMAGES.get(state, "neutral.jpg")
    if not path.exists():
        path = IMG_DIR / STATE_IMAGES["neutral"]
    return Image.open(path).convert("RGB")


def compose(img, factor=1.0):
    """Füllt den Bildschirm aus und streckt das Bild horizontal aus der Mitte."""
    sw, sh = screen_size()
    base_img = ImageOps.fit(img, (sw, sh), Image.LANCZOS)
    if factor <= 1.0:
        return base_img
    stretched_w = int(sw * factor)
    stretched = base_img.resize((stretched_w, sh), Image.LANCZOS)
    left = (stretched_w - sw) // 2
    return stretched.crop((left, 0, left + sw, sh))


def stretch_factor(now):
    hours = now.hour + now.minute / 60
    if hours < STRETCH_START_HOUR:
        return 1.0
    t = min((hours - STRETCH_START_HOUR) / (24 - STRETCH_START_HOUR), 1.0)
    return 1.0 + (MAX_STRETCH - 1.0) * t


# ---------------------------------------------------------------- Sensoren
def online():
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=1.5).close()
        return True
    except OSError:
        return False


def bluetooth_audio_connected():
    """Prüft, ob Bluetooth-Audiogeräte (wie Kopfhörer/Boxen) aktiv verbunden sind."""
    if not AUTO_DETECT_BLUETOOTH_AUDIO:
        return False
    # PowerShell prüft nach verbundenen Audio-Endpoints, die typischerweise Bluetooth/Headsets sind
    cmd = [
        "powershell", "-NoProfile", "-Command",
        "$devs = Get-PnpDevice -Class AudioEndpoint -PresentOnly | Where-Object Status -eq 'OK' | Select-Object -ExpandProperty FriendlyName; "
        "foreach($d in $devs) { if($d -match 'Bluetooth|Headphones|Headset|Hands-Free|Audio|Kopfhörer') { exit 0 } } exit 1"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=0x08000000)
        return res.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- Hauptschleife
def pick_state(pct, plugged, offline, stressed, bt_audio, hour):
    """Reihenfolge = Priorität. Zeilen einfach umsortieren, um das zu ändern."""
    on_battery = pct is not None and plugged is False
    if on_battery and pct < CRITICAL_BATTERY:
        return "crying"
    if on_battery and pct < LOW_BATTERY:
        return "sad"
    if offline:
        return "confused"
    if stressed:
        return "stressed"
    if plugged:
        return "happy"
    if bt_audio:
        return "cool"
    if hour >= NIGHT_START or hour < NIGHT_END:
        return "tired"
    return "neutral"


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        state = sys.argv[2]
        factor = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
        set_wallpaper(compose(load_image(state), factor))
        return

    last_plugged = None
    was_unlocked = True
    temp_state, temp_until = None, 0.0
    stress_hist = deque(maxlen=STRESS_SAMPLES)
    offline_count, next_net = 0, 0.0
    bt_audio, next_bt = False, 0.0
    last_key = None
    psutil.cpu_percent(None)

    while True:
        try:
            now = time.time()

            unlocked = is_unlocked()
            if unlocked and not was_unlocked:
                temp_state, temp_until = "hello", now + HELLO_SECONDS
            was_unlocked = unlocked
            if not unlocked:
                time.sleep(CHECK_INTERVAL)
                continue

            bat = psutil.sensors_battery()
            pct = bat.percent if bat else None
            plugged = bat.power_plugged if bat else None
            if last_plugged is True and plugged is False:
                temp_state, temp_until = "shock", now + SHOCK_SECONDS
            last_plugged = plugged

            cpu = psutil.cpu_percent(None)
            ram = psutil.virtual_memory().percent
            stress_hist.append(cpu > CPU_LIMIT or ram > RAM_LIMIT)
            stressed = len(stress_hist) == stress_hist.maxlen and all(stress_hist)

            if now >= next_net:
                offline_count = 0 if online() else offline_count + 1
                next_net = now + 10
            offline = offline_count >= 2

            if now >= next_bt:
                bt_audio = bluetooth_audio_connected()
                next_bt = now + 10  # Prüft alle 10 Sekunden auf Bluetooth-Audio

            now_dt = dt.datetime.now()
            if now < temp_until:
                state = temp_state
            else:
                state = pick_state(pct, plugged, offline, stressed, bt_audio, now_dt.hour)

            if state in STRETCH_STATES:
                factor = round(stretch_factor(now_dt) * 20) / 20
                key = (state, factor)
            else:
                factor, key = 1.0, (state,)

            if key != last_key:
                set_wallpaper(compose(load_image(state), factor))
                last_key = key
        except Exception as exc:
            print("Fehler:", exc)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()