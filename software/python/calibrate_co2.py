import sys
import time
import argparse
from e_sensor import ESensorClient

def main():
    # 1. 引数のパース
    parser = argparse.ArgumentParser(description='E-Sensor CO2 Calibration Tool')
    parser.add_argument('ppm', type=int, help='Target CO2 concentration in ppm (e.g., 400)')
    parser.add_argument('--port', type=str, default='E-Sensor', help='MIDI port keyword')
    parser.add_argument('--timeout', type=float, default=2.0, help='Wait timeout for response (sec)')
    
    args = parser.parse_args()
    target_ppm = args.ppm

    # 2. クライアントの初期化と接続
    client = ESensorClient(port_keyword=args.port)
    if not client.connect():
        print(f"Error: Device with keyword '{args.port}' not found.")
        sys.exit(1)

    try:
        print(f"Connecting to device...")
        # 接続確認（ID取得を試行）
        device_id = client.get_device_id(timeout=1.0)
        if device_id:
            print(f"Device ID: {device_id}")
        
        # 3. 校正コマンドの送信
        print(f"Sending CO2 calibration command: Target = {target_ppm} ppm")
        client.perform_co2_calibration(target_ppm)

        # 4. 応答の待機
        # ライアント内部の _last_co2_correction が更新されるのを待つ
        client._last_co2_correction = None
        start_time = time.time()
        success = False

        print("Waiting for confirmation from device...")
        while (time.time() - start_time) < args.timeout:
            client.poll()
            if client._last_co2_correction is not None:
                print(f"Calibration successful.")
                print(f"Correction value received: {client._last_co2_correction}")
                success = True
                break
            time.sleep(0.1)

        if not success:
            print("Error: Calibration timed out. No response from device.")
            sys.exit(1)

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)
    finally:
        client.close()
        print("Connection closed.")

if __name__ == "__main__":
    main()