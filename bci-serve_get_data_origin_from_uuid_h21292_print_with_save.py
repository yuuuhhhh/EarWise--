import json
import csv
import atexit
import tornado.web
import tornado.platform.asyncio
import asyncio
import time
import socket
import uuid
from datetime import datetime
from pathlib import Path
from bleak import BleakScanner, BleakClient
import asyncio, uuid, threading, time
import numpy as np
from bleak import BleakScanner, BleakClient
import tkinter as tk
import threading
from collections import deque

ADS_GAIN = 12.0
EEG_SCALE = 0.288486 / ADS_GAIN
SAMPLE_RATE = 250
WINDOW_SEC = 10
DISPLAY_SEC = 5
SMOOTHING_WINDOW = 25
MAX_METRICS_SHOW = 1000
OPENBCI_START_BYTE = 0xA0
OPENBCI_STOP_NIBBLE = 0xC0
OPENBCI_PACKET_SIZE = 33
# Configuration
channel_num = 8
udp_target_ip = "127.0.0.1"
udp_target_port = 12345 # Default UDP port
udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ble_connected = False
ble_client = None
# Global list to store data as requested
collected_data = []
# Buffer for PSD calculation (store voltage values)
eeg_buffer = deque(maxlen=250)

udp_batch_buffer = bytearray()
BATCH_SIZE = 10 # Batch 10 packets (~40ms latency at 250Hz)
packets_sent_counter = 0

rx_ble_buffer = bytearray()

BLE_SEND_POKETS_LEN = 9
ADS_DATA_LEN = 6
IMU_DATA_LEN = 18
BLE_SEND_LEN = 5 + ADS_DATA_LEN * BLE_SEND_POKETS_LEN + IMU_DATA_LEN

rx_seq_last = None
rx_total_received = 0
rx_total_lost = 0
rx_total_duplicates = 0
rx_window_received = 0
rx_window_lost = 0
rx_window_duplicates = 0
rx_last_report_time = 0.0
RX_REPORT_INTERVAL_SEC = 1.0
RX_GAP_LIST_LIMIT = 16

# Data saving configuration
# The device packet used by this bridge contains two real 24-bit EEG channels.
# Save both raw ADC counts and scaled values so the scale can be corrected later
# without repeating the recording.
SAVE_DIR = Path(__file__).resolve().parent / "eeg_data"
SAVE_RAW_PACKETS = True
SAVE_FLUSH_INTERVAL = SAMPLE_RATE  # Flush once per second at 250 Hz

csv_file_handle = None
csv_data_writer = None
raw_file_handle = None
saved_sample_count = 0
save_lock = threading.Lock()
current_csv_path = None
current_raw_path = None


def start_data_recording():
    """Create one CSV file and one raw packet file for this run."""
    global csv_file_handle, csv_data_writer, raw_file_handle
    global saved_sample_count, current_csv_path, current_raw_path

    with save_lock:
        if csv_file_handle is not None:
            return

        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        session_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        current_csv_path = SAVE_DIR / f"eeg_data_{session_name}.csv"
        current_raw_path = SAVE_DIR / f"eeg_raw_{session_name}.bin"

        csv_file_handle = open(
            current_csv_path,
            "w",
            newline="",
            encoding="utf-8",
            buffering=64 * 1024,
        )
        csv_data_writer = csv.writer(csv_file_handle)
        csv_data_writer.writerow([
            "received_timestamp",
            "received_order",
            "sample_index",
            "channel_0_raw",
            "channel_1_raw",
            "channel_0_uv",
            "channel_1_uv",
        ])

        if SAVE_RAW_PACKETS:
            raw_file_handle = open(current_raw_path, "wb", buffering=64 * 1024)

        saved_sample_count = 0
        print(f"CSV recording: {current_csv_path}")
        if raw_file_handle is not None:
            print(f"Raw packet recording: {current_raw_path}")


