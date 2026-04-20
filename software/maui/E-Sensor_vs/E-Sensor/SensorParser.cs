using System;
using System.Collections.Generic;
using System.Text;

namespace E_Sensor
{
  public static class SensorParser
  {
    public static byte CalculateCrc8(ReadOnlySpan<byte> data)
    {
      byte crc = 0xFF;
      foreach (var b in data)
      {
        crc ^= b;
        for (int i = 0; i < 8; i++)
          crc = (byte)((crc & 0x80) != 0 ? (crc << 1) ^ 0x31 : crc << 1);
      }
      return crc;
    }

    public static byte[] DecodeNibbles(byte[] data)
    {
      var res = new byte[data.Length / 2];
      for (int i = 0; i < res.Length; i++)
        res[i] = (byte)((data[i * 2] << 4) | data[i * 2 + 1]);
      return res;
    }

  }
}
