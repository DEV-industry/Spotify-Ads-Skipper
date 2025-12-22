import time
import os
import psutil
import win32gui
import win32process
import win32api
import win32con
import subprocess  


USER_NAME = os.getlogin()
SPOTIFY_PATH = f"C:\\Users\\{USER_NAME}\\AppData\\Roaming\\Spotify\\Spotify.exe"

WHITELISTED_TITLES = ["Spotify", "Spotify Free", "Spotify Premium"]

def get_spotify_title():
    
    found_windows = []

    def callback(hwnd, windows):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc = psutil.Process(pid)
            if proc.name() == "Spotify.exe":
                title = win32gui.GetWindowText(hwnd)
                is_visible = win32gui.IsWindowVisible(hwnd)
                
                if is_visible and title != "":
                    windows.append(title)
                    
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    
    win32gui.EnumWindows(callback, found_windows)
    
    if found_windows:
        best_match = max(found_windows, key=len)
        return best_match
    return None

def restart_spotify():
    print("\n!!! --- Wykryto reklamę! Restartowanie w tle... --- !!!\n")
    
    
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.info['name'] == 'Spotify.exe':
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    
    time.sleep(1.0)
    
    
    try:
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = win32con.SW_SHOWMINNOACTIVE 
        
        subprocess.Popen([SPOTIFY_PATH, "--minimized"], startupinfo=startup_info)
        
        print("--- Spotify uruchomione w tle ---")
        
        time.sleep(5) 
        
        print("--- Przełączanie na następny utwór (Next Track) ---")
        
        win32api.keybd_event(win32con.VK_MEDIA_NEXT_TRACK, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MEDIA_NEXT_TRACK, 0, win32con.KEYEVENTF_KEYUP, 0)

    except FileNotFoundError:
        print(f"BŁĄD: Nie znaleziono pliku pod ścieżką: {SPOTIFY_PATH}")
    except Exception as e:
        print(f"BŁĄD uruchamiania: {e}")

def main():
    print("Monitorowanie Spotify rozpoczęte (Fix dla Pauzy)...")
    print("------------------------------------------------")

    while True:
        try:
            current_title = get_spotify_title()
            
            if current_title:
               
                if "-" not in current_title:
                    
                    if current_title not in WHITELISTED_TITLES:
                        print(f"[{time.strftime('%H:%M:%S')}] REKLAMA WYKRYTA: '{current_title}'")
                        restart_spotify()
                    else:
                        
                        pass 
                        
            

            time.sleep(1.5)
            
        except Exception as e:
            print(f"Wystąpił błąd: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()