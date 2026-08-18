"""
E-Sensor 性能評価 ― 駆動＋記録（1本）

決められたファン運転スケジュールを実行しながら、接続中のセンサ（Kanomax または
E-Sensor）を最速で時刻付き記録する。単孔風洞のため、同じスケジュールを2回流す：

  py perf_run.py kanomax        # 1回目: 基準（真風速の時系列）
  （プローブ交換）
  py perf_run.py esensor        # 2回目: 被試験

  py perf_run.py esensor quick  # 短縮版（配線/動作確認）

出力: perf_data/perf_{sensor}_{YYYYMMDD_HHMMSS}.csv（列: t_s, phase, fan_power,
      velocity, temperature, voltage, sensor）。両CSVを別スクリプトで t 整列して比較。

前提:
  - FAN_INDEX を評価する風洞のQuadroチャンネルに合わせる（センサも同じ風洞に置く）。
  - 向き固定（面を風上）。設定は使える下限 fan8%(≒0.22 m/s)以上・室内域中心。
  - Kanomax は 1Hz、E-Sensor は要求/応答でそれより速い。動特性はステップ多数回の
    アンサンブル平均で分解（スケジュールに反復ステップを内蔵）。
  - Kanomax 本体のオートパワーオフは無効化 or ACアダプタ（長時間ラン対策）。
"""
import sys
import time
import csv
import datetime
from pathlib import Path

from quadro_fan_controller import QuadroFanController

FAN_INDEX = 1                 # 評価する風洞のQuadroチャンネル（センサも同じ風洞へ）
OUTPUT_DIR = Path(__file__).resolve().parent / "perf_data"

# ---- スケジュール定義（ファン出力%、時間s）----
WARMUP_POWER, WARMUP_S = 21, 120        # 暖機（解析では捨てる）
STAIR_UP   = [0, 8, 12, 16, 21, 30, 40] # 階段 昇順（≈0,0.22,0.48,0.75,1.1,1.85,2.5 m/s）
STAIR_DOWN = [30, 21, 16, 12, 8, 0]     # 階段 降順（ヒステリシス）
STAIR_DWELL = 60
STEP_LO, STEP_HI, STEP_CYCLES, STEP_DWELL = 8, 21, 8, 30   # 反復ステップ 0.22⇄1.1
HOLD_LOW  = (12, 300)                    # 低速保持（ノイズ/ドリフト）
HOLD_VLOW = (8, 180)                     # 最低速保持
HOLD_ZERO = (0, 180)                     # ゼロ保持
RAMP_LO, RAMP_HI, RAMP_S = 8, 40, 180    # 緩ランプ（各方向）

# quick（短縮）
Q_STAIR = [0, 8, 21, 40]
Q_DWELL = 8
Q_STEP_CYCLES, Q_STEP_DWELL = 2, 8


def build_schedule(quick):
    """(kind, a, b, label) の並びを返す。kind='hold'→(power,dur), 'ramp'→(p0,p1)。"""
    segs = []
    if quick:
        segs.append(("hold", WARMUP_POWER, 15, "warmup"))
        for p in Q_STAIR:
            segs.append(("hold", p, Q_DWELL, "stair_up"))
        for _ in range(Q_STEP_CYCLES):
            segs.append(("hold", STEP_LO, Q_STEP_DWELL, "step_lo"))
            segs.append(("hold", STEP_HI, Q_STEP_DWELL, "step_hi"))
        segs.append(("hold", 12, 12, "hold_low"))
        segs.append(("hold", 0, 10, "hold_zero"))
        segs.append(("ramp", 8, 40, 20, "ramp_up"))
        return segs

    segs.append(("hold", WARMUP_POWER, WARMUP_S, "warmup"))
    for p in STAIR_UP:
        segs.append(("hold", p, STAIR_DWELL, "stair_up"))
    for p in STAIR_DOWN:
        segs.append(("hold", p, STAIR_DWELL, "stair_down"))
    for _ in range(STEP_CYCLES):
        segs.append(("hold", STEP_LO, STEP_DWELL, "step_lo"))
        segs.append(("hold", STEP_HI, STEP_DWELL, "step_hi"))
    segs.append(("hold", HOLD_LOW[0],  HOLD_LOW[1],  "hold_low"))
    segs.append(("hold", HOLD_VLOW[0], HOLD_VLOW[1], "hold_vlow"))
    segs.append(("hold", HOLD_ZERO[0], HOLD_ZERO[1], "hold_zero"))
    segs.append(("ramp", RAMP_LO, RAMP_HI, RAMP_S, "ramp_up"))
    segs.append(("ramp", RAMP_HI, RAMP_LO, RAMP_S, "ramp_down"))
    return segs


def expand(segs):
    """各セグメントに絶対開始/終了時刻を付与したリストを返す。"""
    out = []
    t = 0.0
    for s in segs:
        if s[0] == "hold":
            _, power, dur, label = s
            out.append({"t0": t, "t1": t + dur, "kind": "hold",
                        "power": power, "label": label})
        else:  # ramp
            _, p0, p1, dur, label = s
            out.append({"t0": t, "t1": t + dur, "kind": "ramp",
                        "p0": p0, "p1": p1, "label": label})
        t += s[2] if s[0] == "hold" else s[3]
    return out, t


