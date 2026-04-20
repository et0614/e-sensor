import mido
import struct
import time
from dataclasses import dataclass
from typing import Optional, List, Callable, Tuple
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
    CMD_REQ_DATA  = 0x02    # 計測値送信要求
    CMD_START     = 0x03    # 計測開始
    CMD_STOP      = 0x04    # 計測終了
    CMD_A_RW      = 0x05    # 特性係数A読み書き
    CMD_A_REQ     = 0x06    # 特性係数A送信
    CMD_B_RW      = 0x07    # 特性係数B読み書き
    CMD_B_REQ     = 0x08    # 特性係数B送信
    CMD_ID_RES    = 0x09    # ID送信
    CMD_ID_REQ    = 0x10    # ID要求
    CMD_VER_RES       = 0x11    # バージョン送信
    CMD_VER_REQ       = 0x12    # バージョン要求
    CMD_CO2_CAL_RES   = 0x13    # CO2校正結果送信
    CMD_CO2_CAL_REQ   = 0x14    # CO2校正実行
    CMD_CO2_RESET_REQ = 0x15    # CO2工場出荷時リセット要求
    CMD_CO2_RESET_RES = 0x16    # CO2リセット完了通知
    CMD_CONDITIONING_REQ   = 0x17  # CO2初期調整要求 (H->D)
    CMD_CONDITIONING_START = 0x18  # CO2初期調整開始通知 (D->H)
    CMD_CONDITIONING_DONE  = 0x19  # CO2初期調整完了通知 (D->H)

    def __init__(self, port_keyword: str = 'E-Sensor'):
        self.port_keyword = port_keyword
        self.inport = None
        self.outport = None

        self.on_id_received: Optional[Callable[[str], None]] = None
        self.on_data_received: Optional[Callable[[SensorData], None]] = None
        self.on_version_received: Optional[Callable[[int, int, int], None]] = None
        self.on_co2_cal_received: Optional[Callable[[int], None]] = None
        self.on_co2_reset_received: Optional[Callable[[], None]] = None
        self.on_conditioning_start: Optional[Callable[[], None]] = None
        self.on_conditioning_done: Optional[Callable[[], None]] = None

        # 同期用一時保存
        self._last_sensor_data: Optional[SensorData] = None
        self._last_device_id: Optional[str] = None
        self._last_version: Optional[Tuple[int, int, int]] = None
        self._last_co2_correction: Optional[int] = None
        self._co2_reset_notified = False
        self._conditioning_start_notified = False
        self._conditioning_done_notified = False


    def connect(self) -> bool:
        """デバイスに接続する"""
        try:
            mido.set_backend('mido.backends.rtmidi')
        except:
            pass

        in_names = mido.get_input_names()
        out_names = mido.get_output_names()
        in_name = next((n for n in in_names if self.port_keyword in n), None)
        out_name = next((n for n in out_names if self.port_keyword in n), None)

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
    def start_measurement(self):
        """計測を開始させる"""
        self._send_cmd(self.CMD_START)


    def stop_measurement(self):
        """計測を停止させる"""
        self._send_cmd(self.CMD_STOP)
    

    def request_data(self):
        """現在の計測値を送信するよう要求する"""
        self._send_cmd(self.CMD_REQ_DATA)

    def get_data(self, timeout: float = 0.5) -> Optional[SensorData]:
        """現在の計測値を要求し、受信するまで待機する"""
        self._last_sensor_data = None
        self.flush()          # 古いパケットを掃除
        self.request_data()   # 送信要求を出す
        
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            # poll内で self._last_sensor_data が更新される
            data = self.poll() 
            if data:
                return data
            time.sleep(0.01)
        return None
    

    def request_device_id(self):
        """マイコンに固有IDの送信を要求する"""
        self._send_cmd(self.CMD_ID_REQ)


    def get_device_id(self, timeout: float = 0.5) -> Optional[str]:
        """IDを要求し、受信するまで待機する"""
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


    def request_version(self):
        """バージョンを要求する"""
        self._send_cmd(self.CMD_VER_REQ)


    def get_version(self, timeout: float = 0.5) -> Optional[Tuple[int, int, int]]:
        """バージョンを要求し、受信するまで待機する"""
        self._last_version = None
        self.flush()
        self.request_version()
        
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            self.poll()
            if self._last_version:
                return self._last_version
            time.sleep(0.01)
        return None


    def perform_co2_calibration(self, target_ppm: int):
        """CO2センサの強制校正を実行する (CMD 0x14)"""
        # Big Endian (2byte) + CRC
        payload = bytearray(struct.pack('>H', target_ppm))
        payload.append(self._crc8(payload))
        self._send_cmd(self.CMD_CO2_CAL_REQ, payload)


    def reset_co2_factory(self):
        """CO2センサを工場出荷時設定にリセットする (CMD 0x15)"""
        self._send_cmd(self.CMD_CO2_RESET_REQ)


    def request_conditioning(self):
        """CO2センサの初期調整を要求する (CMD 0x17)"""
        self._send_cmd(self.CMD_CONDITIONING_REQ)


    def perform_conditioning(self, wait_timeout: float = 30.0) -> bool:
        """
        CO2センサの初期調整を要求し、完了通知を受けるまでブロック待機する。
        初期調整には約22秒かかる。

        Args:
            wait_timeout: 完了通知を待つ最大秒数（デフォルト 30 秒）

        Returns:
            True  : 完了通知 (CMD_CONDITIONING_DONE) を受信
            False : タイムアウトまでに完了通知が来なかった
        """
        self._conditioning_start_notified = False
        self._conditioning_done_notified = False
        self.flush()
        self.request_conditioning()

        start_time = time.time()
        while (time.time() - start_time) < wait_timeout:
            self.poll()
            if self._conditioning_done_notified:
                return True
            time.sleep(0.05)
        return False


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
        受信バッファを確認し、メッセージがあれば処理する。
        センサーデータを受信した場合はそのオブジェクトを返す。
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
            # デコード
            raw_payload = self._decode_nibbles(payload_nibbles)

            # センサーデータ
            if cmd_id == self.CMD_SENS_DATA:
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
                    self._last_sensor_data = data # 同期用変数に保存
                    if self.on_data_received:
                        self.on_data_received(data)
                    return data
            
            # ID応答
            elif cmd_id == self.CMD_ID_RES:
                # 4byte ID + 1byte CRC = 5byte
                if len(raw_payload) >= 5 and self._crc8(raw_payload[:4]) == raw_payload[4]:
                    id_hex = raw_payload[:4].hex().upper()
                    self._last_device_id = id_hex
                    if self.on_id_received:
                        self.on_id_received(id_hex)

            # バージョン応答
            elif cmd_id == self.CMD_VER_RES:
                if len(raw_payload) >= 4 and self._crc8(raw_payload[:3]) == raw_payload[3]:
                    major, minor, rev = struct.unpack('BBB', raw_payload[:3])
                    self._last_version = (major, minor, rev)
                    if self.on_version_received:
                        self.on_version_received(major, minor, rev)

            # CO2校正結果
            elif cmd_id == self.CMD_CO2_CAL_RES:
                if len(raw_payload) >= 3 and self._crc8(raw_payload[:2]) == raw_payload[2]:
                    # 補正値 (差分) は符号付き 16bit。失敗時は -1 (0xFFFF) が入る。
                    correction = struct.unpack('>h', raw_payload[:2])[0]
                    self._last_co2_correction = correction
                    if self.on_co2_cal_received:
                        self.on_co2_cal_received(correction)

            # CO2リセット完了
            elif cmd_id == self.CMD_CO2_RESET_RES:
                self._co2_reset_notified = True
                if self.on_co2_reset_received:
                    self.on_co2_reset_received()

            # CO2初期調整開始通知
            elif cmd_id == self.CMD_CONDITIONING_START:
                # ペイロード無し (CRC 1 byte のみ届く)
                if len(raw_payload) >= 1 and self._crc8(b'') == raw_payload[0]:
                    self._conditioning_start_notified = True
                    if self.on_conditioning_start:
                        self.on_conditioning_start()

            # CO2初期調整完了通知
            elif cmd_id == self.CMD_CONDITIONING_DONE:
                # ペイロード無し (CRC 1 byte のみ届く)
                if len(raw_payload) >= 1 and self._crc8(b'') == raw_payload[0]:
                    self._conditioning_done_notified = True
                    if self.on_conditioning_done:
                        self.on_conditioning_done()
        return None
    

