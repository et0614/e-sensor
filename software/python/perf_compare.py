"""
E-Sensor 性能評価 ― Kanomax との比較解析

perf_run.py が出力した2つのCSV（同一スケジュールを Kanomax / E-Sensor で流したもの）を
スケジュール時刻 t で突き合わせ、以下を算出する:
  - 階段: 定常確度(バイアス/％誤差)・直線性・ヒステリシス
  - ステップ(8⇄21 反復): アンサンブル平均で τ63・t90・行き過ぎ・Kanomax比の遅れ
  - 保持: ノイズ(std)・ドリフト・ゼロ点
  - ランプ: 連続追随（重ね描き）

使い方:
  py perf_compare.py                       # perf_data の最新 kanomax/esensor を自動選択
  py perf_compare.py <kanomax.csv> <esensor.csv>

出力: コンソールに指標表、perf_data/compare_{YYYYMMDD_HHMMSS}.png に図。
両runは同一決定論スケジュール＝t軸が共通（風洞再現性~1%）なので直接整列する。
"""
import sys
import csv
import datetime
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

import perf_run   # スケジュール定義(build_schedule/expand)を共有

OUTPUT_DIR = Path(__file__).resolve().parent / "perf_data"
STEADY_WIN = 20.0     # 各holdの「後半この秒数」を定常とみなして平均
STEP_PRE, STEP_GRID = 3.0, 0.1   # ステップ・アンサンブルの前余白と時間刻み


# ============================================
# 読み込み
# ============================================
def load(path):
    meta, t, phase, fan, vel = {}, [], [], [], []
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0].startswith("#"):
                meta[row[0].lstrip("# ").strip()] = row[1] if len(row) > 1 else ""
            elif row[0] == "t_s":
                continue
            else:
                if row[3] == "":
                    continue
                t.append(float(row[0])); phase.append(row[1]); fan.append(int(row[2]))
                vel.append(float(row[3]))
    return meta, np.array(t), phase, np.array(fan), np.array(vel)


def latest(kind):
    fs = sorted(OUTPUT_DIR.glob(f"perf_{kind}_*.csv"))
    return fs[-1] if fs else None


def win_mean_std(t, v, lo, hi):
    m = (t >= lo) & (t < hi)
    if not np.any(m):
        return None, None, 0
    return float(np.mean(v[m])), float(np.std(v[m])), int(np.sum(m))


# ============================================
# 解析
# ============================================
def analyze_staircase(segs, kt, kv, et, ev):
    """階段 hold の定常値（後半STEADY_WIN秒平均）を両センサで取り、確度/ヒステリシスを出す。"""
    rows = []
    for s in segs:
        if s["kind"] != "hold" or s["label"] not in ("stair_up", "stair_down"):
            continue
        lo, hi = s["t1"] - STEADY_WIN, s["t1"]
        k_m, _, kn = win_mean_std(kt, kv, lo, hi)
        e_m, _, en = win_mean_std(et, ev, lo, hi)
        if k_m is None or e_m is None:
            continue
        rows.append({"fan": s["power"], "dir": s["label"][6:], "k": k_m, "e": e_m,
                     "bias": e_m - k_m, "err": (e_m - k_m) / k_m * 100 if k_m > 0.05 else float("nan")})
    return rows


def hysteresis(stair_rows):
    out = []
    for fan in sorted({r["fan"] for r in stair_rows}):
        up = next((r for r in stair_rows if r["fan"] == fan and r["dir"] == "up"), None)
        dn = next((r for r in stair_rows if r["fan"] == fan and r["dir"] == "down"), None)
        if up and dn:
            out.append((fan, up["e"], dn["e"], dn["e"] - up["e"]))
    return out


def step_edges(segs):
    edges = []
    for i in range(1, len(segs)):
        a, b = segs[i-1], segs[i]
        if a["label"].startswith("step") and b["label"].startswith("step") and a["label"] != b["label"]:
            edges.append((b["t0"], "up" if b["label"] == "step_hi" else "down"))
    return edges


