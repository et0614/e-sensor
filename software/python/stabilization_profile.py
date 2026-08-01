"""
安定化時間プロファイル計測スクリプト。

校正 (calibrate_coefficients) と同じ順番・同じファン出力で風速を切り替え、
各点で電圧を1秒毎に記録する。目的は「各風速で電圧が定常に達するまでの時間」を
可視化し、風速ごとに必要な安定化時間を見極めること（高風速ほど短くて済むはず）。

- 風速の系列・ファン出力は calibrate_coefficients.CALIBRATION_POINTS を使う。
  計測順は --order で選ぶ:
    desc（既定・校正と同じ）: 高→低（最後に 0 m/s）。ファン起動キックが無く、
        低速点を「上から接近」した整定時間を観測できる。先頭の高風速点で
        コールド起動サージも観測できる。
    asc: 低→高（先頭 0 m/s）。0 m/s のコールド立ち上がりを観測したいとき。
- 各点の保持時間は一律 STABILIZATION（既定 25s）。1秒毎に電圧・風速を記録。

出力:
    calibration_data/stab_profile/stab_{id}_{order}_{YYYYmmdd_HHMMSS}.csv / .png
    PNG は上段=通し時間の電圧波形（点の切替を破線で表示）、
          下段=各点を「点開始からの経過秒」で重ね、各点の定常値からの残差[mV]を
               プロット（0 に収束するまでの時間＝その風速の安定化時間）。

使い方:
    python stabilization_profile.py            # desc（校正と同じ降順）
    python stabilization_profile.py --order asc
    python stabilization_profile.py --duration 25
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
from calibrate_coefficients import CALIBRATION_POINTS, CALIBRATOR_ID


# ============================================
# 既定値
# ============================================
STABILIZATION   = 25        # 各点の保持（安定化観測）時間[s]。一旦すべて 25s。
SAMPLE_INTERVAL = 1.0       # サンプリング周期[s]

OUTPUT_DIR = Path(__file__).resolve().parent / "calibration_data" / "stab_profile"


def main():
    ap = argparse.ArgumentParser(description="安定化時間プロファイル（校正と同順）")
    ap.add_argument("--duration", type=int, default=STABILIZATION,
                    help=f"各点の保持時間[s]（既定 {STABILIZATION}）")
    ap.add_argument("--order", choices=["desc", "asc"], default="desc",
                    help="計測順: desc=高→低(校正と同じ, 既定) / asc=低→高")
    ap.add_argument("--no-plot", action="store_true", help="グラフ表示を省略")
    args = ap.parse_args()

    # 計測順の点列。校正は降順(高→低)で計測するので既定は desc。
    # desc なら最初が高風速点になり、ファン起動キックが無く、コールド起動サージも
    # τの小さい高風速で数秒に収まる（＝低速点を「上から接近」した整定を観測できる）。
    points_seq = sorted(CALIBRATION_POINTS, key=lambda p: p["ref_velocity"],
                        reverse=(args.order == "desc"))

    print("=" * 60)
    print(f" 安定化プロファイル  Calibrator #{CALIBRATOR_ID}  各点 {args.duration}s  order={args.order}")
    print("  系列:", " → ".join(f"{p['ref_velocity']}m/s({p['fan_power']}%)"
                                 for p in points_seq))
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

        try:
            input("\n準備ができたら Enter（中止は Ctrl+C）: ")
        except EOFError:
            pass

        client.start_measurement()
        fan.set_power(0)
        # 風速計は起動時OFF。ここでONし、そのまま最初の点の記録に入る。
        # desc(既定)なら最初は高風速点なので、コールド起動サージが数秒で収まる様子も
        # 併せて観測できる。asc なら先頭 0 m/s の立ち上がりを観測する。
        client.start_velocity()

        print(f"\n{'glob[s]':>7} | {'step':>14} | {'step[s]':>7} | {'V[mV]':>8} | {'vel[m/s]':>8} | flags")
        print("-" * 66)

        t_global0 = time.time()
        for idx, pt in enumerate(points_seq):
            pwr, ref_v = pt["fan_power"], pt["ref_velocity"]
            fan.set_power(pwr)
            t_step0 = time.time()
            next_t = t_step0
            while time.time() - t_step0 < args.duration:
                d = client.get_data(timeout=0.5)
                now = time.time()
                if d:
                    rows.append({
                        "global_s": round(now - t_global0, 2),
                        "step_idx": idx,
                        "fan_power": pwr,
                        "ref_velocity": ref_v,
                        "step_s": round(now - t_step0, 2),
                        "voltage_mV": round(d.voltage * 1000, 1),
                        "velocity_mps": round(d.velocity, 3),
                        "anemo_valid": int(d.anemo_valid),
                        "env_valid": int(d.env_valid),
                    })
                    flags = ("A" if d.anemo_valid else "-") + ("E" if d.env_valid else "-")
                    print(f"{now-t_global0:7.1f} | {ref_v:>6}m/s({pwr:>3}%) | "
                          f"{now-t_step0:7.1f} | {d.voltage*1000:8.1f} | {d.velocity:8.3f} | [{flags}]")
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
    stem = f"stab_{device_id}_{args.order}_{stamp}"
    csv_path = OUTPUT_DIR / f"{stem}.csv"
    png_path = OUTPUT_DIR / f"{stem}.png"

    fieldnames = ["global_s", "step_idx", "fan_power", "ref_velocity", "step_s",
                  "voltage_mV", "velocity_mps", "anemo_valid", "env_valid"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# device_id: {device_id}\n")
        f.write(f"# calibrator_id: {CALIBRATOR_ID}\n")
        f.write(f"# per_point_duration_s: {args.duration}\n")
        f.write(f"# order: {args.order}\n")
        f.write(f"# recorded_at: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n")
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV : {csv_path}")

    # ---- グラフ（上: 通し波形 / 下: 各点の定常値からの残差） ----
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 9))

    # 上段: 通し時間の電圧波形＋点の切替線
    ax1.plot([r["global_s"] for r in rows], [r["voltage_mV"] for r in rows], "-")
    for idx, pt in enumerate(points_seq):
        seg = [r["global_s"] for r in rows if r["step_idx"] == idx]
        if not seg:
            continue
        ax1.axvline(seg[0], color="gray", ls="--", lw=0.8)
        ax1.text(seg[0], ax1.get_ylim()[1], f" {pt['ref_velocity']}m/s\n {pt['fan_power']}%",
                 va="top", ha="left", fontsize=8, color="gray")
    ax1.set_xlabel("Global elapsed [s]")
    ax1.set_ylabel("Voltage [mV]")
    ax1.set_title(f"Stabilization profile  Device {device_id}  (each {args.duration}s)")
    ax1.grid(True)

    # 下段: 各点を「点開始からの経過」で重ね、その点の定常値(末尾3点平均)からの残差
    for idx, pt in enumerate(points_seq):
        seg = [r for r in rows if r["step_idx"] == idx]
        if len(seg) < 2:
            continue
        finals = [r["voltage_mV"] for r in seg[-3:]]
        v_final = sum(finals) / len(finals)
        xs = [r["step_s"] for r in seg]
        ys = [r["voltage_mV"] - v_final for r in seg]
        ax2.plot(xs, ys, "-o", ms=3, label=f"{pt['ref_velocity']}m/s ({pt['fan_power']}%)")
    ax2.axhline(0, color="k", lw=0.6)
    ax2.set_xlabel("Elapsed since step start [s]")
    ax2.set_ylabel("Voltage − step final [mV]")
    ax2.set_title("Settling toward steady value (0 = settled)")
    ax2.grid(True)
    ax2.legend(fontsize=8)

    fig.tight_layout()
    fig.savefig(png_path)
    print(f"Plot: {png_path}")

    if not args.no_plot:
        print("\nClose the plot window to exit.")
        plt.show()
    plt.close(fig)
    return 0


if __name__ == "__main__":
    sys.exit(main())
