"""One-line joiners for every kind of machine, and the seed kit that carries them.

A device joins the swarm when its OWNER runs one command on it. That command
is the consent (AGENTS.md: the system never copies itself onto a machine it
found). What the command does, per platform:

- says up front, in plain words, everything it is about to install — the
  run IS the consent, so the list is printed before anything happens;
- finds Python >= 3.9, installing it where that needs no password (winget
  per-user on Windows, pkg on Termux); elsewhere it prints the one command;
- installs llama.cpp (unless SWARM_LLAMA=0) so this device's GPU or RAM can
  hold part of a model: winget on Windows, Homebrew on a Mac that has it,
  the official prebuilt release (Vulkan build where Vulkan exists) into
  ``~/.swarm/llama`` on Linux and Android;
- puts the agent file in ``~/.swarm/`` — downloaded from the hub, or copied
  from the folder the script sits in when it runs off a USB stick / seed;
- registers autostart the platform's own way, so a reboot or a phone
  restart does not drop the node:
    Windows  HKCU\\...\\Run + a hidden VBS shim (Task Scheduler mangles
             quoted arguments on Win11 — that bug cost this fleet a day once)
    Linux    a systemd --user unit (crontab @reboot where systemd is absent)
    macOS    a LaunchAgent
    Android  Termux:Boot script + termux-wake-lock
- starts the agent now, in the background, logging to ``~/.swarm/agent.log``,
  with self-update on: after this one run the device never needs touching.

Uninstall is printed at the end, every time.

These templates live agent-side (stdlib only) so a *seed* — any machine
holding the kit, even one too weak or broken to compute — can serve them to
new devices on its network without the hub (``daemon --seed``).
"""

from __future__ import annotations

from typing import Optional

