namespace E_Sensor;

public partial class SensorCard : ContentView
{
  public static readonly BindableProperty TitleProperty =
        BindableProperty.Create(nameof(Title), typeof(string), typeof(SensorCard), string.Empty);

  public static readonly BindableProperty ValueProperty =
      BindableProperty.Create(nameof(Value), typeof(string), typeof(SensorCard), "0.0");

  public static readonly BindableProperty UnitProperty =
      BindableProperty.Create(nameof(Unit), typeof(string), typeof(SensorCard), string.Empty);

  public string Title
  {
    get => (string)GetValue(TitleProperty);
    set => SetValue(TitleProperty, value);
  }

  public string Value
  {
    get => (string)GetValue(ValueProperty);
    set => SetValue(ValueProperty, value);
  }

  public string Unit
  {
    get => (string)GetValue(UnitProperty);
    set => SetValue(UnitProperty, value);
  }

  public SensorCard()
  {
    InitializeComponent();
  }
}