# Сборка Proyavka.apk без Gradle и Android Studio: javac -> d8 -> aapt -> zipalign -> apksigner.
# Подпись только v1 (JAR) — Android 4.1 на камерах Sony другую не понимает.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#   powershell -ExecutionPolicy Bypass -File build.ps1 -Config my.properties   # встроить настройки в APK (личная сборка)
#
# При первом запуске скачивает JDK 17, Android build-tools 34 и платформу android-16 (~300 МБ) в .tools рядом.
param([string]$Config = "", [string]$Tools = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not $Tools) { $Tools = if ($env:PROYAVKA_TOOLS) { $env:PROYAVKA_TOOLS } else { "$PSScriptRoot\.tools" } }

function Fetch($url, $name) {
    if (Test-Path "$Tools\$name") { return }
    Write-Host "Скачиваю $name ..."
    New-Item -ItemType Directory -Force $Tools | Out-Null
    $ProgressPreference = "SilentlyContinue"
    $zip = "$Tools\$name.zip"
    Invoke-WebRequest -UseBasicParsing $url -OutFile $zip
    $tmp = "$Tools\_unpack"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Expand-Archive $zip -DestinationPath $tmp
    Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName "$Tools\$name"
    Remove-Item -Recurse -Force $tmp, $zip
}
Fetch "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse?project=jdk" "jdk"
Fetch "https://dl.google.com/android/repository/build-tools_r34-windows.zip" "build-tools"
Fetch "https://dl.google.com/android/repository/android-16_r05.zip" "platform-16"
Fetch "https://dl.google.com/android/repository/android-2.3.3_r02.zip" "platform-10"   # проверка: код годится и для камер на Android 2.3

$env:JAVA_HOME = "$Tools\jdk"
$env:PATH = "$Tools\jdk\bin;$env:PATH"
$BT = "$Tools\build-tools"
$JAR = "$Tools\platform-16\android.jar"

Remove-Item -Recurse -Force build -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force build\cls, build\dex, build\assets\certs | Out-Null
if ($Config) {
    if (-not (Test-Path $Config)) { throw "нет файла $Config" }
    Copy-Item $Config build\assets\config.properties
}
Copy-Item certs\*.pem build\assets\certs\

$src = Get-ChildItem -Recurse app\src -Filter *.java | ForEach-Object FullName
& javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -bootclasspath $JAR -d build\cls @src
if ($LASTEXITCODE) { throw "javac" }
# a5000, a6000, RX100 III… — Android 2.3 (API 10): всё, чего там нет, должно быть только за проверкой SDK_INT
New-Item -ItemType Directory -Force build\check10 | Out-Null
& javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -bootclasspath "$Tools\platform-10\android.jar" -d build\check10 @src
if ($LASTEXITCODE) { throw "javac: код не собирается для Android 2.3 (API 10)" }
& "$BT\d8.bat" --release --min-api 10 --lib $JAR --output build\dex (Get-ChildItem -Recurse build\cls -Filter *.class | ForEach-Object FullName)
if ($LASTEXITCODE) { throw "d8" }

& "$BT\aapt.exe" package -f -M app\AndroidManifest.xml -S app\res -I $JAR -A build\assets -F build\unsigned.apk
if ($LASTEXITCODE) { throw "aapt package" }
Push-Location build\dex
& "$BT\aapt.exe" add ..\unsigned.apk classes.dex | Out-Null
Pop-Location
& "$BT\zipalign.exe" -f 4 build\unsigned.apk build\aligned.apk
if ($LASTEXITCODE) { throw "zipalign" }

# ключ подписи один на все версии, иначе обновление поверх установленного не встанет. Храни его, в git он не попадает.
if (-not (Test-Path proyavka.keystore)) {
    & keytool -genkeypair -keystore proyavka.keystore -storepass proyavka -keypass proyavka -alias proyavka `
        -keyalg RSA -keysize 2048 -validity 20000 -dname "CN=Proyavka" | Out-Null
}
& "$BT\apksigner.bat" sign --ks proyavka.keystore --ks-pass pass:proyavka --ks-key-alias proyavka `
    --min-sdk-version 16 --v1-signing-enabled true --v2-signing-enabled false --v3-signing-enabled false `
    --out Proyavka.apk build\aligned.apk
if ($LASTEXITCODE) { throw "apksigner" }
& "$BT\apksigner.bat" verify --min-sdk-version 16 Proyavka.apk
Get-Item Proyavka.apk | Select-Object Name, Length