POSIX_TEMPLATE = r'''#!/bin/sh
# Swarm joiner (Linux / macOS / Android-Termux / Raspberry Pi).
# Running this is your consent for THIS device to join the swarm at:
#   __HUB__
# Options (env vars): DEDICATED=1 keep working while the device is in use
#                     AUTOSTART=0 do not start on boot
#                     SWARM_LLAMA=0 do not install llama.cpp
#                     CODE_WORKER=1 accept code an AI wrote (default on Android: app-sandboxed)
set -e
HUB="__HUB__"
TOKEN="__TOKEN__"
SEED="__SEED__"
LLAMA_TAG="__LLAMA_TAG__"
DEDICATED="${DEDICATED:-__DEDICATED__}"
AUTOSTART="${AUTOSTART:-1}"
SWARM_LLAMA="${SWARM_LLAMA:-1}"
CODE_WORKER="${CODE_WORKER:-}"
DIR="$HOME/.swarm"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo .)"
mkdir -p "$DIR"

IS_TERMUX=0
if [ -n "$TERMUX_VERSION" ] || [ -d /data/data/com.termux/files/usr ]; then IS_TERMUX=1; fi

if [ -z "$CODE_WORKER" ]; then CODE_WORKER="$IS_TERMUX"; fi
echo "== Joining this device to the swarm at $HUB =="
echo "This will:"
echo "  - put the swarm agent (one ~200 KB file) in $DIR"
[ "$IS_TERMUX" = 1 ] && echo "  - install Python with pkg if it is missing"
[ "$SWARM_LLAMA" = 1 ] && echo "  - install llama.cpp so this device can hold part of an AI model (SWARM_LLAMA=0 to skip)"
[ "$AUTOSTART" = 1 ] && echo "  - start it on boot, and keep it updated from the hub"
[ "$CODE_WORKER" = 1 ] && echo "  - accept code written by your AI tools (sandboxed by Android on phones; CODE_WORKER=0 to refuse)"
echo "It runs in userspace only, backs off when you use the device, runs hot, or the battery is low."
echo ""

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ] && [ "$IS_TERMUX" = 1 ]; then
  echo "installing Python (pkg)..."
  pkg install -y python >/dev/null 2>&1 || pkg install -y python
  PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PY" ] && command -v apt-get >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  echo "installing Python (apt)..."
  sudo -n apt-get install -y python3 >/dev/null 2>&1 || true
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "This device needs Python 3.9 or newer first. Install it with:"
  if [ "$IS_TERMUX" = 1 ]; then echo "  pkg install -y python"
  elif command -v apt-get >/dev/null 2>&1; then echo "  sudo apt-get install -y python3"
  elif command -v dnf >/dev/null 2>&1; then echo "  sudo dnf install -y python3"
  elif command -v pacman >/dev/null 2>&1; then echo "  sudo pacman -S python"
  elif command -v apk >/dev/null 2>&1; then echo "  sudo apk add python3"
  elif [ "$(uname)" = "Darwin" ]; then echo "  xcode-select --install     (or: brew install python)"
  else echo "  your package manager's python3 package"
  fi
  echo "then run this joiner again."
  exit 1
fi

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -q "$1" -O "$2"
  else "$PY" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
  fi
}

AGENT="$DIR/swarm-agent.pyz"
if [ -f "$HERE/swarm-agent.pyz" ] && [ "$HERE" != "$DIR" ]; then
  cp "$HERE/swarm-agent.pyz" "$AGENT"
  echo "agent copied from $HERE"
elif [ -n "$SEED" ]; then
  fetch "$SEED/swarm-agent.pyz" "$AGENT"
  echo "agent fetched from the seed at $SEED"
else
  fetch "$HUB/bundle.pyz?token=$TOKEN" "$AGENT"
  echo "agent fetched from the hub"
fi

# llama.cpp: the engine that lets this device hold part of a model.
if [ "$SWARM_LLAMA" = 1 ]; then
  # Every node runs the SAME llama.cpp build (the hub pins it): the RPC
  # protocol changes between builds and a mismatched helper is dropped.
  if [ -n "$LLAMA_TAG" ] && [ -d "$DIR/llama/llama-$LLAMA_TAG" ]; then
    echo "llama.cpp: fleet build $LLAMA_TAG already installed"
  else
    "$PY" - "$DIR/llama" "$IS_TERMUX" "$LLAMA_TAG" <<'PYEOF' || echo "llama.cpp: install failed; the agent still works without it"
import json, os, platform, sys, tarfile, urllib.request, zipfile
dest, termux = sys.argv[1], sys.argv[2] == "1"
tag = sys.argv[3] if len(sys.argv) > 3 else ""
machine = platform.machine().lower()
system = platform.system()
def has_vulkan():
    # A Vulkan loader alone is not a GPU: headless servers ship mesa's
    # llvmpipe, a software rasterizer far slower than the CPU build. Require
    # a real render node too.
    import glob
    if not glob.glob("/dev/dri/renderD*"):
        return False
    for d in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu"):
        if os.path.exists(os.path.join(d, "libvulkan.so.1")):
            return True
    return False
arm = machine in ("aarch64", "arm64")
if termux or (system == "Linux" and "android" in platform.platform().lower()):
    if not arm:
        sys.exit("no prebuilt llama.cpp for Android on " + machine + "; skipping (the agent still works)")
    wants = ["bin-android-arm64.tar.gz"]
elif system == "Darwin":
    wants = ["bin-macos-arm64.tar.gz"] if arm else ["bin-macos-x64.tar.gz"]
elif system == "Linux":
    base = "ubuntu-arm64" if arm else "ubuntu-x64"
    vk = "ubuntu-vulkan-arm64" if arm else "ubuntu-vulkan-x64"
    wants = ["bin-%s.tar.gz" % vk, "bin-%s.tar.gz" % base] if has_vulkan() else ["bin-%s.tar.gz" % base]
else:
    sys.exit("no prebuilt llama.cpp for %s/%s" % (system, machine))
api = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
url = api + ("/tags/" + tag if tag else "?per_page=8")
req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "swarm-joiner"})
releases = json.load(urllib.request.urlopen(req, timeout=30))
if isinstance(releases, dict):
    releases = [releases]
asset = None
for want in wants:
    for rel in releases:
        for a in rel.get("assets", []):
            if a["name"].startswith("llama-") and a["name"].endswith(want):
                asset = a
                break
        if asset: break
    if asset: break
if not asset:
    sys.exit("no matching llama.cpp release asset")
os.makedirs(dest, exist_ok=True)
path = os.path.join(dest, asset["name"])
print("llama.cpp: downloading %s (%.0f MB)..." % (asset["name"], asset["size"] / 1e6))
urllib.request.urlretrieve(asset["browser_download_url"], path)
target = os.path.join(dest, "llama-" + (tag or asset["name"].split("-bin-")[0].split("llama-", 1)[1]))
os.makedirs(target, exist_ok=True)
if path.endswith(".zip"):
    zipfile.ZipFile(path).extractall(target)
else:
    with tarfile.open(path) as tf:
        try:
            tf.extractall(target, filter="data")  # refuses absolute paths, .., devices
        except TypeError:  # Python < 3.12 has no filter argument
            tf.extractall(target)
os.remove(path)
entries = os.listdir(target)
if len(entries) == 1 and os.path.isdir(os.path.join(target, entries[0])):
    inner = os.path.join(target, entries[0])  # tarballs wrap everything in one dir
    for e in os.listdir(inner):
        os.replace(os.path.join(inner, e), os.path.join(target, e))
    os.rmdir(inner)
for root, _, files in os.walk(dest):
    for f in files:
        if f in ("rpc-server", "llama-server", "ggml-rpc-server") or f.endswith(".so") or ".so." in f:
            fp = os.path.join(root, f)
            os.chmod(fp, os.stat(fp).st_mode | 0o755)
# a prebuilt that cannot start here (musl vs glibc, missing libraries) is
# worse than none: remove it and say so
import shutil, subprocess
server = os.path.join(target, "llama-server")
try:
    ok = subprocess.run([server, "--version"], capture_output=True, timeout=60).returncode == 0
except Exception:
    ok = False
if not ok:
    shutil.rmtree(target, ignore_errors=True)
    sys.exit("llama.cpp: the prebuilt build does not run on this system; skipped (the agent still works)")
print("llama.cpp: installed into " + target)
PYEOF
  fi
fi

ARGS="--work --self-update --log $DIR/agent.log"
if [ "$DEDICATED" = 1 ]; then ARGS="$ARGS --dedicated"; fi
if [ "$IS_TERMUX" = 1 ] && [ "$DEDICATED" != 0 ]; then ARGS="--work --self-update --dedicated --log $DIR/agent.log"; fi
if [ "$CODE_WORKER" = 1 ]; then ARGS="$ARGS --code-worker"; fi

# stop an earlier copy so the new one owns the node
pkill -f "[s]warm-agent.pyz" 2>/dev/null || true

STARTED=0
if [ "$AUTOSTART" = 1 ]; then
  if [ "$IS_TERMUX" = 1 ]; then
    mkdir -p "$HOME/.termux/boot"
    cat > "$HOME/.termux/boot/compute-node" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock 2>/dev/null
# watchdog: if the agent ever dies, it is back in 10 s (its lock prevents doubles)
nohup sh -c 'while true; do "$PY" "$AGENT" $ARGS; sleep 10; done' >/dev/null 2>&1 &
EOF
    chmod +x "$HOME/.termux/boot/compute-node"
    echo "autostart: Termux:Boot script installed (install the Termux:Boot app once, open it once)"
    UNINSTALL="rm -f ~/.termux/boot/compute-node; pkill -f '[s]warm-agent.pyz'; rm -rf ~/.swarm/swarm-agent.pyz ~/.swarm/node_keys.json ~/.swarm/agent.log* ~/.swarm/agent*.lock ~/.swarm/node_id ~/.swarm/holo-*.json ~/.swarm/replica ~/.swarm/hub-* ~/.swarm/hub-promoted.log ~/.swarm/llama ~/.swarm/logs"
  elif [ "$(uname)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/local.compute-node.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.compute-node</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$AGENT</string><string>--work</string><string>--self-update</string><string>--log</string><string>$DIR/agent.log</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>Nice</key><integer>10</integer>
</dict></plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST" && STARTED=1
    echo "autostart: LaunchAgent installed"
    UNINSTALL="launchctl unload $PLIST; rm -f $PLIST; rm -rf ~/.swarm/swarm-agent.pyz ~/.swarm/node_keys.json ~/.swarm/agent.log* ~/.swarm/agent*.lock ~/.swarm/node_id ~/.swarm/holo-*.json ~/.swarm/replica ~/.swarm/hub-* ~/.swarm/hub-promoted.log ~/.swarm/llama ~/.swarm/logs"
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/compute-node.service" <<EOF
[Unit]
Description=Swarm agent (compute node)
After=network-online.target

[Service]
ExecStart=$PY $AGENT $ARGS
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now compute-node.service && STARTED=1
    echo "autostart: systemd --user unit installed"
    echo "  (to keep it running while you are logged out: sudo loginctl enable-linger $USER)"
    UNINSTALL="systemctl --user disable --now compute-node; rm -f $UNIT_DIR/compute-node.service; rm -rf ~/.swarm/swarm-agent.pyz ~/.swarm/node_keys.json ~/.swarm/agent.log* ~/.swarm/agent*.lock ~/.swarm/node_id ~/.swarm/holo-*.json ~/.swarm/replica ~/.swarm/hub-* ~/.swarm/hub-promoted.log ~/.swarm/llama ~/.swarm/logs"
  elif command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v swarm-agent.pyz; echo "@reboot sh -c 'while true; do $PY $AGENT $ARGS; sleep 10; done'" ) | crontab -
    echo "autostart: crontab @reboot entry installed"
    UNINSTALL="crontab -l | grep -v swarm-agent.pyz | crontab -; pkill -f '[s]warm-agent.pyz'; rm -rf ~/.swarm/swarm-agent.pyz ~/.swarm/node_keys.json ~/.swarm/agent.log* ~/.swarm/agent*.lock ~/.swarm/node_id ~/.swarm/holo-*.json ~/.swarm/replica ~/.swarm/hub-* ~/.swarm/hub-promoted.log ~/.swarm/llama ~/.swarm/logs"
  fi
fi
# earlier installs used louder names; retire them
systemctl --user disable --now swarm-agent 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/swarm-agent.service" "$HOME/.termux/boot/swarm-agent" 2>/dev/null || true
[ -z "$UNINSTALL" ] && UNINSTALL="pkill -f '[s]warm-agent.pyz'; rm -rf ~/.swarm/swarm-agent.pyz ~/.swarm/node_keys.json ~/.swarm/agent.log* ~/.swarm/agent*.lock ~/.swarm/node_id ~/.swarm/holo-*.json ~/.swarm/replica ~/.swarm/hub-* ~/.swarm/hub-promoted.log ~/.swarm/llama ~/.swarm/logs"

if [ "$STARTED" != 1 ]; then
  if [ "$IS_TERMUX" = 1 ]; then termux-wake-lock 2>/dev/null || true; fi
  # watchdog loop: a crash costs 10 s, not the node (the agent's lock prevents doubles)
  nohup sh -c "while true; do \"$PY\" \"$AGENT\" $ARGS; sleep 10; done" >/dev/null 2>&1 &
fi
printf '%s\n' "$UNINSTALL" > "$DIR/leave.txt"; chmod 600 "$DIR/leave.txt" 2>/dev/null
echo ""
echo "joined: this device is now a swarm node (log: $DIR/agent.log)"
echo "it stays quiet. To look at it later: python3 $AGENT status   (asks for your access code)"
echo "to leave the swarm: $UNINSTALL"
'''

