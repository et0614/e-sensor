"""
風速センサ ON/OFF 切り替えの動作確認スクリプト。

iPhone (MAUI) で OFF にしてもセンサが熱いまま、という症状の切り分け用。
電圧（ブリッジ駆動電圧）と anemo_valid フラグが OFF 指示で実際に変化するかを見る。

重要な前提:
  OFF 時に報告される電圧 0mV は「計測値」ではない。Main MCU の CMD_STOP_VEL
  ハンドラが current_data.voltage = 0 とフィールドをゼロ代入しているだけで、
  アナログ回路（ブリッジ駆動電圧）の実状態は反映していない。
  このスクリプトで電気的に分かるのは「停止コマンドが Velocity MCU まで伝播したか
  （= anemo_valid が False になるか）」までで、ヒータ電源が実際に切れたかは判定
  できない。ヒータが本当に消費電力を失ったかはサーモカメラ／テスタで物理計測する。

判定の考え方:
  - OFF 指示後に anemo_valid が False になる
        → 停止コマンドは firmware まで伝播している。
          それでもサーモカメラで熱いままなら、PF4(SLP) を Low に駆動しても
          ヒータ電源経路が遮断されていない（ハード側）可能性が濃厚。
  - OFF 指示後も anemo_valid が True のまま
        → コマンドが効いていない（firmware が未対応 or コマンド未到達）。

使い方:
  python test_velocity_toggle.py
"""

import time
import statistics
from e_sensor import ESensorClient

# 各フェーズでサンプリングする秒数
SAMPLE_SECONDS = 4.0
# OFF 反映を待つ秒数（Velocity MCU の設定反映は即時だが余裕を持たせる）
SETTLE_OFF_SECONDS = 3.0
# ON 後の予熱待ち（firmware HEATING_MSEC = 5 秒 + 余裕）
SETTLE_ON_SECONDS = 7.0


def sample(client, seconds, label):
    """指定秒数だけ計測値を収集し、電圧・風速・anemo_valid を集計して返す。"""
    voltages = []
    velocities = []
    valid_true = 0
    valid_false = 0
    n_none = 0

    print(f"\n--- [{label}] {seconds:.0f} 秒間サンプリング ---")
    start = time.time()
    while (time.time() - start) < seconds:
        data = client.get_data(timeout=0.5)
        if data is None:
            n_none += 1
            print("  (応答なし)")
            continue
        voltages.append(data.voltage)
        velocities.append(data.velocity)
        if data.anemo_valid:
            valid_true += 1
        else:
            valid_false += 1
        print(f"  V={data.voltage*1000:6.1f} mV | "
              f"vel={data.velocity:5.2f} m/s | "
              f"anemo_valid={data.anemo_valid}")
        time.sleep(0.2)

    if voltages:
        v_avg = statistics.mean(voltages)
        v_min = min(voltages)
        v_max = max(voltages)
    else:
        v_avg = v_min = v_max = float("nan")

    print(f"  => 電圧 平均 {v_avg*1000:.1f} mV "
          f"(min {v_min*1000:.1f} / max {v_max*1000:.1f}) , "
          f"valid True/False = {valid_true}/{valid_false}, 応答なし {n_none}")

    return {
        "v_avg": v_avg,
        "v_min": v_min,
        "v_max": v_max,
        "valid_true": valid_true,
        "valid_false": valid_false,
        "samples": len(voltages),
    }


def main():
    client = ESensorClient()
    if not client.connect():
        print("エラー: デバイス 'E-Sensor' が見つかりません。接続を確認してください。")
        return

    try:
        # バージョン確認（風速 ON/OFF は firmware 1.1.0 以降）
        version = client.get_version()
        if version:
            print(f"Firmware version: {version[0]}.{version[1]}.{version[2]}")
            if (version[0], version[1]) < (1, 1):
                print("  !! このファームは 1.1.0 未満のため風速 ON/OFF コマンドに未対応の可能性があります。")
        else:
            print("Firmware version: 取得できませんでした。")

        client.start_measurement()
        time.sleep(0.5)

        # Phase 1: 初期状態（風速 ON 前提）
        base = sample(client, SAMPLE_SECONDS, "Phase 1: 初期状態 (ON 想定)")

        # Phase 2: OFF を指示
        print("\n>>> stop_velocity() 送信")
        client.stop_velocity()
        time.sleep(SETTLE_OFF_SECONDS)
        off = sample(client, SAMPLE_SECONDS, "Phase 2: OFF 指示後")

        # Phase 3: 再び ON
        print("\n>>> start_velocity() 送信")
        client.start_velocity()
        print(f"    予熱待ち {SETTLE_ON_SECONDS:.0f} 秒...")
        time.sleep(SETTLE_ON_SECONDS)
        on = sample(client, SAMPLE_SECONDS, "Phase 3: 再 ON 後")

        # ---- 判定 ----
        print("\n" + "=" * 64)
        print("判定")
        print("=" * 64)
        print(f"  ON初期   : 平均 {base['v_avg']*1000:.1f} mV, valid={base['valid_true']}/{base['valid_true']+base['valid_false']}")
        print(f"  OFF指示後: 平均 {off['v_avg']*1000:.1f} mV, valid={off['valid_true']}/{off['valid_true']+off['valid_false']}")
        print(f"  再ON後   : 平均 {on['v_avg']*1000:.1f} mV, valid={on['valid_true']}/{on['valid_true']+on['valid_false']}")

        # 注意: 電圧 0mV は停止ハンドラのゼロ代入であり判定根拠にしない。
        # 停止コマンドの伝播は anemo_valid が落ちたか否かで判断する。
        off_flag_cleared = off["valid_true"] == 0 and off["valid_false"] > 0
        on_recovered = on["valid_true"] > 0

        print()
        print("  ※ OFF 時の電圧 0mV は停止ハンドラのゼロ代入であり、回路の実状態ではない。")
        print()
        if off_flag_cleared and on_recovered:
            print("  ✓ OFF 指示で anemo_valid が False になり、再 ON で復帰しました。")
            print("    → 停止コマンドは firmware まで正しく伝播しています。")
            print("    それでもサーモカメラで熱いままなら、PF4(SLP) を Low に駆動しても")
            print("    ヒータ電源経路が遮断されていない（ハード側の問題）可能性が高いです。")
            print("    確認手順: ①OFF 時に PF4 が 0V か ②SLP 制御レギュレータの VOUT が")
            print("    OFF 時も ~5V のままか ③ブリッジ/ヒータが常時 ON レールに繋がっていないか")
        elif not off_flag_cleared:
            print("  ✗ OFF 指示後も anemo_valid が True のままです。")
            print("    → 停止コマンドが効いていません（コマンド未到達 / firmware 未対応 /")
            print("      Velocity MCU 側で enable が反映されていない）。")
        else:
            print("  △ OFF は伝播したが再 ON で復帰しませんでした。予熱待ち時間やログを確認してください。")

    finally:
        # 後始末: 風速を ON に戻して計測停止
        client.start_velocity()
        client.stop_measurement()
        client.close()
        print("\n完了。接続を閉じました。")


if __name__ == "__main__":
    main()
