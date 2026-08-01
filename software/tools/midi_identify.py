"""
複数接続された E-Sensor(USB-MIDI) を、ポート名ではなく SysEx の
CMD_REQ_ID 応答(デバイスID)で一意に識別するディスカバリ。

WinMM 由来でポート名が重複(全部 "E-Sensor <同一シリアル>")しても、
各 OUT ポートに ID 要求を送り、どの IN ポートで応答が返るかを見れば
  device_id  <->  (in_port, out_port)
の対応を確実に作れる。これを USB 側の {ポート->風洞} と device_id で
突き合わせれば、風洞ごとに正しい MIDI ペアを開ける。

実行: py -3.12 midi_identify.py   (rtmidi が要る)
"""
import time
import mido

MANUF        = 0x7D
CMD_REQ_ID   = 0x10
CMD_ID_DATA  = 0x09
KEYWORD      = 'E-Sensor'


def decode_nibbles(nibbles):
    return bytes((nibbles[i] << 4) | nibbles[i + 1]
                 for i in range(0, len(nibbles) - 1, 2))


def main():
    in_names  = [n for n in mido.get_input_names()  if KEYWORD in n]
    out_names = [n for n in mido.get_output_names() if KEYWORD in n]
    print('E-Sensor IN :', in_names)
    print('E-Sensor OUT:', out_names)
    print('-' * 60)

    inports = {n: mido.open_input(n) for n in in_names}
    pairs = []
    try:
        for oname in out_names:
            out = mido.open_output(oname)
            try:
                for ip in inports.values():          # 古い応答を掃除
                    for _ in ip.iter_pending():
                        pass
                out.send(mido.Message('sysex', data=[MANUF, CMD_REQ_ID]))
                time.sleep(0.4)
                found = None
                for iname, ip in inports.items():
                    for msg in ip.iter_pending():
                        if (msg.type == 'sysex' and len(msg.data) >= 2 and
                                msg.data[0] == MANUF and msg.data[1] == CMD_ID_DATA):
                            raw = decode_nibbles(list(msg.data[2:]))
                            dev_id = raw[:4].hex().upper()
                            found = (iname, dev_id)
                            break
                    if found:
                        break
                if found:
                    print(f'OUT {oname!r}\n  -> IN {found[0]!r}\n  -> device_id = {found[1]}')
                    pairs.append({'out': oname, 'in': found[0], 'device_id': found[1]})
                else:
                    print(f'OUT {oname!r}  -> no response')
            finally:
                out.close()
            print('-' * 60)
    finally:
        for ip in inports.values():
            ip.close()

    print('\n=== discovered pairs ===')
    for p in pairs:
        print(f"  device_id={p['device_id']}  in={p['in']!r}  out={p['out']!r}")
    ids = [p['device_id'] for p in pairs]
    print(f"\nunique device_ids: {sorted(set(ids))}  (count={len(set(ids))})")


if __name__ == '__main__':
    main()
