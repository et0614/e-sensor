using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace E_Sensor;

// 記録用データ構造
public record SensorLogEntry(DateTime Timestamp, double Temp, double Hum, double Vel, double Volt, double Ill, int Co2, bool IsTempValid, bool IsVelValid, bool IsIllValid);

public partial class MainViewModel : ObservableObject
{

  #region 定数宣言

  /// <summary>データの鮮度を保つ時間[sec]</summary>
  private const int FRESHNESS_TIMEOUT_SECONDS = 3;

  /// <summary>データを要求する時間間隔[msec]</summary>
  private const int POLLING_INTERVAL_MS = 200;

  /// <summary>データ記録の間隔[msec]</summary>
  private const int RECORDING_INTERVAL_MS = 1000;

  /// <summary>イースターエッグが出るまでの時間[sec]</summary>
  private const int EASTER_EGG_INTERVAL_SECONDS = 30;

  private const bool USE_DUMMY_DATA = false;

  #endregion

  #region ObservablePropertyの定義

  [ObservableProperty]
  [NotifyCanExecuteChangedFor(nameof(ExportDataCommand))]
  private int _recordedCount; // 現在のレコード数

  /// <summary>乾球温度[C]</summary>
  [ObservableProperty]
  private string _temperature = "---";

  /// <summary>相対湿度[%]</summary>
  [ObservableProperty]
  private string _humidity = "---";

  /// <summary>風速電圧[V]</summary>
  [ObservableProperty]
  private string _voltage = "---";

  /// <summary>風速[m/s]</summary>
  [ObservableProperty]
  private string _velocity = "---";

  /// <summary>照度[Lux]</summary>
  [ObservableProperty]
  private string _illuminance = "---";

  /// <summary>CO2濃度[ppm]</summary>
  [ObservableProperty]
  private string _co2Level = "---";

  /// <summary>照度計測値が有効か否か</summary>
  [ObservableProperty]
  private bool _isIlluminanceValid = true;

  /// <summary>温湿度とCO2計測値が有効か否か</summary>
  [ObservableProperty]
  private bool _isTemperatureValid = true;

  /// <summary>風速計測値が有効か否か</summary>
  [ObservableProperty]
  private bool _isVelocityValid = true;

  /// <summary>記録中か否か</summary>
  [ObservableProperty]
  private bool _isRecording;

  /// <summary>デバイスと接続済か否か</summary>
  [ObservableProperty]
  [NotifyCanExecuteChangedFor(nameof(StartRecordCommand))]
  [NotifyCanExecuteChangedFor(nameof(StopRecordCommand))]
  [NotifyCanExecuteChangedFor(nameof(ExportDataCommand))]
  [NotifyCanExecuteChangedFor(nameof(ToggleRecordCommand))]
  private bool _isDeviceConnected;

  /// <summary>計測データが新鮮か否か</summary>
  [ObservableProperty]
  private bool _isDataFresh = false;

  /// <summary>デバイスID</summary>
  [ObservableProperty]
  private string _deviceId = "---";

  /// <summary>ファームウェアのバージョン</summary>
  [ObservableProperty]
  private string _firmwareVersion = "---";

  #endregion

  #region インスタンス変数・プロパティの定義

  private readonly IMidiService _midiService;

  private readonly ILoggingService _loggingService;

  private readonly List<SensorLogEntry> _recordedData = new();

  /// <summary>データ鮮度計算用タイマ</summary>
  private IDispatcherTimer? _freshnessTimer;

  /// <summary>ダミーデータ表示用タイマ</summary>
  private IDispatcherTimer? _dummyDataTimer;

  /// <summary>データ定期収集タスクのトークン</summary>
  private CancellationTokenSource? _pollingCts;

  /// <summary>データ保存タスクのトークン</summary>
  private CancellationTokenSource? _recordingCts;

  /// <summary>最新の計測データ</summary>
  private SensorLogEntry? _latestEntry;

  private bool CanStartRecord() => IsDeviceConnected && !IsRecording;

  private bool CanStopRecord() => IsDeviceConnected && IsRecording;

  private bool CanExport() => RecordedCount > 0;

  /// <summary>接続開始してから有効なデータを受け取ったか否か</summary>
  private bool _hasValidDataReceived = false;

  #endregion

  #region コマンド定義

