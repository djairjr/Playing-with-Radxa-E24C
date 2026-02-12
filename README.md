# Radxa E24C Music Server with Matrix Portal S3 VU Meter and Album Art

This project transforms a 64x64 LED matrix into a smart display that shows album art via MQTT (Base64) and a real-time VU Meter via UDP packets, all managed by a Radxa server running MPD.

## 1. The Technology Stack

* **Server:** Radxa E24C (Debian 12) - 4Gb Ram and NVME 250Gb - running MPD, Cava, and Mosquitto.
* **Display:** Adafruit Matrix Portal S3 (CircuitPython).
* **Communication:** MQTT (Metadata/Images) and UDP (Real-time audio).

---

## 2. Server Preparation (Radxa)

### Package Installation

Debian manages system Python libraries strictly. Use `apt` to ensure compatibility with Systemd:

```bash
sudo apt update
sudo apt install mpd mpc cava mosquitto mosquitto-clients imagemagick python3-paho-mqtt -y

```

### Audio and Visualization Settings

MPD needs to send audio to a "pipe" (FIFO) that Cava can read.

**`/etc/mpd.conf`**

```text
# Files and Directories
music_directory         "/home/radxa/shared_files/Music"
playlist_directory      "/home/radxa/shared_files/Music"
db_file                 "/var/lib/mpd/tag_cache"
state_file              "/var/lib/mpd/state"
sticker_file            "/var/lib/mpd/sticker.sql"

# General Options
user                    "mpd"
group                   "audio"
bind_to_address         "0.0.0.0"
port                    "6600"
auto_update             "yes"
restore_paused          "yes"

# Input (For web radio, if desired)
input {
        plugin "curl"
}

# Output 1: Local Audio (ALSA)
audio_output {
        type            "alsa"
        name            "Radxa Local Output"
        device          "hw:0,0"
        mixer_type      "software"
}

# Output 2: Visualizer (Optional, for apps showing sound waves)
audio_output {
        type            "fifo"
        name            "Visualizer"
        path            "/tmp/mpd.fifo"
        format          "44100:16:2"
}

# Encoding
filesystem_charset      "UTF-8"

```

**`~/.config/cava/config_wled`**
Configured to generate the values that our Python script will convert into UDP packets:

```ini
[general]
bars = 8
framerate = 60

[input]
method = fifo
source = /tmp/mpd.fifo

[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = 16

[smoothing]
monstercat = 1
integral = 77
gravity = 100

```

---

## 3. The "Brain" on Radxa: `visualizador.py`

This script should be placed at `/usr/local/bin/visualizador.py`. It monitors the MPD state, converts the album art to a 64x64 BMP, encodes it in Base64, and dispatches it via MQTT. Simultaneously, it reads Cava and sends 8-byte UDP packets.

