import csv
import time
import datetime
from e_sensor import ESensorClient

def main():
    client = ESensorClient()
    
    if not client.connect():
        print("デバイスが見つかりません。")
        return

    # ファイル名の生成 (yyyyMMdd_HHmm.csv)
    # 例: 20262103_1630.csv
    now = datetime.datetime.now()
    filename = now.strftime("%Y%m%d_%H%M.csv")
    
    # CSVのヘッダー定義
    fieldnames = [
        "Time", "Illuminance_lx", "Temperature_C", "Humidity_pct", 
        "Velocity_ms", "Voltage_V", "CO2_ppm", 
        "Ill_Valid", "Env_Valid", "Anemo_Valid"
    ]

    try:
        # デバイス情報の取得 (内部で自動的にリクエストを投げる)
        device_id = client.get_device_id()
        print(f"Connected: {device_id if device_id else 'Unknown'}")
        
        # デバイス側の計測処理を有効化
        client.start_measurement()
        print(f"Logging to {filename}... Press Ctrl+C to stop.\n")

        # CSVファイルをオープン
        with open(filename, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            # ヘッダー表示 (Volt列を追加)
            print(f"{'Time':<10} | {'Illum':>7} | {'Temp':>6} | {'Hum':>5} | {'Vel':>6} | {'Volt':>6} | {'CO2':>5} | Flags")
            print("-" * 85)

            while True:
                # get_data() を使用して明示的にデータを要求
                data = client.get_data(timeout=0.5)
                
                if data:
                    dt = datetime.datetime.fromtimestamp(data.timestamp)
                    time_str = dt.strftime('%H:%M:%S')

                    # フラグの文字列作成
                    f_ill = "L" if data.ill_valid else "-"
                    f_env = "E" if data.env_valid else "-"
                    f_ane = "A" if data.anemo_valid else "-"
                    flags_str = f"[{f_ill} {f_env} {f_ane}]"

                    # コンソール出力 (VoltとFlagsを追加)
                    print(f"{time_str:<10} | {data.illuminance:7.1f} | {data.temperature:6.2f} | "
                          f"{data.humidity:5.1f} | {data.velocity:6.2f} | {data.voltage:6.3f} | "
                          f"{data.co2:5d} | {flags_str}")

                    # CSV書き出し
                    writer.writerow({
                        "Time": dt.strftime('%Y-%m-%d %H:%M:%S'),
                        "Illuminance_lx": data.illuminance,
                        "Temperature_C": data.temperature,
                        "Humidity_pct": data.humidity,
                        "Velocity_ms": data.velocity,
                        "Voltage_V": data.voltage,
                        "CO2_ppm": data.co2,
                        "Ill_Valid": data.ill_valid,
                        "Env_Valid": data.env_valid,
                        "Anemo_Valid": data.anemo_valid
                    })
                    f.flush()  # 即座にファイルへ書き込み
                else:
                    # 応答がない場合の通知
                    print(f"{datetime.datetime.now().strftime('%H:%M:%S'):<10} | No response...")

                # デバイスの計測周期に合わせて1秒待機
                time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nロギングを停止しました。")
    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
    finally:
        client.stop_measurement()
        client.close()

if __name__ == "__main__":
    main()