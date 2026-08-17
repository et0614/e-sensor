"""
E-Sensor 風速検知部 指向性スキャン

校正済みの個体が「方位（水平角）ごとにどの風速を出力するか」を定量化する。
球状封止による無指向化の"改造前ベースライン"を取り、改造後と直接比較するための
記録専用ツール（★係数は一切書き込まない）。

手順（半自動）:
  1. 治具で検知部を回転軸中心に据え、0°（＝校正に使った角度）に合わせておく。
  2. 各角度で「校正用の点のみ」の風速を降順に自動掃引し、velocity と voltage を記録。
  3. 1角度が終わるとビープ＋ライブのポーラ図が更新される。判定は3択:
        [Enter] = +45°回して次角度へ   [r] = この角度を再計測   [q] = 中断
     （イレギュラーで計測が乱れたら [r] で治具を回さず測り直し、結果を差し替える）
  4. 45°刻みで360°（8方位）。最後に 0° へ戻して再測定（ドリフト/ヒステリシス確認）。
  5. CSV と最終ポーラ図（PNG）を出力。

計測窓・整定時間は校正(calibrate_coefficients)と同一関数を流用するため、校正時の
点と同条件＝比較可能。検証用（内挿）点は使わない（内挿性能は別問題のため）。

出力先: directionality_data/{device_id}_{timestamp}.csv / _polar.png（.gitignore 済み）
"""
import sys
import time
import csv
import statistics
import datetime
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

try:
    import msvcrt          # Windows: プロットを生かしたまま単キーで判定を受けるため
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

from e_sensor import ESensorClient
from quadro_fan_controller import QuadroFanController
# 校正と同一の整定/計測時間・完了ビープを流用（＝校正点と同条件で比較可能に）
from calibrate_coefficients import (
    CALIBRATOR_PROFILES,
    stabilization_time,
    measurement_duration,
    notify_done,
)

# ==========================================
# 設定値
# ==========================================
# 使用する風洞。校正で使ったのと同じ ID にする（点群が一致＝比較可能）。
CALIBRATOR_ID = 1

# この風洞のファン番号（Quadro fan1..4）
FAN_INDEX = 1

# 角度スイープ
ANGLE_STEP_DEG = 45          # 1 ステップの回転角
N_ANGLES = 8                 # 45°×8 = 360°（0,45,…,315）
DO_DRIFT_CHECK = True        # 最後に 0° へ戻して再測（系のドリフト確認）

# 各角度の"最初の点(最高風速)"にだけ足す追加整定[s]。角度切替中はファン0のため、
# 高風速へ跳ね上げる初回はやや整定が長めに欲しい場合に使う（0 で校正と完全同条件）。
EXTRA_SETTLE_FIRST_POINT_S = 0.0

# 本測定前の暖機（捨て運転）。前回 run で開始側が低く出る暖機不足ドリフトが見られたため、
# 風速計/風洞を熱平衡へ近づけてから計測に入る。0 で無効。
WARMUP_SECONDS = 120
WARMUP_FAN_POWER = None    # 暖機時のファン出力%。None なら scan 点の最大出力を使う。

# 校正点に加えて計測する追加点（校正点間のギャップ埋め）。CALIBRATOR_PROFILES 本体は
# 変更せず、この scan だけの点として足す。※CALIBRATOR_ID に整合する値にすること。
EXTRA_POINTS = [
    {"fan_power": 21, "ref_velocity": 1.12},   # 0.48〜2.54 の間（Range B 相当・風洞1, Kanomax実測）
]

# 個体指定（任意）。単体接続なら None のまま（名前先頭一致で1台に接続）。
#   MIDI_IN / MIDI_OUT : esensor_discovery で確定した特定個体の MIDI ポート名。
#   EXPECTED_DEVICE_ID : 接続先が本当にその個体かを get_device_id で照合（取り違え対策）。
MIDI_IN = None
MIDI_OUT = None
EXPECTED_DEVICE_ID = None

# ライブ/最終のポーラ図を表示する（False なら PNG 保存のみ・ヘッドレス可）
SHOW_PLOT = True

