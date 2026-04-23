using Melanchall.DryWetMidi.Multimedia;
using Melanchall.DryWetMidi.Core;

namespace E_Sensor.Platforms.Windows
{
  public class WindowsMidiService : IMidiService, IDisposable
  {

    #region 定数宣言

    private const string TargetDeviceName = "E-Sensor";
    private const byte ManufacturerId = 0x7D;

    #endregion

    #region IMidiService実装

    public event Action<bool> ConnectionChanged;

    public event Action<byte[]> MessageReceived;

    public bool IsConnected { get; private set; }

    public void SendSysEx(byte cmdId, byte[] payload = null)
    {
      if (!IsConnected || _output == null) return;

      var data = new List<byte> { ManufacturerId, cmdId };
      if (payload != null)
      {
        foreach (var b in payload)
        {
          data.Add((byte)((b >> 4) & 0x0F));
          data.Add((byte)(b & 0x0F));
        }
      }

      // SysExの終了バイト(EOX)を追加
      data.Add(0xF7);

      try
      {
        var sysExEvent = new NormalSysExEvent(data.ToArray());
        _output.SendEvent(sysExEvent);
      }
      catch (Exception ex)
      {
        System.Diagnostics.Debug.WriteLine($"[MIDI] Send error: {ex.Message}");
      }
    }

    public void Close()
    {
      IsConnected = false;
      ConnectionChanged?.Invoke(false);
      if (_input != null)
      {
        _input.StopEventsListening();
        _input.Dispose();
        _input = null;
      }
      if (_output != null)
      {
        _output.Dispose();
        _output = null;
      }
    }


    #endregion

    #region インスタンス変数・プロパティ定義

    private IInputDevice _input;
    private IOutputDevice _output;

    private bool _isConnecting = false;

    private System.Timers.Timer _deviceCheckTimer;
    private List<string> _lastDeviceNames = new();

    #endregion
    
    
    public WindowsMidiService()
    {
      // 1秒ごとにデバイスをチェックするタイマーを設定
      _deviceCheckTimer = new System.Timers.Timer(1000);
      _deviceCheckTimer.Elapsed += (s, e) => CheckDevices();
      _deviceCheckTimer.AutoReset = true;
      _deviceCheckTimer.Enabled = true;

      // 起動時に接続されているか確認
      Task.Run(() => TryConnect());
    }

    private void CheckDevices()
    {
      var currentDevices = InputDevice.GetAll().Select(d => d.Name).ToList();

      // デバイスが追加されたかチェック
      foreach (var name in currentDevices)
      {
        if (!_lastDeviceNames.Contains(name))
        {
          if (!IsConnected && name.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase))
          {
            TryConnect();
          }
        }
      }

      // デバイスが削除されたかチェック
      if (IsConnected && !currentDevices.Any(n => n.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase)))
      {
        Close();
        // ViewModel 等に通知が必要な場合はここでイベントを飛ばす
      }

      _lastDeviceNames = currentDevices;
    }

    private async void TryConnect()
    {
      if (IsConnected || _isConnecting) return; // 接続中または試行中は抜ける
      _isConnecting = true;


      try
      {
        // 名前が一致するデバイスが存在するか
        var inputInfo = InputDevice.GetAll().FirstOrDefault(d =>
            d.Name.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase));

        var outputInfo = OutputDevice.GetAll().FirstOrDefault(d =>
            d.Name.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase));

        if (inputInfo != null && outputInfo != null)
        {
          // 念のため既存の接続をクリア
          Close();

          _input = inputInfo;
          _output = outputInfo;

          _output.PrepareForEventsSending();

          _input.EventReceived += OnMidiEventReceived;
          _input.StartEventsListening();
          IsConnected = true;
          ConnectionChanged?.Invoke(true);

          // 自動計測開始
          await Task.Delay(300);
          SendSysEx(MidiCommands.CMD_START_MEAS);
        }
      }
      catch(Exception ex)
      {
        System.Diagnostics.Debug.WriteLine($"[MIDI] Connection failed: {ex.Message}"); 
        IsConnected = false;
        ConnectionChanged?.Invoke(false);
      }
      finally
      {
        _isConnecting = false;
      }
    }

    // イベント受信時の処理
    private void OnMidiEventReceived(object sender, MidiEventReceivedEventArgs e)
    {
      // Normal か Escape かを問わず SysExEvent 全般を対象にする
      if (e.Event is SysExEvent sysEx)
      {
        MessageReceived?.Invoke(sysEx.Data);
      }
    }

    public void Dispose()
    {
      _deviceCheckTimer?.Stop();
      _deviceCheckTimer?.Dispose();
      Close();
    }
  }
}
