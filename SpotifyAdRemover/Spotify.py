import time
import os
import sys
import psutil
import win32gui
import win32process
import win32api
import win32con
import subprocess
import threading
from pystray import Icon, MenuItem as item, Menu
from PIL import Image, ImageDraw


USER_NAME = os.getlogin()
SPOTIFY_PATH = f"C:\\Users\\{USER_NAME}\\AppData\\Roaming\\Spotify\\Spotify.exe"
WHITELISTED_TITLES = ["Spotify", "Spotify Free", "Spotify Premium"]


IS_RUNNING = True
IS_PAUSED = False
ADS_SKIPPED_COUNT = 0
tray_icon = None

def create_image():
    
    if getattr(sys, 'frozen', False):
        
        base_path = os.path.dirname(sys.executable)
    else:
        
        base_path = os.path.dirname(os.path.abspath(__file__))
        
    icon_path = os.path.join(base_path, "cat.ico")

    try:
        return Image.open(icon_path)
    except FileNotFoundError:
        
        width = 64
        height = 64
        color1 = "black"
        color2 = "#1DB954" 

        image = Image.new('RGB', (width, height), color1)
        dc = ImageDraw.Draw(image)
        dc.ellipse((10, 10, 54, 54), fill=color2)
        return image
    

def get_spotify_title():
    found_windows = []
    def callback(hwnd, windows):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc = psutil.Process(pid)
            if proc.name() == "Spotify.exe":
                title = win32gui.GetWindowText(hwnd)
                if win32gui.IsWindowVisible(hwnd) and title != "":
                    windows.append(title)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    win32gui.EnumWindows(callback, found_windows)
    if found_windows:
        return max(found_windows, key=len)
    return None

def restart_spotify():
    global ADS_SKIPPED_COUNT
    
    ADS_SKIPPED_COUNT += 1
    update_tray_title() 
    
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.info['name'] == 'Spotify.exe':
            try:
                proc.kill()
            except:
                pass
    
    time.sleep(1.0)
    
    try:
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = win32con.SW_SHOWMINNOACTIVE 
        subprocess.Popen([SPOTIFY_PATH, "--minimized"], startupinfo=startup_info)
        
        time.sleep(5)
        
        win32api.keybd_event(win32con.VK_MEDIA_NEXT_TRACK, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MEDIA_NEXT_TRACK, 0, win32con.KEYEVENTF_KEYUP, 0)
        
    except FileNotFoundError:
        pass



def on_quit(icon, item):
    global IS_RUNNING
    IS_RUNNING = False
    icon.stop()

def toggle_pause(icon, item):
    global IS_PAUSED
    IS_PAUSED = not IS_PAUSED
    update_tray_title()

def get_pause_label(item):
    return "Wznow działanie" if IS_PAUSED else "Pauzuj"

def construct_menu():
    return Menu(
        item(f"Pominieto reklam: {ADS_SKIPPED_COUNT}", lambda i, k: None, enabled=False),
        item(get_pause_label, toggle_pause),
        item("Zamknij", on_quit)
    )

def update_tray_title():
    if tray_icon:
        tray_icon.title = f"Skipper: Pominieto reklam: {ADS_SKIPPED_COUNT}"
        tray_icon.menu = construct_menu()


def monitoring_loop():
    while IS_RUNNING:
        if not IS_PAUSED:
            try:
                current_title = get_spotify_title()
                if current_title:
                    if "-" not in current_title:
                        if current_title not in WHITELISTED_TITLES:
                            restart_spotify()
            except Exception:
                pass
        time.sleep(1.5)



if __name__ == "__main__":
    t = threading.Thread(target=monitoring_loop)
    t.daemon = True
    t.start()

    initial_menu = construct_menu()

    tray_icon = Icon("SpotifySkipper", create_image(), "Spotify Skipper", initial_menu)
    tray_icon.run()