  [RelayCommand(CanExecute = nameof(IsDeviceConnected))]
  private async Task ToggleRecord()
  {
    if (IsRecording)
    {
      StopRecord(); // 既存の停止ロジック
    }
    else
    {
      await StartRecord(); // 既存の開始（確認ダイアログ付き）ロジック
    }
  }

  /// <summary>記録開始コマンド</summary>
  [RelayCommand(CanExecute = nameof(CanStartRecord))]
  private async Task StartRecord()
  {
    // データが存在する場合のみ確認
    if (0 < RecordedCount)
    {
      // 現在のウィンドウのPageを安全に取得
      var mainPage = Application.Current?.Windows.FirstOrDefault()?.Page;

      if (mainPage != null)
      {
        bool answer = await mainPage.DisplayAlertAsync(
            Resources.Strings.ConfirmTitle,
            Resources.Strings.ConfirmOverwrite,
            Resources.Strings.Yes,
            Resources.Strings.No);

        if (!answer) return;
      }
    }

    _recordedData.Clear();
    RecordedCount = 0; // カウントをリセット
    IsRecording = true;
  }

  /// <summary>記録停止コマンド</summary>
  [RelayCommand(CanExecute = nameof(CanStopRecord))]
  private void StopRecord()
  {
    IsRecording = false;
  }

  /// <summary>記録出力コマンド</summary>
  /// <returns></returns>
  [RelayCommand(CanExecute = nameof(CanExport))]
  private async Task ExportData()
  {
    if (_recordedData.Count == 0)
    {
      await App.Current.MainPage.DisplayAlertAsync("通知", "記録されたデータがありません", "OK");
      return;
    }

    try
    {
      // CSVデータの生成
      var sb = new System.Text.StringBuilder();
      sb.AppendLine("Date,Time,Temperature[C],Humidity[%],Velocity[m/s],Illuminance[Lux],CO2[ppm],Velocity Voltage[V],Temperature Valid,Velocity Valid,Illuminance Valid");
      foreach (var row in _recordedData)
      {
        // InvariantCultureを指定することで、OSの言語設定に関わらず小数点を「.」に固定(温湿度と照度は表示値よりも1桁精度高)
        var line = string.Format(System.Globalization.CultureInfo.InvariantCulture,
            "{0:yyyy-MM-dd,HH:mm:ss},{1:F2},{2:F2},{3:F3},{4:F1},{5},{6:F3},{7},{8},{9}",
            row.Timestamp, 
            row.Temp, 
            row.Hum, 
            row.Vel, 
            row.Ill, 
            row.Co2,
            row.Volt,
            row.IsTempValid,
            row.IsVelValid,
            row.IsIllValid            
            );
        sb.AppendLine(line);
      }

      // 一時ファイルへの保存（全プラットフォーム共通パス）
      string fileName = $"SensorData_{DateTime.Now:yyyyMMdd_HHmmss}.csv";
      string targetFile = Path.Combine(FileSystem.CacheDirectory, fileName);
      await File.WriteAllTextAsync(targetFile, sb.ToString());

      // 共有（Share）機能を使って保存や送信をユーザーに選ばせる
      await Share.Default.RequestAsync(new ShareFileRequest
      {
        Title = Resources.Strings.ExportMessage,
        File = new ShareFile(targetFile)
      });
    }
    catch (Exception ex)
    {
      await App.Current.MainPage.DisplayAlertAsync("エラー", $"エクスポートに失敗しました: {ex.Message}", "OK");
    }
  }

  #endregion

  #region コンストラクタ

  // コンストラクタ（DI経由で受け取る想定）
  public MainViewModel(IMidiService midiService, ILoggingService loggingService)
  {
    _midiService = midiService;
    _midiService.MessageReceived += OnMidiMessageReceived;
    _midiService.ConnectionChanged += OnMidiConnectionChanged;

    _loggingService = loggingService;

    // 起動時の初期状態を反映
    initializeAsync();
  }

  private async void initializeAsync()
  {
    // Application.Current が有効（UIスレッドが準備完了）になるまで待機
    while (Application.Current == null) await Task.Delay(100);

    // 最新の状態を強制的に反映
    MainThread.BeginInvokeOnMainThread(async () =>
    {
      initializeTimers();

      IsDeviceConnected = _midiService.IsConnected;

      // 接続変更時の処理を即座に実行
      OnMidiConnectionChanged(IsDeviceConnected);

      if (!IsDataFresh) _dogTimer?.Start();

      // ダミーデータ表示の開始（画面表示デバッグ用）
      if (USE_DUMMY_DATA) startDummyDataLoop();
    });
  }

