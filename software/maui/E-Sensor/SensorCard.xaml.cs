using System.Windows.Input;

namespace E_Sensor;

public partial class SensorCard : ContentView
{
  // 値テキストの色（警告バッジ無し: 通常、警告バッジ有り: 灰色）
  private static readonly Color ValueColorNormal = Color.FromArgb("#005A9E");
  private static readonly Color ValueColorBadged = Color.FromArgb("#B0B7BF");

  public static readonly BindableProperty TitleProperty =
        BindableProperty.Create(nameof(Title), typeof(string), typeof(SensorCard), string.Empty);

  public static readonly BindableProperty ValueProperty =
      BindableProperty.Create(nameof(Value), typeof(string), typeof(SensorCard), "0.0");

  public static readonly BindableProperty UnitProperty =
      BindableProperty.Create(nameof(Unit), typeof(string), typeof(SensorCard), string.Empty);

  // 警告バッジ。空/null のとき: 非表示・値は通常表示。
  // 非空のとき: バッジ表示・値を灰色化（信頼できない値であることを示す）。
  public static readonly BindableProperty BadgeTextProperty =
      BindableProperty.Create(nameof(BadgeText), typeof(string), typeof(SensorCard), string.Empty,
          propertyChanged: OnBadgeTextChanged);

  // 情報バッジ（補正値の注記）。空/null のとき: 非表示。
  // 非空のとき: 青系バッジを表示（値は灰色化しない）・タップで InfoBadgeCommand 実行。
  public static readonly BindableProperty InfoBadgeTextProperty =
      BindableProperty.Create(nameof(InfoBadgeText), typeof(string), typeof(SensorCard), string.Empty,
          propertyChanged: OnInfoBadgeTextChanged);

  public static readonly BindableProperty InfoBadgeCommandProperty =
      BindableProperty.Create(nameof(InfoBadgeCommand), typeof(ICommand), typeof(SensorCard), null);

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

  public string InfoBadgeText
  {
    get => (string)GetValue(InfoBadgeTextProperty);
    set => SetValue(InfoBadgeTextProperty, value);
  }

  public ICommand InfoBadgeCommand
  {
    get => (ICommand)GetValue(InfoBadgeCommandProperty);
    set => SetValue(InfoBadgeCommandProperty, value);
  }

  public SensorCard()
  {
    InitializeComponent();
    // 初期状態を反映（デフォルト = 通常表示）
    UpdateBadgeVisuals(BadgeText);
    InfoBadgeBorder.IsVisible = !string.IsNullOrEmpty(InfoBadgeText);
  }

  private void UpdateBadgeVisuals(string? badgeText)
  {
    bool hasBadge = !string.IsNullOrEmpty(badgeText);
    BadgeBorder.IsVisible = hasBadge;
    // 値の灰色化は警告バッジの有無だけで決まる（情報バッジは灰色化しない）。
    ValueLabel.TextColor = hasBadge ? ValueColorBadged : ValueColorNormal;
  }

  private static void OnBadgeTextChanged(BindableObject bindable, object oldValue, object newValue)
  {
    if (bindable is SensorCard card)
    {
      card.UpdateBadgeVisuals(newValue as string);
    }
  }

  private static void OnInfoBadgeTextChanged(BindableObject bindable, object oldValue, object newValue)
  {
    if (bindable is SensorCard card)
    {
      card.InfoBadgeBorder.IsVisible = !string.IsNullOrEmpty(newValue as string);
    }
  }
}
