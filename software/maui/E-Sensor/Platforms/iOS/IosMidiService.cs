using AVFoundation;
using CoreMidi;
using System.Runtime.InteropServices;

namespace E_Sensor.Platforms.iOS
{
  public class IosMidiService : IMidiService, IDisposable
  {
    private const string TargetDeviceName = "E-Sensor";
    private const byte ManufacturerId = 0x7D;

    private MidiClient? _client;
    private MidiPort? _inputPort;
    private MidiPort? _outputPort;
    private MidiEndpoint? _source;
    private MidiEndpoint? _destination;

    public bool IsConnected { get; private set; }

    public event Action<byte[]>? MessageReceived;

    public event Action<bool> ConnectionChanged;

    private readonly List<byte> _sysExBuffer = new();

    public IosMidiService()
    {
      InitializeMidi();
    }

    private void InitializeMidi()
    {
      // --- バックグラウンド動作を維持するためのオーディオセッション設定
      var session = AVAudioSession.SharedInstance();
      // 他のアプリの音を邪魔しない (MixWithOthers) 設定でPlaybackカテゴリを指定
      session.SetCategory(AVAudioSessionCategory.Playback, AVAudioSessionCategoryOptions.MixWithOthers);
      session.SetActive(true);

      // MidiClient の作成（引数は名前のみ）
      _client = new MidiClient(TargetDeviceName);

      // 抜き差し等の監視はイベントを購読する
      _client.ObjectAdded += OnMidiObjectAdded;
      _client.ObjectRemoved += OnMidiObjectRemoved;

      // ポートの作成
      _inputPort = _client.CreateInputPort(TargetDeviceName + " Input");
      if (_inputPort != null)
      {
        _inputPort.MessageReceived += OnMidiMessageReceived;
      }

      _outputPort = _client.CreateOutputPort(TargetDeviceName + " Output");

      TryConnect();
    }

    // デバイスが追加された時のイベントハンドラ
    private void OnMidiObjectAdded(object? sender, ObjectAddedOrRemovedEventArgs e)
    {
      // 何らかのMIDIオブジェクト（デバイス、エンティティ、エンドポイント等）が追加されたら再接続を試みる
      if (!IsConnected)
      {
        TryConnect();
      }
    }

    // デバイスが削除された時のイベントハンドラ
    private void OnMidiObjectRemoved(object? sender, ObjectAddedOrRemovedEventArgs e)
    {
      if (IsConnected && e.Child?.Handle == _source?.Handle)
      {
        CloseConnectionOnly();
      }
    }

    private void TryConnect()
    {
      if (IsConnected) return;

      for (int i = 0; i < (int)Midi.SourceCount; i++)
      {
        var endpoint = MidiEndpoint.GetSource(i);
        if (endpoint?.Name?.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase) == true)
        {
          _source = endpoint;
          _inputPort?.ConnectSource(_source);
          break;
        }
      }

      for (int i = 0; i < (int)Midi.DestinationCount; i++)
      {
        var endpoint = MidiEndpoint.GetDestination(i);
        if (endpoint?.Name?.Contains(TargetDeviceName, StringComparison.OrdinalIgnoreCase) == true)
        {
          _destination = endpoint;
          IsConnected = true;
          ConnectionChanged?.Invoke(true);
          break;
        }
      }

      // 接続成立後の計測開始 (CMD_START_MEAS) は ViewModel 側で一元送信する。
    }

    private void OnMidiMessageReceived(object? sender, MidiPacketsEventArgs e)
    {
      foreach (var packet in e.Packets)
      {
        int length = packet.Length;
        if (length == 0) continue;

        byte[] buffer = new byte[length];
        Marshal.Copy(packet.Bytes, buffer, 0, length);

        foreach (var b in buffer)
        {
          if (b == 0xF0)
          {
            // 開始バイトを見つけたらバッファをクリアして開始
            _sysExBuffer.Clear();
            continue;
          }

          if (b == 0xF7)
          {
            // 終了バイトを見つけたら、蓄積したデータをViewModelへ飛ばす
            if (_sysExBuffer.Count > 0)
            {
              // 完了したメッセージを通知（この時点でF0とF7は含まれない）
              MessageReceived?.Invoke(_sysExBuffer.ToArray());
              _sysExBuffer.Clear();
            }
            continue;
          }

          // F0を受け取った後のデータであれば蓄積
          _sysExBuffer.Add(b);
        }
      }
    }

    public void SendSysEx(byte cmdId, byte[]? payload = null)
    {
      if (!IsConnected || _outputPort == null || _destination == null) return;

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
      var packet = new MidiPacket(0, rawData, 0, rawData.Length);
      _outputPort.Send(_destination, new[] { packet });
    }

    // クライアントを破棄せず、接続状態（エンドポイント等）のみをクリアする内部メソッド
    private void CloseConnectionOnly()
    {
      IsConnected = false;
      ConnectionChanged?.Invoke(false);
      if (_source != null)
      {
        _inputPort?.Disconnect(_source);
        _source.Dispose();
        _source = null;
      }
      _destination?.Dispose();
      _destination = null;
    }

    public void Close()
    {
      CloseConnectionOnly();
      _inputPort?.Dispose();
      _inputPort = null;
      _outputPort?.Dispose();
      _outputPort = null;

      if (_client != null)
      {
        _client.ObjectAdded -= OnMidiObjectAdded;
        _client.ObjectRemoved -= OnMidiObjectRemoved;
        _client.Dispose();
        _client = null;
      }
    }

    public void Dispose()
    {
      Close();
    }
  }
}