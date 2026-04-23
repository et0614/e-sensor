using Android.App;
using Android.Content;
using Android.OS;
using AndroidX.Core.App;

namespace E_Sensor.Platforms.Android
{
  [Service(ForegroundServiceType = global::Android.Content.PM.ForegroundService.TypeDataSync)]
  public class LoggingForegroundService : Service
  {
    private const string CHANNEL_ID = "logging_channel";
    private const int NOTIFICATION_ID = 1001;

    public override IBinder OnBind(Intent intent) => null;

    public override StartCommandResult OnStartCommand(Intent intent, StartCommandFlags flags, int startId)
    {
      CreateNotificationChannel();

      // 通知タップ時に MainActivity を開くための設定
      var mainActivityIntent = new Intent(this, typeof(MainActivity));
      mainActivityIntent.SetFlags(ActivityFlags.ClearTop | ActivityFlags.SingleTop);

      var pendingIntent = PendingIntent.GetActivity(this, 0, mainActivityIntent,
          PendingIntentFlags.Immutable | PendingIntentFlags.UpdateCurrent);

      var notification = new NotificationCompat.Builder(this, CHANNEL_ID)
          .SetContentTitle("E-Sensor ロギング中")
          .SetContentText("バックグラウンドでデータを記録しています...")
          .SetSmallIcon(global::Android.Resource.Drawable.StatNotifySync)
          .SetContentIntent(pendingIntent) // タップ時の動作を追加
          .SetOngoing(true)
          .Build();

      // Android 14 (API 34) 以降は serviceType の指定を推奨
      if (Build.VERSION.SdkInt >= BuildVersionCodes.UpsideDownCake)
        StartForeground(NOTIFICATION_ID, notification, global::Android.Content.PM.ForegroundService.TypeDataSync);
      else
        StartForeground(NOTIFICATION_ID, notification);

      return StartCommandResult.Sticky;
    }

    private void CreateNotificationChannel()
    {
      if (Build.VERSION.SdkInt >= BuildVersionCodes.O)
      {
        var channel = new NotificationChannel(CHANNEL_ID, "Logging Service", NotificationImportance.Low);
        var manager = (NotificationManager)GetSystemService(NotificationService);
        manager.CreateNotificationChannel(channel);
      }
    }
  }
}
