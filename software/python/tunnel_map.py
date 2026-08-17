"""
風洞 静的マップ取得（Kanomax 基準）

Quadro ファンの各出力% における「真風速」を Kanomax(6543) で記録し、
  ファン出力 -> 真風速 の対応表 / 低速の安定性(乱れ強度) / 発停(ハンチング)閾値
を求める。後段の E-Sensor 動特性・再現性試験の“真値テーブル”になる。

前提/方針:
  - 単孔風洞のため E-Sensor とは同時計測不可 → まず Kanomax 単独で真値マップを取る。
  - 主戦場は室内域(<1.0 m/s)。★Kanomax の上限 5.0 m/s を超える出力は測らない
    （FAN_LEVELS を v<=5.0 になる範囲に収める。既定 max=55% ≒ 約3.8 m/s）。
  - 低出力(〜7%以下)はファンが発停する見込み → 6〜9% を細かく掃引し std で閾値を見る。
  - 昇順→降順で1往復（ヒステリシス/熱ドリフト検出）。各段は長めに保持。

使い方:
  py tunnel_map.py           # 本番（約1時間・無人）
  py tunnel_map.py quick     # 動作確認（短時間・少数点）

出力: tunnel_map_data/tunnel_map_YYYYMMDD_HHMMSS.csv / _plot.png（.gitignore 済み）

注意: Kanomax 本体のオートパワーオフでCOMが消える。長時間ランは本体設定でオートパワーオフを
      無効化 or ACアダプタ運用にすること。
"""
import sys
import time
import csv
import statistics
import datetime
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

from quadro_fan_controller import QuadroFanController
from kanomax import KanomaxClient

# ==========================================
# 設定
# ==========================================
FAN_INDEX = 1                 # この風洞のファン番号（Quadro fan1..4）

KANOMAX_MAX_MPS = 5.0         # Kanomax の測定上限（参考値。プロット注記に使用）。

# 昇順掃引で真風速がこれ以上に達したら、それ以上のファン出力は測らない。
# 5.0ちょうど〜少しオーバー(≒5.1)で打ち切るための閾値（高出力側は曲線が読めないので
# 固定fanでなく“到達した真風速”で打ち切る）。
STOP_ABOVE_MPS = 5.0

# 掃引するファン出力[%]（昇順）。低速を密に、上は 5.0m/s 到達まで（STOP_ABOVE_MPSで自動打切り）。
# 実測(2026-08-17): 4%は激しい発停(0.5⇄5.5m/s)で使用不可、6%は安定(≒0.13m/s) → 下限は 6%。
# 高出力側(50/60/68/73/78)は 5.0m/s に達した段で打ち切られる（全部は測らない）。
FAN_LEVELS = [0, 6, 7, 8, 10, 12, 14, 16, 18, 21, 25, 30, 40, 50, 60, 68, 73, 78]

# 各段の保持[s]（安定化 settle + 計測 meas）。低速ほど長く。無人前提。
DWELL = {          # (fan出力の上限, settle_s, meas_s) を小さい順に
    12:  (90, 90),   # 低速(〜0.5 m/s 付近)
    25:  (60, 60),   # 中速
    999: (40, 40),   # 高速
}
TIME_SCALE = 1.0   # まとめて伸縮したいとき（例 0.5 で半分）

WARMUP_SECONDS = 60           # 本掃引前にこの出力で暖機（0で無効）
WARMUP_FAN_POWER = 21

SHOW_PLOT = True
OUTPUT_DIR = Path(__file__).resolve().parent / "tunnel_map_data"

# quick モード（配線・通信の動作確認用）
QUICK_LEVELS = [0, 8, 12, 21, 40]
QUICK_DWELL = (8, 8)


# ==========================================
# ヘルパ
# ==========================================
def dwell_for(power, quick):
    if quick:
        s, m = QUICK_DWELL
    else:
        for lim, (s, m) in sorted(DWELL.items()):
            if power <= lim:
                break
        s, m = s * TIME_SCALE, m * TIME_SCALE
    return s, m


