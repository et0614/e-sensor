"""
デバッグ用: 15秒ごとに風速計(ヒータ)の有効/無効を切り替える。

PD3(TPS22965) による 5V On/Off と、その熱影響補正の挙動を確認するための簡易ツール。
- 風速計ON  : velocity/voltage が更新され、温湿度には自己発熱の影響が出る (Tcorr で補正)
- 風速計OFF : velocity/voltage = 0、anemo_valid = False、温湿度は生値 (発熱なし)

実行: python debug_velocity_toggle.py
停止: Ctrl+C
"""

import datetime
import time

from e_sensor import ESensorClient

TOGGLE_SEC = 15.0   # 切り替え間隔[秒]
POLL_SEC = 1.0      # データ取得インターバル[秒]


def main():
    client = ESensorClient()
    if not client.connect():
        print("Error: Device 'E-Sensor' not found.")
        return

    try:
        version = client.get_version()
        ver_str = f"{version[0]}.{version[1]}.{version[2]}" if version else "Unknown"
        print(f"--- Device connected (Firmware {ver_str}) ---")
        if version and (version[0], version[1]) < (1, 1):
            print("注意: ファームウェア v1.1 未満のため風速計 On/Off に対応していない可能性があります。")

        # 計測開始 + 熱影響補正を有効化
        client.start_measurement()
        client.enable_thermal_correction()

        # 初期状態: 風速計ON
        vel_on = True
        client.start_velocity()
        last_toggle = time.time()

        print(f"風速計を {TOGGLE_SEC:.0f} 秒ごとに切り替えます。Ctrl+C で停止。\n")
        print("Time     | Vel状態 | Temp[C] | Tcorr[C] | Hum[%] | Hcorr[%] | Vel[m/s] | Vol[V] | CO2[ppm] | Flags")
        print("---------|---------|---------|----------|--------|----------|----------|--------|----------|-------")

        while True:
            # 15秒経過で On/Off を切り替え
            now = time.time()
            if now - last_toggle >= TOGGLE_SEC:
                vel_on = not vel_on
                if vel_on:
                    client.start_velocity()
                else:
                    client.stop_velocity()
                last_toggle = now
                state = "起動 (ON)" if vel_on else "停止 (OFF)"
                ts = datetime.datetime.now().strftime('%H:%M:%S')
                print(f"--- {ts}  風速計を{state}に切り替え ---")

            data = client.get_data(timeout=0.5)
            if data:
                dt = datetime.datetime.fromtimestamp(data.timestamp)
                time_str = dt.strftime('%H:%M:%S')

                f_ill = "L" if data.ill_valid else "-"
                f_env = "E" if data.env_valid else "-"
                f_ane = "A" if data.anemo_valid else "-"
                flags_str = f"[{f_ill} {f_env} {f_ane}]"

                vel_str = " ON " if vel_on else " OFF"
                tc = data.corrected_temperature
                tc_str = f"{tc:8.2f}" if tc is not None else "    --  "
                hc = data.corrected_humidity
                hc_str = f"{hc:8.2f}" if hc is not None else "    --  "

                print(f"{time_str} |  {vel_str}   | {data.temperature:7.2f} | {tc_str} | "
                      f"{data.humidity:6.2f} | {hc_str} | {data.velocity:8.2f} | "
                      f"{data.voltage:6.3f} | {data.co2:8d} | {flags_str}")
            else:
                print(f"{datetime.datetime.now().strftime('%H:%M:%S')} | No Response from device...")

            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("\n停止します。")
    finally:
        # 終了時は風速計ONに戻して計測停止
        try:
            client.start_velocity()
            client.stop_measurement()
        except Exception:
            pass
        client.close()


if __name__ == "__main__":
    main()