```python
#!/usr/bin/python3
import subprocess
import socket
import paho.mqtt.client as mqtt
import time
import threading
import base64
import sys
import signal
import json

# --- Configurações ---
MATRIX_IP = "192.168.15.4"
UDP_PORT = 21324
MQTT_BROKER = "127.0.0.1"
MQTT_USER = "radxa"
MQTT_PASS = "4ut0l1b3r4c40"
ALBUM_TOPIC = "matrix/album_art"
METADATA_TOPIC = "current_playing"  # Tópico para outros clientes (JSON)
CAVA_CONFIG = "/home/radxa/.config/cava/config_wled"

# --- Inicialização MQTT ---
mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

def limpar_tela_e_sair(sig=None, frame=None):
    """Envia tela preta e desliga os processos antes de sair (Shutdown/Ctrl+C)"""
    try:
        # 1. Gera e envia imagem preta via MQTT
        cmd = ["convert", "-size", "64x64", "canvas:black", "-colors", "2", "-type", "Palette", "BMP3:-"]
        black_bin = subprocess.check_output(cmd)
        img_b64 = base64.b64encode(black_bin).decode('utf-8')
        info = mqtt_client.publish(ALBUM_TOPIC, img_b64, qos=1, retain=True)
        info.wait_for_publish()

        # 2. Envia sinal de silêncio via UDP para baixar as barras
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.sendto(bytes([0]*8), (MATRIX_IP, UDP_PORT))
        print("\n[INFO] Cleanup complete. Exiting...")
    except:
        pass
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        sys.exit(0)

# Registro de sinais do sistema
signal.signal(signal.SIGINT, limpar_tela_e_sair)
signal.signal(signal.SIGTERM, limpar_tela_e_sair)

try:
    mqtt_client.connect(MQTT_BROKER, 1883, 60)
    mqtt_client.loop_start()
except Exception as e:
    print(f"MQTT Error: {e}")

last_song = ""
is_playing = False

def get_black_screen():
    """Retorna um BMP 64x64 preto em Base64"""
    cmd = ["convert", "-size", "64x64", "canvas:black", "-colors", "2", "-type", "Palette", "BMP3:-"]
    black_bin = subprocess.check_output(cmd)
    return base64.b64encode(black_bin).decode('utf-8')

def check_mpc_status():
    """Verifica se o MPC está em modo 'playing'"""
    try:
        status = subprocess.check_output(["mpc", "status"], text=True)
        return "[playing]" in status
    except:
        return False

def get_album_art_64x64():
    """Extrai e redimensiona a capa do álbum do arquivo atual"""
    try:
        file_uri = subprocess.check_output(["mpc", "current", "-f", "%file%"], text=True).strip()
        if not file_uri: return None
        cmd_read = ["mpc", "readpicture", file_uri]
        cmd_resize = ["convert", "-", "-resize", "64x64^", "-gravity", "center", "-extent", "64x64",
                      "-colors", "16", "-type", "Palette", "-compress", "none", "BMP3:-"]
        p1 = subprocess.Popen(cmd_read, stdout=subprocess.PIPE)
        output_bin = subprocess.check_output(cmd_resize, stdin=p1.stdout)
        p1.wait()
        return base64.b64encode(output_bin).decode('utf-8')
    except:
        return None

def monitor_mpc():
    """Thread que monitora mudanças de música e envia metadados/capas"""
    global last_song, is_playing
    black_sent = False
    while True:
        try:
            currently_active = check_mpc_status()
            if currently_active:
                # Obtém metadados individuais
                title = subprocess.check_output(["mpc", "current", "-f", "%title%"], text=True).strip()
                artist = subprocess.check_output(["mpc", "current", "-f", "%artist%"], text=True).strip()
                album = subprocess.check_output(["mpc", "current", "-f", "%album%"], text=True).strip()
                
                # ID único para detectar mudança de faixa
                current_id = f"{artist} - {title}"

                if current_id != last_song:
                    # 1. Envia Capa para a Matrix S3
                    img_b64 = get_album_art_64x64()
                    if img_b64:
                        mqtt_client.publish(ALBUM_TOPIC, img_b64, qos=1, retain=True)
                    
                    # 2. Envia JSON apenas para outros clientes (Broadcast)
                    metadata = {
                        "music": title if title else "Unknown",
                        "artist": artist if artist else "Unknown",
                        "album": album if album else "Unknown"
                    }
                    mqtt_client.publish(METADATA_TOPIC, json.dumps(metadata), qos=0, retain=True)
                    
                    last_song = current_id
                    black_sent = False
            else:
                if not black_sent:
                    mqtt_client.publish(ALBUM_TOPIC, get_black_screen(), qos=1, retain=True)
                    mqtt_client.publish(METADATA_TOPIC, json.dumps({"status": "stopped"}), qos=0, retain=True)
                    last_song = ""
                    black_sent = True
            
            is_playing = currently_active
        except:
            pass
        time.sleep(1.5)

def stream_cava():
    """Loop principal para ler o Cava e enviar via UDP"""
    global is_playing
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cava_proc = None
    try:
        while True:
            if is_playing:
                if cava_proc is None or cava_proc.poll() is not None:
                    # stdbuf -oL força a saída linha a linha sem atraso de buffer
                    cava_cmd = ['stdbuf', '-oL', 'cava', '-p', CAVA_CONFIG]
                    cava_proc = subprocess.Popen(cava_cmd, stdout=subprocess.PIPE, text=True, bufsize=1)

                line = cava_proc.stdout.readline()
                if line:
                    valores = [v for v in line.strip().split(';') if v]
                    if len(valores) == 8:
                        # Converte para bytes brutos (8 bytes) e envia
                        packet = bytes([int(v) for v in valores])
                        udp_socket.sendto(packet, (MATRIX_IP, UDP_PORT))
            else:
                if cava_proc:
                    cava_proc.terminate()
                    cava_proc = None
                # Garante que a Matrix zere as barras no pause/stop
                udp_socket.sendto(bytes([0]*8), (MATRIX_IP, UDP_PORT))
                time.sleep(0.5)
    except:
        if cava_proc: cava_proc.terminate()

if __name__ == "__main__":
    # Inicia monitor de metadados em background
    t = threading.Thread(target=monitor_mpc, daemon=True)
    t.start()
    
    # Executa streaming de áudio no loop principal
    stream_cava()

```

