using Microsoft.Extensions.Logging;
using System.Globalization;
using SkiaSharp.Views.Maui.Controls.Hosting;

#if WINDOWS
using E_Sensor.Platforms.Windows;
#endif

namespace E_Sensor
{
  public static class MauiProgram
  {
    public static MauiApp CreateMauiApp()
    {
      /*// テスト用に英語(en-US)を強制指定
      var culture = new CultureInfo("en-US");
      CultureInfo.DefaultThreadCurrentCulture = culture;
      CultureInfo.DefaultThreadCurrentUICulture = culture;*/
      

      var builder = MauiApp.CreateBuilder();
      builder
        .UseMauiApp<App>()
        .UseSkiaSharp()
        .ConfigureFonts(fonts =>
        {
          fonts.AddFont("OpenSans-Regular.ttf", "OpenSansRegular");
          fonts.AddFont("OpenSans-Semibold.ttf", "OpenSansSemibold");
        });

#if WINDOWS
      builder.Services.AddSingleton<IMidiService, WindowsMidiService>();
      builder.Services.AddSingleton<ILoggingService, DummyLoggingService>();
#elif ANDROID
      builder.Services.AddSingleton<IMidiService, E_Sensor.Platforms.Android.AndroidMidiService>();
      builder.Services.AddSingleton<ILoggingService, E_Sensor.Platforms.Android.AndroidLoggingService>();
#elif IOS || MACCATALYST
      // MacCatalyst は iOS の CoreMIDI 実装をそのまま共有する (csproj で Compile Include)
      builder.Services.AddSingleton<IMidiService, E_Sensor.Platforms.iOS.IosMidiService>();
      builder.Services.AddSingleton<ILoggingService, E_Sensor.Platforms.iOS.IosLoggingService>();
#endif

      //ViewModelとPageの依存関係を登録
      builder.Services.AddSingleton<MainViewModel>();
      builder.Services.AddTransient<MainPage>();

#if DEBUG
      builder.Logging.AddDebug();
#endif

      return builder.Build();
    }
  }
}