def save_eeg_packet(original_packet: bytes, openbci_packet: bytes):
    """Save a valid two-channel packet without interrupting BLE forwarding."""
    global saved_sample_count

    received_timestamp = time.time()
    sample_index = openbci_packet[1]
    channel_0_raw = parse_24bit_signed(openbci_packet[2:5])
    channel_1_raw = parse_24bit_signed(openbci_packet[5:8])
    channel_0_uv = channel_0_raw * EEG_SCALE
    channel_1_uv = channel_1_raw * EEG_SCALE

    with save_lock:
        if csv_data_writer is None or csv_file_handle is None:
            return

        csv_data_writer.writerow([
            received_timestamp,
            saved_sample_count,
            sample_index,
            channel_0_raw,
            channel_1_raw,
            channel_0_uv,
            channel_1_uv,
        ])

        # Store the original validated 33-byte packet, before zero-padding.
        if raw_file_handle is not None:
            raw_file_handle.write(original_packet)

        saved_sample_count += 1
        if saved_sample_count % SAVE_FLUSH_INTERVAL == 0:
            csv_file_handle.flush()
            if raw_file_handle is not None:
                raw_file_handle.flush()


def stop_data_recording():
    """Flush and close recording files. Safe to call more than once."""
    global csv_file_handle, csv_data_writer, raw_file_handle

    closed_any_file = False
    with save_lock:
        if csv_file_handle is not None:
            csv_file_handle.flush()
            csv_file_handle.close()
            csv_file_handle = None
            csv_data_writer = None
            closed_any_file = True

        if raw_file_handle is not None:
            raw_file_handle.flush()
            raw_file_handle.close()
            raw_file_handle = None
            closed_any_file = True

    if closed_any_file:
        print(f"Recording stopped. Saved {saved_sample_count} samples.")


atexit.register(stop_data_recording)

def update_rx_packet_loss(index: int):
    global rx_seq_last
    global rx_total_received, rx_total_lost, rx_total_duplicates
    global rx_window_received, rx_window_lost, rx_window_duplicates
    global rx_last_report_time

    now = time.time()
    rx_total_received += 1
    rx_window_received += 1

    if rx_seq_last is None:
        rx_seq_last = index
    else:
        last = rx_seq_last
        delta = (index - last) & 0xFF
        if delta == 1:
            rx_seq_last = index
        elif delta == 0:
            rx_total_duplicates += 1
            rx_window_duplicates += 1
            print(f"RX dup last={last} now={index}")
        else:
            lost = delta - 1
            if lost:
                rx_total_lost += lost
                rx_window_lost += lost
                missing_start = (last + 1) & 0xFF
                missing_end = (index - 1) & 0xFF
                if lost <= RX_GAP_LIST_LIMIT:
                    missing_list = [((missing_start + k) & 0xFF) for k in range(lost)]
                    print(f"RX gap last={last} now={index} lost={lost} missing={missing_list}")
                else:
                    print(f"RX gap last={last} now={index} lost={lost} missing={missing_start}->{missing_end}")
            rx_seq_last = index

    if now - rx_last_report_time >= RX_REPORT_INTERVAL_SEC:
        expected_total = rx_total_received + rx_total_lost
        expected_window = rx_window_received + rx_window_lost
        total_rate = (rx_total_lost / expected_total) if expected_total else 0.0
        window_rate = (rx_window_lost / expected_window) if expected_window else 0.0
        print(
            "RX loss "
            f"win={window_rate*100:.2f}% "
            f"total={total_rate*100:.2f}% "
            f"recv={rx_total_received} "
            f"lost={rx_total_lost} "
            f"dup={rx_total_duplicates} "
            f"last={index}"
        )
        rx_last_report_time = now
        rx_window_received = 0
        rx_window_lost = 0
        rx_window_duplicates = 0

def parse_24bit_signed(bytes_data):
    val = (bytes_data[0] << 16) | (bytes_data[1] << 8) | bytes_data[2]
    if val & 0x800000: # Check sign bit
        val -= 0x1000000
    return val

def iter_openbci_cyton_packets(rx_buffer: bytearray):
    while True:
        start_index = rx_buffer.find(bytes([OPENBCI_START_BYTE]))
        if start_index < 0:
            if len(rx_buffer) > (OPENBCI_PACKET_SIZE - 1):
                del rx_buffer[: -(OPENBCI_PACKET_SIZE - 1)]
            return

        if start_index > 0:
            del rx_buffer[:start_index]

        if len(rx_buffer) < OPENBCI_PACKET_SIZE:
            return

        packet = bytes(rx_buffer[:OPENBCI_PACKET_SIZE])
        stop = packet[-1]
        if packet[0] != OPENBCI_START_BYTE or (stop & 0xF0) != OPENBCI_STOP_NIBBLE:
            del rx_buffer[0:1]
            continue

        del rx_buffer[:OPENBCI_PACKET_SIZE]
        yield packet

