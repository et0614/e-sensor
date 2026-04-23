using Android.Content;
using Android.OS;

namespace E_Sensor.Platforms.Android
{
  public class AndroidLoggingService : ILoggingService
  {
    public void StartForegroundService()
    {
      var intent = new Intent(Platform.AppContext, typeof(LoggingForegroundService));
      if (Build.VERSION.SdkInt >= BuildVersionCodes.O)
        Platform.AppContext.StartForegroundService(intent);
      else
        Platform.AppContext.StartService(intent);
    }

    public void StopForegroundService()
    {
      var intent = new Intent(Platform.AppContext, typeof(LoggingForegroundService));
      Platform.AppContext.StopService(intent);
    }
  }
}