POWERSHELL_TEMPLATE = r'''# Swarm joiner (Windows). Running this is your consent for THIS PC to join
# the swarm at __HUB__
# Options: $env:SWARM_DEDICATED=1 keep working while you use the PC
#          $env:SWARM_AUTOSTART=0 do not start at logon
#          $env:SWARM_LLAMA=0 do not install llama.cpp
#          $env:SWARM_CODE_WORKER=1 accept code written by your AI tools
$ErrorActionPreference = 'Stop'
$Hub = '__HUB__'
$Token = '__TOKEN__'
$Seed = '__SEED__'
$LlamaTag = '__LLAMA_TAG__'
$Dedicated = if ($env:SWARM_DEDICATED) { $env:SWARM_DEDICATED } else { '__DEDICATED__' }
$Autostart = if ($env:SWARM_AUTOSTART) { $env:SWARM_AUTOSTART } else { '1' }
$Llama = if ($env:SWARM_LLAMA) { $env:SWARM_LLAMA } else { '1' }
$HasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
Write-Host "== Joining this PC to the swarm at $Hub =="
Write-Host 'This will:'
Write-Host "  - put the swarm agent (one ~200 KB file) in $env:USERPROFILE\.swarm"
if ($HasWinget) { Write-Host '  - install Python for your user with winget if it is missing (accepting its license)' }
if ($Llama -eq '1') { Write-Host "  - install llama.cpp $LlamaTag (official release) so this PC's GPU can hold part of an AI model" }
if ($Autostart -ne '0') { Write-Host '  - start it at logon, and keep it updated from the hub' }
Write-Host 'It runs in userspace only and backs off while you use the PC (unless dedicated).'
Write-Host ''
$Dir = Join-Path $env:USERPROFILE '.swarm'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
# low profile: the folder is hidden like other app config folders
try { (Get-Item $Dir -Force).Attributes = (Get-Item $Dir -Force).Attributes -bor [IO.FileAttributes]::Hidden } catch { }
$Here = if ($PSScriptRoot) { $PSScriptRoot } else { '' }

$py = $null
foreach ($c in @('py', 'python', 'python3')) {
    try {
        $exe = (& $c -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $exe) { $py = "$exe".Trim(); break }
    } catch { }
}
if (-not $py -and $HasWinget) {
    Write-Host 'installing Python (winget, per-user)...'
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $cand = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path $cand) { $py = $cand }
}
if (-not $py) {
    Write-Host 'This PC needs Python 3.9 or newer first. Install it with:'
    Write-Host '  winget install -e --id Python.Python.3.12'
    Write-Host 'then run this joiner again.'
    return  # not `exit`: under `irm | iex` that would close your terminal
}
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }

$Agent = Join-Path $Dir 'swarm-agent.pyz'
if ($Here -and (Test-Path (Join-Path $Here 'swarm-agent.pyz')) -and ($Here -ne $Dir)) {
    Copy-Item (Join-Path $Here 'swarm-agent.pyz') $Agent -Force
    Write-Host "agent copied from $Here"
} elseif ($Seed) {
    Invoke-WebRequest -UseBasicParsing "$Seed/swarm-agent.pyz" -OutFile $Agent
    Write-Host "agent fetched from the seed at $Seed"
} else {
    Invoke-WebRequest -UseBasicParsing "$Hub/bundle.pyz?token=$Token" -OutFile $Agent
    Write-Host 'agent fetched from the hub'
}

if ($Llama -eq '1') {
    # Same pinned build as every other node: llama.cpp's RPC protocol changes
    # between builds, and winget's package lags the releases.
    try {
        $LlamaDir = Join-Path $Dir 'llama'
        $api = 'https://api.github.com/repos/ggml-org/llama.cpp/releases'
        $rel = if ($LlamaTag) { Invoke-RestMethod "$api/tags/$LlamaTag" -Headers @{ 'User-Agent' = 'swarm-joiner' } }
               else { (Invoke-RestMethod "$api`?per_page=8" -Headers @{ 'User-Agent' = 'swarm-joiner' }) | Where-Object { $_.assets.Count -gt 0 } | Select-Object -First 1 }
        $tag = $rel.tag_name
        $target = Join-Path $LlamaDir "llama-$tag"
        if (Test-Path (Join-Path $target 'llama-server.exe')) {
            Write-Host "llama.cpp: fleet build $tag already installed"
        } else {
            $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'bin-win-cpu-arm64.zip' } else { 'bin-win-vulkan-x64.zip' }
            $asset = $rel.assets | Where-Object { $_.name -like "llama-*-$arch" } | Select-Object -First 1
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            $zip = Join-Path $LlamaDir $asset.name
            Write-Host ("llama.cpp: downloading {0} ({1:N0} MB)..." -f $asset.name, ($asset.size / 1MB))
            Invoke-WebRequest -UseBasicParsing $asset.browser_download_url -OutFile $zip
            Expand-Archive -Path $zip -DestinationPath $target -Force
            Remove-Item $zip
            Write-Host "llama.cpp: installed into $target"
        }
    } catch {
        Write-Host "llama.cpp: install failed ($_); the agent still works without it"
    }
}

$Log = Join-Path $Dir 'agent.log'
$AgentArgs = "--work --self-update --log ""$Log"""
if ($Dedicated -eq '1') { $AgentArgs = "$AgentArgs --dedicated" }
if ($env:SWARM_CODE_WORKER -eq '1') { $AgentArgs = "$AgentArgs --code-worker" }

# stop an earlier copy (and its watchdog) so the new one owns the node
Get-CimInstance Win32_Process -Filter "Name like 'wscript%' or Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*swarm-agent.*' -or $_.CommandLine -like '*compute-node.vbs*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# A VBS shim runs it with no window; HKCU\Run starts it at logon. (Task
# Scheduler mangles nested quotes on Win11, so it is deliberately not used.)
$Vbs = Join-Path $Dir 'compute-node.vbs'
$cmd = """$pyw"" ""$Agent"" $AgentArgs"
# Watchdog: run hidden, wait for exit, restart after 10 s. The agent holds a
# single-instance lock, so a restart racing a self-update exits harmlessly.
$vbsLines = @(
    'Set sh = CreateObject("WScript.Shell")',
    'Do',
    ('    sh.Run "' + $cmd.Replace('"', '""') + '", 0, True'),  # parens: in @(), ',' binds tighter than '+'
    '    WScript.Sleep 10000',
    'Loop'
)
Set-Content -Path $Vbs -Value $vbsLines -Encoding ASCII
if ($Autostart -ne '0') {
    New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ComputeNode' `
        -Value "wscript.exe ""$Vbs""" -PropertyType String -Force | Out-Null
    Write-Host 'autostart: starts at logon (HKCU Run)'
}
# earlier installs used a louder name; retire it
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'SwarmAgent' -ErrorAction SilentlyContinue
Start-Process -FilePath 'wscript.exe' -ArgumentList """$Vbs"""
Write-Host ''
Write-Host "joined: this PC is now a swarm node (log: $Log)"
Write-Host "it stays quiet. To look at it later: python ""$Agent"" status   (asks for your access code)"
Write-Host "to leave the swarm: Remove-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run ComputeNode; Get-CimInstance Win32_Process | ? { `$_.CommandLine -like '*swarm-agent.pyz*' -or `$_.CommandLine -like '*compute-node.vbs*' } | % { Stop-Process -Id `$_.ProcessId }; Remove-Item -Recurse -Force `"$Dir\swarm-agent.*`", `"$Dir\node_keys.json`", `"$Dir\agent.log*`", `"$Dir\agent*.lock`", `"$Dir\node_id`", `"$Dir\holo-*.json`", `"$Dir\replica`", `"$Dir\hub-*`", `"$Dir\llama`", `"$Dir\logs`" -ErrorAction SilentlyContinue"
Set-Content -Path (Join-Path $Dir 'leave.txt') -Value "Remove-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run ComputeNode; Get-CimInstance Win32_Process | ? { `$_.CommandLine -like '*swarm-agent.pyz*' -or `$_.CommandLine -like '*compute-node.vbs*' } | % { Stop-Process -Id `$_.ProcessId }; Remove-Item -Recurse -Force `"$Dir\swarm-agent.*`", `"$Dir\node_keys.json`", `"$Dir\agent.log*`", `"$Dir\agent*.lock`", `"$Dir\node_id`", `"$Dir\holo-*.json`", `"$Dir\replica`", `"$Dir\hub-*`", `"$Dir\llama`", `"$Dir\logs`" -ErrorAction SilentlyContinue" -Encoding UTF8
'''

