namespace E_Sensor;

public partial class SensorCard : ContentView
{
  // 値テキストの色（バッジ無し: 通常、バッジ有り: 灰色）
  private static readonly Color ValueColorNormal = Color.FromArgb("#005A9E");
  private static readonly Color ValueColorBadged = Color.FromArgb("#B0B7BF");

  public static readonly BindableProperty TitleProperty =
        BindableProperty.Create(nameof(Title), typeof(string), typeof(SensorCard), string.Empty);

  public static readonly BindableProperty ValueProperty =
      BindableProperty.Create(nameof(Value), typeof(string), typeof(SensorCard), "0.0");

  public static readonly BindableProperty UnitProperty =
      BindableProperty.Create(nameof(Unit), typeof(string), typeof(SensorCard), string.Empty);

  // 空/null のとき: バッジ非表示・値は通常表示。
  // 非空のとき: バッジに当該テキストを表示・値は灰色化（信頼できない値であることを示す）。
  // 表示反映は DataTrigger の Value="" 比較が安定しないため propertyChanged で直接実施する。
  public static readonly BindableProperty BadgeTextProperty =
      BindableProperty.Create(nameof(BadgeText), typeof(string), typeof(SensorCard), string.Empty,
          propertyChanged: OnBadgeTextChanged);

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

  public string BadgeText
  {
    get => (string)GetValue(BadgeTextProperty);
    set => SetValue(BadgeTextProperty, value);
  }

  public SensorCard()
  {
    InitializeComponent();
    // 初期状態を反映（デフォルト BadgeText = "" → 通常表示）
    UpdateBadgeVisuals(BadgeText);
  }

  private void UpdateBadgeVisuals(string? badgeText)
  {
    bool hasBadge = !string.IsNullOrEmpty(badgeText);
    BadgeBorder.IsVisible = hasBadge;
    ValueLabel.TextColor = hasBadge ? ValueColorBadged : ValueColorNormal;
  }

  private static void OnBadgeTextChanged(BindableObject bindable, object oldValue, object newValue)
  {
    if (bindable is SensorCard card)
    {
      card.UpdateBadgeVisuals(newValue as string);
    }
  }
}
