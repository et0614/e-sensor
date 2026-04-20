namespace E_Sensor
{
  public partial class MainPage : ContentPage
  {
    public MainPage(MainViewModel viewModel)
    {
      InitializeComponent();
      BindingContext = viewModel;

      viewModel.PropertyChanged += async (s, e) =>
      {
        if (e.PropertyName == nameof(MainViewModel.RecordedCount) && viewModel.IsRecording)
        {
          if (RecordCountLabel != null)
          {
            // レイアウト更新（テキストの書き換え）を待つために1フレーム分譲る
            await Task.Yield();

            // レコード受信アニメーション
            await RecordCountLabel.ScaleToAsync(1.2, 50);
            await RecordCountLabel.ScaleToAsync(1.0, 50);
          }
        }
      };

      // 犬画像表示アニメーション
      viewModel.StartDogAnimationRequested += async () =>
      {
        // --- 設定値 ---
        const double pixelsPerSecond = 120.0; // 1秒間に進む距離（ピクセル）
        const double dogWidth = 100.0;       // 犬の画像の幅（余裕を持って設定）

        // 現在の画面幅を取得
        double screenWidth = this.Width;
        if (screenWidth <= 0) screenWidth = 400; // フォールバック

        // 移動する全距離 = 画面幅 + 犬自身の幅（左右の画面外分）
        double totalDistance = screenWidth + (dogWidth * 2);

        // 距離に基づいた移動時間（ミリ秒）を計算
        // (距離 / 秒速) * 1000
        uint duration = (uint)((totalDistance / pixelsPerSecond) * 1000);

        // 犬を画面外左にセット
        DogLottie.TranslationX = -dogWidth;

        // 横断アニメーション
        await DogLottie.TranslateToAsync(screenWidth + dogWidth, 0, duration, Easing.Linear);

        // 画面から消えたら ViewModel に報告
        // これにより ViewModel 側で次の30秒タイマーが動き出す
        if (viewModel.CompleteDogAnimationCommand.CanExecute(null))
          viewModel.CompleteDogAnimationCommand.Execute(null);
      };

    }
  }
}