WINDOWS_CMD_TEMPLATE = r'''@echo off
rem Double-click to join this PC to the swarm. Consent = you running this.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0join-windows.ps1"
pause
'''

MAC_COMMAND_TEMPLATE = r'''#!/bin/sh
# Double-click (macOS) to join this Mac to the swarm.
cd "$(dirname "$0")" && sh ./join-unix.sh
echo "press return to close"; read _
'''

README_TEMPLATE = r'''SWARM SEED KIT
==============

This folder joins a machine to the swarm at:
  __HUB__

Nothing here runs by itself. A machine joins only when its owner runs one
of these on it:

  Windows          double-click  JOIN-WINDOWS.cmd
  macOS            double-click  JOIN-MAC.command
  Linux / Pi       sh join-unix.sh
  Android (Termux) sh join-unix.sh      (install Termux + `pkg install python` first)

Options: set DEDICATED=1 (or SWARM_DEDICATED=1 on Windows) for machines that
exist to compute - old phones on chargers, GPU boxes - so they keep working
while touched. Battery protection always applies.

Carry it: copy this folder to a USB stick, an SD card, or an old phone. On an
old phone with Termux you can also SERVE it to every device on the same
Wi-Fi, no hub access needed for the download:

  python swarm-agent.pyz --seed

...then on the new device open  http://<phone-ip>:8788  and follow the page.

The agent needs Python 3.9+ and nothing else. It measures the machine,
never claims what it did not measure, backs off when you are using the
device or the battery is low, and runs only in userspace.

Invite token (expires __EXPIRES__): __TOKEN_SHORT__...
Leaving: every joiner prints its own uninstall line (it removes only agent files; a hub sharing ~/.swarm is untouched).
'''


