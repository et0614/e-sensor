"""
E-Sensor 動作確認スクリプト

校正完了後の出荷チェック用。デバイスと 1 回通信してセンサデータを取得し、
温湿度の範囲チェックと、各センサが正常に計測値を返したかを判定して、
Markdown レポートを保存する。

判定基準:
- 温度: 10 ~ 35 C で PASS
- 湿度: 10 ~ 90 % で PASS
- 風速 / 照度 / CO2: 計測ステータスフラグが立てば OK (値の妥当性チェックなし)
- 全項目クリアで result: PASS

出力先: verify_reports/{device_id}.md (同 ID は上書き)

後で `aggregate_verify_reports.py` を実行すると、ディレクトリ内の MD を
CSV にまとめられる。
"""
import sys
import time
import datetime
from pathlib import Path

from e_sensor import ESensorClient


# ============================================
# 設定
# ============================================
TEMP_RANGE = (10.0, 35.0)
HUM_RANGE  = (10.0, 90.0)

# データ取得のリトライ上限 [sec]。env_valid と anemo_valid の両方が立つまで再試行する。
# 風速計は起動時 OFF で、start_velocity 後 約5秒の予熱を経て anemo_valid が立つため、
# 予熱を見込んで長めに取る。
DATA_ACQUIRE_TIMEOUT = 10.0

REPORTS_DIR = Path(__file__).resolve().parent / "verify_reports"


# ============================================
# 内部ユーティリティ
# ============================================

def acquire_one_sample(client: ESensorClient, timeout: float):
    """1 サンプルを取得する。env_valid (温湿度・CO2) と anemo_valid (風速・電圧) が
    ともに立てば即返す。風速計は起動時 OFF で予熱に数秒かかるため anemo_valid も待つ。
    timeout までに両方が立たない場合は、最後に取れた SensorData を返す
    (取れなかった場合は None)。"""
    last = None
    start = time.time()
    while time.time() - start < timeout:
        d = client.get_data(timeout=1.0)
        if d is not None:
            last = d
            if d.env_valid and d.anemo_valid:
                return d
        time.sleep(0.1)
    return last


def _fmt(value, spec):
    if value is None:
        return "N/A"
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return "N/A"


# ============================================
# レポート書き出し
# ============================================