---

## 4. The Display: Matrix Portal S3 (CircuitPython)

### Dependency Installation

Use the `circup` tool in your terminal to install the necessary libraries on the board:

```bash
circup install adafruit_minimqtt adafruit_imageload

```

### Proportional Gradient Logic

The big trick here is the `color_array`. To ensure the VU maintains the gradient (Green -> Red) even if you decrease the maximum height of the bars, the code calculates color bands based on the `MAX_BAR_HEIGHT` parameter.

**`code.py` highlight:**

```python
import board
import binascii
import displayio
import rgbmatrix
import framebufferio
import socketpool
import wifi
import os
import io
import time
import gc
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import adafruit_imageload

# --- VU SETTINGS ---
BAR_WIDTH = 7        # Width of each bar
BAR_COUNT = 8        # Number of bars
MAX_BAR_HEIGHT = 32  # Max height the bar reaches (e.g., 32 out of 64 pixels)
GAP = 1              # Space between bars
# ---------------------------

# --- 1. Hardware and Groups ---
displayio.release_displays()
matrix = rgbmatrix.RGBMatrix(
    width=64, height=64, bit_depth=4,
    rgb_pins=[board.MTX_R1, board.MTX_G1, board.MTX_B1,
              board.MTX_R2, board.MTX_G2, board.MTX_B2],
    addr_pins=[board.MTX_ADDRA, board.MTX_ADDRB, board.MTX_ADDRC,
               board.MTX_ADDRD, board.MTX_ADDRE],
    clock_pin=board.MTX_CLK, latch_pin=board.MTX_LAT, output_enable_pin=board.MTX_OE,
    doublebuffer=True
)
display = framebufferio.FramebufferDisplay(matrix, auto_refresh=False)

main_group = displayio.Group()
art_group = displayio.Group()
vu_group = displayio.Group()
main_group.append(art_group)
main_group.append(vu_group)
display.root_group = main_group

# --- 2. Adaptive Gradient Calculation ---
# We create the color array based on MAX_BAR_HEIGHT
color_array = [0] * 64
for y in range(64):
    # Calculate relative position within allowed max height
    # 0 is the base, MAX_BAR_HEIGHT is the peak
    pos_relativa = 63 - y 
    
    if pos_relativa < MAX_BAR_HEIGHT:
        # Split max height into proportional bands for the gradient
        if pos_relativa > MAX_BAR_HEIGHT * 0.80:    # Top (Red)
            color_array[y] = 5
        elif pos_relativa > MAX_BAR_HEIGHT * 0.65:  # Orange
            color_array[y] = 4
        elif pos_relativa > MAX_BAR_HEIGHT * 0.45:  # Yellow
            color_array[y] = 3
        elif pos_relativa > MAX_BAR_HEIGHT * 0.25:  # Light Green
            color_array[y] = 2
        else:                                       # Base (Green)
            color_array[y] = 1

vu_palette = displayio.Palette(6)
vu_palette[0] = 0x000000 
vu_palette[1], vu_palette[2] = 0x00FF00, 0xADFF2F
vu_palette[3], vu_palette[4] = 0xFFFF00, 0xFF8C00
vu_palette[5] = 0xFF0000
vu_palette.make_transparent(0)

# Dynamic multiplier: converts Cava signal (0-16) to our MAX_BAR_HEIGHT
height_multiplier = MAX_BAR_HEIGHT / 16

bars_bitmaps = []
# Center bars on screen
offset_x = (64 - (BAR_COUNT * (BAR_WIDTH + GAP))) // 2

for i in range(BAR_COUNT):
    b = displayio.Bitmap(BAR_WIDTH, 64, 6) 
    t = displayio.TileGrid(b, pixel_shader=vu_palette, x=offset_x + (i * (BAR_WIDTH + GAP)), y=0)
    bars_bitmaps.append(b)
    vu_group.append(t)

# --- 3. Network and Callbacks ---
pool = socketpool.SocketPool(wifi.radio)
udp_sock = pool.socket(pool.AF_INET, pool.SOCK_DGRAM)
udp_sock.setblocking(False)
udp_sock.bind(("", 21324))
udp_buf = bytearray(8)

def update_art(message):
    try:
        gc.collect()
        img_bin = binascii.a2b_base64(message)
        with io.BytesIO(img_bin) as stream:
            bitmap, palette = adafruit_imageload.load(stream, bitmap=displayio.Bitmap, palette=displayio.Palette)
            tile = displayio.TileGrid(bitmap, pixel_shader=palette)
            while len(art_group) > 0: art_group.pop()
            art_group.append(tile)
        display.refresh()
    except Exception as e:
        print(f"Art Error: {e}")

mqtt_client = MQTT.MQTT(
    broker=os.getenv("MQTT_BROKER"), 
    username=os.getenv("MQTT_USER"), 
    password=os.getenv("MQTT_PASS"), 
    socket_pool=pool,
    socket_timeout=0.1
)
mqtt_client.on_message = lambda c, t, m: update_art(m)
mqtt_client.connect()
mqtt_client.subscribe("matrix/album_art")

for _ in range(5):
    mqtt_client.loop(0.1)

last_mqtt_check = time.monotonic()

# --- 4. Main Loop ---
while True:
    try:
        size, addr = udp_sock.recvfrom_into(udp_buf)
        if size == 8:
            for i in range(BAR_COUNT):
                # Uses automatically calculated multiplier
                target_height = int(udp_buf[i] * height_multiplier)
                bmp = bars_bitmaps[i]
                
                for y in range(64):
                    # Draw only if within current height
                    if (63 - y) < target_height:
                        color = color_array[y]
                    else:
                        color = 0
                    
                    # Optimized painting for configured BAR_WIDTH
                    for x in range(BAR_WIDTH):
                        bmp[x, y] = color
    except: pass

    if time.monotonic() - last_mqtt_check > 0.5:
        try:
            mqtt_client.loop(0.1)
        except Exception:
            try: mqtt_client.reconnect()
            except: pass
        last_mqtt_check = time.monotonic()

    display.refresh()

```