def build_openbci_8ch_from_2ch(packet_33: bytes) -> bytes:
    if len(packet_33) != OPENBCI_PACKET_SIZE:
        raise ValueError(f"Invalid packet length: {len(packet_33)}")
    if packet_33[0] != OPENBCI_START_BYTE:
        raise ValueError("Invalid start byte")
    if (packet_33[-1] & 0xF0) != OPENBCI_STOP_NIBBLE:
        raise ValueError("Invalid stop byte")

    out_packet = bytearray(OPENBCI_PACKET_SIZE)
    out_packet[0] = OPENBCI_START_BYTE
    out_packet[1] = packet_33[1]
    out_packet[2:8] = packet_33[2:8]
    out_packet[8:32] = bytes([0] * 24)
    out_packet[32] = 0xC0
    return bytes(out_packet)

def compute_band_powers(signal, fs):
    n = len(signal)
    if n <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    window = np.hanning(n)
    signal_win = signal * window
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    fft_vals = np.fft.rfft(signal_win)
    psd = np.abs(fft_vals) ** 2

    def band_power(low, high):
        idx = np.logical_and(freqs >= low, freqs <= high)
        if not np.any(idx):
            return 0.0
        return np.sum(psd[idx])

    delta = band_power(1.0, 4.0)
    theta = band_power(4.0, 8.0)
    alpha = band_power(8.0, 12.0)
    beta = band_power(13.0, 30.0)
    total = delta + theta + alpha + beta
    return delta, theta, alpha, beta, total

def compute_mental_state(alpha, beta, theta, delta):
    eps = 1e-8
    
    # 1. Pope Index (警觉度): Beta / (3.5 * Alpha + Theta)
    pope = beta / (3.5 * alpha + theta + eps)
    
    # 2. TBR (Theta/Beta Ratio) (注意力缺陷/走神): Theta / Beta
    tbr = theta / (beta + eps)
    
    # 3. Alpha/Relative Power (闭眼/睡眠特征)
    total_power = alpha + delta + theta + beta
    alpha_rel = alpha / (total_power + eps)
    
    # 基础分数基于 Pope (专注度)
    k = 6.0
    x0 = 0.28
    base_score = 1.0 / (1.0 + np.exp(-k * (pope - x0))) * 100.0
    
    # 修正因子 1: TBR
    if tbr > 4.0:
        base_score -= 15.0
    elif tbr > 2.5:
        base_score -= 5.0
        
    # 修正因子 2: Alpha 增强惩罚
    if alpha_rel > 0.22:
        penalty = (alpha_rel - 0.22) * 200.0
        base_score -= penalty
        
    # 强制钳位逻辑
    if alpha_rel > 0.45:
        if base_score > 15.0:
            base_score = 15.0
            
    if base_score > 100.0:
        base_score = 100.0
    if base_score < 0.0:
        base_score = 0.0
        
    return base_score, pope, tbr, alpha_rel

async def throughput_monitor():
    global packets_sent_counter, eeg_buffer
    print("Starting Throughput Monitor...")
    while True:
        await asyncio.sleep(1)
        print(f"TX Rate: {packets_sent_counter} pkts/s")
        packets_sent_counter = 0
        
        # # Calculate and print PSD if buffer is full enough
        # if len(eeg_buffer) >= 250:
        #     # Convert buffer to numpy array
        #     data_chunk = np.array(eeg_buffer)
            
        #     # Compute for both channels
        #     for ch in range(2):
        #         # Use Channel index ch for calculation
        #         ch_data = data_chunk[:, ch]
                
        #         # Detrend (remove DC offset)
        #         ch_data = signal.detrend(ch_data)
                
        #         delta, theta, alpha, beta, total = compute_band_powers(ch_data, SAMPLE_RATE)
        #         score, pope, tbr, alpha_rel = compute_mental_state(alpha, beta, theta, delta)
        #         print(f"PSD (Ch{ch+1}) -> Score: {score:.1f}, Pope: {pope:.2f}, TBR: {tbr:.2f}, Alpha_Rel: {alpha_rel:.2f}")
            
