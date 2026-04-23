using System;
using System.Collections.Generic;
using System.Text;

namespace E_Sensor
{
  public interface ILoggingService
  {
    void StartForegroundService();

    void StopForegroundService();
  }
}