if __name__ == "__main__":
    client = ESensorClient()
    if not client.connect():
        print("Error: Device 'E-Sensor' not found.")
    else:
        try:
            # 1. デバイス情報の取得と表示
            device_id = client.get_device_id()
            version = client.get_version()

            print("--- Device Information ---")
            print(f"ID      : {device_id if device_id else 'Unknown'}")
            if version:
                print(f"Version : {version[0]}.{version[1]}.{version[2]}")
            else:
                print("Version : Unknown")
            print("--------------------------\n")

            # 2. 計測開始
            client.start_measurement()
            print("Measurement Started. Press Ctrl+C to stop.\n")

            # ヘッダーの表示 (Flags列を追加)
            # L: Illuminance, E: Env(Temp/Hum/CO2), A: Anemometer(Vel/Vol)
            print("Time     | Illum[lx] | Temp[C] | Hum[%] | Vel[m/s] | Vol[V] | CO2[ppm] | Flags")
            print("---------|-----------|---------|--------|----------|--------|----------|-------")
            
            while True:
                # データをリクエストして取得
                data = client.get_data(timeout=0.5)
                
                if data:
                    dt = datetime.datetime.fromtimestamp(data.timestamp)
                    time_str = dt.strftime('%H:%M:%S')
                    
                    # フラグの可視化文字列を作成
                    f_ill = "L" if data.ill_valid else "-"
                    f_env = "E" if data.env_valid else "-"
                    f_ane = "A" if data.anemo_valid else "-"
                    flags_str = f"[{f_ill} {f_env} {f_ane}]"
                    
                    print(f"{time_str} | {data.illuminance:9.1f} | {data.temperature:7.2f} | {data.humidity:6.2f} | {data.velocity:8.2f} | {data.voltage:6.3f} | {data.co2:8d} | {flags_str}")
                else:
                    # 応答がない場合は警告を表示
                    print(f"{datetime.datetime.now().strftime('%H:%M:%S')} | No Response from device...")

                # 次の要求までのインターバル（1秒）
                time.sleep(1.0)
                
        except KeyboardInterrupt:
            client.stop_measurement()
            print("\nMeasurement Stopped by user.")
        except Exception as e:
            print(f"\nAn error occurred: {e}")
        finally:
            client.close()