# BLE Constants
# SERVICE_UUID = uuid.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
# CHAR_UUID    = uuid.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
# WRITE_CHAR_UUID = uuid.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")  # 请修改为你的实际UUID
# TARGET_NAME  = "RNX_001"
# TARGET_NAME  = "NRF_1299"
# TARGET_NAME  = "RHZ_004"



# H2
SERVICE_UUID = uuid.UUID("0000ae30-0000-1000-8000-00805f9b34fb")
WRITE_CHAR_UUID = uuid.UUID("0000ae01-0000-1000-8000-00805f9b34fb")  # 请修改为你的实际UUID
CHAR_UUID    = uuid.UUID("0000ae02-0000-1000-8000-00805f9b34fb")

TARGET_NAME  = "MindBridge-v3.11"

async def send_command(char):
    global ble_client
    if ble_client and ble_client.is_connected:
        try:
            print(f"Sending '{char.decode()}' command...")
            await ble_client.write_gatt_char(WRITE_CHAR_UUID, char, response=True)
        except Exception as e:
            print(f"Error sending command: {e}")
    else:
        print("BLE not connected")

async def ble_task():
    global ble_client, ble_connected
    print("Starting BLE Scan...")
    while True:
        try:
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: d.name == TARGET_NAME, timeout=10)
            if device:
                print(f"Found device: {device.address}")
                async with BleakClient(device, timeout=10) as client:
                    ble_client = client
                    ble_connected = True
                    print(f"Connected to {device.address}")
                    
                    # Start notifications immediately
                    await client.start_notify(CHAR_UUID, notify_handler)
                    print("Notifications started. Ready for commands.")

                    while client.is_connected:
                        await asyncio.sleep(0.1)
                ble_connected = False
                ble_client = None
                print("Disconnected, rescanning...")
            else:
                print("Device not found, retrying...")
        except Exception as e:
            print(f"BLE Error: {e}")
        await asyncio.sleep(2)
count = 0

def notify_handler(sender: int, data: bytearray):
    global collected_data, count, udp_batch_buffer, packets_sent_counter, rx_ble_buffer

    if not data:
        return

    rx_ble_buffer.extend(data)

    for packet in iter_openbci_cyton_packets(rx_ble_buffer):
        try:
            out_packet = build_openbci_8ch_from_2ch(packet)
        except Exception:
            continue

        update_rx_packet_loss(out_packet[1])
        try:
            save_eeg_packet(packet, out_packet)
        except Exception as e:
            # A disk error should be visible, but should not stop UDP forwarding.
            print(f"Data Save Error: {e}")
        udp_batch_buffer.extend(out_packet)
        packets_sent_counter += 1

        if udp_target_ip and udp_target_port and len(udp_batch_buffer) >= OPENBCI_PACKET_SIZE * BATCH_SIZE:
            try:
                udp_socket.sendto(udp_batch_buffer, (udp_target_ip, udp_target_port))
                udp_batch_buffer = bytearray()
            except Exception as e:
                print(f"UDP Send Error: {e}")
class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self) -> None:
        self.set_header('Access-Control-Allow-Origin','*')

class boardHandler(BaseHandler):
    def get(self):
        self.finish({
            "board_connected": ble_connected,
            "board_type": "cyton", # Mimicking Cyton for compatibility
            "num_channels": channel_num,
            "gains": [96 for i in range(channel_num)] # Kept 96 as in original udp file
        })

class TCPHandler(BaseHandler):
    def post(self):
        global udp_target_ip, udp_target_port
        try:
            body = json.loads(self.request.body.decode('utf-8'))
            if "ip" in body:
                udp_target_ip = body['ip']
                udp_target_port = int(body.get('port', 12345))
                print(f"Updated UDP Target: {udp_target_ip}:{udp_target_port}")
                self.finish({
                    "connected": True,
                    "delimiter": True,
                    "ip": udp_target_ip,
                    "output": "raw",
                    "port": udp_target_port,
                    "latency": 10000
                })
            else:
                self.finish({"error": "No IP provided"})
        except Exception as e:
            print(f"Error parsing TCP request: {e}")
            self.finish({"error": str(e)})

