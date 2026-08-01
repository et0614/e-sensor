"""
出荷動作確認 (verify_device) と風速校正 (calibrate_coefficients) を一括実行する。

順に実行する（verify を先、calibration を後にする）:
  1. verify_device.main()
       温湿度レンジと各センサの通信を確認し、verify_reports/{id}.md を出力する。
  2. AnemometerCalibrator().run_calibration(show_plot=True)
       風速計の King の法則係数を測定・書き込み・検証し、
       calibration_data/{id}.json と calibration_data/plot_{id}.png を出力する。
       最後に確認用グラフを表示し、ウィンドウを閉じるまでブロックする
       （校正の都度、目視で当てはまりを確認するため）。calibration を最後に
       置くことで、このブロックが後続処理を妨げない。

動作確認が PASS しなくても校正は続行する（温湿度レンジ等、一時的な要因で
FAIL することがあるため）。最終的な合否は各レポートで確認する。

使い方:
    python run_calibration_and_verify.py
"""
import sys
import time

from calibrate_coefficients import AnemometerCalibrator
import verify_device


def main() -> int:
    print("=" * 48)
    print(" 1/2: 動作確認 (verify_device)")
    print("=" * 48)
    rc = verify_device.main()
    if rc != 0:
        print("\n[注意] 動作確認が PASS しませんでした。レポートを確認してください。"
              "（校正はこのまま続行します）")

    # verify 側が MIDI ポートを解放しきるまで少し待ってから再接続する。
    time.sleep(0.5)

    print("\n" + "=" * 48)
    print(" 2/2: 風速校正 (calibrate_coefficients)")
    print("=" * 48)
    # 校正の最後に確認用グラフを表示（目視確認のためウィンドウを閉じるまでブロック）。
    ok = AnemometerCalibrator().run_calibration(show_plot=True)

    print("\n" + "=" * 48)
    print(" 完了: 動作確認=" + ("PASS" if rc == 0 else "FAIL")
          + " / 校正=" + ("OK" if ok else "NG/中止"))
    print("=" * 48)
    return 0 if (rc == 0 and ok) else 1


if __name__ == "__main__":
    sys.exit(main())
