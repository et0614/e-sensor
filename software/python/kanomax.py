"""
Kanomax 6543 (6501系 Climomaster) 風速計リーダ

E-Sensor と同じシナリオで風洞の“真風速”を記録するための基準器クライアント。
e_sensor.py と似た体裁（connect / start / get_data / stop / close）にしてある。

確定仕様（実機 2026-08-17）:
  - USB→仮想COM（ポート名に "KANOMAX" / "65xx"）。★ボーレート 19200, 8N1
    （Device Manager表示の9600は“PCポート既定”で本体設定とは別物）。
  - コマンド（大文字＋CR）: "D####" で ####個(最大9999)を1秒ごとに出力 / "N" 停止。
  - 応答: 先頭 "AD"(ACK) → 以降 "  0.00;  25.1;0000000"
          （';'区切り・空白詰め・'\r\n'終端 = 風速[m/s]; 気温[℃]; ステータス7桁）。
  - 更新 1 Hz。9999個(≈2.7h)を使い切ったら自動で再要求する。

使い方:
    from kanomax import KanomaxClient
    k = KanomaxClient()          # port 省略で自動検出
    k.connect(); k.start()
    d = k.get_data(timeout=2)    # 最新の KanomaxData（古ければ None）
    print(d.velocity, d.temperature)
    k.stop(); k.close()

  もしくは with 文:
    with KanomaxClient() as k:
        print(k.get_data(timeout=2))

  単体実行（動作確認 / CSVログ）:
    py kanomax.py            # 連続表示（Ctrl-Cで停止）
    py kanomax.py 10         # 10秒表示して終了
    py kanomax.py 60 log.csv # 60秒、CSVに追記保存
"""
import sys
import time
import threading
from dataclasses import dataclass
from typing import Optional, Callable

import serial
from serial.tools import list_ports


DEFAULT_PORT = "COM59"  # 自動検出できないとき使う既定ポート（この機体固定）
DEFAULT_BAUD = 19200
STALE_S = 3.0            # 最新値がこの秒数より古ければ無効(None)扱い
REISSUE_IDLE_S = 3.0     # 無音がこの秒数続いたら D9999 を再送（9999消化/ハングアップ対策）

# 注意: Kanomax本体は電源OFFで仮想COMごと消える（オートパワーオフ）。長時間ランでは
# 本体のオートパワーオフを無効化 or ACアダプタ運用にすること。


@dataclass
class KanomaxData:
    velocity: float          # m/s
    temperature: float       # ℃
    status: str              # 生ステータス文字列（例 "0000000"）
    timestamp: float         # time.time()


def find_port() -> Optional[str]:
    """ポート名/説明に KANOMAX or 65xx を含むCOMを探す。無ければ None。"""
    for p in list_ports.comports():
        hay = " ".join(filter(None, (p.description, p.manufacturer,
                                     getattr(p, "product", None), p.device))).upper()
        if "KANOMAX" in hay or "65XX" in hay or "CLIMO" in hay:
            return p.device
    return None


def _parse(line: str, ts: float) -> Optional[KanomaxData]:
    """'  0.00;  25.1;0000000' → KanomaxData / 失敗時 None"""
    parts = [p.strip() for p in line.split(";")]
    if len(parts) < 2:
        return None
    try:
        vel = float(parts[0])
        temp = float(parts[1])
    except ValueError:
        return None
    status = parts[2] if len(parts) > 2 else ""
    return KanomaxData(vel, temp, status, ts)


