import mido
import struct
import time
import math
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
    # 風速計自己発熱の温度補正(カルマンフィルタ)が有効な場合に、補正後の
    # 空気温度推定値[℃]が入る。補正無効時や温湿度無効時は None。
    # 生の temperature は補正に関わらずそのまま保持される。
    corrected_temperature: Optional[float] = None
    # 温度補正に伴って換算し直した相対湿度[%]。補正無効時や温湿度無効時は None。
    # 生の humidity はそのまま保持される。
    corrected_humidity: Optional[float] = None


class ThermalParams:
    """風速計の自己発熱による温度センサ熱影響モデルの定数。

    資料「熱影響の補正方法」§3 の同定値。詳しい導出・手法比較・検証は
    thermal_correction.py を参照（こちらは実機組み込み用に numpy 非依存で実装）。

        CB dTB/dt = Q - K(v)(TB - Ta) ,  K(v) = A + B*v [mW/K]
        定常: TB - Ta = Q/K(v)

    K(v) は実機モデルの固定値を採用。基板まわりの強制対流は平板/バルク体的で、
    熱線(細円柱)の King 則 √v ではなく風速の一次式の方が実測に合うため線形とした。
    実使用では低風速の場面が多いため、切片 A(=無風時の伝熱係数) を無風実測値に固定。

    offset_scale は定常オフセット(Q/K)に掛ける調整係数。校正時の TB-Ta を真の
    平衡前に測ったため offset がやや過小だったので、当面 1.2 としてつじつまを
    合わせる。平衡まで取り直して offset/K を再校正したら 1.0 に戻すこと。
    (offset=Q/K のため、これは実効的に Q を 1.2 倍/ K を 1/1.2 倍するのと等価。
     校正で Q はスケール自由なので、Q・A・B を触らず1係数で表現する。)
    """

    def __init__(self, Q: float = 50.0, CB: float = 1300.0, dt: float = 1.0,
                 A: float = 11.058, B: float = 130.19, offset_scale: float = 1.2):
        self.Q = Q          # トランジスタ発熱 [mW]
        self.CB = CB        # 基板熱容量 [mJ/K]
        self.dt = dt        # 既定サンプリング周期 [s]
        self.A = A          # K(v) 切片(無風時) [mW/K] — 低風速重視で実測値に固定
        self.B = B          # K(v) 風速係数 [mW/(K·(m/s))]
        self.offset_scale = offset_scale  # 定常オフセット調整係数(再校正後は 1.0)

    def K(self, v: float) -> float:
        """伝熱係数 K(v) = A + B*v [mW/K]。"""
        return max(1e-3, self.A + self.B * max(0.0, v))

    def offset(self, v: float) -> float:
        """定常自己発熱オフセット offset_scale·Q/K(v) [K]。"""
        return self.offset_scale * self.Q / self.K(v)


def _sat_vapor_pressure(t_celsius: float) -> float:
    """飽和水蒸気圧 [hPa] (Magnus/WMO 近似, 0〜50℃で十分な精度)。"""
    return 6.112 * math.exp(17.62 * t_celsius / (243.12 + t_celsius))


def correct_relative_humidity(rh: float, t_measured: float, t_air: float) -> float:
    """自己発熱で温度が変わったぶんの相対湿度補正。

    水蒸気分圧 e は定圧では加熱しても保存される量とみなせる。センサは自分の温度
    (基板温度 t_measured) での相対湿度を読むので e = RH_meas/100·esat(t_measured)。
    真の空気温度 t_air での相対湿度は
        RH_air = e/esat(t_air)·100 = RH_meas · esat(t_measured)/esat(t_air)
    t_air < t_measured (基板加熱) なら RH は上方修正される。0〜100% にクランプ。
    """
    rh_corr = rh * _sat_vapor_pressure(t_measured) / _sat_vapor_pressure(t_air)
    return max(0.0, min(100.0, rh_corr))


