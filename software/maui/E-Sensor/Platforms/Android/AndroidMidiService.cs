using Android.Content;
using Android.Media.Midi;
using Android.OS;
using System.Runtime.Versioning;

namespace E_Sensor.Platforms.Android
{
  [SupportedOSPlatform("android23.0")]
  internal class AndroidMidiService: Java.Lang.Object, IMidiService, MidiManager.IOnDeviceOpenedListener, IDisposable
  {
    private const string TargetDeviceName = "E-Sensor";
    private const byte ManufacturerId = 0x7D;

    private readonly MidiManager _midiManager;
    private MidiDevice _midiDevice;
    private MidiInputPort _inputPort;
    private MidiOutputPort _outputPort;

    private readonly MidiDeviceCallback _deviceCallback;
    private readonly Handler _handler;

    public bool IsConnected { get; private set; }

    public event Action<byte[]>? MessageReceived;

    public event Action<bool> ConnectionChanged;

    private bool _isConnecting = false;

    public AndroidMidiService()
    {
      _midiManager = (MidiManager)Platform.AppContext.GetSystemService(Context.MidiService)!;
      _handler = new Handler(Looper.MainLooper!);
      _deviceCallback = new MidiDeviceCallback(this);

      // コールバックを登録してリアルタイム監視を開始
#pragma warning disable CA1422 // 廃止されたメンバの警告を抑制
      _midiManager.RegisterDeviceCallback(_deviceCallback, new Handler(Looper.MainLooper!));
#pragma warning restore CA1422

      // 起動時に接続試行
      _ = TryConnectAsync();
    }

    private async Task TryConnectAsync()
    {
      if (IsConnected || _isConnecting) return;
      _isConnecting = true;

      try
      {
#pragma warning disable CA1422
        var devices = _midiManager.GetDevices();
#pragma warning restore CA1422
        var deviceInfo = devices.FirstOrDefault(d =>
            d.Properties.GetString(MidiDeviceInfo.PropertyName)?.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase) == true);

        if (deviceInfo != null)
        {
          // デバイスを開く（非同期）
          _midiManager.OpenDevice(deviceInfo, this, new Handler(Looper.MainLooper!));
        }
      }
      catch (Exception ex)
      {
        System.Diagnostics.Debug.WriteLine($"[MIDI] Android Connection Error: {ex.Message}");
      }
      finally
      {
        _isConnecting = false;
      }
    }

    // IOnDeviceOpenedListener の実装
    public void OnDeviceOpened(MidiDevice? device)
    {
      if (device == null) return;

      _midiDevice = device;

      // 通常、センサーデバイスは Port 0 を使用する
      _inputPort = _midiDevice.OpenInputPort(0);
      _outputPort = _midiDevice.OpenOutputPort(0);

      if (_outputPort != null)
      {
        var receiver = new MidiDataReceiver();
        receiver.OnDataReceived += (data) => MessageReceived?.Invoke(data);
        _outputPort.Connect(receiver);
      }

      IsConnected = true;
      ConnectionChanged?.Invoke(true);

      // 接続成立後の計測開始 (CMD_START_MEAS) は ViewModel 側で一元送信する。
    }

    public void SendSysEx(byte cmdId, byte[]? payload = null)
    {
      if (!IsConnected || _inputPort == null) return;

      var data = new List<byte> { 0xF0, ManufacturerId, cmdId };
      if (payload != null)
      {
        foreach (var b in payload)
        {
          data.Add((byte)((b >> 4) & 0x0F));
          data.Add((byte)(b & 0x0F));
        }
      }
      data.Add(0xF7);

      var rawData = data.ToArray();
      _inputPort.Send(rawData, 0, rawData.Length);
    }

    public void Close()
    {
      // 既に切断されている場合は何もしない
      if (!IsConnected) return;

      IsConnected = false;
      ConnectionChanged?.Invoke(false);

      _inputPort?.Close();
      _outputPort?.Close();
      _midiDevice?.Close();

      _inputPort = null!;
      _outputPort = null!;
      _midiDevice = null!;

      // ViewModel側に切断を通知したい場合は、ここでMessageReceivedなどを利用して空データを送るか、
      // 別途「切断イベント」を定義して発行するのがスマートです。
    }

    protected override void Dispose(bool disposing)
    {
      if (disposing)
      {
        // 1. コールバックの監視を解除（重要！）
        _midiManager?.UnregisterDeviceCallback(_deviceCallback);

        // 2. ポートのクローズ
        Close();
      }
      base.Dispose(disposing);
    }

    #region インナークラスの定義

    // --- 抜き差しを検知するための内部クラス ---
    private class MidiDeviceCallback : MidiManager.DeviceCallback
    {
      private readonly AndroidMidiService _parent;
      public MidiDeviceCallback(AndroidMidiService parent) => _parent = parent;

      // デバイスが接続された
      public override void OnDeviceAdded(MidiDeviceInfo? device)
      {
        var name = device?.Properties.GetString(MidiDeviceInfo.PropertyName);
        if (name?.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase) == true)
        {
          _ = _parent.TryConnectAsync();
        }
      }

      // デバイスが切断された
      public override void OnDeviceRemoved(MidiDeviceInfo? device)
      {
        var name = device?.Properties.GetString(MidiDeviceInfo.PropertyName);
        if (name?.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase) == true)
        {
          _parent.Close();
          // ViewModel側でメッセージを更新したい場合は、ここでイベントを発行する
        }
      }
    }

    // 内部用レシーバークラス
    private class MidiDataReceiver : MidiReceiver
    {
      public event Action<byte[]>? OnDataReceived;
      private readonly List<byte> _buffer = new(); // 受信バッファ

      public override void OnSend(byte[]? msg, int offset, int count, long timestamp)
      {
        if (msg == null) return;

        for (int i = 0; i < count; i++)
        {
          byte b = msg[offset + i];
          if (b == 0xF0)
          {
            _buffer.Clear();
            continue;
          }
          if (b == 0xF7)
          {
            if (_buffer.Count > 0)
            {
              OnDataReceived?.Invoke(_buffer.ToArray());
              _buffer.Clear();
            }
            continue;
          }
          _buffer.Add(b);
        }
      }
    }


    #endregion

  }
}
