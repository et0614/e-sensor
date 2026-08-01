import time
import statistics
import math
import datetime
import io
import json
import base64
import os
import threading
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

# 自作ライブラリ
from e_sensor import ESensorClient, SensorData
from quadro_fan_controller import QuadroFanController

# ==========================================
# 定数・設定値
# ==========================================
# 使用する風洞（Calibrator）を 1 または 2 で指定する。
# 風洞ごとにファン power と基準風速の対応が異なるため、ここを切り替えるだけで
# 校正・検証の両方の点群が一括で差し替わる。
CALIBRATOR_ID = 1

CALIBRATOR_PROFILES = {
    1: {
        "calibration_points": [
            {"fan_power": 0,  "ref_velocity": 0.00},
            {"fan_power": 8,  "ref_velocity": 0.23},
            {"fan_power": 12, "ref_velocity": 0.52},
            {"fan_power": 40, "ref_velocity": 2.76},
            {"fan_power": 69, "ref_velocity": 5.00},
        ],
        "validation_points": [
            {"fan_power": 0,  "ref_velocity": 0.00},   # 再現性（下端）
            {"fan_power": 10, "ref_velocity": 0.37},   # 補間 Range A
            {"fan_power": 21, "ref_velocity": 1.24},   # 補間 Range B
            {"fan_power": 53, "ref_velocity": 3.75},   # 補間 Range C
            {"fan_power": 69, "ref_velocity": 5.00},   # 再現性（上端）
        ],
    },
    2: {
        "calibration_points": [
            {"fan_power": 0,  "ref_velocity": 0.00},
            {"fan_power": 8,  "ref_velocity": 0.23},
            {"fan_power": 12, "ref_velocity": 0.50},
            {"fan_power": 38, "ref_velocity": 2.73},
            {"fan_power": 67, "ref_velocity": 5.03},
        ],
        "validation_points": [
            {"fan_power": 0,  "ref_velocity": 0.00},   # 再現性（下端）
            {"fan_power": 10, "ref_velocity": 0.32},   # 補間 Range A
            {"fan_power": 19, "ref_velocity": 1.09},   # 補間 Range B
            {"fan_power": 51, "ref_velocity": 3.80},   # 補間 Range C
            {"fan_power": 67, "ref_velocity": 5.03},   # 再現性（上端）
        ],
    },
}

if CALIBRATOR_ID not in CALIBRATOR_PROFILES:
    raise ValueError(
        f"CALIBRATOR_ID={CALIBRATOR_ID} は未定義です。"
        f"利用可能: {sorted(CALIBRATOR_PROFILES.keys())}"
    )

CALIBRATION_POINTS = CALIBRATOR_PROFILES[CALIBRATOR_ID]["calibration_points"]
VALIDATION_POINTS  = CALIBRATOR_PROFILES[CALIBRATOR_ID]["validation_points"]

# 計測窓（各点で平均を取る時間）。安定化待機の後にこの秒数だけ計測する。
# 高風速は std が小さい(数 mV)ので短く、低風速・無風は環境ノイズが大きいので
# 長めに平均する（校正のみ風速依存。検証は既に短いので 5s 一律）。
CAL_MEAS_HIGH_WIND = 5               # 校正: 高風速(>= MEAS_HIGH_WIND_THRESHOLD)の計測窓[s]
CAL_MEAS_LOW_WIND  = 10             # 校正: 低風速・無風の計測窓[s]
MEAS_HIGH_WIND_THRESHOLD = 2.0     # これ以上を「高風速(低ノイズ)」とみなす[m/s]
VAL_MEASUREMENT_DURATION = 5         # 検証: 一律