# 風速ポーラの半径軸を対数にする（低速側の異方性が読みやすい）。電圧側は常に線形。
# 対数軸は非正値を扱えないため、<=0 の風速点は描画時に除外する（0 m/s は元々対象外）。
LOG_VELOCITY_AXIS = True

OUTPUT_DIR = Path(__file__).resolve().parent / "directionality_data"


# ==========================================
# 小物
# ==========================================
def _fmt(x, nd=3):
    return "N/A" if x is None else f"{x:.{nd}f}"


def _sleep_pumping(seconds, fig):
    """seconds 待つ間もGUIイベントを処理し、プロット窓を「応答なし」にしない。
    flush_events はウィンドウを最前面に上げない（＝コンソールのフォーカスを奪わない）ので、
    キー入力(msvcrt)と両立する。fig が無ければ通常の sleep。計測窓は別途 time.time() で
    区切っているため、この処理は計測時間・精度に影響しない。"""
    if fig is None:
        time.sleep(seconds)
        return
    end = time.time() + seconds
    while True:
        rem = end - time.time()
        if rem <= 0:
            return
        try:
            fig.canvas.flush_events()
        except Exception:
            pass
        time.sleep(min(0.1, rem))


def ask_action(is_last: bool, next_target, fig=None) -> str:
    """1角度の計測後の判定。ビープしてから 'next' / 'redo' / 'abort' を返す。
    GUI表示中(Windows)は input() でブロックせず、プロットを生かしたまま単キーで受ける
    （[Enter]/[r]/[q]）。それ以外は通常の行入力にフォールバック。"""
    notify_done()  # 「この角度が終わった」の合図
    if is_last:
        prompt = ("\n>>> ビープ。全方位完了。[Enter]=終了 / "
                  "[r]=この角度を再計測 / [q]=中断 : ")
    else:
        prompt = (f"\n>>> ビープ。[Enter]=+{ANGLE_STEP_DEG}°回して{next_target}°へ / "
                  f"[r]=この角度を再計測（治具は回さない）/ [q]=中断 : ")
    print(prompt, end="", flush=True)

    # GUI表示中: プロットのイベントを回しながら単キー待ち（窓は閉じなくてよい）
    if _HAS_MSVCRT and fig is not None:
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print(); return "next"
                if ch in ("r", "R"):
                    print("r"); return "redo"
                if ch in ("q", "Q", "\x03"):   # q / Ctrl-C
                    print("q"); return "abort"
                # 未対応キーは無視して待機継続
            try:
                fig.canvas.flush_events()
            except Exception:
                pass
            time.sleep(0.05)

    # フォールバック（非Windows / 図なし）: 通常の行入力
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "abort"
    if ans == "r":
        return "redo"
    if ans == "q":
        return "abort"
    return "next"