def ensemble(edges, direction, t, v, post):
    grid = np.arange(-STEP_PRE, post + 1e-9, STEP_GRID)
    stacks = []
    for te, d in edges:
        if d != direction:
            continue
        if te + post > t[-1] or te - STEP_PRE < t[0]:
            continue
        stacks.append(np.interp(te + grid, t, v))
    if not stacks:
        return grid, None
    return grid, np.mean(np.vstack(stacks), axis=0)


def step_metrics(grid, y):
    """(baseline, final, τ63, t90, overshoot%) を返す。"""
    if y is None:
        return None
    base = np.mean(y[grid < -0.3])
    final = np.mean(y[grid >= grid[-1] - 2.0])
    amp = final - base
    if abs(amp) < 1e-6:
        return None
    norm = (y - base) / amp
    pos = grid >= 0

    def t_at(frac):
        idx = np.where(pos & (norm >= frac))[0]
        return float(grid[idx[0]]) if len(idx) else float("nan")

    tau = t_at(0.63); t90 = t_at(0.90)
    # norm は 0→1 に規格化済み。行き過ぎ/行き足りずは max(norm) が 1 を超えた分
    # （up=オーバーシュート, down=アンダーシュート とも norm>1 になる）。
    over = (np.nanmax(norm[pos]) - 1.0) * 100
    return {"base": base, "final": final, "amp": amp, "tau63": tau, "t90": t90, "overshoot": over}


def analyze_holds(segs, kt, kv, et, ev):
    out = []
    for s in segs:
        if s["kind"] != "hold" or s["label"] not in ("hold_low", "hold_vlow", "hold_zero"):
            continue
        lo, hi = s["t0"] + 30, s["t1"]     # 最初の30sは整定として捨てる
        km, ks, _ = win_mean_std(kt, kv, lo, hi)
        em, es, _ = win_mean_std(et, ev, lo, hi)
        # 線形ドリフト（E-Sensor）
        m = (et >= lo) & (et < hi)
        drift = float(np.polyfit(et[m], ev[m], 1)[0]) if np.sum(m) > 2 else float("nan")
        out.append({"phase": s["label"], "k_mean": km, "k_std": ks,
                    "e_mean": em, "e_std": es, "e_drift_mps_per_s": drift})
    return out