# 安定化待機は風速帯ごとに設定（降順計測前提）。stab_profile(F1A96622, 降順60s)の
# 実測整定に個体差のゆとりを載せた値。最終値(57s)からのズレの実測:
#   5.0/2.5: 12sで±2mV        → 10s
#   0.41   : 15sで+7mV, 20sで+1mV（2.5からの大ステップで最遅）→ 20s
#   0.23   : 12sで+3mV         → 20s帯に含める(ゆとり)
#   0.0    : 20sで+13mV, 25sで+4.5mV（無風は緩慢な尾を引く）→ 25s
# (ref_v 下限[m/s], 安定化[s]) を大きい順に並べ、最初に該当した帯を採用する。
STAB_BANDS = [
    (2.0, 10),   # 高風速
    (0.8, 15),   # ~1 m/s 帯(1.06 等。未実測のため内挿でゆとり)
    (0.1, 20),   # 低風速(0.1〜0.8): 最遅 0.41=20s に合わせる
    (0.0, 25),   # 0 m/s: 無風の緩慢な整定
]

# 0 m/s での電圧がこの値を下回ったら異常と判定する [V]。
# 典型的な無風電圧は数百 mV のオーダー（係数 A[0] のデフォルトは 0.462 V）。
# 100 mV を下回るのは、ブリッジ回路が正しく起動していない／断線等の疑いがある。
ABNORMAL_NO_WIND_VOLTAGE = 0.100

# 校正成果物 (JSON / PNG) の出力先。スクリプトと同階層ではなく、この
# ディレクトリ配下にまとめて出力する（cwd に依らず __file__ 基準）。
OUTPUT_DIR = Path(__file__).resolve().parent / "calibration_data"

# 検証フェーズ (Phase 3) の最大誤差がこの値[%]以下なら合格とみなす
# （JSON の "pass" 判定に使用。0 m/s 点は誤差計算対象外）。
VERIFY_ERROR_THRESHOLD_PCT = 10.0

# ==========================================
# 通知（ビープ）
# ==========================================
def notify_done():
    """校正完了をビープ音で通知する（Windows: winsound、他: BEL 文字）。"""
    try:
        import winsound
        winsound.Beep(880, 200)
        winsound.Beep(1320, 300)
    except Exception:
        print('\a', end='', flush=True)

def notify_abnormal():
    """異常検知時の警告ビープ。下降音 × 3 回で注意を引く。"""
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1500, 180)
            winsound.Beep(700, 220)
    except Exception:
        for _ in range(3):
            print('\a', end='', flush=True)
            time.sleep(0.15)

# ==========================================
# 計算ロジック
# ==========================================
def calculate_kings_law_params(v1, e1, v2, e2, e0):
    """2点のデータからKingの法則のパラメータCとmを算出する"""
    x1 = math.log(max(1e-6, e1**2 - e0**2))
    y1 = math.log(v1)
    x2 = math.log(max(1e-6, e2**2 - e0**2))
    y2 = math.log(v2)
    
    m = (y2 - y1) / (x2 - x1)
    ln_c = y1 - m * x1
    return math.exp(ln_c), m

def stabilization_time(ref_v):
    """風速帯(STAB_BANDS)から安定化待機時間[s]を返す（個体差のゆとり込み）。
    降順計測（高→低）を前提とした実測整定に基づく。ファンを0から起動しない
    ため起動キックは無く、低風速ほど熱整定が緩慢（特に 0 m/s）である点を反映。"""
    for vmin, t in STAB_BANDS:
        if ref_v >= vmin:
            return t
    return STAB_BANDS[-1][1]

def measurement_duration(ref_v):
    """校正の計測窓[s]を返す。高風速は低ノイズ(std 数mV)なので短く、低風速・
    無風は環境ノイズが大きいので長めに平均する。"""
    return CAL_MEAS_HIGH_WIND if ref_v >= MEAS_HIGH_WIND_THRESHOLD else CAL_MEAS_LOW_WIND

# ==========================================
# 校正メインクラス
# ==========================================
# 進捗バーの更新粒度[s]。整定待ちをこの刻みで小分けし、経過時間に比例して
# バーを滑らかに進める(合計待ち時間は time.sleep(duration) と等価＝校正の
# タイミング・精度には影響しない)。
PROGRESS_TICK = 0.5

