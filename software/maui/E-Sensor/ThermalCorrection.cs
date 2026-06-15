using System;

namespace E_Sensor
{
  /// <summary>
  /// 風速計の自己発熱による温度センサ熱影響モデルの定数。
  /// software/python/e_sensor.py の ThermalParams と同一の値・式を移植したもの。
  /// パラメータは資料「熱影響の補正方法」§3 の同定値。
  ///   CB dTB/dt = Q - K(v)(TB - Ta) ,  K(v) = A + B*v [mW/K]
  ///   定常: TB - Ta = Q/K(v)
  /// </summary>
  public class ThermalParams
  {
    public double Q = 50.0;       // トランジスタ発熱 [mW]
    public double CB = 1300.0;    // 基板熱容量 [mJ/K] (実測昇温 τ≈120s に整合)
    public double A = 11.058;     // K(v) 切片(無風時) [mW/K] — 低風速重視で実測値に固定
    public double B = 130.19;     // K(v) 風速係数 [mW/(K·(m/s))]
    // 定常オフセット(Q/K)の調整係数。校正時の TB-Ta を平衡前に測り offset がやや
    // 過小だったため当面 1.2。再校正したら 1.0 に戻す。
    public double OffsetScale = 1.2;

    /// <summary>伝熱係数 K(v) = A + B*v [mW/K]。</summary>
    public double K(double v) => Math.Max(1e-3, A + B * Math.Max(0.0, v));

    /// <summary>定常自己発熱オフセット OffsetScale·Q/K(v) [K]。</summary>
    public double Offset(double v) => OffsetScale * Q / K(v);
  }

  /// <summary>
  /// 2状態カルマンフィルタ(資料 §4)による空気温度の推定。
  /// 状態 x=[TB, Ta] (基板温度, 空気温度)。基板温度 TB(センサ計測値) と風速 v から、
  /// 自己発熱の過渡をモデルで差し引いて空気温度 Ta を推定する。
  /// software/python/e_sensor.py の KalmanThermalCorrector の移植。
  ///
  /// 注意: 温度(STCC4)の更新周期は約1秒なので、本フィルタは「生温度値が更新された
  /// とき」だけ Update を呼ぶこと(200msポーリング毎に呼ぶと同一値を重複入力して
  /// 誤推定する)。dt は前回 Update からの実経過秒を渡す。
  /// </summary>
  public class KalmanThermalCorrector
  {
    private readonly ThermalParams _p;
    private readonly double _R;       // 観測雑音分散
    private readonly double _qb;      // 基板状態のモデル雑音分散
    private readonly double _qa;      // 空気温度のプロセス雑音分散
    private readonly double _gateAbs; // 外れ値ゲート [K]
    private readonly double _initBoardVar;
    private readonly double _initAirVar;

    private bool _hasState;
    private double _xb, _xa;          // 状態 (基板, 空気)
    private double _p00, _p01, _p11;  // 誤差共分散 P (対称2x2)

    public KalmanThermalCorrector(ThermalParams p = null,
        double measStd = 0.03, double airStd = 0.005, double boardStd = 0.002,
        double initAirStd = 0.5, double gateAbs = 2.0)
    {
      _p = p ?? new ThermalParams();
      _R = measStd * measStd;
      _qb = boardStd * boardStd;
      _qa = airStd * airStd;
      _gateAbs = gateAbs;
      _initBoardVar = measStd * measStd;
      _initAirVar = initAirStd * initAirStd;
      Reset();
    }

    public void Reset()
    {
      _hasState = false;
      _xb = 0.0;
      _xa = 0.0;
      // 起動時は「基板≒空気(自己発熱はこれから蓄積)」と分かっているので
      // 空気状態の初期不確かさを小さく取る(コールドスタート前提)。
      _p00 = _initBoardVar;
      _p01 = 0.0;
      _p11 = _initAirVar;
    }

    /// <summary>
    /// 1サンプル進めて推定空気温度[℃]を返す(初期化前で measured=false の場合は z を返す)。
    /// </summary>
    /// <param name="z">基板温度(センサ計測値) [℃]</param>
    /// <param name="v">風速 [m/s]</param>
    /// <param name="dt">前回からの経過時間 [s]</param>
    /// <param name="heating">風速計が稼働(自己発熱)中なら true。停止中は Q=0 扱い。</param>
    /// <param name="measured">有効な温度観測があれば true。false(欠測)なら予測のみ。</param>
    public double Update(double z, double v, double dt, bool heating = true, bool measured = true)
    {
      if (!_hasState)
      {
        if (!measured) return z;            // 初期化には有効な初回観測が必要
        _xb = z; _xa = z; _hasState = true; // 起動直後は Ta ≈ TB とみなす
        return z;
      }

      double K = _p.K(v);
      double E = Math.Exp(-K * dt / _p.CB);
      double off = heating ? _p.OffsetScale * _p.Q / K : 0.0;
      double c = 1.0 - E;

      // --- 予測:  x = F x + b ,  P = F P Fᵀ + Qw   (F=[[E,c],[0,1]]) ---
      double xb = E * _xb + c * _xa + c * off;
      double xa = _xa;
      double m00 = E * _p00 + c * _p01;
      double m01 = E * _p01 + c * _p11;
      double m11 = _p11;
      double n00 = m00 * E + m01 * c + _qb;  // +Qw
      double n01 = m01;
      double n11 = m11 + _qa;

      // 欠測 or 外れ値(基板温度は時定数≫dtのため跳ねない)は観測を使わず予測のみ。
      double y = z - xb;
      if (!measured || Math.Abs(y) > _gateAbs)
      {
        _xb = xb; _xa = xa;
        _p00 = n00; _p01 = n01; _p11 = n11;
        return _xa;
      }

      // --- 更新:  e=z-Hx , S=HPHᵀ+R , g=PHᵀ/S , x+=g e , P=(I-gH)P  (H=[1,0]) ---
      double S = n00 + _R;
      double g0 = n00 / S;
      double g1 = n01 / S;
      _xb = xb + g0 * y;
      _xa = xa + g1 * y;
      _p00 = (1.0 - g0) * n00;
      _p01 = (1.0 - g0) * n01;
      _p11 = n11 - g1 * n01;
      return _xa;
    }
  }

  /// <summary>温度補正に伴う相対湿度の補正(software/python/e_sensor.py と同一式)。</summary>
  public static class HumidityCorrection
  {
    /// <summary>飽和水蒸気圧 [hPa] (Magnus/WMO 近似)。</summary>
    private static double SatVaporPressure(double tCelsius)
        => 6.112 * Math.Exp(17.62 * tCelsius / (243.12 + tCelsius));

    /// <summary>
    /// 自己発熱で温度が変わったぶんの相対湿度補正。水蒸気分圧 e は定圧では保存量
    /// とみなせるので RH_air = RH_meas · esat(T_measured)/esat(T_air)。0〜100% にクランプ。
    /// </summary>
    /// <param name="rh">センサが読んだ相対湿度[%] (基板温度での値)</param>
    /// <param name="tMeasured">センサ温度(基板温度)[℃]</param>
    /// <param name="tAir">補正後の空気温度[℃]</param>
    public static double Correct(double rh, double tMeasured, double tAir)
    {
      double rhCorr = rh * SatVaporPressure(tMeasured) / SatVaporPressure(tAir);
      return Math.Max(0.0, Math.Min(100.0, rhCorr));
    }
  }
}
