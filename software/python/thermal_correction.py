"""風速計の自己発熱による温度センサ熱影響の補正 — 手法比較ツール

資料「熱影響の補正方法」§3 の集中定数熱モデルにもとづき、空気温度 Ta を
基板温度 TB(センサ計測値) と風速 v から推定する。本スクリプトは複数の補正法を
比較し、「カルマンフィルタでどの程度改善するか」を定量化する。

モデル（資料 §3）:
    CB dTB/dt = Q - K(v)(TB - Ta)            … 1次の熱平衡
    K(v) = A + B*v         [mW/K]            … 伝熱係数(風速依存, 一次式)
    離散更新 (Δt 一定, Ta はステップ内一定):
    TB_{n+1} = Ta + Q/K + (TB_n - Ta - Q/K) * E ,  E = exp(-K Δt / CB)
    定常: TB - Ta = Q/K

比較する補正法:
    raw      : 補正なし(基板温度そのもの)
    ss       : 定常オフセット除去   Ta = TB - Q/K(v)      (瞬時, 過渡で遅れる)
    ss+ema   : ss を指数移動平均で平滑
    inv+ema  : 逆算式(資料 式3) + EMA  ← 現行法 (dTB/dt を含むため揺れる)
    kf       : 2状態カルマンフィルタ (資料 §4)  ← 提案法

使い方:
    python thermal_correction.py              # シミュレーションで RMSE 比較
    python thermal_correction.py --plot       # 併せてグラフ表示(matplotlib 必要)
    python thermal_correction.py --live       # 実機に接続しリアルタイム比較表示
    python thermal_correction.py --live --csv log.csv   # 実機ログを CSV 保存
"""

from __future__ import annotations
import argparse
import math
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


# ---------------------------------------------------------------------------
# モデルパラメータ（資料 §3 の同定値）
# ---------------------------------------------------------------------------
@dataclass
class ThermalParams:
    Q: float = 50.0          # トランジスタ発熱 [mW] (資料: 風速によらず概ね一定)
    CB: float = 1300.0       # 基板熱容量 [mJ/K] (実測昇温 τ≈120s に整合)
    dt: float = 1.0          # サンプリング周期 [s] (実機は 1Hz)
    # K(v)=A+B*v [mW/K]。基板の強制対流は平板/バルク体的で、熱線(細円柱)の King
    # 則 √v より風速の一次式の方が実測に合うため線形。切片 A(無風時)は実測値に
    # 固定。e_sensor.ThermalParams と一致。
    A: float = 11.058        # 切片(無風時) [mW/K]
    B: float = 130.19        # 風速係数 [mW/(K·(m/s))]
    # 定常オフセット(Q/K)の調整係数。校正時の TB-Ta を平衡前に測り offset がやや
    # 過小だったため当面 1.2。平衡まで取り直し再校正したら 1.0 に戻す。
    offset_scale: float = 1.2

    def K(self, v: float) -> float:
        """伝熱係数 K(v) = A + B*v [mW/K]。負値はクランプ。"""
        return max(1e-3, self.A + self.B * max(0.0, v))

    def E(self, v: float) -> float:
        """離散減衰係数 E = exp(-K Δt / CB)。"""
        return math.exp(-self.K(v) * self.dt / self.CB)

    def offset(self, v: float) -> float:
        """定常自己発熱オフセット offset_scale·Q/K(v) [K]。"""
        return self.offset_scale * self.Q / self.K(v)


# ---------------------------------------------------------------------------
# 補正器
# ---------------------------------------------------------------------------
class SteadyState:
    """定常オフセット除去のみ（瞬時, 動特性を無視）。"""
    def __init__(self, p: ThermalParams):
        self.p = p

    def update(self, z: float, v: float) -> float:
        return z - self.p.offset(v)


class Ema:
    """任意推定値に対する指数移動平均。"""
    def __init__(self, alpha: float = 0.1):
        self.alpha = alpha
        self.y: Optional[float] = None

    def update(self, x: float) -> float:
        self.y = x if self.y is None else self.alpha * x + (1 - self.alpha) * self.y
        return self.y


