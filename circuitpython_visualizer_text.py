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
import terminalio
import adafruit_minimqtt.adafruit_minimqtt as MQTT
from adafruit_display_text import label
from adafruit_bitmap_font import bitmap_font

# --- VU SETTINGS ---
BAR_WIDTH = 7
BAR_COUNT = 8
MAX_BAR_HEIGHT = 32
GAP = 1
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
vu_group = displayio.Group()
text_group = displayio.Group()

main_group.append(vu_group)
main_group.append(text_group)
display.root_group = main_group

# --- 2. Adaptive Gradient Calculation ---
color_array = [0] * 64
for y in range(64):
    pos_relativa = 63 - y
    if pos_relativa < MAX_BAR_HEIGHT:
        if pos_relativa > MAX_BAR_HEIGHT * 0.80:    color_array[y] = 5
        elif pos_relativa > MAX_BAR_HEIGHT * 0.65:  color_array[y] = 4
        elif pos_relativa > MAX_BAR_HEIGHT * 0.45:  color_array[y] = 3
        elif pos_relativa > MAX_BAR_HEIGHT * 0.25:  color_array[y] = 2
        else:                                       color_array[y] = 1

vu_palette = displayio.Palette(6)
vu_palette[0] = 0x000000 
vu_palette[1], vu_palette[2] = 0x00FF00, 0xADFF2F
vu_palette[3], vu_palette[4] = 0xFFFF00, 0xFF8C00
vu_palette[5] = 0xFF0000
vu_palette.make_transparent(0)

# --- 3. Font and Labels Setup ---
try:
    custom_font = bitmap_font.load_font("/fonts/tom-thumb.pcf")
    CHAR_W = 4 # Largura média da Tom-Thumb
except:
    custom_font = terminalio.FONT
    CHAR_W = 6 # Largura média da Terminalio

# 4 Labels Fixos
m_lab1 = label.Label(custom_font, text="", color=0xFF0000, y=6)
m_lab2 = label.Label(custom_font, text="", color=0xFF0000, y=12)
a_lab1 = label.Label(custom_font, text="", color=0x00FF00, y=19)
a_lab2 = label.Label(custom_font, text="", color=0x00FF00, y=25)

text_group.append(m_lab1)
text_group.append(m_lab2)
text_group.append(a_lab1)
text_group.append(a_lab2)

def update_wrapped_text(music, artist):
    # 1. Limpa labels com um espaço para evitar erros de render
    m_lab1.text = m_lab2.text = a_lab1.text = a_lab2.text = " "
    max_chars = 15

    # 2. Quebra e Centralização Música
    if len(music) > max_chars:
        split = music.rfind(' ', 0, max_chars)
        if split == -1: split = max_chars
        m_lab1.text = music[:split].strip()
        m_lab2.text = music[split:].strip()[:max_chars]
    else:
        m_lab1.text = music

    # 3. Quebra e Centralização Artista
    if len(artist) > max_chars:
        split = artist.rfind(' ', 0, max_chars)
        if split == -1: split = max_chars
        a_lab1.text = artist[:split].strip()
        a_lab2.text = artist[split:].strip()[:max_chars]
    else:
        a_lab1.text = artist

    # 4. Centralização Matemática Manual
    # (Metade da Tela 32) - (Metade da largura do texto)
    for l in [m_lab1, m_lab2, a_lab1, a_lab2]:
        if len(l.text) > 0:
            l.x = max(0, (64 - (len(l.text) * CHAR_W)) // 2)

# --- 4. VU Meter and Network ---
height_multiplier = MAX_BAR_HEIGHT / 16
bars_bitmaps = []
offset_x = (64 - (BAR_COUNT * (BAR_WIDTH + GAP))) // 2

for i in range(BAR_COUNT):
    b = displayio.Bitmap(BAR_WIDTH, 64, 6)
    t = displayio.TileGrid(b, pixel_shader=vu_palette, x=offset_x + (i * (BAR_WIDTH + GAP)), y=0)
    bars_bitmaps.append(b)
    vu_group.append(t)

pool = socketpool.SocketPool(wifi.radio)
udp_sock = pool.socket(pool.AF_INET, pool.SOCK_DGRAM)
udp_sock.setblocking(False)
udp_sock.bind(("", 21324))
udp_buf = bytearray(8)

import json
def on_mqtt_message(client, topic, message):
    if topic == "current_playing":
        try:
            data = json.loads(message)
            if "music" in data:
                update_wrapped_text(data['music'], data['artist'])
            elif data.get("status") == "stopped":
                m_lab1.text = m_lab2.text = a_lab1.text = a_lab2.text = ""
        except: pass

mqtt_client = MQTT.MQTT(
    broker=os.getenv("MQTT_BROKER"), 
    username=os.getenv("MQTT_USER"), 
    password=os.getenv("MQTT_PASS"), 
    socket_pool=pool,
    socket_timeout=0.1
)
mqtt_client.on_message = on_mqtt_message
mqtt_client.connect()
mqtt_client.subscribe("current_playing")

last_mqtt_check = time.monotonic()

# --- 5. Main Loop ---
while True:
    try:
        size, addr = udp_sock.recvfrom_into(udp_buf)
        if size == 8:
            for i in range(BAR_COUNT):
                target_height = int(udp_buf[i] * height_multiplier)
                bmp = bars_bitmaps[i]
                for y in range(64):
                    color = color_array[y] if (63 - y) < target_height else 0
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