# ============================================
# メイン
# ============================================
def main():
    if len(sys.argv) >= 3:
        kpath, epath = Path(sys.argv[1]), Path(sys.argv[2])
    else:
        kpath, epath = latest("kanomax"), latest("esensor")
    if not kpath or not epath:
        print("kanomax/esensor のCSVが見つかりません。", file=sys.stderr)
        return 1
    print(f"Kanomax : {kpath.name}\nE-Sensor: {epath.name}")

    kmeta, kt, kph, kfan, kv = load(kpath)
    emeta, et, eph, efan, ev = load(epath)
    quick = str(kmeta.get("quick", "False")) == "True"
    segs, total = perf_run.expand(perf_run.build_schedule(quick))
    post = (perf_run.Q_STEP_DWELL if quick else perf_run.STEP_DWELL)

    # --- 階段: 確度・ヒステリシス ---
    stair = analyze_staircase(segs, kt, kv, et, ev)
    print("\n===== 階段 定常（後半{:.0f}s平均） =====".format(STEADY_WIN))
    print(" fan dir   Kanomax  E-Sensor   bias    err%")
    for r in stair:
        print(f" {r['fan']:>3} {r['dir']:>4}  {r['k']:7.3f}  {r['e']:7.3f}  "
              f"{r['bias']:+6.3f}  {r['err']:+6.1f}")
    ks = np.array([r["k"] for r in stair]); es = np.array([r["e"] for r in stair])
    if len(ks) >= 2:
        a, b = np.polyfit(ks, es, 1)
        r2 = 1 - np.sum((es - (a*ks+b))**2) / np.sum((es - es.mean())**2)
        print(f" 直線性: E = {a:.4f}*K {b:+.4f}   R^2={r2:.4f}")
    print(" ヒステリシス(同fan up→down のE差):")
    for fan, u, d, dd in hysteresis(stair):
        print(f"   fan{fan:>3}: up {u:.3f} / down {d:.3f}  Δ{dd:+.3f}")

    # --- ステップ: アンサンブル ---
    edges = step_edges(segs)
    print("\n===== ステップ応答（アンサンブル平均） =====")
    ens = {}
    for d in ("up", "down"):
        gk, yk = ensemble(edges, d, kt, kv, post)
        ge, ye = ensemble(edges, d, et, ev, post)
        ens[d] = (gk, yk, ge, ye)
        mk, me = step_metrics(gk, yk), step_metrics(ge, ye)
        if mk and me:
            lag = me["tau63"] - mk["tau63"]
            print(f" [{d}] E: τ63={me['tau63']:.2f}s t90={me['t90']:.2f}s "
                  f"行過ぎ={me['overshoot']:.1f}%  | Kanomax τ63={mk['tau63']:.2f}s "
                  f"→ 遅れ={lag:+.2f}s")

    # --- 保持: ノイズ/ドリフト ---
    print("\n===== 保持（ノイズ/ドリフト, 整定後） =====")
    for h in analyze_holds(segs, kt, kv, et, ev):
        print(f" {h['phase']:>10}: E mean={h['e_mean']:.3f} std={h['e_std']:.4f}  "
              f"| K mean={h['k_mean']:.3f} std={h['k_std']:.4f}  "
              f"E-drift={h['e_drift_mps_per_s']*1000:+.2f} mm/s/s")

    # --- 図 ---
    fig = plt.figure(figsize=(14, 9))
    ax1 = fig.add_subplot(2, 2, 1)
    ax1.plot(kt, kv, lw=0.8, label="Kanomax")
    ax1.plot(et, ev, lw=0.8, alpha=0.8, label="E-Sensor")
    ax1.set_xlabel("t [s]"); ax1.set_ylabel("velocity [m/s]")
    ax1.set_title("Full time series"); ax1.grid(True); ax1.legend()

    ax2 = fig.add_subplot(2, 2, 2)
    if len(ks):
        ax2.scatter(ks, es, s=20)
        lim = [0, max(ks.max(), es.max()) * 1.05]
        ax2.plot(lim, lim, "k--", lw=0.8, label="1:1")
        ax2.set_xlim(lim); ax2.set_ylim(lim)
    ax2.set_xlabel("Kanomax [m/s]"); ax2.set_ylabel("E-Sensor [m/s]")
    ax2.set_title("Parity (staircase steady)"); ax2.grid(True); ax2.legend()

    ax3 = fig.add_subplot(2, 2, 3)
    for d, c in (("up", "C0"), ("down", "C1")):
        gk, yk, ge, ye = ens[d]
        if yk is not None:
            ax3.plot(gk, yk, c + "--", lw=1, label=f"K {d}")
        if ye is not None:
            ax3.plot(ge, ye, c + "-", lw=1.2, label=f"E {d}")
    ax3.axvline(0, color="k", lw=0.5)
    ax3.set_xlabel("t since step [s]"); ax3.set_ylabel("velocity [m/s]")
    ax3.set_title("Step response (ensemble)"); ax3.grid(True); ax3.legend(fontsize=8)

    ax4 = fig.add_subplot(2, 2, 4)
    rseg = [s for s in segs if s["label"].startswith("ramp")]
    if rseg:
        r0, r1 = rseg[0]["t0"], rseg[-1]["t1"]
        mk = (kt >= r0) & (kt <= r1); me = (et >= r0) & (et <= r1)
        ax4.plot(kt[mk], kv[mk], lw=1, label="Kanomax")
        ax4.plot(et[me], ev[me], lw=1, alpha=0.8, label="E-Sensor")
    ax4.set_xlabel("t [s]"); ax4.set_ylabel("velocity [m/s]")
    ax4.set_title("Ramp tracking"); ax4.grid(True); ax4.legend()

    fig.suptitle(f"Perf compare  K:{kpath.name}  E:{epath.name}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"compare_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print(f"\n図を保存: {out}")
    plt.show()
    plt.close(fig)
    return 0


if __name__ == "__main__":
    sys.exit(main())