class InverseEma:
    """現行法: 逆算式(資料 式3) + EMA。

        Ta_inst = (TB_n - TB_{n-1} * E) / (1 - E) - Q/K
    dTB/dt を含むためノイズを 1/(1-E) 倍に増幅する。EMA で平滑するが遅れる。
    """
    def __init__(self, p: ThermalParams, alpha: float = 0.1):
        self.p = p
        self.ema = Ema(alpha)
        self.prev_z: Optional[float] = None

    def update(self, z: float, v: float) -> float:
        if self.prev_z is None:
            self.prev_z = z
            return self.ema.update(z - self.p.offset(v))  # 初回は定常推定で初期化
        E = self.p.E(v)
        ta_inst = (z - self.prev_z * E) / (1.0 - E) - self.p.offset(v)
        self.prev_z = z
        return self.ema.update(ta_inst)


class KalmanCorrector:
    """2状態カルマンフィルタ（資料 §4）。状態 x=[TB, Ta]。

    予測:  x = F x + b ,  P = F P Fᵀ + Qw
    更新:  e = z - H x , S = H P Hᵀ + R , g = P Hᵀ/S
           x += g e ,  P = (I - g H) P
    F, b, E は風速 v から毎ステップ計算する(線形時変)。
    """
    def __init__(self, p: ThermalParams,
                 meas_std: float = 0.03,      # 観測(センサ)雑音 σ [K]
                 air_std: float = 0.005,      # 空気温度の1ステップ変動 σ [K] (プロセス雑音)
                 board_std: float = 0.002):   # 基板状態のモデル雑音 σ [K]
        self.p = p
        self.H = np.array([[1.0, 0.0]])
        self.R = np.array([[meas_std ** 2]])
        self.Qw = np.diag([board_std ** 2, air_std ** 2])
        self.x: Optional[np.ndarray] = None
        self.P = np.diag([1.0, 100.0])        # 初期不確かさ(Ta は大きめ)

    def update(self, z: float, v: float) -> float:
        if self.x is None:
            # 起動直後は自己発熱前 → Ta ≈ TB とみなす
            self.x = np.array([[z], [z]])
            return z
        E = self.p.E(v)
        off = self.p.offset(v)
        F = np.array([[E, 1.0 - E],
                      [0.0, 1.0]])
        b = np.array([[(1.0 - E) * off],
                      [0.0]])
        # 予測
        x = F @ self.x + b
        P = F @ self.P @ F.T + self.Qw
        # 更新
        e = z - (self.H @ x)[0, 0]
        S = (self.H @ P @ self.H.T + self.R)[0, 0]
        g = (P @ self.H.T) / S
        x = x + g * e
        P = (np.eye(2) - g @ self.H) @ P
        self.x, self.P = x, P
        return x[1, 0]   # Ta


# ---------------------------------------------------------------------------
# シミュレーション（グラウンドトゥルース付き）
# ---------------------------------------------------------------------------
@dataclass
class Scenario:
    name: str
    duration: float                 # [s]
    ta_func: callable               # t -> 真の空気温度 [C]
    v_func: callable                # t -> 風速 [m/s]


def make_scenarios() -> List[Scenario]:
    def warmup_then_step(t):       # 室温がゆっくり上昇し途中で +2℃ ステップ
        base = 25.0 + 0.0008 * t
        return base + (2.0 if t >= 600 else 0.0)

    return [
        Scenario("A: 無風・空気温度ステップ", 1200,
                 ta_func=warmup_then_step,
                 v_func=lambda t: 0.0),
        Scenario("B: 空気温度一定・風速変化", 1200,
                 ta_func=lambda t: 25.0,
                 v_func=lambda t: 0.0 if t < 300 else (0.35 if t < 600 else (2.0 if t < 900 else 0.0))),
        Scenario("C: 両方変化", 1500,
                 ta_func=lambda t: 25.0 + 1.5 * math.sin(2 * math.pi * t / 900.0),
                 v_func=lambda t: 0.0 if t < 400 else (1.0 if t < 800 else (2.0 if t < 1100 else 0.3))),
    ]