def _fill(template: str, hub: str, token: str, dedicated: bool, seed: Optional[str], llama_tag: str = "") -> str:
    return (
        template.replace("__HUB__", hub.rstrip("/"))
        .replace("__TOKEN__", token)
        .replace("__SEED__", (seed or "").rstrip("/"))
        .replace("__DEDICATED__", "1" if dedicated else "0")
        .replace("__LLAMA_TAG__", llama_tag or "")
    )


def render_posix(
    hub: str, token: str, dedicated: bool = False, seed: Optional[str] = None, llama_tag: str = ""
) -> str:
    return _fill(POSIX_TEMPLATE, hub, token, dedicated, seed, llama_tag)


def render_powershell(
    hub: str, token: str, dedicated: bool = False, seed: Optional[str] = None, llama_tag: str = ""
) -> str:
    return _fill(POWERSHELL_TEMPLATE, hub, token, dedicated, seed, llama_tag)


def render_readme(hub: str, token: str, expires: str) -> str:
    return (
        README_TEMPLATE.replace("__HUB__", hub)
        .replace("__EXPIRES__", expires)
        .replace("__TOKEN_SHORT__", token[:12])
    )


def build_seed_kit(
    hub: str, token: str, agent_pyz: bytes, expires: str, dedicated: bool = False, llama_tag: str = ""
) -> bytes:
    """A zip that turns any storage (USB stick, SD card, old phone) into a
    seed. Stdlib zipfile; the .sh/.command files get the executable bit."""
    import io
    import zipfile

    files = {
        "swarm-seed/swarm-agent.pyz": agent_pyz,
        "swarm-seed/join-unix.sh": render_posix(hub, token, dedicated, llama_tag=llama_tag).encode("utf-8"),
        "swarm-seed/join-windows.ps1": render_powershell(hub, token, dedicated, llama_tag=llama_tag).encode("utf-8"),
        "swarm-seed/JOIN-WINDOWS.cmd": WINDOWS_CMD_TEMPLATE.replace("\n", "\r\n").encode("utf-8"),
        "swarm-seed/JOIN-MAC.command": MAC_COMMAND_TEMPLATE.encode("utf-8"),
        "swarm-seed/README.txt": render_readme(hub, token, expires).encode("utf-8"),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if name.endswith((".sh", ".command", ".pyz")) else 0o644
            info.external_attr = (0o100000 | mode) << 16
            zf.writestr(info, data)
    return buf.getvalue()
