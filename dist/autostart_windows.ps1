$ExeName = "Spotify.exe"


$CurrentDir = $PSScriptRoot
$ExePath = Join-Path -Path $CurrentDir -ChildPath $ExeName
$StartupDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$ShortcutPath = Join-Path -Path $StartupDir -ChildPath "Spotify-Ads-Skipper.lnk"

Write-Host "--- Instalator Spotify Ads Skipper ---" -ForegroundColor Cyan


if (-Not (Test-Path $ExePath)) {
    Write-Host "BLAD: Nie znaleziono pliku '$ExeName' w folderze:" -ForegroundColor Red
    Write-Host $CurrentDir
    Write-Host "Upewnij sie, że ten skrypt jest obok pliku .exe!" -ForegroundColor Yellow
    Read-Host "Nacisnij Enter, aby zakonczyc..."
    exit
}


try {
    $WshShell = New-Object -comObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $ExePath
    $Shortcut.WorkingDirectory = $CurrentDir
    $Shortcut.Description = "Uruchamia Spotify Ads Skipper"
    $Shortcut.Save()

    Write-Host "SUKCES!" -ForegroundColor Green
    Write-Host "Pomyslnie dodano skrot do Autostartu."
    Write-Host "Sciezka skrotu: $ShortcutPath"
} catch {
    Write-Host "WYSTAPIL BLAD:" -ForegroundColor Red
    Write-Host $_
}

Write-Host "`nGotowe. Program uruchomi sie automatycznie przy nastepnym starcie systemu." -ForegroundColor Gray
Read-Host "Nacisnij Enter, aby zamknac..."