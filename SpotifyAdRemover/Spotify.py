import time
import os
import psutil
import win32gui
import win32process
import win32api  
import win32con  


AD_TITLES = ["Spotify", "Advertisement", "Reklama", ""] 
USER_NAME = os.getlogin()
SPOTIFY_PATH = f"C:\\Users\\{USER_NAME}\\AppData\\Roaming\\Spotify\\Spotify.exe"

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
    print("\n!!! --- Wykryto reklamę! Restartowanie Spotify... --- !!!\n")
    
    
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.info['name'] == 'Spotify.exe':
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    
    time.sleep(1.0)
    
    
    try:
        os.startfile(SPOTIFY_PATH)
        print("--- Spotify uruchomione ponownie ---")
        
        
        time.sleep(5) 
        
        print("--- Próba wznowienia odtwarzania (Symulacja Play/Pause) ---")
        
        win32api.keybd_event(win32con.VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MEDIA_PLAY_PAUSE, 0, win32con.KEYEVENTF_KEYUP, 0)

    except FileNotFoundError:
        print(f"BŁĄD: Nie znaleziono pliku pod ścieżką: {SPOTIFY_PATH}")
        print("Sprawdź czy ścieżka do Spotify.exe jest poprawna!")

def main():
    print("Monitorowanie Spotify rozpoczęte (Auto-Start Muzyki)...")
    print("------------------------------------------------")

    while True:
        try:
            current_title = get_spotify_title()
            
            if current_title:
                print(f"[{time.strftime('%H:%M:%S')}] Widzę okno: {current_title}")

                if current_title in AD_TITLES or "Reklama" in current_title:
                    restart_spotify()
            else:
                
                print(f"[{time.strftime('%H:%M:%S')}] Nie wykryto aktywnego okna. Upewnij się, że Spotify jest zmaksymalizowane.")

            time.sleep(1.5)
            
        except Exception as e:
            print(f"Wystąpił błąd pętli głównej: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()