  private void initializeTimers()
  {
    var dispatcher = Application.Current?.Dispatcher;
    if (dispatcher == null) return;

    // 犬用タイマー
    _dogTimer = dispatcher.CreateTimer();
    _dogTimer.Interval = TimeSpan.FromSeconds(EASTER_EGG_INTERVAL_SECONDS);
    _dogTimer.IsRepeating = false;
    _dogTimer.Tick += (s, e) =>
    {
      if (!IsDataFresh)
      {
        IsDogVisible = true;
        StartDogAnimationRequested?.Invoke();
      }
    };

    // 鮮度管理タイマー
    _freshnessTimer = dispatcher.CreateTimer();
    _freshnessTimer.Interval = TimeSpan.FromSeconds(FRESHNESS_TIMEOUT_SECONDS);
    _freshnessTimer.IsRepeating = true;
    _freshnessTimer.Tick += (s, e) =>
    {
      IsDataFresh = false;
    };
    _freshnessTimer?.Start();
  }


  private void UpdateUI()
  {
    // UI スレッド（メインスレッド）に切り替えて更新
    Application.Current?.Dispatcher.Dispatch(() =>
    {
      // データ鮮度の更新
      IsDataFresh = true;
      _freshnessTimer?.Stop();
      _freshnessTimer?.Start();

      // 有効データ取得フラグ
      IsIlluminanceValid = _latestEntry?.IsIllValid ?? false;
      IsTemperatureValid = _latestEntry?.IsTempValid ?? false;
      IsVelocityValid = _latestEntry?.IsVelValid ?? false;

      // 温湿度とCO2は起動に時間がかかるセンサーのため、初回有効データ受信前は INI と表示する
      if (IsTemperatureValid)
      {
        Temperature = _latestEntry!.Temp.ToString("F1");
        Humidity = _latestEntry!.Hum.ToString("F1");
        Co2Level = _latestEntry!.Co2.ToString();
        _hasValidDataReceived = true;
      }
      else if (!_hasValidDataReceived)
      {
        Temperature = "---";
        Humidity = "---";
        Co2Level = "---";
      }

      // 風速
      if (IsVelocityValid)
      {
        Velocity = _latestEntry!.Vel.ToString("F2");
        Voltage = _latestEntry!.Volt.ToString("F3");
      }
      else if (!_hasValidDataReceived) Velocity = "---";

      // 照度
      if (IsIlluminanceValid)
        Illuminance = _latestEntry!.Ill.ToString("F0");
      else if (!_hasValidDataReceived)
        Illuminance = "---";
    });
  }

  #endregion

  #region イベント発生時の動作定義

  /// <summary>センサデータ受信時の処理</summary>
  /// <param name="data">データ</param>
  private void OnMidiMessageReceived(byte[] data)
  {
    if (data.Length < 2 || data[0] != MidiCommands.MANUFACTURER_ID) return;
    var payload = SensorParser.DecodeNibbles(data.Skip(2).ToArray());

    // センサーデータ受信
    if (data[1] == MidiCommands.CMD_SENS_DATA)
    {
      if (payload.Length < 16) return;

      var dataPart = payload.AsSpan(0, 15);
      if (SensorParser.CalculateCrc8(dataPart) == payload[15])
      {
        parseSensorPacket(dataPart);
        UpdateUI();
      }
    }
    // ID応答
    else if (data[1] == MidiCommands.CMD_ID_RES)
    {
      Application.Current?.Dispatcher.Dispatch(() => {
        // 16進数表示
        DeviceId = BitConverter.ToString(payload).Replace("-", "");
      });
    }
    // バージョン応答
    else if (data[1] == MidiCommands.CMD_VER_RES)
    {
      Application.Current?.Dispatcher.Dispatch(() => {
        // 3バイト（メジャー.マイナー.リビジョン）形式
        FirmwareVersion = payload.Length >= 3 ? $"{payload[0]}.{payload[1]}.{payload[2]}" : "Unknown";
      });
    }
  }  