# 複数風洞を並行実行すると generate_report の matplotlib(pyplot 大域状態)が
# 競合しうるため、プロット生成はこのロックで直列化する。
_REPORT_LOCK = threading.Lock()


class AnemometerCalibrator:
    def __init__(self, midi_in=None, midi_out=None, fan_index=1,
                 calibration_points=None, validation_points=None,
                 calibrator_id=CALIBRATOR_ID,
                 on_progress=None, on_abnormal=None, should_cancel=None):
        """
        引数を省略すると従来通り（名前先頭一致で接続・fan1・モジュールの
        既定プロファイル・対話プロンプト）動作する。複数風洞GUIからは以下を渡す:

          midi_in / midi_out : 対象個体の MIDI ポート名（esensor_discovery で確定）。
                               両方指定時は open_ports で開く（名前先頭一致を使わない）。
          fan_index          : この風洞のファン番号（Quadro fan1..4）。
          calibration_points / validation_points : この風洞のプロファイル点群。
          calibrator_id      : レポートに残す風洞ID。
          on_progress(msg, frac) : 進捗通知（frac は 0..1 または None）。
          on_abnormal(voltage)->bool : 0m/s 異常時の続行可否（True=続行）。
                               未指定時は従来の input() プロンプト。
          should_cancel()->bool : True で校正を中断（抜線・ユーザ中止用）。
        """
        self.client = ESensorClient()
        self.fan = QuadroFanController()
        self.device_id = "UNKNOWN"
        self.midi_in = midi_in
        self.midi_out = midi_out
        self.fan_index = fan_index
        self.calibration_points = (calibration_points
                                   if calibration_points is not None else CALIBRATION_POINTS)
        self.validation_points = (validation_points
                                  if validation_points is not None else VALIDATION_POINTS)
        self.calibrator_id = calibrator_id
        self.on_progress = on_progress
        self.on_abnormal = on_abnormal
        self.should_cancel = should_cancel


    def _progress(self, message, frac=None):
        """進捗をコールバック（あれば）＋標準出力へ流す。"""
        if self.on_progress:
            try:
                self.on_progress(message, frac)
            except Exception:
                pass
        if message:               # frac のみのティック更新では標準出力しない
            print(message)


    def _cancelled(self) -> bool:
        return bool(self.should_cancel and self.should_cancel())


    def _sleep_ticking(self, duration, base_w, total_w) -> bool:
        """duration 秒を PROGRESS_TICK 刻みで待ちつつ、経過時間に比例して進捗バーを
        進める(メッセージ無し=printしない)。合計待ち時間は time.sleep(duration) と
        等価。キャンセルされたら False を返す(呼び出し側で中断)。"""
        t0 = time.time()
        while True:
            el = time.time() - t0
            if el >= duration:
                return True
            if self._cancelled():
                return False
            self._progress(None, (base_w + el) / total_w)
            time.sleep(min(PROGRESS_TICK, duration - el))


    def wait_for_data(self, timeout=2.0) -> SensorData:
        """データが届くまで待機するヘルパー"""
        data = self.client.get_data(timeout=timeout)
        if data:
            return data
        raise TimeoutError("Device not responding.")
    

    def run_calibration(self, show_plot: bool = True) -> bool:
        """校正・書き込み・検証を実行する。

        Args:
            show_plot: True なら最後に確認用グラフを表示してブロックする。
                       一括実行スクリプトから呼ぶ場合は False（非対話）にする。

        Returns:
            True  = 正常終了（係数書き込み・レポート出力まで完了）
            False = 接続失敗、または異常検知でユーザーが中止した
        """
        connected = (self.client.open_ports(self.midi_in, self.midi_out)
                     if self.midi_in and self.midi_out else self.client.connect())
        if not connected:
            print("Error: Could not connect to E-Sensor.")
            return False

        try:
            hash_id = self.client.get_device_id()
            if not hash_id:
                print("Warning: Could not retrieve Device ID.")
                hash_id = "UNKNOWN"

            self.client.start_measurement()

            # 風速計は電源投入時 OFF（5V遮断）。明示的に起動する。起動後 約5秒の
            # 予熱を経て電圧/風速が有効化される（最初の安定化待機に含まれる）。
            # これを呼ばないと voltage=0.0V・anemo_valid=False のままになる。
            self.client.start_velocity()

            # トレーサビリティ用に FW バージョンと校正時の周囲環境を記録する。
            # King の法則は空気密度（気温）に依存するため、後日の参照用に残す。
            version = self.client.get_version()
            fw_str = f"{version[0]}.{version[1]}.{version[2]}" if version else "unknown"
            ambient_t = ambient_h = None
            try:
                amb = self.wait_for_data()
                if amb and amb.env_valid:
                    ambient_t, ambient_h = amb.temperature, amb.humidity
            except TimeoutError:
                pass

            # 進捗の時間重み: 各ステップ(整定+計測)の秒数を見積り、経過時間に比例して
            # バーを進める。合計はほぼ実測と一致し、ステップ長の差も反映される。
            W_PHASE2 = 2.0
            w_total = (sum(stabilization_time(p["ref_velocity"])
                           + measurement_duration(p["ref_velocity"])
                           for p in self.calibration_points)
                       + W_PHASE2
                       + sum(stabilization_time(p["ref_velocity"])
                             + VAL_MEASUREMENT_DURATION
                             for p in self.validation_points))
            done_w = 0.0

            # Phase 1: データ収集
            self._progress("=== Phase 1: Data Collection ===", 0.0)
            phase1_results = []

            # 降順（高風速→低風速、最後に 0 m/s）で計測する。理由:
            #  - ファンを0から起動しないので、起動キックの気流スパイクを回避できる。
            #  - 風速計ONのコールド起動サージが、τの小さい高風速点で数秒に収まる
            #    （0 m/s で起きると 20 秒級のサージになる）。
            #  - 遅い整定が必要なのは最後の 0 m/s だけになる。
            # フィット（Phase 2）は昇順（index0=0 m/s）前提なので、計測後に並べ替える。
            cal_points = sorted(self.calibration_points,
                                key=lambda p: p["ref_velocity"], reverse=True)
            n_cal = len(cal_points)
            for i, pt in enumerate(cal_points):
                if self._cancelled():
                    print("\n[Cancel] Phase 1 中断。")
                    return False
                pwr, ref_v = pt["fan_power"], pt["ref_velocity"]
                stab = stabilization_time(ref_v)
                meas = measurement_duration(ref_v)
                self._progress(
                    f"Phase1 {i+1}/{n_cal}: {ref_v} m/s (Fan {pwr}%) 整定{stab}s/計測{meas}s",
                    done_w / w_total)

                self.fan.set_power(pwr, self.fan_index)
                # 整定(小刻みに進捗を進める)
                if not self._sleep_ticking(stab, done_w, w_total):
                    print("\n[Cancel] Phase 1 中断。")
                    return False
                self.client.flush() # 非定常時のデータは捨てる

                # 計測(1s サンプリングしつつ経過に比例して進捗)
                samples = []
                mstart = time.time()
                while time.time() - mstart < meas:
                    data = self.client.get_data(timeout=0.5)
                    if data and data.anemo_valid:
                        samples.append(data.voltage)
                    self._progress(None, (done_w + stab
                                          + min(time.time() - mstart, meas)) / w_total)
                    time.sleep(1.0)
                
                avg_vol = statistics.mean(samples) if samples else 0
                std_dev = statistics.stdev(samples) if len(samples) > 1 else 0
                print(f"Result: {avg_vol*1000:.1f} mV (StdDev: {std_dev*1000:.1f})")
                phase1_results.append({"fan_power": pwr, "ref_velocity": ref_v, "measured_avg": avg_vol, "std_dev": std_dev})
                done_w += stab + meas

            # 計測は降順で行ったので、以降のフィット・出力のため風速昇順に並べ替える
            # （e0 = e_volts[0] が 0 m/s、レンジも昇順であることを担保する）。
            phase1_results.sort(key=lambda r: r["ref_velocity"])

            # Phase 1.5: 異常電圧チェック（0 m/s 電圧が異常に低くないか）
            zero_wind = next((r for r in phase1_results if r["ref_velocity"] == 0.0), None)
            if zero_wind is not None and zero_wind["measured_avg"] < ABNORMAL_NO_WIND_VOLTAGE:
                # ファンを止めてから警告
                self.fan.set_power(0, self.fan_index)
                notify_abnormal()
                print("\n" + "!" * 64)
                print(f"!! WARNING: 0 m/s での電圧が異常に低い: "
                      f"{zero_wind['measured_avg']*1000:.1f} mV "
                      f"(基準: > {ABNORMAL_NO_WIND_VOLTAGE*1000:.0f} mV)")
                print("!! 風速計回路の不具合（ブリッジ未起動・断線等）が疑われます。")
                print("!! このまま続行すると不正な係数が書き込まれます。")
                print("!! 一旦中止してデバイスを確認・再接続のうえ再校正することを推奨します。")
                print("!" * 64)
                if self.on_abnormal is not None:
                    cont = bool(self.on_abnormal(zero_wind["measured_avg"]))
                else:
                    ans = input("\nそれでも続行しますか? 続行するには 'yes' と入力 / それ以外で中止: ").strip().lower()
                    cont = (ans == "yes")
                if not cont:
                    print("\n校正を中止しました。デバイスを確認のうえ、再度実行してください。")
                    return False

            # Phase 2: 係数計算と書き込み
            self._progress("=== Phase 2: Fitting & Writing ===", done_w / w_total)
            e_volts = [r["measured_avg"] for r in phase1_results]
            v_speeds = [r["ref_velocity"] for r in phase1_results]
            e0 = e_volts[0]

            c1, m1 = calculate_kings_law_params(v_speeds[1], e_volts[1], v_speeds[2], e_volts[2], e0)
            c2, m2 = calculate_kings_law_params(v_speeds[2], e_volts[2], v_speeds[3], e_volts[3], e0)
            c3, m3 = calculate_kings_law_params(v_speeds[3], e_volts[3], v_speeds[4], e_volts[4], e0)

            # Coef A: [E0, m1, lnC1, m2, lnC2]
            coef_a = [float(e0), float(m1), float(math.log(c1)), float(m2), float(math.log(c2))]
            # Coef B: [m3, lnC3, v_split1, v_split2, 0.0]
            coef_b = [float(m3), float(math.log(c3)), float(v_speeds[2]), float(v_speeds[3]), 0.0]

            print(f"Writing Coef A: {coef_a}")
            self.client.write_coefficients(coef_a, type_a=True)
            time.sleep(0.5)
            print(f"Writing Coef B: {coef_b}")
            self.client.write_coefficients(coef_b, type_a=False)
            time.sleep(0.5)
            print("Write successful.")
            done_w += W_PHASE2

            # Phase 3: 検証
            self._progress("=== Phase 3: Verification ===", done_w / w_total)
            phase3_results = []
            # 検証も校正と同じ降順で計測（ファン起動キック回避・整定短縮のため）。
            val_points = sorted(self.validation_points,
                                key=lambda p: p["ref_velocity"], reverse=True)
            n_val = len(val_points)
            for i, pt in enumerate(val_points):
                if self._cancelled():
                    print("\n[Cancel] Phase 3 中断。")
                    return False
                pwr = pt["fan_power"]
                ref_v = pt["ref_velocity"]
                stab = stabilization_time(ref_v)
                self._progress(
                    f"Phase3 {i+1}/{n_val}: {ref_v} m/s (Fan {pwr}%) 整定{stab}s",
                    done_w / w_total)
                self.fan.set_power(pwr, self.fan_index)
                if not self._sleep_ticking(stab, done_w, w_total):
                    print("\n[Cancel] Phase 3 中断。")
                    return False
                self.client.flush() # 非定常時のデータは捨てる

                v_samples, vol_samples = [], []
                mstart = time.time()
                while time.time() - mstart < VAL_MEASUREMENT_DURATION:
                    data = self.client.get_data(timeout=0.5)
                    if data and data.anemo_valid:
                        v_samples.append(data.velocity)
                        vol_samples.append(data.voltage)
                    self._progress(None, (done_w + stab
                                          + min(time.time() - mstart, VAL_MEASUREMENT_DURATION)) / w_total)
                    time.sleep(1.0)
                done_w += stab + VAL_MEASUREMENT_DURATION

                avg_v = statistics.mean(v_samples) if v_samples else 0
                avg_vol = statistics.mean(vol_samples) if vol_samples else 0
                err = (abs(avg_v - pt["ref_velocity"]) / pt["ref_velocity"] * 100) if pt["ref_velocity"] > 0 else 0
                print(f"Ref: {pt['ref_velocity']:.2f} m/s -> Meas: {avg_v:.3f} m/s, {avg_vol*1000:.1f} mV (Err: {err:.1f}%)")
                phase3_results.append({"ref": pt["ref_velocity"], "meas": avg_v, "vol": avg_vol, "error": err})

            # 計測は降順だったので、出力（JSON/プロット）のため風速昇順に並べ替える。
            phase3_results.sort(key=lambda r: r["ref"])

            # レポート生成
            self.generate_report(phase1_results, coef_a, coef_b, phase3_results, hash_id,
                                 fw_version=fw_str, ambient_t=ambient_t, ambient_h=ambient_h,
                                 show_plot=show_plot)
            return True

        finally:
            self.fan.set_power(0, self.fan_index)
            self.client.stop_velocity()   # 自己発熱を止める（5V遮断）
            self.client.stop_measurement()
            self.client.close()

    def generate_report(self, p1, ca, cb, p3, id,
                        fw_version="unknown", ambient_t=None, ambient_h=None, show_plot=True):
        # 係数のアンパック
        e0 = ca[0]
        m1, ln_c1 = ca[1], ca[2]
        m2, ln_c2 = ca[3], ca[4]
        m3, ln_c3 = cb[0], cb[1]
        v_split1, v_split2 = cb[2], cb[3]

        # グラフ作成
        v_curve = np.linspace(0.01, 5.5, 200)
        vol_curve = []
        for v in v_curve:
            # 3区分による係数選択
            if v < v_split1:
                m, ln_c = m1, ln_c1
            elif v < v_split2:
                m, ln_c = m2, ln_c2
            else:
                m, ln_c = m3, ln_c3
            e_sq = e0**2 + np.exp((np.log(v) - ln_c) / m)
            vol_curve.append(np.sqrt(e_sq) * 1000)

        # pyplot は大域状態を持つため、複数風洞の並行実行に備えてロックで直列化する。
        # figure ハンドル(fig)を明示的に扱い、暗黙の「現在の図」に依存しない。
        with _REPORT_LOCK:
            fig, ax = plt.subplots(figsize=(8, 5))
            ax.plot(v_curve, vol_curve, 'r-', label="King's Law Fit", alpha=0.7)
            ax.scatter([r['ref_velocity'] for r in p1], [r['measured_avg']*1000 for r in p1], label='Reference')
            ax.scatter([r['ref'] for r in p3], [r['vol']*1000 for r in p3], marker='x', label='Verification')
            ax.set_xlabel('Velocity [m/s]'); ax.set_ylabel('Voltage [mV]'); ax.grid(True); ax.legend()

            # 出力先ディレクトリを用意（JSON / PNG をスクリプト直下ではなくここへ集約）
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            # ローカル確認用 PNG（FTP 対象外）
            img_path = OUTPUT_DIR / f"plot_{id}.png"
            fig.savefig(img_path)

            # JSON 同梱用に base64 化
            buf = io.BytesIO()
            fig.savefig(buf, format='png')
            buf.seek(0)
            plot_b64 = base64.b64encode(buf.read()).decode('ascii')
            # 図は最後に表示するため、ここでは閉じない

        # 検証フェーズの最大誤差と合否（0 m/s 点は誤差計算対象外）
        errs = [float(r["error"]) for r in p3 if r["ref"] > 0]
        max_err = max(errs) if errs else 0.0

        # 風速計の校正データ
        anemometer_data = {
            "calibrated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "firmware_version": fw_version,
            "calibrator_id": self.calibrator_id,
            "ambient_temp_c": round(float(ambient_t), 2) if ambient_t is not None else None,
            "ambient_humidity_pct": round(float(ambient_h), 2) if ambient_h is not None else None,
            "model": "kings_law_3range",
            "E0": round(float(e0), 6),
            "E0_mV": round(float(e0) * 1000, 1),
            "ranges": [
                {"v_min": 0.0,                    "v_max": round(float(v_split1), 4), "m": round(float(m1), 4), "lnC": round(float(ln_c1), 4)},
                {"v_min": round(float(v_split1), 4), "v_max": round(float(v_split2), 4), "m": round(float(m2), 4), "lnC": round(float(ln_c2), 4)},
                {"v_min": round(float(v_split2), 4), "v_max": None,                   "m": round(float(m3), 4), "lnC": round(float(ln_c3), 4)},
            ],
            # フィットに用いた実測点（電圧とばらつき）。std_dev はセンサ/流れの
            # 安定性の指標で、大きい場合は不良や気流乱れの疑い。
            "calibration_points": [
                {
                    "ref_velocity": round(float(r["ref_velocity"]), 2),
                    "voltage_mV": round(float(r["measured_avg"]) * 1000, 1),
                    "std_dev_mV": round(float(r["std_dev"]) * 1000, 1),
                }
                for r in p1
            ],
            "verification": [
                {
                    "ref_velocity": round(float(r['ref']), 2),
                    "measured_velocity": round(float(r['meas']), 3),
                    "error_pct": round(float(r['error']), 1),
                    "voltage_mV": round(float(r['vol'] * 1000), 1),
                }
                for r in p3
            ],
            "max_error_pct": round(float(max_err), 1),
            "pass": bool(max_err <= VERIFY_ERROR_THRESHOLD_PCT),
            "plot": {
                "format": "image/png",
                "data_url": f"data:image/png;base64,{plot_b64}",
            },
        }

        # 既存 JSON があればマージ（将来 CO2 等の校正を別キーで追記できるように）
        json_path = OUTPUT_DIR / f"{id}.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        else:
            doc = {"schema_version": 1, "device_id": id, "calibrations": {}}

        doc.setdefault("calibrations", {})
        doc["calibrations"]["anemometer"] = anemometer_data

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

        print(f"\nReport generated: {json_path}")
        print(f"Plot saved      : {img_path}")
        print(f"Verification    : max error {max_err:.1f}% "
              f"({'PASS' if anemometer_data['pass'] else 'FAIL'}, "
              f"threshold {VERIFY_ERROR_THRESHOLD_PCT:.0f}%)")

        # 完了通知（ビープ）
        notify_done()

        # 確認用にグラフを表示（ウィンドウを閉じるまでブロック）。一括実行時は表示しない。
        with _REPORT_LOCK:
            if show_plot:
                print("\nClose the plot window to exit.")
                plt.show()
            plt.close(fig)

if __name__ == "__main__":
    AnemometerCalibrator().run_calibration()