class KanomaxClient:
    def __init__(self, port: Optional[str] = None, baud: int = DEFAULT_BAUD,
                 stale_s: float = STALE_S,
                 on_data: Optional[Callable[[KanomaxData], None]] = None):
        """port を省略すると自動検出（KANOMAX/65xx を含むCOM）。"""
        self.port = port
        self.baud = baud
        self.stale_s = stale_s
        self.on_data = on_data
        self._ser: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[KanomaxData] = None
        self._last_rx = 0.0
        self.last_error: Optional[str] = None

    # ---- 接続 ----
    def connect(self) -> bool:
        """シリアルを開く。port 省略時は自動検出。成功で True。"""
        # 自動検出→ダメなら既定ポート。Kanomaxドライバは pyserial/SERIALCOMM に
        # 出ないことがあるため、既定ポートへのフォールバックを持つ。
        port = self.port or find_port() or DEFAULT_PORT
        try:
            self._ser = serial.Serial(port, self.baud, bytesize=8,
                                      parity=serial.PARITY_NONE, stopbits=1,
                                      timeout=1.0)
        except Exception as e:
            self.last_error = (f"{port} を開けません: {e} "
                               "／本体の電源が入っているか確認（オートパワーオフで"
                               "COMごと消えることがある）。")
            print("Error:", self.last_error, file=sys.stderr)
            return False
        self.port = port
        try:
            self._ser.dtr = True
            self._ser.rts = True
        except Exception:
            pass
        time.sleep(0.2)
        self._ser.reset_input_buffer()
        return True

    # ---- ストリーム開始/停止 ----
    def start(self):
        """1秒ごとの連続出力を開始し、受信スレッドで最新値を保持する。"""
        if not self._ser:
            raise RuntimeError("connect() を先に呼んでください。")
        self._stop.clear()
        self._request_stream()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self):
        """受信スレッドを止め、本体へ停止コマンドを送る。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._send(b"N\r")

    def close(self):
        try:
            self.stop()
        except Exception:
            pass
        if self._ser:
            try:
                self._ser.close()
            finally:
                self._ser = None

    # ---- データ取得 ----
    def get_data(self, timeout: Optional[float] = None) -> Optional[KanomaxData]:
        """最新の KanomaxData を返す。stale_s より古ければ None。
        timeout を与えると、新鮮な値が来るまで最大 timeout 秒待つ。"""
        deadline = (time.time() + timeout) if timeout else None
        while True:
            with self._lock:
                d = self._latest
            if d is not None and (time.time() - d.timestamp) <= self.stale_s:
                return d
            if deadline is None or time.time() >= deadline:
                return None
            time.sleep(0.02)

    def flush(self):
        """保持中の最新値を破棄し、受信バッファもクリアする。"""
        with self._lock:
            self._latest = None
        if self._ser:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass

    # ---- 内部 ----
    def _send(self, data: bytes):
        if self._ser:
            try:
                self._ser.write(data)
                self._ser.flush()
            except Exception as e:
                self.last_error = str(e)

    def _request_stream(self):
        if self._ser:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass
        self._send(b"D9999\r")     # 1秒ごとに最大9999データ
        self._last_rx = time.time()

    def _reader(self):
        while not self._stop.is_set():
            try:
                raw = self._ser.readline()          # '\r\n' 終端
            except Exception as e:
                self.last_error = str(e)
                break
            now = time.time()
            if not raw:
                # 無音が続く＝9999消化 or ハングアップ → 再要求
                if now - self._last_rx > REISSUE_IDLE_S:
                    self._request_stream()
                continue
            line = raw.decode("ascii", "ignore").strip()
            if not line or line.startswith("A"):    # "AD"/"AN"/"AS"/"AU" などACK
                continue
            d = _parse(line, now)
            if d is None:
                continue
            self._last_rx = now
            with self._lock:
                self._latest = d
            if self.on_data:
                try:
                    self.on_data(d)
                except Exception:
                    pass

    # ---- with 文 ----
    def __enter__(self):
        if not self.connect():
            raise RuntimeError(self.last_error or "connect失敗")
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


# ============================================
# 単体実行: 動作確認 / CSVログ
# ============================================
def _main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else None
    csv_path = sys.argv[2] if len(sys.argv) > 2 else None

    k = KanomaxClient()
    if not k.connect():
        return 1
    print(f"connected: {k.port} @ {k.baud}bps "
          + (f"/ {duration}s" if duration else "/ Ctrl-C to stop")
          + (f" / log -> {csv_path}" if csv_path else ""))
    k.start()

    fh = None
    if csv_path:
        fh = open(csv_path, "a", encoding="utf-8", newline="")
        if fh.tell() == 0:
            fh.write("timestamp,iso,velocity_mps,temperature_c,status\n")

    t0 = time.time()
    last_ts = 0.0
    try:
        while True:
            if duration and (time.time() - t0) >= duration:
                break
            d = k.get_data(timeout=2.0)
            if d is None:
                print("(no fresh data)")
                continue
            if d.timestamp == last_ts:     # 同じ1Hzサンプルの重複は出さない
                time.sleep(0.05)
                continue
            last_ts = d.timestamp
            print(f"{time.strftime('%H:%M:%S', time.localtime(d.timestamp))}  "
                  f"v={d.velocity:5.2f} m/s  T={d.temperature:4.1f} C  st={d.status}")
            if fh:
                iso = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(d.timestamp))
                fh.write(f"{d.timestamp:.3f},{iso},{d.velocity},{d.temperature},{d.status}\n")
                fh.flush()
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if fh:
            fh.close()
        k.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