def simulate(p: ThermalParams, sc: Scenario, meas_std: float, seed: int,
             q_true: Optional[float] = None, cb_true: Optional[float] = None):
    """真の Ta, v から基板温度 TB を生成し、計測値(雑音付き)を返す。

    q_true/cb_true を与えるとモデル不整合(実機ズレ)を模擬できる。
    """
    rng = np.random.default_rng(seed)
    Q = p.Q if q_true is None else q_true
    CB = p.CB if cb_true is None else cb_true
    n = int(sc.duration / p.dt)
    t = np.arange(n) * p.dt
    ta = np.array([sc.ta_func(ti) for ti in t])
    v = np.array([sc.v_func(ti) for ti in t])

    tb = np.empty(n)
    tb[0] = ta[0]                         # 起動時は基板=空気温度
    for k in range(1, n):
        K = p.K(v[k])                     # 真の K も同じ式（雑音は計測側のみ）
        E = math.exp(-K * p.dt / CB)
        off = Q / K
        tb[k] = ta[k] + off + (tb[k - 1] - ta[k] - off) * E
    z = tb + rng.normal(0.0, meas_std, n)  # センサ雑音
    z = np.round(z, 2)                     # 0.01℃ 量子化(実機の分解能)
    return t, ta, v, tb, z


def run_methods(p: ThermalParams, v: np.ndarray, z: np.ndarray,
                alpha: float, meas_std: float):
    """各補正法を適用し、推定 Ta 系列の dict を返す。"""
    ss = SteadyState(p)
    ss_e = Ema(alpha)
    inv = InverseEma(p, alpha)
    kf = KalmanCorrector(p, meas_std=meas_std)

    out = {k: np.empty(len(z)) for k in ("raw", "ss", "ss+ema", "inv+ema", "kf")}
    for k in range(len(z)):
        out["raw"][k] = z[k]
        s = ss.update(z[k], v[k])
        out["ss"][k] = s
        out["ss+ema"][k] = ss_e.update(s)
        out["inv+ema"][k] = inv.update(z[k], v[k])
        out["kf"][k] = kf.update(z[k], v[k])
    return out


def rmse(est: np.ndarray, truth: np.ndarray, warmup: int) -> float:
    e = est[warmup:] - truth[warmup:]
    return float(np.sqrt(np.mean(e ** 2)))


def maxerr(est: np.ndarray, truth: np.ndarray, warmup: int) -> float:
    return float(np.max(np.abs(est[warmup:] - truth[warmup:])))


def benchmark(args):
    p = ThermalParams()
    print(f"同定モデル: Q={p.Q:.0f}mW  CB={p.CB:.0f}mJ/K  "
          f"K(v)={p.A:.2f}+{p.B:.2f}*v mW/K  dt={p.dt:.0f}s")
    print(f"自己発熱オフセット: 無風 {p.offset(0):.2f}K / "
          f"0.35m/s {p.offset(0.35):.2f}K / 2.0m/s {p.offset(2.0):.2f}K\n")

    warmup_s = 120                      # 評価から除く初期過渡 [s]
    warmup = int(warmup_s / p.dt)
    methods = ["raw", "ss", "ss+ema", "inv+ema", "kf"]

    all_results = {}
    for sc in make_scenarios():
        t, ta, v, tb, z = simulate(
            p, sc, meas_std=args.meas_std, seed=args.seed,
            q_true=args.q_true, cb_true=args.cb_true)
        out = run_methods(p, v, z, alpha=args.alpha, meas_std=args.meas_std)
        all_results[sc.name] = (t, ta, v, tb, z, out)

        print(f"=== {sc.name} ===")
        print(f"  {'method':8s}  {'RMSE[K]':>9s}  {'maxerr[K]':>10s}")
        kf_rmse = rmse(out["kf"], ta, warmup)
        for m in methods:
            r = rmse(out[m], ta, warmup)
            mx = maxerr(out[m], ta, warmup)
            tag = ""
            if m == "inv+ema":
                tag = f"  (現行法)"
            elif m == "kf":
                tag = f"  (提案法)"
            print(f"  {m:8s}  {r:9.3f}  {mx:10.3f}{tag}")
        inv_rmse = rmse(out["inv+ema"], ta, warmup)
        if kf_rmse > 0:
            print(f"  → カルマンは現行法(inv+ema)比で RMSE {inv_rmse / kf_rmse:.1f}x 改善"
                  f" (raw比 {rmse(out['raw'], ta, warmup) / kf_rmse:.1f}x)\n")

    if args.plot:
        _plot(all_results, warmup)