def collect(kano, seconds, live_label=None):
    """seconds 秒、Kanomax の新鮮サンプル(1Hz)を集めて返す。応答が無ければ警告。"""
    samples = []
    last_ts = 0.0
    t0 = time.time()
    while time.time() - t0 < seconds:
        d = kano.get_data(timeout=2.0)
        if d is None:
            print("  ! Kanomax応答なし（電源/オートパワーオフ/接続を確認）")
            time.sleep(0.3)
            continue
        if d.timestamp == last_ts:
            time.sleep(0.05)
            continue
        last_ts = d.timestamp
        samples.append(d)
        if live_label:
            print(f"  [{live_label}] v={d.velocity:5.2f} m/s  T={d.temperature:4.1f} C")
    return samples


def measure_level(fan, kano, power, direction, quick):
    """1つのファン出力で settle→measure し、統計行を返す。"""
    settle_s, meas_s = dwell_for(power, quick)
    print(f"\n== Fan {power}% ({direction}) settle{settle_s:.0f}s / meas{meas_s:.0f}s ==")
    fan.set_power(power, FAN_INDEX)
    collect(kano, settle_s, live_label=f"settle {power}%")   # settle分は捨てる
    samples = collect(kano, meas_s)                          # 計測分
    if not samples:
        return None
    vels = [s.velocity for s in samples]
    temps = [s.temperature for s in samples]
    row = {
        "fan_power": power,
        "direction": direction,
        "v_mean": statistics.mean(vels),
        "v_std": statistics.stdev(vels) if len(vels) > 1 else 0.0,
        "v_min": min(vels),
        "v_max": max(vels),
        "n": len(vels),
        "temp_mean": statistics.mean(temps),
        "t_end": time.time(),
    }
    ti = row["v_std"] / row["v_mean"] * 100 if row["v_mean"] > 0.05 else float("nan")
    print(f"  -> v={row['v_mean']:.3f} m/s (std {row['v_std']:.3f}, "
          f"乱れ {ti:4.1f}%, n={row['n']})")
    if row["v_mean"] > KANOMAX_MAX_MPS:
        print(f"  ! 注意: {row['v_mean']:.2f} m/s は Kanomax上限({KANOMAX_MAX_MPS})超。以降は無意味。")
    return row


