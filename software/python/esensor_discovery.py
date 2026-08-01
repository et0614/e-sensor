"""
複数接続された E-Sensor(USB-MIDI) の発見・相関モジュール。

2つの独立した情報源を突き合わせて、物理USBポートと MIDI ペアを結ぶ:

  USB(PnP)  : {usb_port -> device_id}    … 物理ポート = 風洞のアンカー
  MIDI(mido): {device_id -> (in,out)}    … CMD_REQ_ID 応答で個体確定
  相関キー   : device_id == USB シリアル

WinMM 由来で MIDI ポート名が重複(全部同名)しても、CMD_REQ_ID の応答で
個体を確定できるため、名前に依存せず「風洞に割り当てた device_id の
正しい MIDI ペア」を開ける。詳細は midi_identify.py / memory 参照。

Python 3.12 で実行(rtmidi が要る)。
"""
import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

import mido

MANUF       = 0x7D
CMD_REQ_ID  = 0x10
CMD_ID_DATA = 0x09
KEYWORD     = 'E-Sensor'

# --- USB 列挙は SetupAPI(ctypes)でネイティブに行う。PowerShell(Get-PnpDevice /
#     Get-PnpDeviceProperty)は 1 回 7〜15 秒かかりポーリングに使えないため。
#     E-Sensor の複合デバイス親 (InstanceId='USB\VID_04D8&PID_0057\<serial>') を
#     列挙し、シリアルと物理ポート(LocationPaths)を取得する(数十 ms)。
_setupapi = ctypes.WinDLL('setupapi', use_last_error=True)

_DIGCF_PRESENT        = 0x00000002
_DIGCF_ALLCLASSES     = 0x00000004
_SPDRP_LOCATION_PATHS = 0x00000023
_INVALID_HANDLE       = ctypes.c_void_p(-1).value
# 複合親のみ対象(子 '...&MI_00\...' は末尾 '\\' で除外される)
_INSTID_PREFIX        = 'USB\\VID_04D8&PID_0057\\'


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('ClassGuid', _GUID),
                ('DevInst', wintypes.DWORD), ('Reserved', ctypes.POINTER(ctypes.c_ulong))]


_setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                           wintypes.HWND, wintypes.DWORD]
_setupapi.SetupDiEnumDeviceInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            ctypes.POINTER(_SP_DEVINFO_DATA)]
_setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
_setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_SP_DEVINFO_DATA), wintypes.LPWSTR,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
_setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_SP_DEVINFO_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ubyte),
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
_setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL


def _dev_instance_id(h, dev) -> str:
    req = wintypes.DWORD(0)
    _setupapi.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(dev), None, 0, ctypes.byref(req))
    if req.value == 0:
        return ''
    buf = ctypes.create_unicode_buffer(req.value)
    if _setupapi.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(dev), buf, req.value, None):
        return buf.value
    return ''


def _dev_location_paths(h, dev) -> list:
    req = wintypes.DWORD(0)
    _setupapi.SetupDiGetDeviceRegistryPropertyW(
        h, ctypes.byref(dev), _SPDRP_LOCATION_PATHS, None, None, 0, ctypes.byref(req))
    if req.value == 0:
        return []
    buf = (ctypes.c_ubyte * req.value)()
    if not _setupapi.SetupDiGetDeviceRegistryPropertyW(
            h, ctypes.byref(dev), _SPDRP_LOCATION_PATHS, None, buf, req.value, None):
        return []
    raw = bytes(buf).decode('utf-16-le', errors='ignore')
    return [s for s in raw.split('\x00') if s]


@dataclass
class UsbUnit:
    """USB 側で見えた 1 台。"""
    device_id: str      # == USB シリアル (例 'DFB8B859')
    port_addr: str      # ハブ上のポート番号 (表示用)
    usb_path: str       # LocationPath (物理ソケット固定 = 風洞アンカー)


@dataclass
class MidiPair:
    """MIDI 側で確定した 1 台の in/out ポート名。"""
    device_id: str
    in_name: str
    out_name: str


