using Microsoft.Extensions.DependencyInjection;

namespace E_Sensor
{
  public partial class App : Application
  {

    private readonly MainPage _mainPage;

    public App(MainPage mainPage)
    {
      InitializeComponent();
      _mainPage = mainPage;
    }

    protected override Window CreateWindow(IActivationState? activationState)
    {
      var window = new Window(_mainPage);

      // バックグラウンドへ移ったら（Web の入力ページへ切り替えたとき等）、平均中なら確定する。
      // 切り替えた時点で測定は終わったとみなし、値を固定しておく。
      window.Stopped += (_, _) =>
      {
        if (_mainPage.BindingContext is MainViewModel vm)
          MainThread.BeginInvokeOnMainThread(vm.FinalizeAverage);
      };

      // --- ウィンドウサイズの規定 ---
      window.Width = 1200;      // 幅
      window.Height = 400;     // 高さ

      // 必要に応じて最小サイズも制限可能
      //window.MinimumWidth = 600;
      //window.MinimumHeight = 500;

      // 画面中央に配置したい場合
      // window.X = -1; 
      // window.Y = -1;

      return window;
      //return new Window(_mainPage);
    }
  }
}