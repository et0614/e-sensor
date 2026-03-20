import mido
import struct
import time
from dataclasses import dataclass
from typing import Optional, List, Callable
import datetime

@dataclass
class SensorData:
    """パース済みのセンサーデータ構造体"""
    timestamp: float
    illuminance: float  # lx
    temperature: float   # C
    humidity: float      # %
    velocity: float     # m/s (内部ではmm/sで管理されているものを変換)
    voltage: float      # V
    co2: int           # ppm
    ill_valid: bool    # Bit 0 (照度)
    env_valid: bool    # Bit 1 (温湿度・CO2)
    anemo_valid: bool  # Bit 2 (風速・電圧)

class ESensorClient:
    # 定数定義
    MANUFACTURER_ID = 0x7D
    CMD_SENS_DATA = 0x01    # 計測値送信
    CMD_START     = 0x02    # 計測開始
    CMD_STOP      = 0x03    # 計測終了
    CMD_A_RW      = 0x04    # 特性係数A読み書き
    CMD_A_REQ     = 0x05    # 特性係数A送信
    CMD_B_RW      = 0x06    # 特性係数B読み書き
    CMD_B_REQ     = 0x07    # 特性係数B送信
    CMD_ID_RES    = 0x08    # ID送信
    CMD_ID_REQ    = 0x09    # ID要求

    def __init__(self, port_keyword: str = 'E-Sensor'):
        self.port_keyword = port_keyword
        self.inport = None
        self.outport = None
        self.on_id_received: Optional[Callable[[str], None]] = None
        self.on_data_received: Optional[Callable[[SensorData], None]] = None


    def connect(self) -> bool:
        """デバイスに接続する"""
        try:
            mido.set_backend('mido.backends.rtmidi')
        except:
            pass

        in_name = next((n for n in mido.get_input_names() if self.port_keyword in n), None)
        out_name = next((n for n in mido.get_output_names() if self.port_keyword in n), None)

        if in_name and out_name:
            self.inport = mido.open_input(in_name)
            self.outport = mido.open_output(out_name)
            return True
        return False


    def close(self):
        """接続を閉じる"""
        if self.inport: self.inport.close()
        if self.outport: self.outport.close()


    # --- 内部ユーティリティ ---
    @staticmethod
    def _crc8(data: bytes) -> int:
        crc = 0xFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc << 1) ^ 0x31 if crc & 0x80 else (crc << 1)
                crc &= 0xFF
        return crc


    @staticmethod
    def _encode_nibbles(data: bytes) -> List[int]:
        res = []
        for b in data:
            res.extend([(b >> 4) & 0x0F, b & 0x0F])
        return res


    @staticmethod
    def _decode_nibbles(nibbles: List[int]) -> bytearray:
        return bytearray((nibbles[i] << 4) | nibbles[i+1] for i in range(0, len(nibbles), 2))


    def _send_cmd(self, cmd_id: int, payload: bytes = b''):
        data = [self.MANUFACTURER_ID, cmd_id]
        if payload:
            data.extend(self._encode_nibbles(payload))
        self.outport.send(mido.Message('sysex', data=data))


    # --- デバイス操作 API ---
    def request_device_id(self):
        """マイコンに固有IDの送信を要求する (CMD 0x09)"""
        self._send_cmd(self.CMD_ID_REQ)


    def get_device_id(self, timeout: float = 1.0) -> Optional[str]:
        """IDを要求し、受信するまで待機する(同期型ヘルパー)"""
        self._last_device_id = None
        self.flush()
        self.request_device_id()
        
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            self.poll()
            if self._last_device_id:
                return self._last_device_id
            time.sleep(0.01)
        return None


    def start_measurement(self):
        """計測を開始させる"""
        self._send_cmd(self.CMD_START)


    def stop_measurement(self):
        """計測を停止させる"""
        self._send_cmd(self.CMD_STOP)


    def request_coefficients(self, type_a: bool = True):
        """係数の送信を要求する"""
        cmd = self.CMD_A_REQ if type_a else self.CMD_B_REQ
        self._send_cmd(cmd)


    def write_coefficients(self, values: List[float], type_a: bool = True):
        """係数(5つのfloat)を書き込む"""
        cmd = self.CMD_A_RW if type_a else self.CMD_B_RW
        payload = bytearray(struct.pack('>5f', *values)) # Big Endian
        payload.append(self._crc8(payload))
        self._send_cmd(cmd, payload)


    def flush(self):
        """受信バッファをすべて読み飛ばして空にする"""
        if self.inport:
            # 溜まっているメッセージをすべて消費する
            for _ in self.inport.iter_pending():
                pass


    def poll(self) -> Optional[SensorData]:
        """
        メッセージを1つ読み取り、センサーデータがあればパースして返す。
        アプリケーションのメインループ内で呼び出すことを想定。
        """
        if not self.inport: return None

        for msg in self.inport.iter_pending():
            if msg.type != 'sysex' or len(msg.data) < 2:
                continue
            
            # Manufacturer ID チェック
            if msg.data[0] != self.MANUFACTURER_ID:
                continue

            cmd_id = msg.data[1]
            payload_nibbles = msg.data[2:]

            if cmd_id == self.CMD_SENS_DATA:
                raw_payload = self._decode_nibbles(payload_nibbles)
                if len(raw_payload) >= 16 and self._crc8(raw_payload[:15]) == raw_payload[15]:
                    # Little Endian パース
                    val = struct.unpack('<I h H H H H B', raw_payload[:15])
                    status = val[6]
                    data = SensorData(
                        timestamp=time.time(),
                        illuminance=val[0] / 10.0,
                        temperature=val[1] / 100.0,
                        humidity=val[2] / 100.0,
                        velocity=val[3] / 1000.0, # mm/s -> m/s
                        voltage=val[4] / 1000.0, # mV -> V
                        co2=val[5],
                        ill_valid=bool(status & (1 << 0)),   # Bit 0
                        env_valid=bool(status & (1 << 1)),   # Bit 1
                        anemo_valid=bool(status & (1 << 2))  # Bit 2
                    )
                    if self.on_data_received:
                        self.on_data_received(data)
                    return data
                
            elif cmd_id == self.CMD_ID_RES:
                raw_payload = self._decode_nibbles(payload_nibbles)
                # 10byte ID + 1byte CRC = 11byte
                if len(raw_payload) >= 5 and self._crc8(raw_payload[:4]) == raw_payload[4]:
                    id_hex = raw_payload[:4].hex().upper()
                    self._last_device_id = id_hex
                    if self.on_id_received:
                        self.on_id_received(id_hex)    
        return None
    

if __name__ == "__main__":
    client = ESensorClient()
    if not client.connect():
        print("Device not found.")
    else:
        try:
            device_id = client.get_device_id()
            if device_id:
                print(f"Connected to Device ID: {device_id}")
            else:
                print("Failed to get Device ID.")

            client.start_measurement()
            print("Measurement Started. Press Ctrl+C to stop.\n")

            # ヘッダーの表示
            print("Time     | Illum[lx] | Temp[C] | Hum[%] | Vel[m/s] | Vol[V] | CO2[ppm]")
            print("---------|-----------|---------|--------|----------|--------|---------")
            
            while True:
                data = client.poll()
                if data:
                    dt = datetime.datetime.fromtimestamp(data.timestamp)
                    time_str = dt.strftime('%H:%M:%S')
                    print(f"{time_str} | {data.illuminance:9.1f} | {data.temperature:7.2f} | {data.humidity:6.2f} | {data.velocity:8.2f} | {data.voltage:6.3f} | {data.co2:5d}")
                time.sleep(0.01)
                
        except KeyboardInterrupt:
            client.stop_measurement()
        finally:
            client.close()