# ==========================================
# 1角度ぶんの自動計測（校正 Phase1 と同じ降順スイープ・記録のみ）
# ==========================================
def measure_angle(client, fan, cal_desc, angle_deg, label, session_index, fig=None):
    """cal_desc（風速降順の校正点）を1点ずつ計測し、行のリストを返す。
    各点で velocity と voltage を約1Hzでサンプルし平均・標準偏差を取る。
    fig を渡すと待機中もGUIイベントを回し、プロット窓の「応答なし」を防ぐ。"""
    rows = []
    n = len(cal_desc)
    print(f"\n=== 角度 {label} の計測開始 ===")
    for i, pt in enumerate(cal_desc):
        pwr = pt["fan_power"]
        ref_v = pt["ref_velocity"]
        stab = stabilization_time(ref_v) + (EXTRA_SETTLE_FIRST_POINT_S if i == 0 else 0.0)
        meas = measurement_duration(ref_v)
        print(f"  [{label}] {i+1}/{n}: {ref_v:.2f} m/s (Fan {pwr}%) "
              f"整定{stab:.0f}s / 計測{meas}s")

        fan.set_power(pwr, FAN_INDEX)
        _sleep_pumping(stab, fig)   # 整定待ち（GUIを生かす）
        client.flush()  # 非定常時のデータは捨てる

        v_samples, vol_samples = [], []
        t0 = time.time()
        while time.time() - t0 < meas:
            d = client.get_data(timeout=0.5)
            if d and d.anemo_valid:
                v_samples.append(d.velocity)
                vol_samples.append(d.voltage)
            _sleep_pumping(1.0, fig)   # 1Hz サンプリング刻み（GUIを生かす）

        v_mean = statistics.mean(v_samples) if v_samples else None
        v_std = statistics.stdev(v_samples) if len(v_samples) > 1 else 0.0
        vol_mean = statistics.mean(vol_samples) if vol_samples else None
        vol_std = statistics.stdev(vol_samples) if len(vol_samples) > 1 else 0.0

        vol_mv = None if vol_mean is None else vol_mean * 1000.0
        print(f"      -> v={_fmt(v_mean)} m/s (sd {_fmt(v_std)}), "
              f"{_fmt(vol_mv, 1)} mV, n={len(v_samples)}")

        rows.append({
            "session_index": session_index,
            "angle_deg": angle_deg,
            "angle_label": label,
            "fan_power": pwr,
            "ref_velocity": ref_v,
            "meas_velocity_mean": v_mean,
            "meas_velocity_std": v_std,
            "voltage_mV_mean": vol_mv,
            "voltage_mV_std": vol_std * 1000.0,
            "n_samples": len(v_samples),
        })
    # 降順スイープの最終点は 0 m/s（Fan 0）なので、角度終了時にはファンは停止済み。
    return rows


# ==========================================
# ポーラ図（ライブ更新＆最終保存で共用）
# ==========================================
def new_polar_fig():
    fig = plt.figure(figsize=(13, 6))
    ax1 = fig.add_subplot(1, 2, 1, projection="polar")   # 風速 [m/s]
    ax2 = fig.add_subplot(1, 2, 2, projection="polar")   # 電圧 [mV]
    return fig, ax1, ax2


def render_polar(fig, ax1, ax2, all_rows, primary_angles, speeds, device_id):
    """現時点までの生値（velocity/voltage）を方位別に描く。途中経過の確認に使う。
    8方位が揃った速度はリングを閉じ、途中は開いた折れ線で表示（＝どこまで測ったか一目瞭然）。"""
    N = len(primary_angles)
    ax1.clear()
    ax2.clear()
    for ref_v in speeds:
        if ref_v <= 0:      # 0 m/s は方位依存が無意味なので図では省略（CSV/集計には残る）
            continue
        rows = []
        for a in primary_angles:
            r = next((x for x in all_rows
                      if x["session_index"] < N and x["angle_deg"] == a
                      and x["ref_velocity"] == ref_v), None)
            rows.append((a, r))
        # 風速
        vp = [(a, r["meas_velocity_mean"]) for a, r in rows
              if r and r["meas_velocity_mean"] is not None]
        if vp:
            ang = [p[0] for p in vp]
            val = [p[1] for p in vp]
            if LOG_VELOCITY_AXIS:            # 対数軸: 非正値は描けないので nan にして除外
                val = [v if v > 0 else np.nan for v in val]
            if len(vp) == N:                 # 全周揃ったら閉じる
                ang, val = ang + [ang[0]], val + [val[0]]
            ax1.plot(np.radians(ang), val, marker="o", label=f"{ref_v:.2f} m/s")
        # 電圧
        volp = [(a, r["voltage_mV_mean"]) for a, r in rows
                if r and r["voltage_mV_mean"] is not None]
        if volp:
            ang = [p[0] for p in volp]
            val = [p[1] for p in volp]
            if len(volp) == N:
                ang, val = ang + [ang[0]], val + [val[0]]
            ax2.plot(np.radians(ang), val, marker="o", label=f"{ref_v:.2f} m/s")

    vel_title = "Velocity [m/s] (log r)" if LOG_VELOCITY_AXIS else "Velocity [m/s]"
    for ax, title in ((ax1, vel_title), (ax2, "Anemometer voltage [mV]")):
        ax.set_theta_zero_location("N")   # 0°を上に
        ax.set_theta_direction(-1)        # 時計回り（治具の回転向きに合わせる）
        ax.set_title(title)
        ax.grid(True)
    if LOG_VELOCITY_AXIS:
        # clear() で線形に戻るため毎回設定し直す。半径の対数軸化（低速側を拡大表示）。
        try:
            ax1.set_rscale("log")
        except Exception:
            pass
    if ax1.has_data():
        ax1.legend(loc="upper right", bbox_to_anchor=(1.28, 1.12), fontsize=8)
    fig.suptitle(f"Directionality scan: {device_id}")