class AllHandler(BaseHandler):
    def get(self):
        self.finish({"board_connected": ble_connected, 
            "heap": 19048, 
            "ip": "0.0.0.0", 
            "name": "openbci-8C8F",
             "num_channels": channel_num, 
             "version": "v2.0.5", 
             "latency": 16667
        })

class CommandHandler(BaseHandler):
    def post(self):
        # Handle commands if needed, e.g., start/stop streaming
        self.write('Success')

class StreamStartHandler(BaseHandler):
    def get(self):
        print('Stream started command received')
        self.write('Stream started')

class StreamStopHandler(BaseHandler):
    def get(self):
        print('Stream stopped command received')
        self.write('Stream stopped')

class locaStreamStartHandler(BaseHandler):
    def post(self):
        self.finish()

class ImpedanceHandler(BaseHandler):
    def post(self):
        self.write('Impedance check')

class TriggerHandle(BaseHandler):
    def post(self):
        self.finish()

def start_server_loop(loop):
    asyncio.set_event_loop(loop)
    
    # Install asyncio loop for Tornado
    tornado.platform.asyncio.AsyncIOMainLoop().install()
    
    app = tornado.web.Application([
        (r'/board', boardHandler),
        (r'/all', AllHandler),
        (r'/tcp', TCPHandler),
        (r'/command', CommandHandler),
        (r'/trigger', TriggerHandle),
        (r'/stream/start', StreamStartHandler),
        (r'/stream/stop', StreamStopHandler),
        (r'/local_stream/enable', locaStreamStartHandler),
        (r'/impedance', ImpedanceHandler)
    ])
    
    # Using port 80 as seen in the original bci-serve.py
    # Ensure this port is free or change it if necessary
    try:
        app.listen(80)
        print("Server listening on port 80")
    except OSError as e:
        print(f"Port 80 occupied or restricted, trying 9932. Error: {e}")
        app.listen(9932)
        print("Server listening on port 9932")
    
    # Start BLE task
    asyncio.ensure_future(ble_task(), loop=loop)
    asyncio.ensure_future(throughput_monitor(), loop=loop)
    
    # Start Event Loop
    loop.run_forever()

def main():
    # Start a fresh recording session every time the program launches.
    start_data_recording()

    # Create a new loop for the server thread
    loop = asyncio.new_event_loop()
    
    # Start server thread
    t = threading.Thread(target=start_server_loop, args=(loop,))
    t.daemon = True # Kill thread when main exits
    t.start()
    
    # GUI Setup
    root = tk.Tk()
    root.title("BLE Control")
    root.geometry("300x250")
    
    label = tk.Label(root, text="BLE Command Control", font=("Arial", 14))
    label.pack(pady=10)
    
    def on_send_s():
        asyncio.run_coroutine_threadsafe(send_command(b'S'), loop)
        
    def on_send_b():
        asyncio.run_coroutine_threadsafe(send_command(b'b'), loop)
        
    def on_send_r():
        asyncio.run_coroutine_threadsafe(send_command(b'R'), loop)

    def on_send_i():
        asyncio.run_coroutine_threadsafe(send_command(b'I'), loop)
    
    btn_s = tk.Button(root, text="Send 'S' (Start Data)", command=on_send_s, width=25, height=2)
    btn_s.pack(pady=5)
    
    btn_b = tk.Button(root, text="Send 'B' (Start Origin)", command=on_send_b, width=25, height=2)
    btn_b.pack(pady=5)

    btn_r = tk.Button(root, text="Send 'R' (Reset)", command=on_send_r, width=25, height=2)
    btn_r.pack(pady=5)

    btn_i = tk.Button(root, text="Send 'I' (Start Attention)", command=on_send_i, width=25, height=2)
    btn_i.pack(pady=5)

    def on_close():
        stop_data_recording()
        loop.call_soon_threadsafe(loop.stop)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    
    print("GUI Started. Close window to exit.")
    root.mainloop()

if __name__ == '__main__':
    main()
