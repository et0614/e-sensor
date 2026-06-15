using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace E_Sensor;

// 記録用データ構造 (CorrTemp/CorrHum は熱影響補正後の温湿度。記録時に設定される)
public record SensorLogEntry(DateTime Timestamp, double Temp, double Hum, double Vel, double Volt, double Ill, int Co2, bool IsTempValid, bool IsVelValid, bool IsIllValid, double CorrTemp = 0, double CorrHum = 0);

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

  /// <summary>CO2 センサのバイパス位相（起動・初期調整直後の固定値出力期間）[sec]</summary>
  private const int CO2_WARMUP_SECONDS = 20;

  /// <summary>風速センサの白金抵抗予熱時間[sec]</summary>
  /// <remarks>
  /// Velocity サブ MCU のファームウェア (HEATING_MSEC = 5000ms) と合わせる。
  /// 予熱中は計測値が更新されないが、Main MCU 側からは値は読み取れて見えるため、
  /// アプリ接続時に同じ秒数だけ警告表示する。
  /// </remarks>
  private const int VELOCITY_WARMUP_SECONDS = 5;

  /// <summary>初期調整完了通知のフェイルセーフタイムアウト[sec]</summary>
  /// <remarks>本来は約 22 秒で完了通知が来るが、取りこぼし対策として余裕を持たせる。</remarks>
  private const int CONDITIONING_TIMEOUT_SECONDS = 60;

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

  /// <summary>CO2 がバイパス位相中（起動直後または初期調整完了後 20 秒間）か否か</summary>
  /// <remarks>
  /// STCC4 のデータシート §1.1.4 に記載のとおり、連続計測開始からの最初の 20 秒間は
  /// CO2 値が 390 ppm 固定で出力される。ユーザーがこれを実測値と誤解しないよう、
  /// この期間中は SensorCard 上に WARM-UP バッジを表示し、値を灰色で表示する。
  /// </remarks>
  [ObservableProperty]
  [NotifyPropertyChangedFor(nameof(Co2BadgeText))]
  private bool _isCo2Warmup = false;

  /// <summary>初期調整（コンディショニング）実行中か否か</summary>
  /// <remarks>
  /// CMD_CONDITIONING_START から CMD_CONDITIONING_DONE までの期間。
  /// この間はファームウェアからの readMeasurement が NACK となり、温湿度・CO2 ともに
  /// 直前の値が固定で表示されるため、全 3 カードに CONDITIONING バッジを出して
  /// 「現在値は信頼できない」ことをユーザに示す。
  /// </remarks>
  [ObservableProperty]
  [NotifyPropertyChangedFor(nameof(Co2BadgeText))]
  [NotifyPropertyChangedFor(nameof(TempHumBadgeText))]
  [NotifyPropertyChangedFor(nameof(TempHumInfoBadgeText))]
  private bool _isConditioning = false;

  /// <summary>CO2 カード用バッジ表示文字列（空文字列のとき非表示）</summary>
  public string Co2BadgeText =>
      IsConditioning ? Resources.Strings.ConditioningBadge :
      IsCo2Warmup ? Resources.Strings.WarmupBadge :
      string.Empty;

  /// <summary>温度・湿度カード用バッジ表示文字列（警告系・空文字列のとき非表示）</summary>
  public string TempHumBadgeText =>
      IsConditioning ? Resources.Strings.ConditioningBadge : string.Empty;

  /// <summary>温度・湿度カード用 情報バッジ（補正値の注記。タップで説明を表示）</summary>
  /// <remarks>
  /// 風速計 ON（自己発熱中）は温湿度を熱影響補正した推定値を表示するため「補正値」を
  /// 注記する。初期調整中は値自体が無効なので警告バッジを優先し、情報バッジは出さない。
  /// </remarks>
  public string TempHumInfoBadgeText =>
      (IsVelocityOn && !IsConditioning) ? Resources.Strings.CorrectedBadge : string.Empty;

  /// <summary>風速が予熱中か否か（接続直後の 5 秒間）</summary>
  /// <remarks>
  /// 風速回路の白金抵抗が予熱中はサブ MCU が計測値を更新しないが、Main MCU の
  /// VELS_readMeasurement は CRC 付きの 0 (= 旧値) を成功で読み取るため、MAUI 側
  /// からは「正常データ」と区別がつかない。接続時に 5 秒間 WARM-UP を表示する。
  /// </remarks>
  [ObservableProperty]
  [NotifyPropertyChangedFor(nameof(VelocityBadgeText))]
  private bool _isVelocityWarmup = false;

  /// <summary>風速カード用バッジ表示文字列（空文字列のとき非表示）</summary>
  public string VelocityBadgeText =>
      IsVelocityWarmup ? Resources.Strings.WarmupBadge : string.Empty;

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
  [NotifyCanExecuteChangedFor(nameof(OpenMaintenanceMenuCommand))]
  [NotifyPropertyChangedFor(nameof(IsDeviceLive))]
  private bool _isDeviceConnected;

  /// <summary>計測データが新鮮か否か</summary>
  /// <remarks>
  /// バックグラウンド中に MIDI デバイスが抜かれた場合、OS から切断コールバックが
  /// 配信されず IsDeviceConnected が古いまま残ることがある。これを補うため、
  /// 各操作コマンドの CanExecute はデータ鮮度も併せて見るようにしている。
  /// </remarks>
  [ObservableProperty]
  [NotifyCanExecuteChangedFor(nameof(StartRecordCommand))]
  [NotifyCanExecuteChangedFor(nameof(ToggleRecordCommand))]
  [NotifyCanExecuteChangedFor(nameof(OpenMaintenanceMenuCommand))]
  [NotifyPropertyChangedFor(nameof(IsDeviceLive))]
  private bool _isDataFresh = false;

  /// <summary>UI 上「実質的に接続が生きているか」を表す合成状態</summary>
  /// <remarks>
  /// IsDeviceConnected はバックグラウンド中の切断を取りこぼすことがあるため、
  /// 表示用インジケータ等は本プロパティを参照する。
  /// </remarks>
  public bool IsDeviceLive => IsDeviceConnected && IsDataFresh;

  /// <summary>デバイスID</summary>
  [ObservableProperty]
  private string _deviceId = "---";

  /// <summary>ファームウェアのバージョン</summary>
  [ObservableProperty]
  [NotifyPropertyChangedFor(nameof(IsVelocityToggleSupported))]
  private string _firmwareVersion = "---";

  /// <summary>風速計が ON か否か（アプリ側で保持する稼働状態。電源投入時は ON）</summary>
  /// <remarks>
  /// 「ヒーター ON/OFF」を表す専用テレメトリは無いため、送信した START/STOP コマンドで
  /// 状態を保持する。ON のとき自己発熱補正値を、OFF のとき生値を表示する。
  /// </remarks>
  [ObservableProperty]
  [NotifyPropertyChangedFor(nameof(TempHumInfoBadgeText))]
  private bool _isVelocityOn = true;

  /// <summary>風速計 On/Off トグルが利用可能か（ファーム v1.1.0 以降）</summary>
  /// <remarks>
  /// On/Off コマンドはファーム v1.1.0 で追加。物理的な発熱停止はさらにハードウェア
  /// version 1.1 以降が必要だが、アプリからはファームバージョンで可否を判定する。
  /// v1.0 系では本トグルを無効化（灰色表示）する。
  /// </remarks>
  public bool IsVelocityToggleSupported => IsFirmwareAtLeast(1, 1, 0);

  #endregion

  #region インスタンス変数・プロパティの定義

  private readonly IMidiService _midiService;

  private readonly ILoggingService _loggingService;

  private readonly List<SensorLogEntry> _recordedData = new();

  /// <summary>データ鮮度計算用タイマ</summary>
  private IDispatcherTimer? _freshnessTimer;

  /// <summary>CO2 バイパス位相終了用タイマ</summary>
  private IDispatcherTimer? _co2WarmupTimer;

  /// <summary>風速予熱終了用タイマ</summary>
  private IDispatcherTimer? _velocityWarmupTimer;

  /// <summary>初期調整のフェイルセーフ用タイマ</summary>
  /// <remarks>
  /// CMD_CONDITIONING_DONE を取りこぼした場合（バックグラウンドで MIDI 切断等）に、
  /// CONDITIONING バッジが永続表示されないようにするための安全弁。
  /// </remarks>
  private IDispatcherTimer? _conditioningTimeoutTimer;

  /// <summary>ダミーデータ表示用タイマ</summary>
  private IDispatcherTimer? _dummyDataTimer;

  /// <summary>データ定期収集タスクのトークン</summary>
  private CancellationTokenSource? _pollingCts;

  /// <summary>データ保存タスクのトークン</summary>
  private CancellationTokenSource? _recordingCts;

  /// <summary>最新の計測データ</summary>
  private SensorLogEntry? _latestEntry;

  /// <summary>風速計自己発熱の温度補正フィルタ（カルマン）</summary>
  private readonly KalmanThermalCorrector _corrector = new();

  /// <summary>最後に KF へ入力した生温度値（温度更新=新規読み取りの検出用）</summary>
  private double? _lastFedTemp;

  /// <summary>最後に KF を更新した時刻（dt 計算用）</summary>
  private DateTime? _lastCorrTime;

  /// <summary>最新の補正済み温度[℃]</summary>
  private double _correctedTemp;

  /// <summary>最新の補正済み相対湿度[%]</summary>
  private double _correctedHum;

  /// <summary>FirmwareVersion ("major.minor.rev") が指定バージョン以上かを判定する。</summary>
  private bool IsFirmwareAtLeast(int major, int minor, int rev)
  {
    var parts = FirmwareVersion?.Split('.');
    if (parts == null || parts.Length < 3) return false;
    if (!int.TryParse(parts[0], out var ma) ||
        !int.TryParse(parts[1], out var mi) ||
        !int.TryParse(parts[2], out var re)) return false;
    if (ma != major) return ma > major;
    if (mi != minor) return mi > minor;
    return re >= rev;
  }

  private bool CanStartRecord() => IsDeviceConnected && IsDataFresh && !IsRecording;

  private bool CanStopRecord() => IsDeviceConnected && IsRecording;

  // 記録中ならデータが途切れても停止操作は許可する（操作不能で記録が継続する状況を避ける）
  private bool CanToggleRecord() => CanStartRecord() || CanStopRecord();

  private bool CanOpenMaintenance() => IsDeviceConnected && IsDataFresh;

  private bool CanExport() => RecordedCount > 0;

  /// <summary>接続開始してから有効なデータを受け取ったか否か</summary>
  private bool _hasValidDataReceived = false;

  #endregion

  #region コマンド定義

  [RelayCommand(CanExecute = nameof(CanToggleRecord))]
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

  /// <summary>メンテナンスメニュー（CO2 初期調整 / 工場出荷時リセット）コマンド</summary>
  /// <remarks>
  /// 頻度が低く誤操作の影響が大きい操作なので、ActionSheet → 確認ダイアログ
  /// （工場リセットは "RESET" の入力プロンプトによる二段階確認）の経路で
  /// 実行する。
  /// </remarks>
  [RelayCommand(CanExecute = nameof(CanOpenMaintenance))]
  private async Task OpenMaintenanceMenu()
  {
    var page = Application.Current?.Windows.FirstOrDefault()?.Page;
    if (page == null) return;

    // 校正レポート（外部 Web）はデバイス ID が取得できているときだけ提示する。
    // 接続直後の CMD_ID_RES 受信前に開かれた場合は項目を出さない。
    bool hasDeviceId = !string.IsNullOrEmpty(DeviceId) && DeviceId != "---";
    var options = new List<string>
    {
      Resources.Strings.MaintConditioning,
      Resources.Strings.MaintFactoryReset,
    };
    if (hasDeviceId) options.Add(Resources.Strings.MaintCalibrationReport);

    string action = await page.DisplayActionSheet(
        Resources.Strings.Maintenance,
        Resources.Strings.Cancel,
        destruction: null,
        options.ToArray());

    if (action == Resources.Strings.MaintConditioning)
    {
      bool ok = await page.DisplayAlertAsync(
          Resources.Strings.ConfirmConditioningTitle,
          Resources.Strings.ConfirmConditioningMsg,
          Resources.Strings.Yes,
          Resources.Strings.No);
      if (ok)
      {
        _midiService.SendSysEx(MidiCommands.CMD_CONDITIONING_REQ);
      }
    }
    else if (action == Resources.Strings.MaintFactoryReset)
    {
      // 工場出荷時リセットは校正係数を消すわけではなく、過去のトレンドから
      // 自動校正し直すだけなので Yes/No の確認だけで十分。
      bool ok = await page.DisplayAlertAsync(
          Resources.Strings.ConfirmFactoryResetTitle,
          Resources.Strings.ConfirmFactoryResetMsg,
          Resources.Strings.Yes,
          Resources.Strings.No);
      if (ok)
      {
        _midiService.SendSysEx(MidiCommands.CMD_CO2_RESET_REQ);
      }
    }
    else if (hasDeviceId && action == Resources.Strings.MaintCalibrationReport)
    {
      var url = $"https://e-sensor.jp/calibration/viewer.html?id={DeviceId}";
      try
      {
        await Browser.OpenAsync(url, BrowserLaunchMode.SystemPreferred);
      }
      catch (Exception)
      {
        // 既定ブラウザが無い等で開けない端末では何もしない。
      }
    }
  }

  /// <summary>プロジェクト Web サイトを既定ブラウザで開く</summary>
  [RelayCommand]
  private async Task OpenWebsite()
  {
    try
    {
      await Browser.OpenAsync("https://e-sensor.jp", BrowserLaunchMode.SystemPreferred);
    }
    catch (Exception)
    {
      // 既定ブラウザが無い等で開けない端末では何もしない。
    }
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
      sb.AppendLine("Date,Time,Temperature[C],Humidity[%],CorrectedTemperature[C],CorrectedHumidity[%],Velocity[m/s],Illuminance[Lux],CO2[ppm],Velocity Voltage[V],Temperature Valid,Velocity Valid,Illuminance Valid");
      foreach (var row in _recordedData)
      {
        // InvariantCultureを指定することで、OSの言語設定に関わらず小数点を「.」に固定(温湿度と照度は表示値よりも1桁精度高)
        var line = string.Format(System.Globalization.CultureInfo.InvariantCulture,
            "{0:yyyy-MM-dd,HH:mm:ss},{1:F2},{2:F2},{3:F2},{4:F2},{5:F3},{6:F1},{7},{8:F3},{9},{10},{11}",
            row.Timestamp,
            row.Temp,
            row.Hum,
            row.CorrTemp,
            row.CorrHum,
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

    // CO2 バイパス位相管理タイマー（ワンショット）
    _co2WarmupTimer = dispatcher.CreateTimer();
    _co2WarmupTimer.Interval = TimeSpan.FromSeconds(CO2_WARMUP_SECONDS);
    _co2WarmupTimer.IsRepeating = false;
    _co2WarmupTimer.Tick += (s, e) =>
    {
      IsCo2Warmup = false;
    };

    // 風速予熱管理タイマー（ワンショット）
    _velocityWarmupTimer = dispatcher.CreateTimer();
    _velocityWarmupTimer.Interval = TimeSpan.FromSeconds(VELOCITY_WARMUP_SECONDS);
    _velocityWarmupTimer.IsRepeating = false;
    _velocityWarmupTimer.Tick += (s, e) =>
    {
      IsVelocityWarmup = false;
    };

    // 初期調整フェイルセーフタイマー（ワンショット）
    _conditioningTimeoutTimer = dispatcher.CreateTimer();
    _conditioningTimeoutTimer.Interval = TimeSpan.FromSeconds(CONDITIONING_TIMEOUT_SECONDS);
    _conditioningTimeoutTimer.IsRepeating = false;
    _conditioningTimeoutTimer.Tick += (s, e) =>
    {
      // 完了通知を取りこぼしたとみなしてバッジを解除し、続けて 20 秒の warm-up に入る
      IsConditioning = false;
      StartCo2WarmupCountdown();
    };
  }

  /// <summary>CO2 バイパス位相のカウントダウンを開始する（既存タイマは再起動される）</summary>
  private void StartCo2WarmupCountdown()
  {
    Application.Current?.Dispatcher.Dispatch(() =>
    {
      _co2WarmupTimer?.Stop();
      IsCo2Warmup = true;
      _co2WarmupTimer?.Start();
    });
  }

  /// <summary>風速予熱のカウントダウンを開始する（既存タイマは再起動される）</summary>
  private void StartVelocityWarmupCountdown()
  {
    Application.Current?.Dispatcher.Dispatch(() =>
    {
      _velocityWarmupTimer?.Stop();
      IsVelocityWarmup = true;
      _velocityWarmupTimer?.Start();
    });
  }

  /// <summary>初期調整中状態に入る（フェイルセーフタイマも起動）</summary>
  private void BeginConditioning()
  {
    Application.Current?.Dispatcher.Dispatch(() =>
    {
      _conditioningTimeoutTimer?.Stop();
      // 警告表示の優先度は CONDITIONING > WARM-UP のため、進行中の warm-up は一旦解除
      _co2WarmupTimer?.Stop();
      IsCo2Warmup = false;
      IsConditioning = true;
      _conditioningTimeoutTimer?.Start();
    });
  }

  /// <summary>初期調整中状態を解除する</summary>
  private void EndConditioning()
  {
    Application.Current?.Dispatcher.Dispatch(() =>
    {
      _conditioningTimeoutTimer?.Stop();
      IsConditioning = false;
    });
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

      // 熱影響補正(温度の更新周期=約1Hz で実行)してから表示に反映する
      UpdateThermalCorrection();
      ApplyTempHumDisplay();

      // 風速 (保証レンジは 5.0 m/s まで。超過時はレンジオーバー表示)
      if (IsVelocityValid)
      {
        var vel = _latestEntry!.Vel;
        Velocity = vel > 5.0 ? ">5.0" : vel.ToString("F2");
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

  /// <summary>
  /// 風速計自己発熱の温度・湿度補正を進める。温度(STCC4)は約1秒ごとにしか更新されない
  /// ため、生温度値が更新されたとき(=新規読み取り)だけ KF を1ステップ進める。200ms の
  /// ポーリング毎に呼ぶと同一値を重複入力して誤推定するので、ここで間引く。
  /// </summary>
  private void UpdateThermalCorrection()
  {
    var e = _latestEntry;
    if (e == null) return;

    var now = DateTime.Now;
    double sinceLast = _lastCorrTime.HasValue ? (now - _lastCorrTime.Value).TotalSeconds : 1.0;
    bool tempChanged = !_lastFedTemp.HasValue || e.Temp != _lastFedTemp.Value;

    // 新規温度観測があれば本更新。無ければ(欠測・値据置)2秒超過時のみ予測のみで前進。
    bool isFreshMeasurement = e.IsTempValid && tempChanged;
    if (!isFreshMeasurement && sinceLast < 2.0) return;

    double dt = _lastCorrTime.HasValue ? Math.Clamp(sinceLast, 0.05, 10.0) : 1.0;
    double v = e.IsVelValid ? Math.Clamp(e.Vel, 0.0, 30.0) : 0.0;

    _correctedTemp = _corrector.Update(e.Temp, v, dt, heating: IsVelocityOn, measured: isFreshMeasurement);
    _correctedHum = HumidityCorrection.Correct(e.Hum, e.Temp, _correctedTemp);
    _lastCorrTime = now;
    if (e.IsTempValid) _lastFedTemp = e.Temp;
  }

  /// <summary>
  /// 温度・湿度・CO2 の表示文字列を反映する。風速計 ON のときは補正値、OFF のときは
  /// 生値を表示する。トグル切替時にも即座に反映できるよう独立メソッドにしている。
  /// </summary>
  private void ApplyTempHumDisplay()
  {
    var e = _latestEntry;
    // 温湿度とCO2は起動に時間がかかるセンサーのため、初回有効データ受信前は --- 表示
    if (IsTemperatureValid && e != null)
    {
      double dispT = IsVelocityOn ? _correctedTemp : e.Temp;
      double dispH = IsVelocityOn ? _correctedHum : e.Hum;
      Temperature = dispT.ToString("F1");
      Humidity = dispH.ToString("F1");
      Co2Level = e.Co2.ToString();
      _hasValidDataReceived = true;
    }
    else if (!_hasValidDataReceived)
    {
      Temperature = "---";
      Humidity = "---";
      Co2Level = "---";
    }
  }

  /// <summary>風速計 ON/OFF 切替時：実コマンド送信(対応ファームのみ)と表示の即時更新。</summary>
  partial void OnIsVelocityOnChanged(bool value)
  {
    if (IsDeviceConnected && IsVelocityToggleSupported)
    {
      _midiService.SendSysEx(value ? MidiCommands.CMD_VEL_START : MidiCommands.CMD_VEL_STOP);
    }
    // 補正値↔生値の表示を即座に切り替える
    Application.Current?.Dispatcher.Dispatch(ApplyTempHumDisplay);
  }

  /// <summary>「補正値」バッジのタップで熱影響補正の説明をポップアップ表示する。</summary>
  [RelayCommand]
  private async Task ShowCorrectionInfo()
  {
    var page = Application.Current?.Windows.FirstOrDefault()?.Page;
    if (page != null)
    {
      await page.DisplayAlertAsync(
          Resources.Strings.CorrectionInfoTitle,
          Resources.Strings.CorrectionInfoMsg,
          "OK");
    }
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
    // ID応答 (4 byte ハッシュ + 1 byte CRC)
    else if (data[1] == MidiCommands.CMD_ID_RES)
    {
      if (payload.Length < 5) return;
      var idBytes = payload.AsSpan(0, 4);
      if (SensorParser.CalculateCrc8(idBytes) != payload[4]) return;
      var idHex = BitConverter.ToString(idBytes.ToArray()).Replace("-", "");
      Application.Current?.Dispatcher.Dispatch(() => {
        DeviceId = idHex;
      });
    }
    // バージョン応答 (3 byte: メジャー.マイナー.リビジョン + 1 byte CRC)
    else if (data[1] == MidiCommands.CMD_VER_RES)
    {
      if (payload.Length < 4) return;
      var verBytes = payload.AsSpan(0, 3);
      if (SensorParser.CalculateCrc8(verBytes) != payload[3]) return;
      var verStr = $"{verBytes[0]}.{verBytes[1]}.{verBytes[2]}";
      Application.Current?.Dispatcher.Dispatch(() => {
        FirmwareVersion = verStr;
      });
    }
    // CO2 初期調整 開始通知
    else if (data[1] == MidiCommands.CMD_CONDITIONING_START)
    {
      BeginConditioning();
      ShowMaintenanceNotice(Resources.Strings.ConditioningStarted);
    }
    // CO2 初期調整 完了通知
    else if (data[1] == MidiCommands.CMD_CONDITIONING_DONE)
    {
      // 初期調整完了直後はファームウェアが start_continuous_measurement を再送するため、
      // データシート §1.1.4 のバイパス位相が再発生する。CONDITIONING を解除して
      // 20 秒間 WARM-UP に切り替える。
      EndConditioning();
      StartCo2WarmupCountdown();
      ShowMaintenanceNotice(Resources.Strings.ConditioningDone);
    }
    // CO2 工場出荷時リセット 完了通知
    else if (data[1] == MidiCommands.CMD_CO2_RESET_RES)
    {
      ShowMaintenanceNotice(Resources.Strings.FactoryResetDone);
    }
  }

  /// <summary>メンテナンス系操作の完了通知をユーザーに見せる</summary>
  private void ShowMaintenanceNotice(string message)
  {
    Application.Current?.Dispatcher.Dispatch(async () =>
    {
      var page = Application.Current?.Windows.FirstOrDefault()?.Page;
      if (page != null)
      {
        await page.DisplayAlertAsync(Resources.Strings.Maintenance, message, "OK");
      }
    });
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
        // 接続のたびに補正フィルタを初期化（基板≒空気のコールドスタート前提に戻す）
        _corrector.Reset();
        _lastFedTemp = null;
        _lastCorrTime = null;
        StartPolling();

        // 接続時に計測開始を指示し、ID とバージョンを 1 回だけ要求する。
        // CMD_START_MEAS はファーム側 EM_Sensing_Enabled を有効化するため、
        // 以前に別クライアントが CMD_STOP_MEAS で停止させていた場合でも
        // このアプリ起動で計測が再開される。
        _midiService.SendSysEx(MidiCommands.CMD_START_MEAS);
        _midiService.SendSysEx(MidiCommands.CMD_ID_REQ);
        _midiService.SendSysEx(MidiCommands.CMD_VER_REQ);

        // 風速計は接続時に既定の ON に揃える（ファーム既定も電源投入時 ON）。
        // 表示は補正値になる。v1.1.0 未満では未知コマンドとして無視されるため無害。
        IsVelocityOn = true;
        _midiService.SendSysEx(MidiCommands.CMD_VEL_START);

        // 接続直後はデバイスが起動直後である可能性が高い。STCC4 のバイパス位相中は
        // CO2 値が 390 ppm 固定となるため、安全側に倒して 20 秒間 WARM-UP を表示する。
        // 風速回路も予熱 5 秒間は値が更新されないため、同様に WARM-UP を表示する。
        // 既に起動から十分時間が経過していた場合の不要な表示は許容する。
        StartCo2WarmupCountdown();
        StartVelocityWarmupCountdown();
      }
      else
      {
        StopPolling();
        DeviceId = "---";
        FirmwareVersion = "---";
        // 切断時はバッジ状態もリセット（再接続時に古い CONDITIONING が残らないように）
        EndConditioning();
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
    ToggleRecordCommand.NotifyCanExecuteChanged();
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
          // 記録時点の補正値を併せて保存する
          var entryToRecord = _latestEntry with
          {
            Timestamp = DateTime.Now,
            CorrTemp = _correctedTemp,
            CorrHum = _correctedHum
          };
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
    else
    {
      _dogTimer?.Start();
      // 記録中にデータが途絶えたら自動で停止する。バックグラウンド中に
      // ケーブルが抜かれた場合、復帰後も古い _latestEntry がコピーされ
      // 続けるのを防ぐため。
      if (IsRecording) StopRecord();
    }
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