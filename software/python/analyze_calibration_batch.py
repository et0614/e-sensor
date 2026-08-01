"""
校正・検証結果の一括解析（~100台規模）。

calibration_data/<id>.json（calibrate_coefficients の出力）と
verify_reports/<id>.md（verify_device の出力）を横断集計し、次を行う:

 1. 特異値・エラー疑いの検出
    - 検証(verify): result=FAIL / 各チェック FAIL・NG / 温湿度レンジ外 / 通信NG
    - 校正(calibration): E0 外れ値・各校正点電圧の外れ値（ロバスト z, MAD基準）・
      std_dev 過大・電圧の単調性違反・max_error 過大 / pass=false・
      各台の点が平均特性からどれだけ離れているか（残差RMS）
    - データ整合: device_id 重複 / ファイル名とJSON内IDの不一致 /
      verify・calibration の片方欠落 / calibration_points 欠落(旧スキーマ)
 2. 全台平均の特性式（King則3領域）を calibrator_id ごとに作成
    （各点の外れ値を除外して平均 → フィット）
 3. その他の健全性チェック（FW版・校正日範囲・周囲環境・E0×気温相関 等）

出力（analysis/ 配下）:
    summary.md                  … 人が読むサマリ（所見・警告一覧）
    per_device.csv              … 台ごとの指標とフラグ
    average_curve_cal<N>.json   … calibrator ごとの平均特性式
    overlay_cal<N>.png          … 全台の点＋平均フィット曲線

使い方:
    python analyze_calibration_batch.py
"""
import csv
import glob
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")          # バッチ実行（画面表示なし）
import matplotlib.pyplot as plt


BASE = Path(__file__).resolve().parent
CAL_DIR = BASE / "calibration_data"
VER_DIR = BASE / "verify_reports"
OUT_DIR = BASE / "analysis"

# --- しきい値（必要に応じて調整） ---
Z_THRESH            = 3.5     # ロバスト z（MAD基準）の外れ値しきい値
STD_DEV_WARN_MV     = 25.0    # 校正点 std がこれを超えたら高ノイズ警告 [mV]
MAX_ERROR_WARN_PCT  = 10.0    # 検証最大誤差の警告しきい値 [%]
RESID_WARN_MV       = 40.0    # 平均特性からの残差RMSがこれを超えたら「形が異常」[mV]
TEMP_RANGE          = (10.0, 40.0)   # 校正/検証の妥当な気温レンジ [℃]
HUM_RANGE           = (10.0, 90.0)   # 同 相対湿度 [%]