class KalmanThermalCorrector:
    """2状態カルマンフィルタ(資料 §4)による空気温度の推定。

    状態 x=[TB, Ta] (基板温度, 空気温度)。基板温度 TB(センサ計測値) と風速 v
    から、自己発熱の過渡をモデルで差し引いて空気温度 Ta を推定する。
    MCU/軽量実行を想定し numpy 非依存(2x2 を手計算)。
    """

    def __init__(self, params: Optional[ThermalParams] = None,
                 meas_std: float = 0.03,     # 観測(センサ)雑音 σ [K]
                 air_std: float = 0.005,     # 空気温度の1ステップ変動 σ [K] (プロセス雑音)
                 board_std: float = 0.002,   # 基板状態のモデル雑音 σ [K]
                 init_air_std: float = 0.5,  # 起動時の空気温度の初期不確かさ σ [K]
                 gate_abs: float = 2.0):     # 外れ値ゲート: 残差の許容上限 [K]
        # air_std を小さくするほど補正後の揺れは小さくなる(平滑)が、真の空気温度
        # 変化への追従は遅くなる。室内空気はゆるやかに変化するため小さめが良い。
        # 基板の自己発熱リップル(対流/制御由来)は空気変動ではないので、小さい
        # air_std で抑制するのが正しい。
        self.p = params or ThermalParams()
        self.R = meas_std ** 2
        self.qb = board_std ** 2
        self.qa = air_std ** 2
        self.gate_abs = gate_abs
        self._init_board_var = meas_std ** 2
        self._init_air_var = init_air_std ** 2
        self.reset()

    def reset(self):
        """フィルタ状態を初期化する。"""
        self.xb: Optional[float] = None   # 基板温度状態
        self.xa: Optional[float] = None   # 空気温度状態
        # 誤差共分散 P (対称 2x2)。
        # 接続開始時は「基板温度 ≒ 周囲空気温度 (自己発熱はこれから蓄積する)」と
        # 分かっているため、空気状態の初期不確かさを小さく取る(コールドスタート前提)。
        # こうしないと開始直後にフィルタが空気温度を過剰に引き下げ(基板はまだ昇温前
        # なのに定常オフセット分を差し引こうとする)、推定値がストンと落ちてしまう。
        # 自己発熱の立ち上がりはモデル(F の動特性)が時間をかけて表現する。
        self.p00 = self._init_board_var
        self.p01 = 0.0
        self.p11 = self._init_air_var

    def update(self, z: float, v: float, dt: Optional[float] = None,
               heating: bool = True, measured: bool = True) -> Optional[float]:
        """1サンプル進めて推定空気温度[℃]を返す。

        z        : 基板温度(センサ計測値) [℃]
        v        : 風速 [m/s]
        dt       : 前回からの経過時間 [s] (None なら params.dt)
        heating  : 風速計が稼働(自己発熱)中なら True。停止中は Q=0 として扱い、
                   オフセットを与えず基板が空気温度へ冷える過程をモデル化する。
        measured : 有効な温度観測があれば True。False(欠測=温度の読み取り失敗)の
                   ときは観測を使わず予測ステップのみで状態を進める。これにより
                   欠測時も補正値が途切れず連続する(z は無視される)。
        """
        if dt is None:
            dt = self.p.dt
        if self.xb is None:
            # 初期化には有効な初回観測が必要
            if not measured:
                return None
            # 起動直後は自己発熱前 → Ta ≈ TB とみなして初期化
            self.xb = z
            self.xa = z
            return z

        K = self.p.K(v)
        E = math.exp(-K * dt / self.p.CB)
        # 自己発熱オフセット(調整係数込み)。停止中は加熱なしで 0。
        off = (self.p.offset_scale * self.p.Q / K) if heating else 0.0
        c = 1.0 - E

        # --- 予測:  x = F x + b ,  P = F P Fᵀ + Qw   (F=[[E,c],[0,1]]) ---
        xb = E * self.xb + c * self.xa + c * off
        xa = self.xa
        m00 = E * self.p00 + c * self.p01
        m01 = E * self.p01 + c * self.p11
        m11 = self.p11
        n00 = m00 * E + m01 * c + self.qb   # +Qw
        n01 = m01
        n11 = m11 + self.qa

        # --- 更新:  e=z-Hx , S=HPHᵀ+R , g=PHᵀ/S , x+=g e , P=(I-gH)P  (H=[1,0]) ---
        y = z - xb            # 残差 (H x = xb)
        # 欠測(measured=False)、または外れ値(基板温度は時定数 ≫ dt のため dt 秒で
        # 大きく跳ねない)の観測は使わず、予測のみを採用する。これで欠測・化け
        # データがあっても補正値は連続したまま保たれる。
        if (not measured) or abs(y) > self.gate_abs:
            self.xb, self.xa = xb, xa
            self.p00, self.p01, self.p11 = n00, n01, n11
            return self.xa
        S = n00 + self.R
        g0 = n00 / S
        g1 = n01 / S
        self.xb = xb + g0 * y
        self.xa = xa + g1 * y
        self.p00 = (1.0 - g0) * n00
        self.p01 = (1.0 - g0) * n01
        self.p11 = n11 - g1 * n01
        return self.xa


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
    CMD_VEL_START          = 0x1A  # 風速センサ起動 (H->D)
    CMD_VEL_STOP           = 0x1B  # 風速センサ停止 (H->D)

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

        # 風速計自己発熱の温度補正 (任意・既定は無効)
        self.corrector: Optional[KalmanThermalCorrector] = None
        self._velocity_on: bool = True       # 風速計の稼働状態 (電源投入時は ON)
        self._last_corr_ts: Optional[float] = None


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


    def open_ports(self, in_name: str, out_name: str) -> bool:
        """明示した MIDI in/out ポート名で接続する（複数台環境用）。

        connect() は名前が "E-Sensor" を含む最初の1台を開くため、同型の
        E-Sensor が複数挿さっていると個体を選べない。esensor_discovery で
        CMD_REQ_ID 応答から確定した特定個体の (in,out) ペアをここに渡して開く。
        """
        try:
            mido.set_backend('mido.backends.rtmidi')
        except Exception:
            pass
        try:
            self.inport = mido.open_input(in_name)
            self.outport = mido.open_output(out_name)
            return True
        except (IOError, OSError):
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


    def start_velocity(self):
        """風速センサを起動する。起動後 約5秒の予熱を経て計測値が更新される。"""
        self._velocity_on = True   # 加熱開始 (温度補正のオフセット計算に使用)
        self._send_cmd(self.CMD_VEL_START)


    def stop_velocity(self):
        """風速センサを停止する。基板の発熱を抑え、温度/CO2 への熱影響を避けたい時に使う。
        停止中は velocity/voltage は 0、anemo_valid は False になる。"""
        self._velocity_on = False  # 加熱停止 (以降は自己発熱オフセット 0 で補正)
        self._send_cmd(self.CMD_VEL_STOP)


    # --- 温度補正 (風速計自己発熱) ---
    def enable_thermal_correction(self, params: Optional[ThermalParams] = None,
                                  meas_std: float = 0.03, air_std: float = 0.005,
                                  init_air_std: float = 0.5, gate_abs: float = 2.0):
        """風速計の自己発熱による温度センサ熱影響の補正を有効化する。

        以降、受信した SensorData の corrected_temperature にカルマンフィルタで
        推定した空気温度[℃]が、corrected_humidity にそれに合わせて換算し直した
        相対湿度[%]が格納される。生の temperature / humidity はそのまま保持される。

        air_std: 小さいほど補正後の揺れが小さく(平滑)なるが、真の空気温度変化への
            追従は遅くなる。基板の自己発熱リップルを抑えるため小さめが良い。
        init_air_std: 起動時の空気温度の初期不確かさ[K]。接続時は基板≒空気である
            という前提を表し、小さいほど開始直後の推定が安定する(コールドスタート)。
        gate_abs: 外れ値棄却の残差しきい値[K]。欠測・化けデータによるスパイクを防ぐ。
        """
        self.corrector = KalmanThermalCorrector(
            params or ThermalParams(), meas_std=meas_std, air_std=air_std,
            init_air_std=init_air_std, gate_abs=gate_abs)
        self._last_corr_ts = None

    def disable_thermal_correction(self):
        """温度補正を無効化する (以降 corrected_temperature/humidity は None)。"""
        self.corrector = None
        self._last_corr_ts = None


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


    @staticmethod
    def _quantize_to_safe_float(value: float) -> float:
        """
        Main MCU (バージョン 1.0.0 まで) の is_valid_coef_array バグへの対策。

        旧ファームウェアの受信側バリデーションが BE で受け取ったバイトを
        誤って LE float として解釈し、|誤解釈値| > 1e30 を弾いてしまうため、
        BE バイトの LSB バイト下位 7 ビットが 0x71-0x7F の値はサイレントに
        書き込みが拒否される。本関数はその範囲を回避する最寄りの float に
        丸める (最大ずれは数 ULP, 1e-6 相対程度)。

        ファームウェア修正版 (>=1.0.1 想定) では不要だが、適用しても無害。
        """
        if not (value == value) or value in (float('inf'), float('-inf')):
            return value  # NaN/Inf はそのまま (どのみち弾かれる)

        be = struct.pack('>f', value)
        if (be[3] & 0x7F) <= 0x70:
            return value  # すでに安全

        bits = struct.unpack('>I', be)[0]
        # 上下に ±N ULP ずらして最初に安全になる値を返す
        for delta in range(1, 17):
            for try_bits in (bits + delta, bits - delta):
                try_bytes = struct.pack('>I', try_bits & 0xFFFFFFFF)
                if (try_bytes[3] & 0x7F) <= 0x70:
                    return struct.unpack('>f', try_bytes)[0]
        # 通常の浮動小数では到達しないはず
        raise ValueError(f"Cannot find safe float near {value}")


    def write_coefficients(self, values: List[float], type_a: bool = True):
        """係数(5つのfloat)を書き込む

        旧ファームウェア (Main MCU is_valid_coef_array のバグ) で
        サイレントに弾かれる値を回避するため、各要素を自動的に
        「安全な」最寄り float に量子化する。新ファームウェアでは
        量子化は実質的に no-op (1e-6 相対以下の誤差) で害は無い。
        """
        safe_values = [self._quantize_to_safe_float(v) for v in values]
        cmd = self.CMD_A_RW if type_a else self.CMD_B_RW
        payload = bytearray(struct.pack('>5f', *safe_values)) # Big Endian
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
                    # 風速計自己発熱の温度補正。生データには手を加えず、
                    # corrected_temperature に推定空気温度を付与する。温度の欠測
                    # (env_valid=False, STCC4 読み取り失敗で温度は前回値が保持される)
                    # の時は観測を使わず予測のみで進め、補正値を途切れさせない。
                    if self.corrector is not None:
                        if data.anemo_valid:
                            self._velocity_on = True  # 風速値が有効＝加熱中
                        v = data.velocity if data.anemo_valid else 0.0
                        v = min(30.0, max(0.0, v))    # 異常な風速値をクランプ
                        if self._last_corr_ts is None:
                            dt = self.corrector.p.dt
                        else:
                            dt = data.timestamp - self._last_corr_ts
                            dt = min(10.0, max(0.05, dt))  # 異常な間隔をクランプ
                        data.corrected_temperature = self.corrector.update(
                            data.temperature, v, dt,
                            heating=self._velocity_on, measured=data.env_valid)
                        # 温度補正に伴い相対湿度も換算し直す (水蒸気分圧を保存)
                        tc = data.corrected_temperature
                        if tc is not None and data.env_valid:
                            data.corrected_humidity = correct_relative_humidity(
                                data.humidity, data.temperature, tc)
                        self._last_corr_ts = data.timestamp
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
            # 風速計自己発熱の温度補正を有効化 (生の Temp[C] と補正後 Tcorr[C] を併記)
            client.enable_thermal_correction()
            print("Measurement Started. Press Ctrl+C to stop.\n")

            # ヘッダーの表示 (Flags列を追加)
            # L: Illuminance, E: Env(Temp/Hum/CO2), A: Anemometer(Vel/Vol)
            # Temp[C]=生データ, Tcorr[C]=熱影響補正後(KF), Hcorr[%]=補正後相対湿度
            print("Time     | Illum[lx] | Temp[C] | Tcorr[C] | Hum[%] | Hcorr[%] | Vel[m/s] | Vol[V] | CO2[ppm] | Flags")
            print("---------|-----------|---------|----------|--------|----------|----------|--------|----------|-------")

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

                    # 補正後温度 (補正無効・温湿度無効時は None)
                    tc = data.corrected_temperature
                    tc_str = f"{tc:8.2f}" if tc is not None else "    --  "
                    hc = data.corrected_humidity
                    hc_str = f"{hc:8.2f}" if hc is not None else "    --  "

                    print(f"{time_str} | {data.illuminance:9.1f} | {data.temperature:7.2f} | {tc_str} | {data.humidity:6.2f} | {hc_str} | {data.velocity:8.2f} | {data.voltage:6.3f} | {data.co2:8d} | {flags_str}")
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