  /// <summary>MIDI Packetを解析して最新データとして保存する</summary>
  private void parseSensorPacket(ReadOnlySpan<byte> p)
  {
    // 数値の解析
    double ill = BitConverter.ToUInt32(p.Slice(0, 4)) / 10.0;
    double tmp = BitConverter.ToInt16(p.Slice(4, 2)) / 100.0;
    double hmd = BitConverter.ToUInt16(p.Slice(6, 2)) / 100.0;
    double vel = BitConverter.ToUInt16(p.Slice(8, 2)) / 1000.0;
    double vol = BitConverter.ToUInt16(p.Slice(10, 2)) / 1000.0;
    int co2 = BitConverter.ToUInt16(p.Slice(12, 2));

    // ステータスフラグの解析
    byte status = p[14];
    bool isIllValid = (status & (1 << 0)) != 0;
    bool isTmpValid = (status & (1 << 1)) != 0;
    bool isVelValid = (status & (1 << 2)) != 0;

    // 最新エントリの更新（記録用タイマーがここからデータを参照する）
    _latestEntry = new SensorLogEntry(DateTime.Now, tmp, hmd, vel, vol, ill, co2, isTmpValid, isVelValid, isIllValid);
  }

  /// <summary>MIDI Device接続状況変化時の処理</summary>
  /// <param name="isConnected"></param>
  private void OnMidiConnectionChanged(bool isConnected)
  {
    Application.Current?.Dispatcher.Dispatch(() =>
    {
      IsDeviceConnected = isConnected;
      if (isConnected)
      {
        _hasValidDataReceived = false;
        StartPolling();

        // 接続時に計測開始を指示し、ID とバージョンを 1 回だけ要求する。
        // CMD_START_MEAS はファーム側 EM_Sensing_Enabled を有効化するため、
        // 以前に別クライアントが CMD_STOP_MEAS で停止させていた場合でも
        // このアプリ起動で計測が再開される。
        _midiService.SendSysEx(MidiCommands.CMD_START_MEAS);
        _midiService.SendSysEx(MidiCommands.CMD_ID_REQ);
        _midiService.SendSysEx(MidiCommands.CMD_VER_REQ);
      }
      else
      {
        StopPolling();
        DeviceId = "---";
        FirmwareVersion = "---";
      }
    });
  }

  /// <summary>記録中状態変更時の処理</summary>
  /// <param name="value"></param>
  partial void OnIsRecordingChanged(bool value)
  {
    // スリープ防止の切り替え
    MainThread.BeginInvokeOnMainThread(() =>
    {
      DeviceDisplay.Current.KeepScreenOn = value;
    });


    // 記録タスクの制御
    if (value)
    {
      // フォアグラウンドサービス開始
      _loggingService.StartForegroundService();

      // 以前のセッションが残っていればキャンセルして破棄
      _recordingCts?.Cancel();
      _recordingCts?.Dispose();

      // 新しいトークンを発行
      _recordingCts = new CancellationTokenSource();

      // 非同期メソッドを待機せずに実行（Fire and Forget）
      // 変数に代入しない（_ = ）ことで意図的な非同期実行であることを明示
      _ = StartRecordingLoop(_recordingCts.Token);
    }
    else
    {
      // フォアグラウンドサービス停止
      _loggingService.StopForegroundService();

      // 記録停止時にキャンセルを実行
      _recordingCts?.Cancel();
    }

    StartRecordCommand.NotifyCanExecuteChanged();
    StopRecordCommand.NotifyCanExecuteChanged();
  }

  #endregion

  #region 計測値のポーリング処理

  private void StartPolling()
  {
    StopPolling(); // 二重起動防止
    _pollingCts = new CancellationTokenSource();
    _ = StartPollingLoop(_pollingCts.Token);
  }

  private void StopPolling()
  {
    _pollingCts?.Cancel();
    _pollingCts?.Dispose();
    _pollingCts = null;
  }