def live_update(fig, ax1, ax2, *args):
    """ライブ表示を更新（描画に失敗しても計測は続行）。
    draw_idle + flush_events は窓を最前面に上げない＝コンソールのフォーカスを奪わない。"""
    try:
        render_polar(fig, ax1, ax2, *args)
        fig.canvas.draw_idle()
        fig.canvas.flush_events()
    except Exception as e:
        print(f"（ライブ描画をスキップ: {e}）", file=sys.stderr)


# ==========================================
# 出力（CSV / 集計 / ドリフト）
# ==========================================
def write_csv(path, all_rows, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["session_index", "angle_deg", "angle_label", "fan_power", "ref_velocity",
              "meas_velocity_mean", "meas_velocity_std",
              "voltage_mV_mean", "voltage_mV_std", "n_samples"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for k, v in meta.items():          # メタ（気温＝空気密度依存の King's law 参照用）
            w.writerow([f"# {k}", v])
        w.writerow(fields)
        for r in all_rows:
            w.writerow([
                r["session_index"], r["angle_deg"], r["angle_label"], r["fan_power"],
                f'{r["ref_velocity"]:.2f}',
                "" if r["meas_velocity_mean"] is None else f'{r["meas_velocity_mean"]:.4f}',
                f'{r["meas_velocity_std"]:.4f}',
                "" if r["voltage_mV_mean"] is None else f'{r["voltage_mV_mean"]:.1f}',
                f'{r["voltage_mV_std"]:.1f}',
                r["n_samples"],
            ])


def summarize(all_rows, primary_angles, speeds):
    """1次スイープ（8方位）の風速別ばらつきを表示する。"""
    N = len(primary_angles)
    print("\n===== 指向性サマリ（1次スイープ 8方位） =====")
    for ref_v in speeds:
        if ref_v <= 0:
            continue
        vs, vols = [], []
        for a in primary_angles:
            r = next((x for x in all_rows if x["session_index"] < N
                      and x["angle_deg"] == a and x["ref_velocity"] == ref_v), None)
            if r and r["meas_velocity_mean"] is not None:
                vs.append(r["meas_velocity_mean"])
            if r and r["voltage_mV_mean"] is not None:
                vols.append(r["voltage_mV_mean"])
        if vs:
            vmean = statistics.mean(vs)
            vspread = (max(vs) - min(vs)) / vmean * 100.0 if vmean else float("nan")
            volmean = statistics.mean(vols) if vols else float("nan")
            volspread = ((max(vols) - min(vols)) / volmean * 100.0
                         if vols and volmean else float("nan"))
            print(f"  {ref_v:>4.2f} m/s : v {min(vs):.3f}〜{max(vs):.3f} "
                  f"(mean {vmean:.3f}) 偏差 {vspread:5.1f}%  |  "
                  f"電圧偏差 {volspread:5.1f}%")


def drift_report(all_rows, primary_count, speeds):
    """開始 0°（session 0）と末尾ドリフト測（最終 session）を風速別に比較。"""
    last_idx = max(r["session_index"] for r in all_rows)
    if last_idx < primary_count:   # ドリフト測が無い（途中中断等）
        return
    print("\n===== ドリフト確認（開始0° vs 末尾0°） =====")
    for ref_v in speeds:
        r0 = next((x for x in all_rows if x["session_index"] == 0
                   and x["ref_velocity"] == ref_v), None)
        rd = next((x for x in all_rows if x["session_index"] == last_idx
                   and x["ref_velocity"] == ref_v), None)
        if (r0 and rd and r0["meas_velocity_mean"] is not None
                and rd["meas_velocity_mean"] is not None):
            d = rd["meas_velocity_mean"] - r0["meas_velocity_mean"]
            print(f"  {ref_v:>4.2f} m/s : {r0['meas_velocity_mean']:.3f} -> "
                  f"{rd['meas_velocity_mean']:.3f}  (Δ {d:+.3f} m/s)")


# ==========================================
# メイン
# ==========================================
def main():
    if CALIBRATOR_ID not in CALIBRATOR_PROFILES:
        print(f"CALIBRATOR_ID={CALIBRATOR_ID} は未定義です。", file=sys.stderr)
        return 1

    # 校正点 ＋ EXTRA_POINTS（ギャップ埋め）。検証用の点群は使わない。
    # 計測は降順、集計・図は昇順で扱う。同一風速は EXTRA_POINTS 側で上書き。
    base = CALIBRATOR_PROFILES[CALIBRATOR_ID]["calibration_points"]
    merged = {p["ref_velocity"]: p for p in base}
    for p in EXTRA_POINTS:
        merged[p["ref_velocity"]] = p
    scan_points = list(merged.values())
    cal_desc = sorted(scan_points, key=lambda p: p["ref_velocity"], reverse=True)
    speeds = sorted(merged.keys())
    primary_angles = [ANGLE_STEP_DEG * k for k in range(N_ANGLES)]  # 0,45,…,315

    client = ESensorClient()
    fan = QuadroFanController()

    connected = (client.open_ports(MIDI_IN, MIDI_OUT)
                 if MIDI_IN and MIDI_OUT else client.connect())
    if not connected:
        print("Error: E-Sensor に接続できませんでした。", file=sys.stderr)
        return 1

    # ライブ用の図（表示できない環境でも計測は続行）
    fig = ax1 = ax2 = None
    if SHOW_PLOT:
        try:
            plt.ion()
            fig, ax1, ax2 = new_polar_fig()
            plt.show(block=False)   # 窓を出しておく（以降 draw_idle/flush_events で更新）
        except Exception as e:
            print(f"（ライブ図を初期化できず、最後にまとめて描画します: {e}）", file=sys.stderr)
            fig = None

    all_rows = []
    device_id = "UNKNOWN"
    ambient_t = ambient_h = None
    try:
        device_id = client.get_device_id() or "UNKNOWN"
        if (EXPECTED_DEVICE_ID is not None and device_id != "UNKNOWN"
                and device_id.upper() != EXPECTED_DEVICE_ID.upper()):
            print(f"Error: 期待した個体 {EXPECTED_DEVICE_ID} ではなく {device_id} に接続。"
                  f"中止します。", file=sys.stderr)
            return 2

        version = client.get_version()
        fw_str = f"{version[0]}.{version[1]}.{version[2]}" if version else "unknown"

        fan.set_power(0, FAN_INDEX)     # 既知状態から開始
        client.start_measurement()
        # 風速計は起動時 OFF。明示起動し、以降スキャン中は入れっぱなし（角度毎の予熱待ち回避）。
        client.start_velocity()

        # 周囲環境（King's law は気温＝空気密度に依存するため記録）
        t0 = time.time()
        while time.time() - t0 < 8.0:
            d = client.get_data(timeout=1.0)
            if d and d.env_valid:
                ambient_t, ambient_h = d.temperature, d.humidity
                break

        print("\n" + "=" * 60)
        print(f" 指向性スキャン開始  device={device_id}  fw={fw_str}")
        print(f" 風洞={CALIBRATOR_ID}  風速点={speeds}  方位={primary_angles}"
              + ("（+末尾0°ドリフト）" if DO_DRIFT_CHECK else ""))
        print(" ※現在、治具が 0°（校正角）に合っていることを確認してください。")
        print("=" * 60)

        # 暖機（捨て運転）: 前回 run の「開始側が低い」暖機不足ドリフト対策。
        # scan 点の最高風速でしばらく回し、風速計/風洞を熱平衡へ近づける。
        if WARMUP_SECONDS > 0:
            wp = (WARMUP_FAN_POWER if WARMUP_FAN_POWER is not None
                  else max(p["fan_power"] for p in scan_points))
            print(f"\n暖機運転中… {WARMUP_SECONDS}s (Fan {wp}%)。"
                  f"熱平衡に達してから計測を開始します。")
            fan.set_power(wp, FAN_INDEX)
            _sleep_pumping(WARMUP_SECONDS, fig)
            # 0 には戻さない（最初の角度は最高風速からの降順なので、そのまま繋げる）

        # --- 計測プラン（累積角度）---
        plan = [(a, f"{a}°") for a in primary_angles]
        if DO_DRIFT_CHECK:
            plan.append((360, "360°(=0°/ドリフト確認)"))

        aborted = False
        for idx, (cum_angle, label) in enumerate(plan):
            is_last = (idx == len(plan) - 1)
            next_target = None if is_last else plan[idx + 1][0]
            while True:  # redo で同一角度を測り直せるループ
                rows = measure_angle(client, fan, cal_desc, cum_angle, label, idx, fig)
                # 同一角度の既存行を置き換える（redo 対応）
                all_rows = [r for r in all_rows if r["session_index"] != idx]
                all_rows.extend(rows)
                if fig is not None:
                    live_update(fig, ax1, ax2, all_rows, primary_angles, speeds, device_id)

                action = ask_action(is_last, next_target, fig)
                if action == "redo":
                    print("→ この角度を再計測します（治具は回さないでください）。")
                    continue
                if action == "abort":
                    aborted = True
                break
            if aborted:
                print("\n[中断] ユーザー操作により終了。ここまでを保存します。")
                break

    finally:
        try:
            fan.set_power(0, FAN_INDEX)
        except Exception:
            pass
        try:
            client.stop_velocity()
            client.stop_measurement()
        except Exception:
            pass
        client.close()

    if not all_rows:
        print("計測データがありません。", file=sys.stderr)
        return 1

    # --- 出力 ---
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    meta = {
        "device_id": device_id,
        "calibrator_id": CALIBRATOR_ID,
        "scanned_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "ambient_temp_c": ambient_t,
        "ambient_humidity_pct": ambient_h,
        "angle_step_deg": ANGLE_STEP_DEG,
        "n_angles": N_ANGLES,
        "speeds_mps": speeds,
        "warmup_seconds": WARMUP_SECONDS,
        "extra_points_mps": [p["ref_velocity"] for p in EXTRA_POINTS],
        "note": "calibration points + EXTRA_POINTS; coefficients NOT written",
    }
    csv_path = OUTPUT_DIR / f"{device_id}_{ts}.csv"
    png_path = OUTPUT_DIR / f"{device_id}_{ts}_polar.png"
    write_csv(csv_path, all_rows, meta)
    print(f"\nCSV saved: {csv_path}")

    summarize(all_rows, primary_angles, speeds)
    drift_report(all_rows, len(primary_angles), speeds)

    # --- 最終ポーラ図（保存＋表示）---
    try:
        if fig is None:                     # ライブ表示していなかった場合は新規作成
            fig, ax1, ax2 = new_polar_fig()
        render_polar(fig, ax1, ax2, all_rows, primary_angles, speeds, device_id)
        fig.canvas.draw_idle()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fig.savefig(png_path, dpi=120, bbox_inches="tight")
        print(f"Polar plot saved: {png_path}")
        if SHOW_PLOT:
            plt.ioff()
            print("（プロットのウィンドウを閉じると終了します）")
            plt.show()
        plt.close(fig)
    except Exception as e:
        print(f"（ポーラ図の生成に失敗: {e}。CSV は保存済み）", file=sys.stderr)

    notify_done()
    return 0


if __name__ == "__main__":
    sys.exit(main())