# =====================================================================
# 読み込み
# =====================================================================
def parse_frontmatter(text: str):
    """MD 先頭の '---' から次の '---' までを key: value としてパースする。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    out = {}
    for line in text[4:end].split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def load_calibrations():
    devices = []
    for p in sorted(CAL_DIR.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARN: {p.name} を読めません: {e}")
            continue
        a = (doc.get("calibrations") or {}).get("anemometer")
        if not a:
            continue
        pts = a.get("calibration_points") or []
        ver = a.get("verification") or []
        # max_error は無ければ verification から再計算
        me = a.get("max_error_pct")
        if me is None:
            errs = [float(r["error_pct"]) for r in ver
                    if r.get("ref_velocity", 0) and r.get("error_pct") is not None]
            me = max(errs) if errs else None
        devices.append({
            "file": p.name,
            "id_file": p.stem.upper(),
            "id_json": str(doc.get("device_id", "")).upper(),
            "calibrator_id": a.get("calibrator_id"),
            "fw": a.get("firmware_version"),
            "calibrated_at": a.get("calibrated_at"),
            "ambient_t": a.get("ambient_temp_c"),
            "ambient_h": a.get("ambient_humidity_pct"),
            "E0_mV": (a.get("E0_mV")
                      if a.get("E0_mV") is not None
                      else (float(a["E0"]) * 1000 if a.get("E0") is not None else None)),
            "points": [(round(float(pt["ref_velocity"]), 2),
                        float(pt["voltage_mV"]),
                        float(pt.get("std_dev_mV", 0.0))) for pt in pts],
            "ranges": a.get("ranges") or [],
            "verification": ver,
            "max_error_pct": me,
            "pass": a.get("pass"),
        })
    return devices


def load_verifies():
    out = {}
    for p in sorted(VER_DIR.glob("*.md")):
        fm = parse_frontmatter(p.read_text(encoding="utf-8"))
        if fm is None:
            print(f"WARN: {p.name} にフロントマターがありません")
            continue
        out[p.stem.upper()] = fm
    return out


# =====================================================================
# 統計ユーティリティ
# =====================================================================
def robust_z(values):
    """MAD基準のロバスト z スコア配列と (中央値, スケール) を返す。"""
    a = np.asarray(values, float)
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med)))
    scale = 1.4826 * mad
    if scale == 0:                    # 全て同値等 → 標準偏差にフォールバック
        sd = float(a.std())
        scale = sd if sd > 0 else 1.0
    return (a - med) / scale, med, scale


def kings_params(v1, e1, v2, e2, e0):
    """2点+E0 から King 則 (C, m)。電圧単位は任意（e,e0 同一単位）。"""
    x1 = math.log(max(1e-6, e1 ** 2 - e0 ** 2)); y1 = math.log(v1)
    x2 = math.log(max(1e-6, e2 ** 2 - e0 ** 2)); y2 = math.log(v2)
    m = (y2 - y1) / (x2 - x1)
    return math.exp(y1 - m * x1), m


def fit_kings_3range(v_speeds, e_volts):
    """5点(V, 昇順, index0=0 m/s)から3領域フィット。coef_a/b 相当と curve関数を返す。"""
    e0 = e_volts[0]
    c1, m1 = kings_params(v_speeds[1], e_volts[1], v_speeds[2], e_volts[2], e0)
    c2, m2 = kings_params(v_speeds[2], e_volts[2], v_speeds[3], e_volts[3], e0)
    c3, m3 = kings_params(v_speeds[3], e_volts[3], v_speeds[4], e_volts[4], e0)
    ln_c1, ln_c2, ln_c3 = math.log(c1), math.log(c2), math.log(c3)
    v_split1, v_split2 = v_speeds[2], v_speeds[3]

    def volt_at(v):
        if v < v_split1:
            m, ln_c = m1, ln_c1
        elif v < v_split2:
            m, ln_c = m2, ln_c2
        else:
            m, ln_c = m3, ln_c3
        return math.sqrt(e0 ** 2 + math.exp((math.log(v) - ln_c) / m))

    ranges = [
        {"v_min": 0.0,      "v_max": round(v_split1, 4), "m": round(m1, 4), "lnC": round(ln_c1, 4)},
        {"v_min": round(v_split1, 4), "v_max": round(v_split2, 4), "m": round(m2, 4), "lnC": round(ln_c2, 4)},
        {"v_min": round(v_split2, 4), "v_max": None,      "m": round(m3, 4), "lnC": round(ln_c3, 4)},
    ]
    return e0, ranges, volt_at


# =====================================================================
# メイン解析
# =====================================================================
def main():
    OUT_DIR.mkdir(exist_ok=True)
    devices = load_calibrations()
    verifies = load_verifies()
    lines = []   # summary.md 用
    def out(s=""):
        # Windows コンソール(cp932)で表示できない文字があっても落ちないようにする
        # （summary.md へは utf-8 でそのまま残す）。
        try:
            print(s)
        except UnicodeEncodeError:
            print(s.encode("ascii", "replace").decode("ascii"))
        lines.append(s)

    out(f"# 校正バッチ解析レポート")
    out(f"- calibration_data: {len(devices)} 台 / verify_reports: {len(verifies)} 台")
    out(f"- しきい値: robust-z={Z_THRESH}, std警告={STD_DEV_WARN_MV}mV, "
        f"max_error警告={MAX_ERROR_WARN_PCT}%, 残差RMS警告={RESID_WARN_MV}mV")
    out()

    if not devices:
        out("校正データがありません。終了します。")
        (OUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")
        return

    cal_ids = {d["id_file"] for d in devices}
    ver_ids = set(verifies.keys())

    # ---- (A) データ整合チェック ----
    out("## A. データ整合")
    dup = [x for x in cal_ids if sum(1 for d in devices if d["id_file"] == x) > 1]
    mism = [(d["id_file"], d["id_json"]) for d in devices
            if d["id_json"] and d["id_file"] != d["id_json"]]
    no_ver = sorted(cal_ids - ver_ids)
    no_cal = sorted(ver_ids - cal_ids)
    no_points = [d["id_file"] for d in devices if not d["points"]]
    out(f"- 校正はあるが検証レポートが無い: {len(no_ver)} 台 {no_ver if no_ver else ''}")
    out(f"- 検証はあるが校正データが無い: {len(no_cal)} 台 {no_cal if no_cal else ''}")
    if mism:  out(f"- ⚠ ファイル名とJSON内IDの不一致: {mism}")
    if dup:   out(f"- ⚠ device_id 重複: {dup}")
    if no_points: out(f"- ⚠ calibration_points 欠落(旧スキーマ?): {no_points}")
    out()

    # ---- (B) 検証(verify)のエラー ----
    out("## B. 検証(verify) の不合格・異常")
    ver_fail = []
    for did, fm in sorted(verifies.items()):
        problems = []
        if fm.get("result") != "PASS":
            problems.append(f"result={fm.get('result')}")
        for k in ("temp_check", "humidity_check"):
            if fm.get(k) not in ("PASS", None):
                problems.append(f"{k}={fm.get(k)}")
        for k in ("velocity_comm", "illuminance_comm", "co2_comm"):
            if fm.get(k) not in ("OK", None):
                problems.append(f"{k}={fm.get(k)}")
        if problems:
            ver_fail.append((did, problems))
    if ver_fail:
        for did, pr in ver_fail:
            out(f"- ⚠ {did}: {', '.join(pr)}")
    else:
        out("- 全台 PASS、通信・レンジ異常なし")
    out()

    # ---- (C) calibrator ごとの外れ値検出＋平均特性式 ----
    out("## C. 校正データの外れ値と平均特性式（calibrator別）")
    groups = defaultdict(list)
    for d in devices:
        groups[d["calibrator_id"]].append(d)

    per_device_flags = defaultdict(list)   # id_file -> [flag,...]

    for cal_id in sorted(groups, key=lambda x: (x is None, x)):
        grp = groups[cal_id]
        out(f"### Calibrator #{cal_id}  ({len(grp)} 台)")

        # E0 外れ値
        e0s = [(d, d["E0_mV"]) for d in grp if d["E0_mV"] is not None]
        if len(e0s) >= 4:
            z, med, sc = robust_z([v for _, v in e0s])
            out(f"- E0: 中央値 {med:.1f} mV, ロバストσ {sc:.1f} mV "
                f"(範囲 {min(v for _,v in e0s):.1f}〜{max(v for _,v in e0s):.1f})")
            for (d, v), zz in zip(e0s, z):
                if abs(zz) > Z_THRESH:
                    msg = f"E0={v:.1f}mV (z={zz:+.1f})"
                    out(f"  - ⚠ {d['id_file']}: {msg}")
                    per_device_flags[d["id_file"]].append(msg)

        # 点ごとの電圧外れ値・std警告・単調性
        by_v = defaultdict(list)   # ref_v -> [(d, mV, std)]
        for d in grp:
            for (rv, mv, sd) in d["points"]:
                by_v[rv].append((d, mv, sd))
            # 単調性（電圧が風速に対して単調増加か）
            volts = [mv for (_, mv, _) in sorted(d["points"])]
            if len(volts) >= 2 and any(b <= a for a, b in zip(volts, volts[1:])):
                per_device_flags[d["id_file"]].append("電圧が非単調")
                out(f"  - ⚠ {d['id_file']}: 校正点電圧が非単調（{[round(x) for x in volts]}）")
            # std 過大
            for (rv, mv, sd) in d["points"]:
                if sd > STD_DEV_WARN_MV:
                    per_device_flags[d["id_file"]].append(f"std大@{rv}={sd:.0f}mV")

        # 各 ref_v で外れ値検出 → 外れを除いた平均で特性式をフィット
        mean_by_v = {}
        for rv in sorted(by_v):
            items = by_v[rv]
            if len(items) >= 4:
                z, med, sc = robust_z([mv for _, mv, _ in items])
                keep = []
                for (d, mv, sd), zz in zip(items, z):
                    if abs(zz) > Z_THRESH:
                        msg = f"点{rv}m/s 電圧{mv:.0f}mV (z={zz:+.1f})"
                        out(f"  - ⚠ {d['id_file']}: {msg}")
                        per_device_flags[d["id_file"]].append(msg)
                    else:
                        keep.append(mv)
                mean_by_v[rv] = (statistics.mean(keep) if keep else med, med,
                                 (statistics.pstdev([mv for _, mv, _ in items]) if len(items) > 1 else 0.0),
                                 len(keep), len(items) - len(keep))
            else:
                vals = [mv for _, mv, _ in items]
                mean_by_v[rv] = (statistics.mean(vals), statistics.median(vals), 0.0, len(vals), 0)

        # 平均特性式（5点そろい・0含む場合のみ）
        vs = sorted(mean_by_v)
        avg_curve = None
        if len(vs) == 5 and abs(vs[0]) < 1e-6:
            v_speeds = vs
            e_volts = [mean_by_v[v][0] / 1000.0 for v in vs]   # mV -> V
            try:
                e0, ranges, volt_at = fit_kings_3range(v_speeds, e_volts)
                avg_curve = {"calibrator_id": cal_id, "n_devices": len(grp),
                             "model": "kings_law_3range",
                             "E0": round(e0, 6), "E0_mV": round(e0 * 1000, 1),
                             "ranges": ranges,
                             "points": [{"ref_velocity": v,
                                         "mean_mV": round(mean_by_v[v][0], 1),
                                         "median_mV": round(mean_by_v[v][1], 1),
                                         "std_mV": round(mean_by_v[v][2], 1),
                                         "n_used": mean_by_v[v][3],
                                         "n_excluded": mean_by_v[v][4]} for v in vs]}
                out(f"- 平均特性式: E0={e0*1000:.1f}mV, "
                    f"ranges m=[{ranges[0]['m']:.3f}, {ranges[1]['m']:.3f}, {ranges[2]['m']:.3f}]")
                (OUT_DIR / f"average_curve_cal{cal_id}.json").write_text(
                    json.dumps(avg_curve, ensure_ascii=False, indent=2), encoding="utf-8")

                # 各台の平均特性からの残差RMSで「形が異常」な個体を検出
                for d in grp:
                    if not d["points"]:
                        continue
                    res = [mv - volt_at(rv) * 1000.0 for (rv, mv, _) in d["points"] if rv > 0]
                    if res:
                        rms = math.sqrt(sum(r * r for r in res) / len(res))
                        if rms > RESID_WARN_MV:
                            per_device_flags[d["id_file"]].append(f"平均から残差RMS {rms:.0f}mV")
                            out(f"  - ⚠ {d['id_file']}: 平均特性から乖離 (残差RMS {rms:.0f}mV)")

                # オーバーレイ図
                _plot_overlay(cal_id, grp, mean_by_v, volt_at)
                out(f"- 図: analysis/overlay_cal{cal_id}.png")
            except Exception as e:
                out(f"- 平均特性式のフィットに失敗: {e}")
        else:
            out(f"- 平均特性式スキップ（点構成が5点・0含みで揃っていない: {vs}）")

        # E0 × 校正時気温 の相関（空気密度依存の把握）
        pair = [(d["ambient_t"], d["E0_mV"]) for d in grp
                if d["ambient_t"] is not None and d["E0_mV"] is not None]
        if len(pair) >= 5:
            r = float(np.corrcoef([t for t, _ in pair], [e for _, e in pair])[0, 1])
            out(f"- E0 と校正時気温の相関 r={r:+.2f}（|r|大なら気温依存に注意）")
        out()

    # ---- (D) その他の健全性 ----
    out("## D. その他の健全性チェック")
    fw_dist = defaultdict(int)
    for d in devices:
        fw_dist[d["fw"]] += 1
    out(f"- FW版分布: {dict(fw_dist)}")
    cal_dist = defaultdict(int)
    for d in devices:
        cal_dist[d["calibrator_id"]] += 1
    out(f"- calibrator分布: {dict(cal_dist)}")

    dates = [d["calibrated_at"] for d in devices if d["calibrated_at"]]
    if dates:
        out(f"- 校正日時レンジ: {min(dates)} 〜 {max(dates)}")

    n_pass = sum(1 for d in devices if d["pass"] is True)
    n_fail = sum(1 for d in devices if d["pass"] is False)
    out(f"- 校正 pass: {n_pass} / fail: {n_fail}")
    for d in devices:
        if d["pass"] is False or (d["max_error_pct"] is not None and d["max_error_pct"] > MAX_ERROR_WARN_PCT):
            per_device_flags[d["id_file"]].append(f"max_error={d['max_error_pct']}%")
            out(f"  - ⚠ {d['id_file']}: max_error={d['max_error_pct']}% (pass={d['pass']})")

    # 周囲環境レンジ外
    for d in devices:
        if d["ambient_t"] is not None and not (TEMP_RANGE[0] <= d["ambient_t"] <= TEMP_RANGE[1]):
            per_device_flags[d["id_file"]].append(f"校正時気温外れ {d['ambient_t']}℃")
            out(f"  - ⚠ {d['id_file']}: 校正時気温 {d['ambient_t']}℃ がレンジ外")
        if d["ambient_h"] is not None and not (HUM_RANGE[0] <= d["ambient_h"] <= HUM_RANGE[1]):
            per_device_flags[d["id_file"]].append(f"校正時湿度外れ {d['ambient_h']}%")
    out()

    # ---- 総括 ----
    flagged = sorted(per_device_flags)
    out("## 総括")
    out(f"- 何らかのフラグが付いた台数: {len(flagged)} / {len(devices)}")
    if flagged:
        out(f"- 要確認: {flagged}")
    out()

    # ---- 出力ファイル ----
    _write_per_device_csv(devices, verifies, per_device_flags)
    (OUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n出力: {OUT_DIR}/summary.md, per_device.csv, average_curve_cal*.json, overlay_cal*.png")


def _plot_overlay(cal_id, grp, mean_by_v, volt_at):
    plt.figure(figsize=(9, 5.5))
    # 各台の点（薄く）
    for d in grp:
        if d["points"]:
            xs = [rv for rv, _, _ in d["points"]]
            ys = [mv for _, mv, _ in d["points"]]
            plt.scatter(xs, ys, s=12, color="steelblue", alpha=0.25, zorder=2)
    # 平均点
    vs = sorted(mean_by_v)
    plt.scatter(vs, [mean_by_v[v][0] for v in vs], s=60, color="black",
                marker="D", label="All-device mean", zorder=4)
    # 平均フィット曲線（凡例は matplotlib 既定フォントの都合で ASCII）
    vv = np.linspace(0.01, max(vs) * 1.05, 200)
    plt.plot(vv, [volt_at(v) * 1000.0 for v in vv], "r-",
             lw=2, label="Average King's Law fit", zorder=3)
    plt.xlabel("Velocity [m/s]"); plt.ylabel("Voltage [mV]")
    plt.title(f"Calibrator #{cal_id}: {len(grp)} devices overlay + average fit")
    plt.grid(True, ls="--", alpha=0.5); plt.legend()
    plt.tight_layout()
    plt.savefig(OUT_DIR / f"overlay_cal{cal_id}.png", dpi=120)
    plt.close()


def _write_per_device_csv(devices, verifies, flags):
    cols = ["device_id", "calibrator_id", "fw", "calibrated_at",
            "ambient_t", "ambient_h", "E0_mV", "max_error_pct", "cal_pass",
            "verify_result", "temp_c", "hum_pct", "n_flags", "flags"]
    with open(OUT_DIR / "per_device.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for d in sorted(devices, key=lambda x: x["id_file"]):
            fm = verifies.get(d["id_file"], {})
            fl = flags.get(d["id_file"], [])
            w.writerow([
                d["id_file"], d["calibrator_id"], d["fw"], d["calibrated_at"],
                d["ambient_t"], d["ambient_h"],
                round(d["E0_mV"], 1) if d["E0_mV"] is not None else "",
                d["max_error_pct"], d["pass"],
                fm.get("result", ""), fm.get("temperature_c", ""), fm.get("humidity_pct", ""),
                len(fl), " | ".join(fl),
            ])


if __name__ == "__main__":
    main()
