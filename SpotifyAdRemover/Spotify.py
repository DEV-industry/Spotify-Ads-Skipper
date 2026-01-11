import os
import sys
import ctypes
import threading
import subprocess
from pystray import Icon, MenuItem as item, Menu
from PIL import Image, ImageDraw


HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
START_MARKER = "### SPOTIFY AD BLOCK START ###"
END_MARKER = "### SPOTIFY AD BLOCK END ###"


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


tray_icon = None

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)

def create_image():
    icon_path = resource_path("cat.ico")

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

def log_debug(message):
    try:
        
        if getattr(sys, 'frozen', False):
             base_path = os.path.dirname(sys.executable)
        else:
             base_path = os.path.dirname(os.path.abspath(__file__))
             
        log_path = os.path.join(base_path, "debug_log.txt")
        with open(log_path, "a", encoding="utf-8") as f:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{timestamp}] {message}\n")
    except:
        pass

def fetch_ad_hosts():
    local_hosts_path = resource_path("ad_hosts.txt")
    log_debug(f"Reading ad list from: {local_hosts_path}")

    try:
        with open(local_hosts_path, 'r', encoding='utf-8') as f:
            content = f.read()
            log_debug(f"Read {len(content)} bytes from ad list.")
            return content
    except Exception as e:
        log_debug(f"Error reading ad list: {e}")
        return None

def update_hosts():
    log_debug("Starting update_hosts...")
    
    new_hosts_content = fetch_ad_hosts()
    if not new_hosts_content:
        log_debug("New hosts content is empty/None.")
        if tray_icon:
            tray_icon.notify("Błąd odczytu ad_hosts.txt.", "Spotify Skipper")
        return

    try:
        log_debug(f"Opening HOSTS_PATH: {HOSTS_PATH}")
        with open(HOSTS_PATH, 'r', encoding='utf-8') as file:
            current_content = file.read()

        
        if START_MARKER in current_content and END_MARKER in current_content:
            log_debug("Removing old block...")
            before = current_content.split(START_MARKER)[0]
            after = current_content.split(END_MARKER)[1]
            current_content = before + after

        
        current_content = current_content.strip()

        
        new_block = f"\n\n{START_MARKER}\n{new_hosts_content}\n{END_MARKER}\n"
        
        
        log_debug("Writing new content to hosts...")
        with open(HOSTS_PATH, 'w', encoding='utf-8') as file:
            file.write(current_content + new_block)
            
        
        log_debug("Flushing DNS...")
        subprocess.run(["ipconfig", "/flushdns"], creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log_debug("Update completed successfully.")
            
    except Exception as e:
        log_debug(f"Error updating hosts: {e}")
        if tray_icon:
            tray_icon.notify(f"Błąd zapisu hosts: {e}", "Spotify Skipper")

def restore_hosts():
    
    try:
        if os.path.exists(HOSTS_PATH):
            with open(HOSTS_PATH, 'r', encoding='utf-8') as file:
                current_content = file.read()

            if START_MARKER in current_content and END_MARKER in current_content:
                before = current_content.split(START_MARKER)[0]
                after = current_content.split(END_MARKER)[1]
                clean_content = before.strip() + "\n" + after.strip()
                
                with open(HOSTS_PATH, 'w', encoding='utf-8') as file:
                    file.write(clean_content)
                    
                subprocess.run(["ipconfig", "/flushdns"], creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        pass 

def on_quit(icon, item):
    restore_hosts()
    icon.stop()

def construct_menu():
    return Menu(
        item("Zamknij", on_quit)
    )

if __name__ == "__main__":
    if getattr(sys, 'frozen', False):
        script_path = sys.executable
        script_dir = os.path.dirname(script_path)
    else:
        script_path = os.path.abspath(__file__)
        script_dir = os.path.dirname(script_path)

    if not is_admin():
        print("Requesting admin privileges...")
        try:
            params = f'"{script_path}"'
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, script_dir, 0)
        except Exception as e:
            print(f"Failed to elevate: {e}")
            input("Press Enter to close...")
        sys.exit(0)

        
    try:
        log_path = os.path.join(script_dir, "debug_log.txt")
        
        tray_icon = Icon("SpotifySkipper", create_image(), "Spotify Skipper", construct_menu())
        
        threading.Thread(target=update_hosts).start()
        
        def notify_start():
            import time
            time.sleep(2) 
            if tray_icon:
                tray_icon.notify("Blokowanie reklam aktywne.", "Spotify Skipper")
                
        threading.Thread(target=notify_start).start()
        
        tray_icon.run()
        
    except Exception as e:
        crash_file = os.path.join(script_dir, "crash_log.txt")
        with open(crash_file, "w") as f:
            f.write(f"Crash Error: {e}")
        
        try:
            ctypes.windll.user32.MessageBoxW(0, f"Aplikacja napotkala blad: {e}", "Spotify Skipper Error", 0)
        except:
            pass
