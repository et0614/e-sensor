import subprocess
import threading
import time

"""
QuadroFanをコマンドから使うには以下のインストールが必要
pip install liquidctl
"""

# 複数風洞GUIでは各ワーカーが別スレッドから set_power を呼ぶ。liquidctl は
# 同一 USB(HID) デバイスへ同時アクセスすると衝突しうるため、呼び出しを直列化する。
# (fan_index が違っても、対象は同じ Quadro 1台なので直列化が必要)
_LIQUIDCTL_LOCK = threading.Lock()


class QuadroFanController:
    """QUADROファンコントローラー制御"""
    def __init__(self):
        pass

    def set_power(self, power_percent: int, fan_index = 1):
        """ファン出力を 0-100% で設定"""
        target = f"fan{fan_index}"
        cmd = ["liquidctl", "--match", "quadro", "set", target, "speed", str(power_percent)]
        try:
            # check=Trueにすることで、コマンドが失敗した時に例外を投げる
            with _LIQUIDCTL_LOCK:
                subprocess.run(cmd, capture_output=True, check=True)
            print(f"[Fan] Set {target} to {power_percent}%")
        except subprocess.CalledProcessError as e:
            print(f"Error: Failed to set fan speed. {e}")
        except FileNotFoundError:
            print("Error: 'liquidctl' command not found. Please install it.")


# テストコード
if __name__ == "__main__":
    """5秒ごとに20%ずつ出力を上げるテスト"""
    fan = QuadroFanController()
    
    try:
        print("=== Fan Output Ramp-up Test Started ===")
        # 0% から 100% まで 20刻みでループ
        for power in range(0, 101, 20):
            print(f"\n[Next Step] Target: {power}%")
            fan.set_power(power)
            
            print(f"Waiting 5 seconds at {power}%...")
            time.sleep(5)
            
        print("\nTest sequence completed at 100%.")

    except KeyboardInterrupt:
        print("\nTest interrupted by user.")
    
    finally:
        print("Cleaning up: Setting fan to 0% and shutting down.")
        fan.set_power(0)