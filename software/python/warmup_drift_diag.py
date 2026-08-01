"""
風速計ゼロ点（0 m/s 電圧）の暖機ドリフト診断スクリプト。

目的:
    無風時の電圧が「熱履歴」でどう変わるかを切り分ける。0 m/s の電圧を時間
    ログして、2 つの履歴で比較する:

      mode a (cold) : コールドスタート直後から 0 m/s を保持して記録
      mode b (preheat): まず高風速(既定 74% ≈5 m/s)に一定時間さらして基板を
                        温めてから 0 m/s に落として記録

    予想:
      - (b) が高い値から始まり (a) の漸近値へ減衰していく
          → 高風速が基板を温め、その熱が数分残って低風速を持ち上げる、が実証。
            真の定常値と時定数(τ)も同時に得られる。
      - (a)(b) が同じ漸近値へ収束
          → 違いは純粋に過渡(履歴)。狙うべき定常値/経過時間が決まる。

★重要（クールダウン）:
    (a) と (b) は連続実行してはならない。基板の熱時定数は無風で τ≈2 分あるため、
    直前の加熱が数分残る。各実行の前に、風速計OFFの状態で数分（目安 5〜10 分）
    放置し、基板を常温へ戻してから開始すること。常温へ戻っているかの確認は
    実施者の責任で行う（本スクリプトは温度監視をしない＝監視のための計測稼働
    自体がわずかな発熱要因になり、コールド状態を乱すのを避けるため）。

出力:
    calibration_data/warmup_diag/warmup_{id}_{mode}_{YYYYmmdd_HHMMSS}.csv / .png
    同一 ID で他モードの最新 CSV があれば、グラフに重ねて a/b を直接比較する。

使い方:
    python warmup_drift_diag.py a            # コールドスタート
    python warmup_drift_diag.py b            # 予熱→0 m/s
    python warmup_drift_diag.py a --duration 240
    python warmup_drift_diag.py b --preheat 45 --preheat-power 74
"""
import sys
import csv
import time
import argparse
import datetime
from pathlib import Path

import matplotlib.pyplot as plt

from e_sensor import ESensorClient
from quadro_fan_controller import QuadroFanController


# ============================================
# 既定値
# ============================================
PREHEAT_FAN_POWER = 74      # mode b の予熱ファン出力[%]（≈5 m/s）
PREHEAT_SECONDS   = 30      # mode b の予熱時間[s]
LOG_DURATION      = 180     # 0 m/s ロギング時間[s]（τ≈2分の数倍を見たい）
SAMPLE_INTERVAL   = 1.0     # サンプリング周期[s]

OUTPUT_DIR = Path(__file__).resolve().parent / "calibration_data" / "warmup_diag"


# ============================================
# ユーティリティ
# ============================================
def load_series(csv_path):
    """診断 CSV から (elapsed_s, voltage_mV) の列を読み出す（重ね描き用）。"""
    xs, ys = [], []
    with open(csv_path, "r", encoding="utf-8") as f:
        header = None
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if header is None:
                header = parts
                ix = header.index("elapsed_s")
                iy = header.index("voltage_mV")
                continue
            try:
                xs.append(float(parts[ix]))
                ys.append(float(parts[iy]))
            except (ValueError, IndexError):
                pass
    return xs, ys