def power_and_label(expanded, t):
    """時刻 t における (fan_power[int], phase_label)。終端超過なら (None, 'end')。"""
    for seg in expanded:
        if seg["t0"] <= t < seg["t1"]:
            if seg["kind"] == "hold":
                return seg["power"], seg["label"]
            frac = (t - seg["t0"]) / (seg["t1"] - seg["t0"])
            return int(round(seg["p0"] + (seg["p1"] - seg["p0"]) * frac)), seg["label"]
    return None, "end"


# ============================================
# センサ接続（Kanomax / E-Sensor）
# ============================================
def open_sensor(kind):
    """(read_fn, close_fn, meta) を返す。read_fn()->(vel,temp,voltage) or None。"""
    if kind == "kanomax":
        from kanomax import KanomaxClient
        k = KanomaxClient()
        if not k.connect():
            return None
        k.start()
        if k.get_data(timeout=5) is None:
            print("Kanomaxからデータが来ません。", file=sys.stderr)
            k.close()
            return None
        state = {"last_ts": None}

        def read():
            d = k.get_data(timeout=0)          # 最新（非ブロッキング, 1Hz更新）
            if d is None or d.timestamp == state["last_ts"]:
                return None                     # 新規サンプルのみ
            state["last_ts"] = d.timestamp
            return (d.velocity, d.temperature, None)

        return read, k.close, {"device": k.port or "kanomax", "native_hz": "~1"}

    elif kind == "esensor":
        from e_sensor import ESensorClient
        c = ESensorClient()
        if not c.connect():
            print("E-Sensorに接続できません。", file=sys.stderr)
            return None
        c.start_measurement()
        c.start_velocity()                      # 起動後 約5秒予熱
        dev = c.get_device_id() or "esensor"
        # 予熱待ち: anemo_valid が立つまで（最大15s）
        t0 = time.time()
        while time.time() - t0 < 15:
            d = c.get_data(timeout=0.5)
            if d and d.anemo_valid:
                break

        # get_data はデバイス未更新でもキャッシュ値を返す（実測: 生~70Hzの94%が重複）。
        # (velocity,voltage) が変わったときだけ = 新規測定だけ記録する（実効 ~4-5Hz）。
        last = {"key": None}

        def read():
            d = c.get_data(timeout=0.3)         # 要求/応答（デバイス最速でポーリング）
            if d is None or not d.anemo_valid:
                return None
            key = (d.velocity, d.voltage)
            if key == last["key"]:
                return None                     # 未更新（キャッシュ重複）はログしない
            last["key"] = key
            return (d.velocity, d.temperature, d.voltage)

        def close():
            try:
                c.stop_velocity()
                c.stop_measurement()
            except Exception:
                pass
            c.close()

        return read, close, {"device": dev, "native_hz": ">1"}

    return None


# ============================================
# メイン
# ============================================
def main():
    args = [a.lower() for a in sys.argv[1:]]
    kind = next((a for a in args if a in ("kanomax", "esensor")), None)
    quick = "quick" in args
    if kind is None:
        print("使い方: py perf_run.py <kanomax|esensor> [quick]", file=sys.stderr)
        return 2

    expanded, total_s = expand(build_schedule(quick))
    print(f"スケジュール総時間: {total_s:.0f}s ({total_s/60:.1f}min)"
          + ("  [QUICK]" if quick else ""))

    opened = open_sensor(kind)
    if opened is None:
        return 1
    read, close_sensor, meta = opened
    fan = QuadroFanController()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"perf_{kind}_{ts}.csv"

    n = 0
    current_power = None
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([f"# sensor", kind])
            w.writerow([f"# device", meta["device"]])
            w.writerow([f"# fan_index", FAN_INDEX])
            w.writerow([f"# started_at", datetime.datetime.now().astimezone().isoformat(timespec="seconds")])
            w.writerow([f"# schedule_total_s", f"{total_s:.0f}"])
            w.writerow([f"# quick", quick])
            w.writerow(["t_s", "phase", "fan_power", "velocity", "temperature", "voltage", "sensor"])

            t_start = time.time()
            while True:
                t = time.time() - t_start
                power, phase = power_and_label(expanded, t)
                if power is None:               # スケジュール終了
                    break
                if power != current_power:      # 指令が変わったらファン更新
                    fan.set_power(power, FAN_INDEX)
                    current_power = power

                sample = read()                 # 1サンプル（無ければ None）
                if sample is not None:
                    vel, temp, volt = sample
                    w.writerow([f"{t:.3f}", phase, power,
                                "" if vel is None else f"{vel:.4f}",
                                "" if temp is None else f"{temp:.2f}",
                                "" if volt is None else f"{volt:.5f}",
                                kind])
                    n += 1
                    if n % 50 == 0:
                        f.flush()
                        print(f"  t={t:6.1f}s  {phase:10s} fan{power:>3}  v={vel:.3f}")

                if kind == "kanomax":
                    time.sleep(0.02)            # 1Hz更新なので軽くスリープ
                # E-Sensorは get_data がブロックしてペース調整するので sleep 不要
    except KeyboardInterrupt:
        print("\n[中断] ここまでを保存。")
    finally:
        try:
            fan.set_power(0, FAN_INDEX)
        except Exception:
            pass
        close_sensor()

    print(f"\n保存: {csv_path}  ({n} サンプル)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
