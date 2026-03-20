#include "tusb.h"
#include "../serial.h"

/* MicrochipのVID/PIDを使用する設定（ユーザー指定） */
#define USB_VID   0x04D8
#define USB_PID   0x0057
#define USB_BCD   0x0200

//--------------------------------------------------------------------+
// Device Descriptors
//--------------------------------------------------------------------+
tusb_desc_device_t const desc_device = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = USB_BCD,

    /* MIDIはインターフェース側でクラスを定義するため、デバイスレベルは0でOK */
    .bDeviceClass       = 0x00,
    .bDeviceSubClass    = 0x00,
    .bDeviceProtocol    = 0x00,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,

    .idVendor           = USB_VID,
    .idProduct          = USB_PID,
    .bcdDevice          = 0x0100,

    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,

    .bNumConfigurations = 0x01
};

uint8_t const *tud_descriptor_device_cb(void) {
  return (uint8_t const *) &desc_device;
}

//--------------------------------------------------------------------+
// Configuration Descriptor
//--------------------------------------------------------------------+

enum {
  ITF_NUM_MIDI = 0,
  ITF_NUM_MIDI_STREAMING, // MIDIは通常2つのインターフェース（ACとMS）を使う
  ITF_NUM_TOTAL
};

/* エンドポイント番号の定義（AVR64DU32で一般的に使われる番号） */
#define EPNUM_MIDI_OUT   0x01
#define EPNUM_MIDI_IN    0x81

/* MIDI記述子の合計サイズを計算 */
#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_MIDI_DESC_LEN)

uint8_t const desc_fs_configuration[] = {
    // 構成記述子の基本ヘッダー
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 100),

    // MIDIインターフェース記述子（マクロで自動生成）
    // 引数: インターフェース番号, 文字列索引, OUTエンドポイント, INエンドポイント, EPサイズ
    TUD_MIDI_DESCRIPTOR(ITF_NUM_MIDI, 4, EPNUM_MIDI_OUT, EPNUM_MIDI_IN, 64)
};

/* 接続速度に応じた記述子を返すコールバック */
uint8_t const *tud_descriptor_configuration_cb(uint8_t index) {
  (void) index;
  return desc_fs_configuration;
}

//--------------------------------------------------------------------+
// String Descriptors
//--------------------------------------------------------------------+

enum {
  STRID_LANGID = 0,
  STRID_MANUFACTURER,
  STRID_PRODUCT,
  STRID_SERIAL,
  STRID_MIDI_ITF
};

char const *string_desc_arr[] = {
    (const char[]) { 0x09, 0x04 }, // 0: English (0x0409)
    "Togashi Research Institute LLC",   // 1: Manufacturer
    "E-Sensor Module",                  // 2: Product
    NULL,                           // 3: Serial (固定文字列または独自取得)
    "E-Sensor",                         // 4: MIDI String
};

static uint16_t _desc_str[32 + 1];



// --- シリアル番号をSIGROWから読み取って16進文字列にする関数 ---
/*static void get_serial_string(char* buf) {
    const char hex_map[] = "0123456789ABCDEF";
    // AVR64DUのシリアル番号は SIGROW の 0x08 ~ 0x11 (10 bytes)
    uint8_t *sigrow_ptr = (uint8_t*) 0x1108; 

    for (int i = 0; i < 10; i++) {
        uint8_t b = sigrow_ptr[i];
        buf[i * 2]     = hex_map[b >> 4];
        buf[i * 2 + 1] = hex_map[b & 0x0F];
    }
    buf[20] = '\0';
}*/

uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
  (void) langid;
    size_t chr_count;
    char serial_buf[21]; // 10bytes * 2 + null

    if (index == 0) { // STRID_LANGID
        memcpy(&_desc_str[1], string_desc_arr[0], 2);
        chr_count = 1;
    } else {
        if (!(index < sizeof(string_desc_arr) / sizeof(string_desc_arr[0]))) return NULL;

        const char *str;
        if (index == 3) { // Serial Number ID
            SERIAL_get_serial_string(serial_buf);
            str = serial_buf;
        } else {
            str = string_desc_arr[index];
        }

        if (!str) return NULL;

        chr_count = strlen(str);
        if (chr_count > 31) chr_count = 31;

        for (size_t i = 0; i < chr_count; i++) {
            _desc_str[1 + i] = str[i];
        }
    }

    _desc_str[0] = (uint16_t) ((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));
    return _desc_str;
}