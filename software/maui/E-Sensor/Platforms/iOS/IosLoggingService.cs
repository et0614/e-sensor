using UIKit;

namespace E_Sensor.Platforms.iOS
{
  public class IosLoggingService : ILoggingService
  {
    private nint _backgroundTaskId = -1;

    public void StartForegroundService()
    {
      // 既に実行中の場合は一度終了
      StopForegroundService();

      // バックグラウンドタスクの開始
      _backgroundTaskId = UIApplication.SharedApplication.BeginBackgroundTask("MidiLogging", () =>
      {
        // 時間切れになった時の処理
        StopForegroundService();
      });
    }

    public void StopForegroundService()
    {
      if (_backgroundTaskId != -1)
      {
        UIApplication.SharedApplication.EndBackgroundTask(_backgroundTaskId);
        _backgroundTaskId = -1;
      }
    }
  }
}