# ==========================================
# 出力
# ==========================================
def write_csv(path, rows, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for k, v in meta.items():
            w.writerow([f"# {k}", v])
        w.writerow(["fan_power", "direction", "v_mean", "v_std", "v_min", "v_max",
                    "n", "temp_mean"])
        for r in rows:
            w.writerow([r["fan_power"], r["direction"],
                        f'{r["v_mean"]:.4f}', f'{r["v_std"]:.4f}',
                        f'{r["v_min"]:.4f}', f'{r["v_max"]:.4f}',
                        r["n"], f'{r["temp_mean"]:.2f}'])


def make_plot(path, rows, device_id):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    for direction, marker in (("up", "o"), ("down", "x")):
        rs = [r for r in rows if r["direction"] == direction]
        if not rs:
            continue
        x = [r["fan_power"] for r in rs]
        y = [r["v_mean"] for r in rs]
        e = [r["v_std"] for r in rs]
        ax1.errorbar(x, y, yerr=e, marker=marker, capsize=3, label=direction)
        ti = [(r["v_std"] / r["v_mean"] * 100 if r["v_mean"] > 0.05 else np.nan) for r in rs]
        ax2.plot(x, ti, marker=marker, label=direction)
    ax1.axhline(KANOMAX_MAX_MPS, color="r", ls="--", lw=0.8, label=f"Kanomax max {KANOMAX_MAX_MPS}")
    ax1.set_xlabel("Fan power [%]"); ax1.set_ylabel("True velocity [m/s]")
    ax1.set_title("Fan power -> true velocity"); ax1.grid(True); ax1.legend()
    ax2.set_xlabel("Fan power [%]"); ax2.set_ylabel("Turbulence std/mean [%]")
    ax2.set_title("Steadiness (low-end hunting shows as spikes)"); ax2.grid(True); ax2.legend()
    fig.suptitle(f"Wind tunnel static map (Kanomax): {device_id}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    print(f"Plot saved: {path}")
    if SHOW_PLOT:
        plt.show()
    plt.close(fig)


def summarize(rows):
    print("\n===== 風洞マップ サマリ =====")
    print(" fan%  dir   v[m/s]   std     乱れ%   n")
    for r in rows:
        ti = r["v_std"] / r["v_mean"] * 100 if r["v_mean"] > 0.05 else float("nan")
        print(f" {r['fan_power']:>4} {r['direction']:>4}  {r['v_mean']:6.3f}  "
              f"{r['v_std']:5.3f}  {ti:5.1f}  {r['n']:>3}")
    # 発停閾値の目安: 乱れ% が跳ねる最小fan
    ups = [r for r in rows if r["direction"] == "up" and r["fan_power"] > 0]
    noisy = [r for r in ups if r["v_mean"] > 0.05 and (r["v_std"] / r["v_mean"]) > 0.15]
    if noisy:
        print(f"\n 乱れ>15%（発停疑い）の最小fan: {min(n['fan_power'] for n in noisy)}% 付近")


# ==========================================
# メイン
# ==========================================
def main():
    quick = len(sys.argv) > 1 and sys.argv[1].lower() == "quick"
    levels = QUICK_LEVELS if quick else FAN_LEVELS

    kano = KanomaxClient()
    if not kano.connect():
        return 1
    kano.start()
    fan = QuadroFanController()

    rows = []
    device_id = kano.port or "kanomax"
    try:
        # 通信確認
        if kano.get_data(timeout=5) is None:
            print("Kanomaxからデータが来ません。中止。", file=sys.stderr)
            return 1

        fan.set_power(0, FAN_INDEX)
        if WARMUP_SECONDS > 0 and not quick:
            print(f"暖機 {WARMUP_SECONDS}s (Fan {WARMUP_FAN_POWER}%)...")
            fan.set_power(WARMUP_FAN_POWER, FAN_INDEX)
            collect(kano, WARMUP_SECONDS)

        print(f"\n掃引レベル: {levels}" + ("  [QUICK]" if quick else ""))
        # 昇順（真風速が STOP_ABOVE_MPS に達したら以降の上位fanは省略）
        achieved = []
        for power in levels:
            r = measure_level(fan, kano, power, "up", quick)
            if r is not None:
                rows.append(r)
                achieved.append(power)
                if r["v_mean"] >= STOP_ABOVE_MPS:
                    print(f"  → {r['v_mean']:.2f} m/s が上限目標({STOP_ABOVE_MPS})到達。"
                          f"これ以上のファン出力は測定しない。")
                    break
        # 降順（実際に測った段を逆順で）
        for power in reversed(achieved):
            r = measure_level(fan, kano, power, "down", quick)
            if r is not None:
                rows.append(r)
    except KeyboardInterrupt:
        print("\n[中断] ここまでを保存します。")
    finally:
        try:
            fan.set_power(0, FAN_INDEX)
        except Exception:
            pass
        kano.close()

    if not rows:
        print("データなし。", file=sys.stderr)
        return 1

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    meta = {
        "device_id": device_id,
        "scanned_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "fan_index": FAN_INDEX,
        "kanomax_max_mps": KANOMAX_MAX_MPS,
        "quick": quick,
        "note": "Kanomax-only wind-tunnel static map (fan power -> true velocity)",
    }
    csv_path = OUTPUT_DIR / f"tunnel_map_{ts}.csv"
    png_path = OUTPUT_DIR / f"tunnel_map_{ts}_plot.png"
    write_csv(csv_path, rows, meta)
    print(f"\nCSV saved: {csv_path}")
    summarize(rows)
    try:
        make_plot(png_path, rows, device_id)
    except Exception as e:
        print(f"(プロット失敗: {e}。CSVは保存済み)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