def list_usb_esensors() -> list:
    """接続中の E-Sensor を USB(SetupAPI)から列挙する。空ポートは列挙されない
    ため、ここに現れる = そのポートに個体が挿さっている。数十 ms で返る。"""
    h = _setupapi.SetupDiGetClassDevsW(None, 'USB', None,
                                       _DIGCF_PRESENT | _DIGCF_ALLCLASSES)
    if not h or h == _INVALID_HANDLE:
        return []
    units = []
    try:
        dev = _SP_DEVINFO_DATA()
        dev.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        i = 0
        while _setupapi.SetupDiEnumDeviceInfo(h, i, ctypes.byref(dev)):
            i += 1
            instid = _dev_instance_id(h, dev)
            if not instid.startswith(_INSTID_PREFIX):
                continue
            serial = instid.split('\\')[-1]
            paths = _dev_location_paths(h, dev)
            usb = next((p for p in paths if p.startswith('PCIROOT')), None)
            if not usb:
                continue
            addr = usb.rsplit('USB(', 1)[1].split(')')[0] if 'USB(' in usb else ''
            units.append(UsbUnit(device_id=serial, port_addr=addr, usb_path=usb))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(h)
    return units


def _decode_nibbles(nibbles) -> bytes:
    return bytes((nibbles[i] << 4) | nibbles[i + 1]
                 for i in range(0, len(nibbles) - 1, 2))


def identify_midi_pairs(probe_wait: float = 0.4) -> list:
    """各 OUT ポートに CMD_REQ_ID を送り、応答が返る IN ポートと device_id を
    確定して in/out ペアを作る。ポート名が重複していても個体を一意に識別できる。
    rtmidi のポート番号は抜き差しでズレうるので、開く直前に毎回呼ぶこと。"""
    in_names  = [n for n in mido.get_input_names()  if KEYWORD in n]
    out_names = [n for n in mido.get_output_names() if KEYWORD in n]

    # 既に別ワーカーが校正中で使用中のポートは開けない(busy)。その個体は
    # 割当済みで device_id が既知なので、開けないポートはスキップして続行する。
    inports = {}
    for n in in_names:
        try:
            inports[n] = mido.open_input(n)
        except (IOError, OSError):
            pass
    pairs = []
    try:
        for oname in out_names:
            try:
                out = mido.open_output(oname)
            except (IOError, OSError):
                continue
            try:
                for ip in inports.values():
                    for _ in ip.iter_pending():
                        pass
                out.send(mido.Message('sysex', data=[MANUF, CMD_REQ_ID]))
                time.sleep(probe_wait)
                for iname, ip in inports.items():
                    hit = None
                    for msg in ip.iter_pending():
                        if (msg.type == 'sysex' and len(msg.data) >= 2 and
                                msg.data[0] == MANUF and msg.data[1] == CMD_ID_DATA):
                            raw = _decode_nibbles(list(msg.data[2:]))
                            hit = raw[:4].hex().upper()
                            break
                    if hit:
                        pairs.append(MidiPair(device_id=hit, in_name=iname, out_name=oname))
                        break
            finally:
                out.close()
    finally:
        for ip in inports.values():
            ip.close()
    return pairs


def find_midi_pair(device_id: str, probe_wait: float = 0.4) -> Optional[MidiPair]:
    """指定 device_id の現在の MIDI ペアを返す(無ければ None)。"""
    for p in identify_midi_pairs(probe_wait=probe_wait):
        if p.device_id.upper() == device_id.upper():
            return p
    return None


if __name__ == '__main__':
    print('=== USB E-Sensors ===')
    usb = list_usb_esensors()
    for u in usb:
        print(f'  device_id={u.device_id}  port_addr={u.port_addr}  path={u.usb_path}')
    if not usb:
        print('  (none)')

    print('\n=== MIDI identification ===')
    pairs = identify_midi_pairs()
    for p in pairs:
        print(f'  device_id={p.device_id}  in={p.in_name!r}  out={p.out_name!r}')
    if not pairs:
        print('  (none)')

    print('\n=== correlation (usb_port <-> midi) ===')
    midi_by_id = {p.device_id.upper(): p for p in pairs}
    for u in usb:
        p = midi_by_id.get(u.device_id.upper())
        state = f'in={p.in_name!r} out={p.out_name!r}' if p else 'NO MIDI MATCH'
        print(f'  port_addr={u.port_addr}  device_id={u.device_id}  ->  {state}')
