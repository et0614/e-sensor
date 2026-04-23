using System;
using System.Collections.Generic;
using System.Text;

namespace E_Sensor
{

  public static class MidiCommands
  {
    public const byte MANUFACTURER_ID = 0x7D;

    // コマンドID定義
    public const byte CMD_SENS_DATA = 0x01; // D->H: データ送信
    public const byte CMD_REQ_DATA = 0x02; // H->D: データ要求
    public const byte CMD_START_MEAS = 0x03; // H->D: 計測開始
    public const byte CMD_STOP_MEAS = 0x04; // H->D: 計測停止
    public const byte CMD_ID_RES = 0x09; // D->H: ID応答
    public const byte CMD_ID_REQ = 0x10; // H->D: ID要求
    public const byte CMD_VER_RES = 0x11; // D->H: バージョン応答
    public const byte CMD_VER_REQ = 0x12; // H->D: バージョン要求
  }

  public interface IMidiService
  {
    event Action<byte[]> MessageReceived;

    event Action<bool> ConnectionChanged;

    void SendSysEx(byte cmdId, byte[] payload = null);

    bool IsConnected { get; }

    void Close();
  }
}