def _plot(all_results, warmup):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib が無いためグラフは省略します (pip install matplotlib)")
        return
    n = len(all_results)
    fig, axes = plt.subplots(n, 1, figsize=(11, 3.2 * n), squeeze=False)
    for ax, (name, (t, ta, v, tb, z, out)) in zip(axes[:, 0], all_results.items()):
        ax.plot(t, z, color="0.7", lw=0.8, label="raw (board, noisy)")
        ax.plot(t, out["inv+ema"], color="tab:orange", lw=1.0, label="inv+ema (現行)")
        ax.plot(t, out["kf"], color="tab:blue", lw=1.4, label="kf (提案)")
        ax.plot(t, ta, "k--", lw=1.2, label="true Ta")
        ax2 = ax.twinx()
        ax2.plot(t, v, color="tab:green", lw=0.7, alpha=0.5)
        ax2.set_ylabel("v [m/s]", color="tab:green")
        ax.set_title(name)
        ax.set_xlabel("t [s]"); ax.set_ylabel("T [C]")
        ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# 実機モード
# ---------------------------------------------------------------------------
def live(args):
    from e_sensor import ESensorClient
    p = ThermalParams()
    kf = KalmanCorrector(p, meas_std=args.meas_std)
    inv = InverseEma(p, alpha=args.alpha)
    ss = SteadyState(p)

    client = ESensorClient()
    if not client.connect():
        print("Error: Device 'E-Sensor' not found.")
        return
    csv_f = open(args.csv, "w", encoding="utf-8") if args.csv else None
    if csv_f:
        csv_f.write("t,v,tb_raw,ss,inv_ema,kf\n")
    try:
        client.start_measurement()
        client.start_velocity()
        print("計測開始 (Ctrl+C で終了)。風速 ON。安定した静止空気で観察すると効果が分かります。\n")
        print(f"{'time':8s} | {'v[m/s]':>6s} | {'raw[C]':>7s} | {'ss[C]':>7s} | "
              f"{'inv+ema':>8s} | {'kf[C]':>7s}")
        print("-" * 60)
        t0 = time.time()
        while True:
            d = client.get_data(timeout=0.5)
            if d and d.env_valid:
                v = d.velocity if d.anemo_valid else 0.0
                tb = d.temperature
                est_ss = ss.update(tb, v)
                est_inv = inv.update(tb, v)
                est_kf = kf.update(tb, v)
                ts = time.time() - t0
                print(f"{ts:8.1f} | {v:6.2f} | {tb:7.2f} | {est_ss:7.2f} | "
                      f"{est_inv:8.2f} | {est_kf:7.2f}")
                if csv_f:
                    csv_f.write(f"{ts:.1f},{v:.3f},{tb:.2f},{est_ss:.3f},"
                                f"{est_inv:.3f},{est_kf:.3f}\n")
                    csv_f.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n停止します。")
    finally:
        client.stop_measurement()
        client.close()
        if csv_f:
            csv_f.close()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="風速計 自己発熱の温度補正 手法比較")
    ap.add_argument("--live", action="store_true", help="実機に接続してリアルタイム比較")
    ap.add_argument("--plot", action="store_true", help="シミュレーション結果をグラフ表示")
    ap.add_argument("--csv", type=str, default=None, help="実機ログ CSV 出力先")
    ap.add_argument("--alpha", type=float, default=0.1, help="EMA 係数 (現行法)")
    ap.add_argument("--meas-std", type=float, default=0.03, dest="meas_std",
                    help="温度センサ雑音 σ [K]")
    ap.add_argument("--seed", type=int, default=0, help="乱数シード")
    ap.add_argument("--q-true", type=float, default=None, dest="q_true",
                    help="模擬: 真の Q [mW] (モデル不整合の検証用)")
    ap.add_argument("--cb-true", type=float, default=None, dest="cb_true",
                    help="模擬: 真の CB [mJ/K] (モデル不整合の検証用)")
    args = ap.parse_args()

    if args.live:
        live(args)
    else:
        benchmark(args)


if __name__ == "__main__":
    main()
