
# 關閉所有 Hermes 進程
Get-Process | Where-Object {$_.ProcessName -like '*Hermes*'} | Stop-Process -Force

# 等待 3 秒
Start-Sleep -Seconds 3

# 以管理員身份重新啟動
$desktopExe = "C:\Users\PC\AppData\Local\hermes\hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe"
Start-Process -FilePath $desktopExe -Verb RunAs