---

## 5. Automation and Robustness

### Systemd (The service that never dies)

To ensure the system starts on Radxa boot and clears the screen on shutdown (sending a black image), we use this service file:

**`/etc/systemd/system/matrix-display.service`**

```ini
[Unit]
Description=Matrix Display MPC/Cava Visualizer
After=network.target mpd.service

[Service]
User=radxa
ExecStart=/usr/local/bin/visualizador.py
KillSignal=SIGTERM
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

```

### Log Management (Journald)

To protect your NVMe from infinite log writes, we limit `journald`:
**`/etc/systemd/journald.conf`**

```ini
[Journal]
SystemMaxUse=100M
MaxRetentionSec=2week

```

---

## 6. Final Result

The result is an extremely responsive display. Cava on Radxa processes at 60 FPS, ensuring that the bars on the S3 matrix keep pace with the music with no perceptible lag, while the album art floats elegantly in the background.

## Pro-Tip: Why Base64 over MQTT?

When building IoT projects with microcontrollers like the ESP32-S3, you might be tempted to publish raw binary data for images. However, raw binaries often contain "null" bytes or control characters that can trigger encoding errors in MQTT brokers or cause string-handling functions in C++/CircuitPython to terminate prematurely.

By encoding our BMP files into Base64 on the Radxa server, we transform the binary image into a safe, standard string of ASCII characters. This ensures:

    Data Integrity: No more corrupted images due to hidden control characters.

    Ease of Debugging: You can easily monitor the image payload using a standard MQTT client (like MQTT Explorer).

    Library Compatibility: The adafruit_minimqtt library handles string payloads with high reliability, making the transition from a Python BytesIO stream to a displayio.Bitmap seamless and crash-free.