  private async Task StartPollingLoop(CancellationToken token)
  {
    using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(POLLING_INTERVAL_MS));
    try
    {
      while (await timer.WaitForNextTickAsync(token))
      {
        if (IsDeviceConnected)
        {
          _midiService.SendSysEx(MidiCommands.CMD_REQ_DATA);
        }
      }
    }
    catch (OperationCanceledException) { }
  }

  #endregion

  #region 書き出し関連の処理

  private async Task StartRecordingLoop(CancellationToken token)
  {
    using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(RECORDING_INTERVAL_MS));

    try
    {
      // トークンがキャンセルされるまでループ
      while (await timer.WaitForNextTickAsync(token))
      {
        // 接続中の場合のみ記録
        if (IsRecording && _latestEntry != null)
        {
          var entryToRecord = _latestEntry with { Timestamp = DateTime.Now };
          _recordedData.Add(entryToRecord);

          // UIへの反映（カウント更新）はメインスレッドで行う
          MainThread.BeginInvokeOnMainThread(() =>
          {
            RecordedCount = _recordedData.Count;
          });
        }
      }
    }
    catch (OperationCanceledException)
    {
      // キャンセル時
    }
    catch (Exception ex)
    {
      // 予期せぬエラーのログ出力など
      System.Diagnostics.Debug.WriteLine($"Loop Error: {ex.Message}");
    }
  }

  #endregion

  #region イースターエッグ関連

  /// <summary>犬が見えているか否か</summary>
  [ObservableProperty]
  private bool _isDogVisible = false;

  // アニメーション開始をViewに通知するためのイベント（またはメソッド）
  public event Action StartDogAnimationRequested;

  // 犬表示用タイマー
  private IDispatcherTimer _dogTimer;

  // アニメーションが終わったら View から呼んでもらう
  [RelayCommand]
  private void CompleteDogAnimation()
  {
    IsDogVisible = false;
    // 接続が切れたままなら、ここでタイマーを再始動して次の秒をカウント開始
    if (!IsDataFresh) _dogTimer?.Start();
  }

  // IsDataFresh が True になったらタイマー停止 & IsDogVisible = false
  partial void OnIsDataFreshChanged(bool value)
  {
    if (value)
    {
      _dogTimer?.Stop();
      IsDogVisible = false;
    }
    else _dogTimer?.Start();
  }

  #endregion

  #region ダミーデータ生成ロジック

  private readonly Random _random = new();

  private void startDummyDataLoop()
  {
    // 既に接続されているように見せる
    IsDeviceConnected = true;

    _dummyDataTimer = Application.Current?.Dispatcher.CreateTimer();
    if (_dummyDataTimer != null)
    {
      _dummyDataTimer.Interval = TimeSpan.FromMilliseconds(POLLING_INTERVAL_MS);
      _dummyDataTimer.Tick += (s, e) =>
      {
        var dummyPacket = generateRandomSensorPacket();
        OnMidiMessageReceived(dummyPacket);
      };
      _dummyDataTimer.Start();
    }
  }

  /// <summary>
  /// 実機の通信仕様に基づいた15バイトのバイナリパケットをランダム生成
  /// </summary>
  private byte[] generateRandomSensorPacket()
  {
    // 15バイトの生データ（14バイト数値 + 1バイトフラグ）を作成
    byte[] raw = new byte[15];

    uint ill = (uint)_random.Next(1000, 10000);
    BitConverter.TryWriteBytes(raw.AsSpan(0, 4), ill);

    short tmp = (short)_random.Next(2000, 2800);
    BitConverter.TryWriteBytes(raw.AsSpan(4, 2), tmp);

    ushort hmd = (ushort)_random.Next(4000, 6000);
    BitConverter.TryWriteBytes(raw.AsSpan(6, 2), hmd);

    ushort vel = (ushort)_random.Next(0, 5000);
    BitConverter.TryWriteBytes(raw.AsSpan(8, 2), vel);

    ushort vol = (ushort)_random.Next(3000, 4000);
    BitConverter.TryWriteBytes(raw.AsSpan(10, 2), vol);

    ushort co2 = (ushort)_random.Next(400, 3000);
    BitConverter.TryWriteBytes(raw.AsSpan(12, 2), co2);

    raw[14] = 0b00000111; // 全てのフラグを有効にする

    // CRC8の計算
    byte crc = SensorParser.CalculateCrc8(raw);

    // 16バイト(15+CRC)を32バイトのニブルに変換
    byte[] encoded = new byte[32];
    for (int i = 0; i < 15; i++)
    {
      encoded[i * 2] = (byte)(raw[i] >> 4);
      encoded[i * 2 + 1] = (byte)(raw[i] & 0x0F);
    }
    encoded[30] = (byte)(crc >> 4);
    encoded[31] = (byte)(crc & 0x0F);

    // MIDIヘッダー(2byte)を付与
    byte[] packet = new byte[34];
    packet[0] = MidiCommands.MANUFACTURER_ID;
    packet[1] = MidiCommands.CMD_SENS_DATA;
    Array.Copy(encoded, 0, packet, 2, 32);

    return packet;
  }

  #endregion

}