def write_report(device_id, fw_version, data, judgments):
    """MD レポートを書き出し、(path, overall_pass) を返す。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    now       = datetime.datetime.now().astimezone()
    tested_at = now.isoformat(timespec="seconds")

    overall    = all(judgments.values())
    result_str = "PASS" if overall else "FAIL"

    temp_v = data.temperature if data else None
    hum_v  = data.humidity    if data else None
    vel_v  = data.velocity    if data else None
    ill_v  = data.illuminance if data else None
    co2_v  = data.co2         if data else None

    def pf(key): return "PASS" if judgments[key] else "FAIL"
    def ok(key): return "OK"   if judgments[key] else "NG"

    # 表セル用の整形済み文字列 (列幅をそろえる)
    cells = {
        "temp_val": f"{_fmt(temp_v, '.2f')} C",
        "hum_val":  f"{_fmt(hum_v, '.2f')} %",
        "vel_val":  f"{_fmt(vel_v, '.3f')} m/s",
        "ill_val":  f"{_fmt(ill_v, '.1f')} lx",
        "co2_val":  f"{_fmt(co2_v, 'd')} ppm",
    }
    val_w = max(len(s) for s in cells.values())

    fname = REPORTS_DIR / f"{device_id}.md"

    with fname.open("w", encoding="utf-8") as f:
        # ---- YAML フロントマター ----
        f.write("---\n")
        f.write(f"device_id: {device_id}\n")
        f.write(f"firmware_version: {fw_version}\n")
        f.write(f"tested_at: {tested_at}\n")
        f.write(f"result: {result_str}\n")
        f.write(f"temperature_c: {_fmt(temp_v, '.2f')}\n")
        f.write(f"humidity_pct: {_fmt(hum_v, '.2f')}\n")
        f.write(f"velocity_mps: {_fmt(vel_v, '.3f')}\n")
        f.write(f"illuminance_lx: {_fmt(ill_v, '.1f')}\n")
        f.write(f"co2_ppm: {_fmt(co2_v, 'd')}\n")
        f.write(f"temp_check: {pf('temp_check')}\n")
        f.write(f"humidity_check: {pf('humidity_check')}\n")
        f.write(f"velocity_comm: {ok('velocity_comm')}\n")
        f.write(f"illuminance_comm: {ok('illuminance_comm')}\n")
        f.write(f"co2_comm: {ok('co2_comm')}\n")
        f.write("---\n\n")

        # ---- 本文 ----
        f.write("# E-Sensor Verification Report\n\n")
        f.write(f"**Device ID:** `{device_id}`  \n")
        f.write(f"**FW Version:** `{fw_version}`  \n")
        f.write(f"**Tested at:** {now.strftime('%Y-%m-%d %H:%M:%S %z')}  \n")
        f.write(f"**Result:** {result_str}\n\n")

        f.write("## Measurements (single sample)\n\n")
        f.write("| Item        | Value | Check | Result |\n")
        f.write("|-------------|-------|-------|--------|\n")
        temp_check_str = f"{TEMP_RANGE[0]:.0f}-{TEMP_RANGE[1]:.0f} C"
        hum_check_str  = f"{HUM_RANGE[0]:.0f}-{HUM_RANGE[1]:.0f} %"
        f.write(f"| Temperature | {cells['temp_val']:<{val_w}} | {temp_check_str:<13} | {pf('temp_check')}   |\n")
        f.write(f"| Humidity    | {cells['hum_val']:<{val_w}}  | {hum_check_str:<13} | {pf('humidity_check')}   |\n")
        f.write(f"| Velocity    | {cells['vel_val']:<{val_w}}  | communication | {ok('velocity_comm')}     |\n")
        f.write(f"| Illuminance | {cells['ill_val']:<{val_w}}  | communication | {ok('illuminance_comm')}     |\n")
        f.write(f"| CO2         | {cells['co2_val']:<{val_w}}  | communication | {ok('co2_comm')}     |\n")

        if data is None:
            f.write("\n> Note: No sensor data was received within the timeout. All checks marked FAIL/NG.\n")

    return fname, overall


# ============================================
# メイン
# ============================================

def main(midi_in=None, midi_out=None, expected_device_id=None):
    """動作確認を実行し 0(PASS)/1(FAIL)/2(取り違え) を返す。

    midi_in / midi_out を指定すると、その MIDI ペア（esensor_discovery で確定した
    特定個体）に接続する。省略時は従来通り名前先頭一致で最初の1台に接続する。
    expected_device_id を指定すると、接続先が本当にその個体かを get_device_id で
    照合し、不一致なら 2 を返す（MIDI名の解決ブレによる取り違え対策）。
    """
    client = ESensorClient()
    connected = (client.open_ports(midi_in, midi_out)
                 if midi_in and midi_out else client.connect())
    if not connected:
        print("Error: Device 'E-Sensor' not found.", file=sys.stderr)
        return 1

    try:
        device_id     = client.get_device_id()
        version_tuple = client.get_version()

        if device_id is None:
            print("Error: Could not retrieve device ID.", file=sys.stderr)
            return 1

        # 本人確認: 期待した個体と違うポートに繋がっていたら中止(取り違え対策)
        if (expected_device_id is not None
                and device_id.upper() != expected_device_id.upper()):
            print(f"Error: expected {expected_device_id} but connected to "
                  f"{device_id}.", file=sys.stderr)
            return 2

        fw_version = (f"{version_tuple[0]}.{version_tuple[1]}.{version_tuple[2]}"
                      if version_tuple else "unknown")

        client.start_measurement()
        # 風速計は起動時 OFF。通信確認 (anemo_valid) のため明示的に起動して予熱を待つ。
        client.start_velocity()

        data = acquire_one_sample(client, timeout=DATA_ACQUIRE_TIMEOUT)

        # 判定
        if data:
            in_temp = TEMP_RANGE[0] <= data.temperature <= TEMP_RANGE[1]
            in_hum  = HUM_RANGE[0]  <= data.humidity    <= HUM_RANGE[1]
            judgments = {
                "temp_check":       data.env_valid and in_temp,
                "humidity_check":   data.env_valid and in_hum,
                "velocity_comm":    data.anemo_valid,
                "illuminance_comm": data.ill_valid,
                "co2_comm":         data.env_valid,
            }
        else:
            judgments = {k: False for k in (
                "temp_check", "humidity_check",
                "velocity_comm", "illuminance_comm", "co2_comm")}

        fname, overall = write_report(device_id, fw_version, data, judgments)

        result_str = "PASS" if overall else "FAIL"
        print(f"Device: {device_id} | FW: {fw_version} | Result: {result_str}")
        print(f"Report: {fname}")

        return 0 if overall else 1

    finally:
        try:
            client.stop_velocity()   # 自己発熱を止める（5V遮断）
        except Exception:
            pass
        try:
            client.stop_measurement()
        except Exception:
            pass
        client.close()


if __name__ == "__main__":
    sys.exit(main())
