# API bilgisayarında güvenlik duvarına DAR bir kural ekler:
#   sadece API'yi çalıştıran python.exe, sadece TCP 8001, sadece "Etki alanı" ve "Özel" ağlar.
# "Genel" ağlarda (kafe, otel Wi-Fi'si vb.) API'ye dışarıdan bağlanılamaz.
#
# Yönetici olarak açılmış PowerShell'de, api klasöründe çalıştırılır:
#   powershell -ExecutionPolicy Bypass -File .\install_firewall.ps1
#
# Tekrar çalıştırılabilir: önce kendi eski kuralını siler, sonra yeniden oluşturur.

$RuleName = "Backup API (TCP 8001)"
$Port = 8001

# 1) Yönetici mi? (Güvenlik duvarı kuralı eklemek yönetici yetkisi ister)
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Bu script yönetici olarak çalıştırılmalı (PowerShell'e sağ tık > Yönetici olarak çalıştır)." -ForegroundColor Red
    exit 1
}

# 2) API'yi çalıştıran python.exe'yi bul.
# venv\Scripts\python.exe sadece bir yönlendirici; 8001 kapısını fiilen bilgisayardaki asıl Python açıyor.
# Bu yüzden venv'deki Python'a "asıl Python'un yolu ne?" diye soruyoruz (her bilgisayarda farklı olabilir).
$VenvPython = Join-Path $PSScriptRoot "..\venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "venv bulunamadı: $VenvPython" -ForegroundColor Red
    exit 1
}
$Python = (& $VenvPython -c "import sys; print(sys._base_executable)").Trim()
if (-not (Test-Path $Python)) {
    Write-Host "Asıl python.exe bulunamadı: $Python" -ForegroundColor Red
    exit 1
}

# 3) Bu script'in daha önce eklediği kural varsa sil (aynı kural iki kez oluşmasın)
Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue

# 4) Yeni kural: gelen bağlantı, TCP 8001, sadece bu python.exe, sadece Etki alanı + Özel ağlar
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Program $Python -Profile Domain,Private | Out-Null

# 5) Özet
Write-Host "Kural eklendi: $RuleName" -ForegroundColor Green
Write-Host "  Program : $Python"
Write-Host "  Kapı    : TCP $Port (gelen bağlantılar)"
Write-Host "  Ağlar   : Etki alanı, Özel  (Genel ağlarda kapalı)"
Write-Host ""
Write-Host "Bu bilgisayarın bağlı olduğu ağlar ve türleri:"
Get-NetConnectionProfile | Format-Table InterfaceAlias, Name, NetworkCategory -AutoSize
Write-Host "Not: NetworkCategory'si 'Public' (Genel) olan ağlardan API'ye bağlanılamaz."