def find_other_mode_csv(device_id, this_mode):
    """同一 ID・別モードの最新 CSV を探す（無ければ None）。"""
    other = "b" if this_mode == "a" else "a"
    cands = sorted(OUTPUT_DIR.glob(f"warmup_{device_id}_{other}_*.csv"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


# ============================================
# メイン
# ============================================
def main():
    ap = argparse.ArgumentParser(description="風速計ゼロ点の暖機ドリフト診断")
    ap.add_argument("mode", choices=["a", "b"],
                    help="a=コールドスタート / b=予熱してから0 m/s")
    ap.add_argument("--duration", type=int, default=LOG_DURATION,
                    help=f"0 m/s ロギング時間[s]（既定 {LOG_DURATION}）")
    ap.add_argument("--preheat", type=int, default=PREHEAT_SECONDS,
                    help=f"mode b の予熱時間[s]（既定 {PREHEAT_SECONDS}）")
    ap.add_argument("--preheat-power", type=int, default=PREHEAT_FAN_POWER,
                    help=f"mode b の予熱ファン出力[%%]（既定 {PREHEAT_FAN_POWER}）")
    ap.add_argument("--no-plot", action="store_true", help="グラフ表示を省略")
    args = ap.parse_args()

    print("=" * 60)
    print(f" 暖機ドリフト診断  mode={args.mode}"
          + (f" (preheat {args.preheat}s @ {args.preheat_power}%)" if args.mode == "b" else " (cold start)"))
    print(" ★ (a)(b) は基板が常温に戻ってから個別に実行すること")
    print("=" * 60)

    client = ESensorClient()
    if not client.connect():
        print("Error: Device 'E-Sensor' not found.", file=sys.stderr)
        return 1

    fan = QuadroFanController()
    rows = []
    device_id = "UNKNOWN"

    try:
        device_id = client.get_device_id() or "UNKNOWN"
        version = client.get_version()
        fw = f"{version[0]}.{version[1]}.{version[2]}" if version else "unknown"
        print(f"Device: {device_id} | FW: {fw}")

        # 常温復帰の確認は実施者の責任。温度監視はしない（監視のための計測稼働が
        # わずかな発熱要因になり、コールド状態を乱すのを避けるため）。計測ループは
        # 確認が済むまで開始しない。
        try:
            input("\n基板が常温に戻っていることを確認し、準備ができたら Enter（中止は Ctrl+C）: ")
        except EOFError:
            pass

        client.start_measurement()
        fan.set_power(0)   # 念のためファン停止（風速計はまだOFF）

        # mode b: 予熱（高風速）。velocity ON にして高風速へ。
        if args.mode == "b":
            print(f"\n[予熱] 風速計ON + ファン {args.preheat_power}% で {args.preheat}s 加熱します...")
            client.start_velocity()
            fan.set_power(args.preheat_power)
            time.sleep(args.preheat)
            fan.set_power(0)
            print("[予熱完了] 0 m/s へ移行してロギング開始。")
            t0 = time.time()            # 0 m/s に落とした瞬間を基準に
        else:
            # mode a: コールドスタート。velocity ON した瞬間を基準に（ファンは0）。
            print("\n[コールド] 風速計ON、0 m/s のままロギング開始。")
            client.start_velocity()
            t0 = time.time()

        # ロギング（0 m/s 保持）
        print(f"\n{'elapsed[s]':>10} | {'V[mV]':>8} | {'vel[m/s]':>8} | {'T[℃]':>6} | flags")
        print("-" * 52)
        next_t = t0
        while time.time() - t0 < args.duration:
            d = client.get_data(timeout=0.5)
            elapsed = time.time() - t0
            if d:
                rows.append({
                    "elapsed_s": round(elapsed, 2),
                    "voltage_mV": round(d.voltage * 1000, 1),
                    "velocity_mps": round(d.velocity, 3),
                    "temp_c": round(d.temperature, 2),
                    "hum_pct": round(d.humidity, 2),
                    "anemo_valid": int(d.anemo_valid),
                    "env_valid": int(d.env_valid),
                })
                flags = ("A" if d.anemo_valid else "-") + ("E" if d.env_valid else "-")
                print(f"{elapsed:10.1f} | {d.voltage*1000:8.1f} | {d.velocity:8.3f} | "
                      f"{d.temperature:6.2f} | [{flags}]")
            # 次サンプルまで（ドリフトを追うため厳密な周期に寄せる）
            next_t += SAMPLE_INTERVAL
            sleep_s = next_t - time.time()
            if sleep_s > 0:
                time.sleep(sleep_s)

    except KeyboardInterrupt:
        print("\n[中断] 取得済みデータまでを保存します。")
    finally:
        try:
            fan.set_power(0)
            client.stop_velocity()
            client.stop_measurement()
        except Exception:
            pass
        client.close()

    if not rows:
        print("データが取得できませんでした。")
        return 1

    # 保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"warmup_{device_id}_{args.mode}_{stamp}"
    csv_path = OUTPUT_DIR / f"{stem}.csv"
    png_path = OUTPUT_DIR / f"{stem}.png"

    fieldnames = ["elapsed_s", "voltage_mV", "velocity_mps", "temp_c",
                  "hum_pct", "anemo_valid", "env_valid"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# device_id: {device_id}\n")
        f.write(f"# mode: {args.mode}\n")
        f.write(f"# duration_s: {args.duration}\n")
        if args.mode == "b":
            f.write(f"# preheat_s: {args.preheat}\n")
            f.write(f"# preheat_power_pct: {args.preheat_power}\n")
        f.write(f"# recorded_at: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n")
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV : {csv_path}")

    # グラフ（電圧 vs 経過時間）。他モードの最新があれば重ねて a/b 比較。
    xs = [r["elapsed_s"] for r in rows]
    ys = [r["voltage_mV"] for r in rows]
    plt.figure(figsize=(9, 5))
    plt.plot(xs, ys, "-", label=f"mode {args.mode} (this run)")

    other = find_other_mode_csv(device_id, args.mode)
    if other is not None:
        ox, oy = load_series(other)
        if ox:
            other_mode = "b" if args.mode == "a" else "a"
            plt.plot(ox, oy, "--", alpha=0.8, label=f"mode {other_mode} ({other.name})")

    plt.xlabel("Elapsed at 0 m/s [s]")
    plt.ylabel("Anemometer voltage [mV]")
    plt.title(f"Warm-up drift @0 m/s  Device {device_id}")
    plt.grid(True)
    plt.legend()
    plt.savefig(png_path)
    print(f"Plot: {png_path}")

    v_start = ys[0]
    v_end = ys[-1]
    print(f"\n0 m/s 電圧: 開始 {v_start:.1f} mV → 終了 {v_end:.1f} mV "
          f"(変化 {v_end - v_start:+.1f} mV, {args.duration}s)")

    if not args.no_plot:
        print("\nClose the plot window to exit.")
        plt.show()
    plt.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
